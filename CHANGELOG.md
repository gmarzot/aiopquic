# Changelog

## v0.3.0 (2026-05-11)

Receiver-side durability + WT backpressure. The 0.2.x WT path
ack'd inbound bytes synchronously into a transient SPSC ring entry
regardless of consumer drain rate, so a slow consumer led to UDP
kernel buffer overflow → packet loss → thousands of parse rejects
at 1+ Gbps in aiomoqt mp-loopback. v0.3.0 moves WT RX onto the same
per-stream byte-ring + drain-driven `picoquic_open_flow_control`
architecture raw QUIC already used correctly. Effect: data loss is
replaced by honest latency growth (the TCP trade-off) — slow
consumers back-pressure the publisher all the way to TX pacing
instead of silently dropping packets.

Sustained verification at this commit (Ryzen WSL2, 60 s windows):

| Target rate | Delivered | avg latency | Loss |
|---|---|---|---|
|   83 Mbps |   83 Mbps |    5 ms | 0 |
|  249 Mbps |  249 Mbps |   32 ms | 0 |
|  490 Mbps |  493 Mbps |  567 ms | 0 |
|  980 Mbps |  534 Mbps (capped) | 3938 ms | 0 |

The 534 Mbps cap at 980 Mbps target is the aiomoqt consumer-CPU
saturation point — Phase B holds data integrity (0 loss in 3.6M
objects) but the queue grows. At sub-saturation rates the latency
floor is tight (5 ms avg at 83 Mbps). Aiopquic raw-QUIC sustained
holds 2.5 Gbps with 0 errors over 5 s — the bound at the aiomoqt
layer is parser/dispatch CPU, not transport.

Pre-v0.3.0 aiomoqt mp-loopback parse-reject counts (15 s runs):
250M = 1-2, 500M = 2, 1000M = 4063, 1500M = 7130, 2000M = many.

### SPSC RX-event ring durability (A0/A1/A2)

The `aiopquic_stream_cb` callback silently dropped events when the
RX SPSC event ring filled — bytes for raw-QUIC streams were already
in the per-stream `sc->rx` buffer, but the notification event was
gone, so asyncio was never told the bytes arrived. Short streams
whose only callback fell in an overflow window appeared missing
from the receiver's stream dict (the "split-write stream-loss"
investigation reproducer at `tests/bench/bench_split_writes_stress.py`).

- `event_ring_capacity` knob on `QuicConfiguration`, plumbed through
  `_start_transport`. Defaults to 262144 entries (was 4096).
- New worker counters `worker_rx_event_drops` /
  `_drops_stream_data` / `_byte_ring_overflow` exposed on
  `TransportContext` for diagnostics.

### Drain coalescing (C1/C2)

- **C1**: rx-side eventfd coalesce. `aiopquic_notify_rx` writes the
  wake fd only on the 0→1 transition of `rx_notify_pending`.
  `drain_rx` now (a) drains the ring snapshot, (b) RELEASE-stores
  pending=0, (c) drains the wake-fd counter, (d) re-arms via a
  fresh wake-fd write if the ring is still non-empty. Re-arm closes
  the race where a producer observed pending=1 between our
  peek-empty and pending=0 and skipped its own wake.
- **C2**: tx-side wake-up coalesce. `tx_send_atomic` invokes
  `picoquic_wake_up_network_thread` only on the 0→1 transition of
  `tx_wake_pending`. The packet-loop wake handler clears the flag
  back to 0 BEFORE its drain-until-empty loop, so any push that
  races against the worker either lands in the loop's next peek or
  causes a fresh wake.

Throughput impact on `bench_split_writes_stress`:
`500s × 60o × 1024B` went from 3,566 → 6,255 Mbps (+75%) vs
pre-coalesce baseline.

### C5: env-gated overflow log

Replace `fprintf(stderr,...)` on RX byte-ring overflow with a
shared counter + AIOPQUIC_RX_LOG=1 one-shot stderr (libc stdio
holds a global lock and stalls the picoquic worker under load).

### WT first-touch deduplication (`h3wt_callback.h`)

