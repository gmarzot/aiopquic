# Changelog

## v0.2.1 (2026-05-05)

Stability + performance fix release. Targets an intermittent segfault
seen under sustained `QuicConnection.send_stream_data` stress in 0.2.0.

### Highlights

- **Segfault under sustained `send_stream_data` closed.** Reproducer
  (extended object-stress matrix, 13 cases × 5 back-to-back runs)
  was reliably triggering a segfault on 0.2.0; runs 65/65 byte-perfect
  on 0.2.1.
- **Latency win:** lowlevel 1 KiB p99 latency **−62%** (3,689 µs →
  1,387 µs). Smaller per-stream cache footprint.
- **Throughput win:** highlevel 1 KiB throughput **+6.1%** (215K →
  228K obj/s). Wrapper cost (highlevel/lowlevel) improved from 73%
  to 78%.
- All 0.2.0 byte-conservation guarantees preserved.

### Changes

**Combined Cython send-data fast path.** New entry
`stream_ctx_send_data` collapses 5 Python↔Cython transitions per
send (`ensure_tx` + `get_tx` + `free_check` + `push` + `set_fin`)
into 1. Atomic from Python's perspective: ring full returns 0, no
partial commit, caller safely retries the same buffer. Pull model
unchanged. `QuicConnection.send_stream_data` rewired to use it.

**RX ring sized 1× the advertised window (was 2×).** Audit
confirmed picoquic gates auto-extend on
`!stream->use_app_flow_control` (`picoquic/frames.c:4638`); the
opt-in inside the first stream_data callback closes the auto-extend
path before any frame can ship out. The previous 2× headroom was
unnecessary memory traffic + cache footprint per stream. Matches
the canonical picoquic flow-control pattern
(`picoquictest/flow_control_test.c`). RX ring overflow remains a
hard error — the spec-correct response to a peer that exceeds the
advertised window is `FLOW_CONTROL_ERROR` connection close.

### Why the segfault closed (hypothesis)

The two changes together narrowed the per-call worker-thread race
window: fewer Cython entries per send (fewer GIL acquire/release
cycles for the picoquic worker to race against ctx state) and half
the per-stream memory pressure (faster ctx-cleanup, smaller
allocator footprint). The underlying lifecycle race (Landing C
UAF suspect) may still exist in principle; empirically, 65/65 cases
pass across 5 back-to-back runs of the same matrix that segfaulted
on 0.2.0.

### New benches

- `tests/bench/bench_baselines_lowlevel.py` (renamed) — 30s
  steady-state SPSC-direct baseline, the transport-layer ceiling.
- `tests/bench/bench_baselines_highlevel.py` — 30s steady-state
  baseline through `QuicConnection.send_stream_data`, the path
  client libraries (aiomoqt) actually use. Gap between the two
  quantifies wrapper cost; tracked separately so transport vs
  wrapper regressions stay distinguishable.
- `tests/bench/bench_stream_object_stress.py` — high-level API,
  byte-verifying, varying obj sizes (64 B – 256 KiB) and rates.
  This is the segfault reproducer that closed on 0.2.1.


## v0.2.0 (2026-05-04)

First high-performance release. Transport rewrite around a canonical
pull-model that puts byte conservation, real backpressure, and
spec-compliant flow control on solid ground.

### Highlights

- **2.5 Gbps sustained** loopback throughput (single stream)
- **~300,000 obj/sec at 1 KiB** with sub-ms p50 latency
- **Latency floor:** 52 µs single-object round-trip (1 KiB)
- **0 byte corruption** across 1.47 million objects in CI bench
- **End-to-end backpressure**: peer's send rate clamps to consumer
  drain rate via MAX_STREAM_DATA — no silent drops, no ring overflow
  for spec-compliant peers
- Cross-stack interop validated against qh3 (1, 10, 100 MiB transfers,
  byte-CRC matched)

Full numbers: [`tests/bench/RESULTS.md`](tests/bench/RESULTS.md).

### Transport rewrite

**Pull-model TX path.** The producer pushes bytes into a per-stream
ring (`aiopquic_stream_buf_t`); picoquic pulls at wire rate via the
`prepare_to_send` callback. `QuicConnection.send_stream_data` raises
`BufferError` when the ring is full, giving callers real backpressure
instead of unbounded queuing.

**Per-stream RX byte ring (Landing A).** Replaces the previous
"push bytes through the SPSC RX ring; drop on full" path that could
silently lose bytes under high-rate sustained delivery. Now: bytes
land in a per-stream byte ring synchronously inside picoquic's
`stream_data` callback (matching the picoquic-sample idiom). The
SPSC ring carries event metadata only — never bytes — so it can no
longer drop payload.

