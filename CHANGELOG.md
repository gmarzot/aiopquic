# Changelog

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