WT_NEW_STREAM events were being emitted on every `post_data`
callback (~104 events per real stream at 60 objs/stream). picoquic's
h3zero only auto-increments `stream_ctx->post_received` for
POST-method requests (`h3zero_common.c:1350: if (is_post)`), NOT
for WebTransport path callbacks. We now own this field as a
first-touch sentinel and set it ourselves after pushing
WT_NEW_STREAM. Same fix in `post_data` and `post_fin`.

Throughput impact (`bench_wt_split_writes_stress.py` 1000-stream
SP test): 373 → 1,130 Mbps (+200%, the win was wasted CPU on racing
collectors spawned per duplicate event).

### `max_data` plumbed through to picoquic transport parameters

`QuicConfiguration.max_data` (default 16 MiB) was unplumbed —
picoquic's compiled-in default of 1 MiB for `initial_max_data`
applied, and any MP-loopback workload >1 MiB had its sustained rate
capped at 1 MiB / RTT by the MAX_DATA roundtrip. Now passed via
`TransportContext.start(initial_max_data=...)` to `picoquic_tp_t`.

### WT Phase B: per-stream byte ring + drain-driven flow control

The pre-0.3.0 WT data-receive path malloc'd+memcpy'd inline into
SPSC ring entries and returned 0 to h3zero — picoquic ACKed the
bytes immediately regardless of the consumer's drain rate. With a
slow consumer the publisher's MAX_STREAM_DATA window kept opening,
the kernel UDP socket buffer filled, and packets dropped on the
wire (visible as thousands of missing streams at sustained 1+ Gbps
in aiomoqt mp-loopback).

This release migrates WT RX to the same architecture raw QUIC has
always used:

1. Per-stream `aiopquic_stream_ctx_t` side-table in each WT session
   (open-addressed hash table, capacity 4096, auto-resize at 50%
   load, hot-slot cache for sequential access). h3zero owns the
   stream-ctx slot picoquic provides, so we can't attach via
   `picoquic_set_app_stream_ctx`; the hash table works around that
   while keeping lookup O(1) regardless of total streams seen.
2. `post_data` pushes bytes into `sc->rx`. First-touch opts into
   app-driven flow control (`picoquic_set_app_flow_control` +
   initial `picoquic_open_flow_control`); subsequent callbacks
   extend MAX_STREAM_DATA proportional to `consumed` (hysteresis:
   extend when consumed + advertise exceeds current credit +
   advertise/4 — same as raw QUIC).
3. SPSC events carry a borrowed pointer to the per-stream sc
   (`data_buf=sc`, `data_length=0`) — new `spsc_ring_push_borrowed`
   variant; `spsc_ring_pop` only frees `data_buf` when
   `data_length > 0`.
4. Cython `drain_rx` recognizes the WT_STREAM_DATA / WT_STREAM_FIN
   borrowed-pointer events: pop bytes from `sc->rx`,
   atomic-increment `consumed`. The worker reads `consumed` in the
   next callback and extends the window.

Result: slow consumer → ring fills → no extension → publisher
window exhausts → publisher stops sending → backpressure all the
way to TX pacing. No UDP-buffer-overrun loss possible.

### New stress tests (`tests/bench/bench_wt_split_writes_stress.py`)

- SP and MP variants of the aiomoqt-shape WT split-write pattern
  (5B header + K × 1024B objects per stream + FIN, at high
  stream-churn rate).
- Concurrent-writers MP variant (P=2/4/8 asyncio tasks sharing one
  WT session) mirrors `PublishedTrack._generate_subgroup`.
- Sustained-duration MP variant runs at a target rate for a fixed
  wall-clock window. Holds **1 Gbps × 20s × P=2 with 40,670
  streams byte-perfect**.

### Verification matrix (this release)

Pre-fix vs post-fix on aiomoqt mp-loopback over WT:

| Rate (Mbps) | parse fails (pre) | parse fails (post) |
|---|---|---|
| 250  | 1-2 per run | **0** |
| 500  | 2          | **0** |
| 1000 | 4063       | **0** |
| 1500 | 7130       | **0** |
| 2000 | many       | **0** |

aiopquic stress suite 27/27 PASS (10 SP/MP WT + 7 raw-QUIC
split-write + 3 sustained-1Gbps + 7 concurrent-writers). aiopquic
unit suite 99/99.

### Known limit (deferred)

