# Changelog

## v0.2.4 (2026-05-07)

Two correctness fixes upstream from us, plus README cleanup.

### Picoquic upstream double-close fix

aiopquic 0.2.2 shipped a TX_CLOSE state guard that filtered
`picoquic_close()` calls when the cnx was already in a terminal
state. That worked around a fall-through in `picoquic_close_ex`
which set `ret = -1` for the already-closed branch but still ran
`picoquic_reinsert_by_wake_time`, manipulating wake-list state that
had been cleaned up at the original close — UAF on the worker
thread.

picoquic [#2097](https://github.com/private-octopus/picoquic/pull/2097)
fixes this at the source: wraps the side effects in `if (ret == 0)`
and adds an `ecdc_double_close_test` that drives a real cnx through
close and verifies a second `picoquic_close()` is a safe no-op.

This release pins the picoquic submodule to that PR's HEAD
(`a9e58d8e`) and removes our TX_CLOSE state guard. Verified end-to-
end: 0 segfaults in 100 runs of the aiomoqt full pytest stress that
previously hit at ~15% rate on the original release wheel.

### WebTransport empty-path normalization

`MOQTServer(path="")` against a client connecting to root used to
fail WT CONNECT with code 2. picoquic's path table is exact-match
(no default route); the server-side empty path didn't match the
client's HTTP/3 default `:path: /`. This release normalizes at the
Cython layer:

  - Server-side empty `wt_path` → `"*"` — picoquic's wildcard match
    fallback (PR #2085, already in our pin) — accepts any client
    path.
  - Client-side empty `path` → `"/"` — HTTP/3 root request semantics
    (RFC 9114 §4.3.1).

Consumers can pass `path=""` or omit it on either side and have
WebTransport CONNECT route correctly without thinking about
picoquic path-match rules. aiopquic 0.2.2's helper-only
normalization (`serve_webtransport` / `connect_webtransport`)
moved down to `_transport.pyx` so direct class users (e.g.
aiomoqt's `MOQTSessionWTClient`) get the fix automatically.

### README

Refreshed to reflect current state: wheels-first install, real perf
table (lowlevel + highlevel + multi-stream + stream churn) on the
test host, drops stale "Python 3.14+ required" / "Source build only"
limits, accurate datagram-support note (QUIC datagrams TX/RX, WT
datagram TX deferred), `uv pip install` examples, TODO list updated.


## v0.2.3 (2026-05-07)

Wheel-build perf release. Closes the ~30% throughput / ~36× p50
latency gap between the manylinux wheel and locally-built artifacts
on AMD Ryzen.

### Change

Switched the Linux wheel base image from `manylinux_2_28` (RHEL 8 /
OpenSSL 1.1.1k) to `manylinux_2_34` (RHEL 9 / OpenSSL 3.5.1).
RHEL 8's OpenSSL 1.1.1k FIPS build routes through crypto-policies
+ FIPS-provider overhead that takes a slower ASM path on Ryzen even
with `AESNI_ASM` / `GHASH_ASM` / `X25519_ASM` compile-time defines
present. OpenSSL 3.x doesn't have this overhead. The manylinux
container's gcc version (14.2.1) was previously suspected but ruled
out via A/B test: gcc-toolset-11 in `_2_28` produced even slower
wheels (148K obj/s @ 1K vs 185K with default gcc 14).

### Verification

30s sustained `bench_baselines_lowlevel` 1K loopback, AMD Ryzen,
WSL2, Python 3.14:

| metric              | 0.2.2 wheel | 0.2.3 wheel | local source |
|---------------------|-------------|-------------|--------------|
| obj/s               | 185,712     | 278,061     | 250,391      |
| Mbps                | 1,521       | 2,278       | 2,051        |
| latency floor µs    | (n/a)       | 11          | 12           |
| latency p50 µs      | 5,478       | 107         | 150          |
| latency p99 µs      | (n/a)       | 886         | 1,774        |
| latency max µs      | (n/a)       | 6,058       | 14,502       |

AB7 stream-object stress: 13/13 byte-perfect.
AB8 stream-churn: 10/10 byte-perfect.
AB9 concurrent streams: 11/11 byte-perfect, peak 4,623 Mbps at
P=64 × 16 KiB.

### Compatibility trade-off

Wheel compatibility narrows from glibc 2.28 (RHEL 8 / Ubuntu 20.04)
to glibc 2.34 (RHEL 9 / Ubuntu 22.04). Older Linux systems install
via sdist (no behavioral change for them). Apple Silicon macOS
wheels are unchanged.

### Build configuration

- `[tool.cibuildwheel.linux.environment]` block pins
  `CFLAGS=-O3 -fno-strict-overflow -fPIC` and
  `PICOQUIC_C_FLAGS=-O3 -DNDEBUG`.
- `build_picoquic.sh` honors `PICOQUIC_C_FLAGS` via
  `-DCMAKE_C_FLAGS_RELEASE=...` for picoquic + picotls compile.

All 0.2.0 / 0.2.1 / 0.2.2 byte-conservation guarantees preserved.
Pull model unchanged. No source-code changes outside build config.


## v0.2.2 (2026-05-06)

Stability release. Closes the close-time segfault that survived 0.2.1
on full integration matrices (e.g. aiomoqt's pytest suite). 100/100
clean across the new aiomoqt regression workload (was 15/100 on 0.2.1
release wheel, 6/100 after the picoquic upstream perf bump alone).

### Fix

**Double-close race on `picoquic_close()`.** Pattern: peer-initiated
`CONNECTION_CLOSE` arrives at the worker; our `aiopquic_stream_cb`
queues an `_EVT_CLOSE` to the RX SPSC ring; **before the asyncio
thread drains and sets `self._closed = True`**, application code
(typically a test fixture's `finally:` clause) calls
`QuicConnection.close()` which pushes a `_TX_CLOSE` event. The worker
drains the `_TX_CLOSE` and calls `picoquic_close()` on a cnx whose
state is already `picoquic_state_disconnected`. picoquic's
`picoquic_close_ex` (sender.c) sets `ret = -1` for the already-closed
branch but **falls through** to `picoquic_reinsert_by_wake_time`,
which manipulates wake-list state cleaned up at the original close.
UAF / inconsistent-list-pointer crash on the worker thread.

The fix lives in our TX_CLOSE handler ([callback.h](src/aiopquic/_binding/c/callback.h)):
filter out the call when cnx state is already terminal.

```c
case SPSC_EVT_TX_CLOSE: {
    picoquic_state_enum st = picoquic_get_cnx_state(cnx);
    if (st < picoquic_state_disconnecting) {
        picoquic_close(cnx, entry->error_code);
    }
    spsc_ring_pop(ctx->tx_ring);
    break;
}
```

Filed picoquic upstream issue suggesting `picoquic_close_ex` should
early-return on the already-closed branch.

### Other changes

- **picoquic submodule bumped** to `f54239a3` (upstream master HEAD as
  of 2026-05-04). Pulls in #2095 — "perf: split due-now connections
  out of the wake tree" — independently reduced segfault rate from
  15% to 6% by tightening the worker-loop timing window. Also
  includes:
  - #2092 RFC 9000 fixes
  - #2086 WebTransport authority exposure
  - #2085 wildcard `*` path matcher for WebTransport CONNECT
  - #2052 unidirectional stream leak fix (relevant to subgroup-heavy
    workloads)

- **`engine.close()` ordering fix** ([connection.py](src/aiopquic/quic/connection.py)):
  stop the transport (which joins the worker and runs `picoquic_free`,
  firing close callbacks while Python state is alive) before clearing
  `self._connections` / `self._protocols`. The prior order risked
  Python-side cnx wrappers being GC'd before picoquic_free's per-cnx
  close callbacks fired. Defensive correctness; not load-bearing for
  this segfault but the right invariant.

- **WebTransport empty-path normalization** ([webtransport.py](src/aiopquic/asyncio/webtransport.py)):
  `path == ""` is now normalized to `"/"` in both `serve_webtransport`
  and `connect_webtransport`. picoquic's path table is exact-match
  with no default route, while HTTP/3 clients send `:path: /` for
  root requests (RFC 9114 §4.3.1). Prior behavior: empty-path setups
  silently failed CONNECT with `code=2`.

### New regression benches

- `tests/bench/bench_stream_churn_highlevel.py` — many short-lived
  uni streams, each with N small objects + FIN, byte-verified. 10/10
  cases pass; peak 8,675 streams/s at 500-stream churn (1 KiB × 16
  objs each).
- `tests/bench/bench_concurrent_streams_highlevel.py` — N parallel
  uni streams on one cnx, round-robin object dispatch, byte-verified.
  11/11 cases pass; peak 3,141 Mbps at P=64 × 16 KiB.
- `bench_baselines_highlevel.py` `HIGHLEVEL_MIN_MBPS` floor locked at
  1500 / 1900 / 1900 Mbps (1K / 4K / 16K) — ~80% of measured.

### Verification

- 100x aiomoqt full pytest under release-equivalent build (`-O3 -g`):
  0 segfaults, 0 byte-conservation failures across all 13 cases of
  AB7 stream-object stress, 10/10 AB8, 11/11 AB9.
- Perf preserved: 1 KiB single-stream sustained throughput within
  noise of v0.2.1 baseline (302,786 obj/s @ 2,480 Mbps vs prior 305K
  @ 2,499 Mbps). picoquic #2095 should also help multi-cnx workloads
  on the worker hot path; not visible in single-cnx benches.

All 0.2.0 / 0.2.1 byte-conservation guarantees preserved. Pull model
unchanged.


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