**MAX_STREAM_DATA backpressure (Landing B).** Worker-thread
side-effect: as the consumer drains bytes (atomically advancing
`sc->rx_consumed`), the picoquic worker reads it on the next
`stream_data` callback and calls `picoquic_open_flow_control` to
extend the peer's send window. Hysteresis at 1/4 of the advertised
window keeps MAX_STREAM_DATA frame rate bounded.

**Per-stream wrapper + connection-close cleanup (Landing C).**
`aiopquic_stream_ctx_t` wraps both TX and RX rings + flow-control
state; bound to picoquic's app_stream_ctx slot for both directions.
Wrappers freed at connection close — bounded leak per cnx instead
of leak-until-process-exit.

**Ring sized to FC window with safety headroom.** The configured
`QuicConfiguration.max_stream_data` is advertised verbatim at
handshake; the physical RX ring is allocated at `2 * advertised` to
guarantee a spec-compliant peer can never overrun the buffer even
during initial flow-control transient races.

### Public API additions

- `QuicConfiguration.max_stream_data` — peer-advertised
  flow-control window AND the per-stream RX byte ring sizing knob.
  Default 1 MiB advertised / 2 MiB physical.
- `QuicConfiguration.congestion_control_algorithm` — `"newreno"`
  (default), `"cubic"`, `"bbr"`, `"bbr1"`, `"prague"`, `"dcubic"`,
  `"fast"`. Wraps `picoquic_set_default_congestion_algorithm_by_name`.
- `QuicConfiguration.secrets_log_file` — NSS Key Log Format file
  for Wireshark TLS decryption. Honors `SSLKEYLOGFILE` env var.
- `QuicConnection.get_stream_buf_stats(stream_id)` — `(pushed,
  popped, push_hash, pop_hash)` for byte-conservation diagnostics
  (push/pop hashes populated when `AIOPQUIC_TX_HASH=1`).

### Tests + benches

- 91/91 transport tests pass.
- New `tests/interop/` cross-stack harness against qh3 (CI),
  with skeletons + build instructions for ngtcp2 and s2n-quic
  (local-only).
- New `tests/bench/`:
  - `bench_throughput_pullmodel.py` — sustained 2.5 Gbps with
    integrity verification.
  - `bench_small_object_rate.py` — 1/4/16 KiB objects/sec at line rate.
  - `bench_latency_floor.py` — single-shot RTT, sub-saturation
    sustained latency, latency-vs-rate sweep (finds bufferbloat knee).
  - `bench_backpressure.py` — proves MAX_STREAM_DATA clamps sender
    to consumer rate end-to-end with zero ring overflow.
- `tests/bench/RESULTS.md` — reference numbers + reproduction
  commands.

### Distribution

- **Wheels:** manylinux_2_28 x86_64, macOS 11.0+ x86_64, macOS 11.0+
  arm64. Built via cibuildwheel.
- **sdist:** universal source distribution as fallback for any
  platform without a published wheel (BSD family, exotic Linux,
  etc.).
- Python 3.14+.

### Removed / not-yet

- Drop-in compatibility with aioquic / qh3: NOT a goal. We share
  similar shapes (`QuicConfiguration`, `QuicConnection`,
  `connect()` / `serve()`, event types) but `send_stream_data`
  raises `BufferError` on backpressure (aioquic does not), and our
  flow-control sizing semantics differ.
- Free-threaded Python 3.14t: deferred pending producer-side
  locking audit.
- Per-stream wrapper cleanup before connection close: deferred
  (bounded leak per cnx until then).

### Changelog of recent commits

- `e007360` README: aioquic/qh3 ordering
- `04ba0f6` README + __init__: honest framing
- `fb3dcfa` setup.py: auto-detect Homebrew openssl@3 / openssl@1.1
- `f87a5cb` setup.py: skip picoquic-built check for sdist commands
- `be7ff0e` cibuildwheel: bump to v3.1.4 (Python 3.14 support)
- `659de95` Wheels dry-run workflow
- `ffcfe54` v0.2.0: bump version + wheel build matrix
- `212275f` RESULTS.md: refresh numbers
- `e31f6db` Stable RX path: 2x physical ring + handshake-time MAX_STREAM_DATA TP
- `43ef344` Add tests/bench/RESULTS.md
- `d281355` Latency-floor microbenches
- `a9a6381` Strip RX trace instrumentation; add small-object bench
- `1744222` Landing C: connection-close cleanup of per-stream wrappers
- `d904614` Landing B: MAX_STREAM_DATA backpressure + ring sizing + CC selector
- `3ca3b20` Landing A: per-stream RX byte ring + stream_ctx wrapper
- `390f856` interop cross-stack test harness
- `bb52dcd` pull-model TX foundation