Per-stream sc allocated in WT Phase B is not yet reclaimed on
FIN/RESET — bounded for bench runs (~40K × ring-bytes fits in RAM)
but long-running sessions will leak. Reap-on-FIN needs
worker/consumer ref-counting; deliberately scoped out to keep the
byte-flow correctness change isolated.

## v0.2.7 (2026-05-07)

### sim_link_bench: protocol-only throughput reference

New standalone C bench at `tests/bench/sim_link/` that drives two
`picoquic_quic_t` instances over `picoquictest_sim_link` — packets
routed in-process between them, no kernel UDP, no sockets, no
sendmsg/recvmsg syscalls. Isolates picoquic protocol CPU cost from
the kernel UDP-loopback wall.

On Ryzen 7 PRO 7840U / WSL2: 11.2 Gbps single-stream sustained
(30s steady-state). Compare to picoquic-over-UDP-loopback (2.18 Gbps
via `picoquicdemo -a perf`) and `aiopquic` highlevel (2.03 Gbps);
the gap is the kernel sendmsg-rate ceiling, not picoquic.

Build: `./tests/bench/sim_link/build.sh` after `./build_picoquic.sh`.

### README: corrected loopback-ceiling analysis

Earlier README framing claimed kernel UDP loopback was the dominant
wall ("5× drop"). That was wrong. iperf3 baseline shows kernel UDP
loopback at QUIC MTU (1,400 B) is **3.15 Gbps** — bandwidth scales
cleanly with datagram size up to 32 KiB at 33+ Gbps. The wall at
QUIC MTU is the per-syscall sendmsg rate (~280 K/s), not bandwidth.

Updated Performance section with the iperf3 anchor, full layer
breakdown (raw UDP / picoquicdemo / aiopquic lowlevel / aiopquic
highlevel / sim_link), and notes that QUIC-over-loopback at MTU
landing at 64-74% of UDP ceiling is normal QUIC overhead, not a
defect to chase.

## v0.2.6 (2026-05-07)

### Bench: `--duration` pytest option for opt-in benches

Added a `--duration` flag to the bench pytest config (default 30.0s).
The new `bench_duration` fixture exposes it to benches that opt in.

Wired into `bench_baselines_highlevel.py` and `bench_baselines_lowlevel.py`,
which previously used a hardcoded `[30.0]` parametrize. Other benches
that intentionally compare multiple windows (e.g. `bench_small_object_rate`
uses `[2.0, 5.0]`, `bench_throughput_pullmodel` uses `[1.0, 5.0]`)
keep their own parametrize lists.

Background: short-window microbenches inflate sustained-rate numbers
3–10× from warmup transients. A run of `bench_stream_churn_highlevel`
showed the same `4o-256B` parametrize jumping from 823 Mbps at 100
streams down to 305 Mbps at 1000 streams — only the longer window
converged to the true sustained rate. The `--duration` knob lets
qualifying runs hold the measurement window long enough for
convergence without rebuilding fixed-N benches.

Library API unchanged.

## v0.2.5 (2026-05-07)

### Concurrent WT `create_stream` correctness fix

`WebTransportSession.create_stream()` previously serialized
concurrent callers via `asyncio.shield(self._pending_create)` with a
single-slot pending future. The pattern is racy with three or more
concurrent callers: when the first response arrives, the second
caller wakes and installs a new `_pending_create` future, but a
third (also waking from the same shield) overwrites it with its
own future before the second's response lands. The second's
`wait_for` then times out after 5s, dropping the stream.

Symptom in aiomoqt: `PublishedTrack` with `num_subgroups=P > 2`
silently drops `P − 2` subgroups under load — observed transmit
rate is `target × 2 / P` instead of `target`.

Replaced the single-slot design with a FIFO `deque` of pending
futures. Each caller appends its own future and awaits it
individually. The `WT_STREAM_CREATED` handler `popleft`'s and
resolves. Picoquic processes the TX ring serially and emits
responses in the same order, so 1:1 pairing is correct.

No locks added — single-threaded asyncio + SPSC ring ordering is
sufficient. Per-stream cost: same big-O as before (one future
allocation, one deque op). Concurrent open path is now actually
faster, since the old shield path added an extra context switch
per waiter that the deque path skips.

`SESSION_CLOSED` also fails any still-pending creates with a
`WebTransportError` so callers don't sit on the 5s timeout.

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
