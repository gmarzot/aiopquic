# Changelog

## v0.3.6 (unreleased)

### WT pull-model migration (parity with raw-QUIC)

- WT data streams now use per-stream `sc->tx` byte ring + `picoquic_mark_active_stream_v2` (preserves `app_stream_ctx`), matching the raw-QUIC pull-model lifecycle. Eliminates per-callback `h3zero_find_stream` splay-tree lookup on every send.
- `WebTransportSession.send_stream_data_drained` symmetric to `QuicConnection.send_stream_data_drained`: edge-trigger backpressure via per-stream `tx_drain_pending`.
- `STREAM_DESTROY` event surfaced to user code so per-stream dicts (`_data_streams`, `_stream_tx_ctxs`) self-clean without explicit teardown.
- `picohttp_callback_free` forwards stream-released → universal sc destroy on raw-QUIC + WT. WT-side sc destroy gap from 0.3.5 closed for normal lifecycle (saturation orphan deferred — see Known Issues).

### Perf delta vs 0.3.5

- 5-run WT loopback at saturation (`-P 1 -s 4096 -g 10000 -r 0 -t 30 -i 30`): **2540 Mbps / p99 35.8 ms** vs 0.3.5 baseline 2233 Mbps / p99 49.7 ms. **+14% throughput / −28% p99.** Run-to-run variance also tighter post-session. Root cause: patch 0004 below.

### picoquic submodule advance + drop all local patches

- Pin advanced through `6a147578` → `eee50c1d` → `d6c5653d` (PR #2107 merged upstream). All four local patches landed in upstream picoquic — **`third_party/picoquic` is now vanilla upstream**.
- Patch 0003 (h3zero forwards `picoquic_callback_stream_released` as `picohttp_callback_free` so apps can release per-stream context mid-session) — upstream.
- Patch 0004 (`picoquic_mark_active_stream` with NULL `app_stream_ctx` preserves the existing value via `picoquic_mark_active_stream_v2`) — upstream; eliminates per-callback h3zero lookup; primary driver of the +14% / −28% delta.

### Lens B primitives + observability

- Per-stream typed pressure primitives: `stream_tx_buf_used(sid)`, `arm_stream_tx_drain_pending(sid)`, `clear_stream_tx_drain_pending(sid)`. Lets higher layers express "wait until per-stream sc->tx drains below N bytes" without leaking sc internals.
- `path_quality()` bridge exposing `picoquic_get_default_path_quality` (`cwin`, `bytes_in_transit`, `smoothed_rtt`, `pacing_rate`, `bytes_lost`, `bytes_sent`, `bytes_received`).
- `SIGUSR2` dump extension: per-cnx `path_quality` block with **BBR FREEZE SUSPECT** marker when `cwin ≈ 5 MSS` AND `pacing_rate` drops sharply (highlights picoquic#2118 reproduction).
- `send_stream_data_drained` robustness: catch up to picoquic's `wt_callback_free` change in `worker_callback_close` (rather than NULL-deref); propagate cancellations through `tx_drain_pending` so callers don't hang.

### Per-site sc lifecycle counters

- 12 counters across all sc create / ref / destroy call sites, exposed in `TransportContext.counters`:
  - Per-ctx: `sc_create_raw_quic`, `sc_create_wt_link`, `sc_ref_fc_credit`, `sc_destroy_wt_link_callback_free`, `sc_destroy_wt_link_close_walker`, `sc_destroy_fc_credit_pushfail`, `sc_destroy_fc_credit_worker`, `wt_callback_free_skipped`.
  - Process-wide: `sc_ref_chunk_wrap_total`, `sc_destroy_chunk_dealloc_total`, `sc_destroy_rx_event_total`, `sc_create_python_helper_total`, `sc_destroy_python_helper_total`.
- Invariant: `Σ creates + Σ refs == Σ destroys` at process end. Per-site imbalance localizes leaks. The split `cnt_sc_destroy_wt_link_{callback_free, close_walker}` is set at call sites (not inside `aiopquic_wt_stream_link_destroy`) so they remain safe under cnx-teardown when `link->session` may already be freed.

### Nomenclature pass (partial)

- `tx_ring` / `rx_ring` → `tx_event_ring` / `rx_event_ring` everywhere. SPSC event-rings now consistently distinguished from per-stream data byte-rings (`sc->tx`, `sc->rx`).
- `tx_pressure` / `tx_count` / `tx_capacity` and `get_tx_drain_event` renames to follow (deferred to 0.3.7 / 0.4.0).

### Build

- `AIOPQUIC_PERF=1` is now the **default** in `build_picoquic.sh` (host-tuned `-O3 -march=native -flto -fno-plt`). Bare `./build_picoquic.sh` now produces fast builds.
- `AIOPQUIC_WHEEL_BUILD=1` is the escape hatch: suppresses host tuning for portable wheel / CI builds.

### Cosmetic

- Suppress h3zero "Received a connection close request" stdout banner on WT clients (`h3_ctx->no_print = 1`). Raw-QUIC has no equivalent print; this matches WT logging to raw-QUIC.

### Known issues (deferred)

- **Pub-stream orphan at saturation.** Under sustained max-rate sends (typically `-P 1 -g 200 -r 0`), ~30% of publisher-side WT streams have their `picoquic_callback_stream_released` callback deferred until session close. All orphans clear at session close, so the leak is bounded, not permanent. Sub-saturation workloads (`-r ≤ 80K obj/s`, even at `-g 100 -P 8`) are completely clean. Per-leaked-link memory cost is bounded by `AIOPQUIC_TX_STREAM_RING_CAP_DEFAULT = 4 MiB`. Localized to picoquic-side `delete_stream_if_closed` timing under high churn; root cause investigation continues in 0.3.7.
- **Shutdown rx_event_ring drain miss.** At cnx-teardown picoquic's `picosplay_empty_tree` cascade fires `callback_free` for every remaining stream, pushing `STREAM_DESTROY + LINK_RELEASE` pairs into `rx_event_ring` after the dispatcher reader has detached. These events sit unprocessed until process exit. Bounded by `(stream count at close) × event size`. Correct fix is worker-side flush before join; deferred.
- **Double-free at `-P 8 -g 200`.** ASan-confirmed (2026-05-31), runtime crash signature. Same family as the orphan + drain-miss above. Deferred.
- **picoquic #2118 BBR ProbeBW_UP cwnd freeze on sub-128 µs RTT loopback.** Operator-overridable workaround: `--cc-algo newreno` or `bbr1`, or use Christian's purpose-built `c4` algorithm (1 ms RTT floor specifically for low-delay paths). Adaptive `MINRTT_THRESHOLD` PR pending upstream; Christian green-lit the design ("the relative comparison proposal makes sense").

## v0.3.5 (2026-05-25)

### RX flow control (release-blocker fix)

- Hysteresis-gated, per-cycle FC push: `drain_rx` / `drain_rx_callback` dedupe `SPSC_EVT_TX_OPEN_FLOW_CONTROL` to at most one per stream per cycle; further skipped when `rx_consumed` hasn't advanced by `ring_cap / 16` (~256 KB at default 4 MB cap). Reduces FC events from ~280K/s to single thousands/s, eliminating the `rx_event_ring` overflow cliff.
- `aiopquic_stream_ctx_t.bytes_pending_release` atomic + reference-counted `StreamChunk`. Worker grant = `buf_free − pending`. `AIOPQUIC_FC_RAW=1` bypasses for diagnostics.
- Sender-side sc lifecycle gap: no terminal callback after our TX FIN; bounded by stream count, deferred to 0.3.6 (xfail test surfaces it).
- WT-side sc destroy: `picohttp_callback_free → wt_link_destroy` not firing under MoQT subgroup workloads (`sc_destroyed_total = 0`). Bounded by stream count × ring_cap; deferred to 0.3.6.

### Observability

- Process-wide counters: `sc_{created,destroyed,alive}_total`, `chunks_alive_total`, `sc_rx_bytes_{pushed,popped}_total`, `sc_rx_bytes_in_flight`. Per-cnx: `fc_credit_{pushed,handled,dropped}`. All in `TransportContext.counters`.
- `dump_all_counters` per-stream walk is opt-in via `AIOPQUIC_DUMP_PER_STREAM=1` (default ctx-level dump is safe; per-stream walks deref stale `_stream_ctxs` entries — proper cleanup deferred).
- `TransportContext.enable_sigusr2_dump()` low-level entry; aiomoqt's `taskdump.install()` remains canonical.

### CC engagement (root-cause fix)

- `picoquic_register_all_congestion_control_algorithms()` now called at `TransportContext.start()`. Without it `cnx->congestion_alg` was NULL → cwin pinned at IW=15360. Raw-QUIC: ~575 Mbps → 2+ Gbps.

### TX pull-model

- `TransportContext.tx_send_stream(cnx, sid, data, end_stream, ring_cap)` — low-level pull primitive with per-`(cnx, sid)` sc lifecycle. Raises `BufferError` on ring-full.
- Legacy push events `SPSC_EVT_TX_STREAM_DATA` / `_FIN` (codepoints 128/129) removed; reserved one release cycle.
- Engine-path `_enqueue_raw` dispatches `_EVT_STREAM_TX_DRAINED` (fixes 2-proc pull-model producer hangs).

### TX/RX ring caps

- `aiopquic_ctx_create(tx_cap, rx_cap, low_water_pct)` — independent per-direction caps. Defaults: TX 2048, RX 16384 entries (was shared 262144). `AIOPQUIC_TX_RING_WAKE_PCT` env override.
- `TransportContext.__cinit__` accepts `tx_ring_cap` / `rx_ring_cap` / `tx_ring_low_water_pct` (back-compat: positional `ring_capacity` still works).

### Tests

- `tests/bench/test_rx_fc_counters.py` (4 tests, 1 xfail for the sender-side sc gap).
- `tests/bench/test_rx_bloat_during_load.py` — RSS-bound, parametrized over QUIC and WT.

### Docs

- `WAKE_PROTOCOL.md` rewritten: event-vs-data ring model, four-datapath summary, QUIC-vs-WT differences, naming intent (`tx_ring → tx_event_ring` rename deferred to 0.3.6). Existing 8 sequence diagrams + SIGUSR2 triage retained.

### Tooling / observability surface

- `--cc-algo` flag on `MOQTServer` / `MOQTClient` and 11 example tools; default BBR.
- `python -m aiopquic.versions` / `aiopquic-versions` enriched with picoquic + picotls `git describe --tags --always --long` + commit date + subject.
- `AIOPQUIC_RX_LOG=1` surfaces `rx_event_drops` and `rx_byte_ring_overflow` on stderr.
- `[tool.ruff] line-length = 100` (matches aiomoqt).

### Build

- `AIOPQUIC_PERF=1` env switch in `build_picoquic.sh`: host-tuned `-O3 -march=native -flto` + Fusion AES-GCM on x86_64 + `DISABLE_DEBUG_PRINTF`. Default OFF; never used in PyPI wheels.
- `AIOPQUIC_IO_URING=1` env switch (EXPERIMENTAL / DORMANT). Builds `picoquic_packet_loop_uring` into `libpicoquic-core.a` and statically links liburing; no runtime effect today (worker still uses portable loop). Linux-only. ABI footgun documented; setup.py mirrors `PICOQUIC_WITH_IO_URING` to picoquic build and Cython compile.
- New patch `patches/0002-picoquic-sockloop-drop-system-io_uring-header.patch` (dead code when `WITH_IO_URING=OFF`).
- Wheel perf flags via `[tool.cibuildwheel.linux.environment]`: `-flto` + `DISABLE_DEBUG_PRINTF` for all Linux; x86_64 adds `-march=x86-64-v3` + `PTLS_WITH_FUSION` (Haswell+; older systems install via sdist).
- Vendored `third_party/liburing` submodule pinned at `liburing-2.7`; auto-fetched / built static-only when `AIOPQUIC_IO_URING=1`, inert otherwise.
- `bootstrap_python.sh`: `PYTHON_VERSION="cpython-3.14"` (unambiguous GIL build); adds `setuptools_scm[toml]>=6.2` for editable installs.

## v0.3.4 (2026-05-19)

### TX stale-cnx UAF guard

The peer's `CLOSE_CONNECTION` (or app-side close, or timeout) can
free a `picoquic_cnx_t*` between Python's `push_tx` and the C
worker's SPSC pop. The previous `if (!cnx)` guard only caught
explicit NULL — not a stale-but-non-NULL pointer to freed memory.

Surfaced as `picoquic_create_stream` NULL-page faults on macOS at
multi-stream small-object unbounded-rate publishes. Same shape as
the `TX_CLOSE` UAF fixed in 0.3.2, but in the other TX_* handlers
(`MARK_ACTIVE`, `STREAM_DATA`, `STREAM_FIN`, `DATAGRAM`,
`STREAM_RESET`, `STOP_SENDING`, `OPEN_FLOW_CONTROL`,
`SET_APP_FLOW_CONTROL`).

Fix: factor the live-cnx-list walk that 0.3.2 added inline for
`TX_CLOSE` into a `static inline aiopquic_cnx_is_alive()` helper.
Apply the helper at the TX dispatch entry — every TX_* handler is
now UAF-safe. The `TX_CLOSE` case loses its duplicate inline walk.

Walk cost: O(N_cnx) per TX event. For aiomoqt's current publisher /
client shape (1–2 cnxs) it's a few pointer compares per event —
effectively free. Marked TODO for replacement with a generation
counter (O(1)) if a many-cnx relay role emerges.

WT events (SPSC IDs 136–142) bypass the new guard via a short-circuit
at the dispatch entry. Their `entry->cnx` is an
`aiopquic_wt_session_t*`, not a `picoquic_cnx_t*`; for `TX_WT_OPEN`
specifically the picoquic cnx doesn't exist yet (it's created inside
`picowt_prepare_client_cnx`). Walking picoquic's live-cnx list with a
wt_session pointer always misses and dropped every WT TX event,
timing out WT CONNECT at 10 s. WT sessions own their own lifecycle
in `aiopquic_wt_handle_tx`.

### `QuicConnection.send_stream_data_drained()` — async send with built-in backpressure

New high-level method that bundles ring-saturation wait + soft post-
send yield around `send_stream_data`. The "obvious correct way" to
send from an async producer loop:

```python
await quic.send_stream_data_drained(stream_id, data, end_stream=False)
```

Composes the existing primitives (`tx_pressure`, `get_tx_drain_event`,
`send_stream_data`) so any caller using this gets worker-thread-aware
pacing without copying the heuristic. Behavior layered into one
coroutine:

- **Hard ring guard**: `tx_pressure > hard_wait_at` (default 0.9)
  awaits the SPSC drain event before queuing.
- **Send**: atomic `send_stream_data` (push + MARK_ACTIVE + worker
  wake under one GIL hold; all-or-nothing on BufferError).
- **Soft yield**: after a successful send, `tx_pressure > soft_yield_at`
  (default 0.5) does `await asyncio.sleep(0)` so the worker thread
  sees the GIL. Avoids the count-based heuristic that starved the
  worker on fast Python paths (notably Linux + UDP GSO).
- **BufferError retry**: ring went full between the pressure check
  and the send — await drain, retry the same buffer.

Application-level policies (byte budgets, fairness across streams,
etc.) belong above this layer. aiomoqt 0.9.5's `MOQTSession.
stream_write_drain` shrinks accordingly: only the bytes-aware
producer-budget policy stays in MOQT land; the transport-level wait/
yield mechanics move down here.

Raw-QUIC only in this release. The WT data-stream send path still
uses the older push model (`push_stream_data` → `TX_STREAM_DATA` with
inline payload → `picoquic_add_to_stream`), so it has no per-stream
`sc->tx` byte ring and no edge-trigger drain event. `WebTransportSession`
therefore doesn't expose `get_tx_drain_event` / `tx_pressure` and the
WT path keeps the existing polling-sleep fallback.

Deferred architectural follow-up: migrate WT data streams to the
pull model (per-WT-stream `sc->tx` + `tx_send_atomic` + MARK_ACTIVE
+ `prepare_to_send`). That gives WT real per-stream backpressure —
essential because publishers commonly multiplex streams with very
different object sizes and pacing profiles, where a per-session
waker would be a half-measure. The data streams are real QUIC streams
under picowt; the divergence is implementation history, not a picowt
constraint.

## v0.3.3 (2026-05-18)

Pairs with [aiomoqt 0.9.5](https://pypi.org/project/aiomoqt/0.9.5/).

### `QuicConnection.tx_pressure(stream_id) -> float`

New public method: returns the TX-ring fill ratio in `[0.0, 1.0]`.
A soft companion to the hard `BufferError` boundary that
`send_stream_data` already raises. Tight send loops can use this to
release the GIL when the picoquic worker has pending TX entries to
drain, avoiding count-based yield heuristics that can starve the
worker on fast Python paths.

`stream_id` is reserved for future per-stream ring accounting; today
the ring is connection-global, so the returned value is global.
aiomoqt 0.9.5 uses this in `MOQTSession.stream_write_drain` to
auto-yield under pressure.

## v0.3.2 (2026-05-16)

Pairs with [aiomoqt 0.9.4](https://pypi.org/project/aiomoqt/0.9.4/).
Send-path perf wins + a macOS-only UAF that crashed at session close.

### TX_CLOSE UAF (macOS argo segfault)

`SPSC_EVT_TX_CLOSE` dereferenced a stale `cnx*`: the peer's
CLOSE_CONNECTION could free the cnx between the Python push and the
worker's pop, and any `cnx->state` read (including a state guard)
hit the freed pointer. macOS exposed it as `picoquic_close+0x1c`
segfaults at session teardown.

Fix: walk picoquic's live-cnx list to confirm the pointer is still
valid before calling `picoquic_close`. Two commits: `517928a`
restored a state-guard scaffold, `005de88` replaced it with the
live-list walk.

### Perf

- `send_length_max = 65535` (GSO max) — single sendmsg covers a
  full 64KB worth of UDP datagrams on Linux.
- Cython `drain_rx_callback` — coalesces stream-data events in C
  before they cross into Python.
- Cython `parse_object_subgroup` / `encode_object_subgroup` — moves
  the hot MoQT body-shape parser into the binding.
- Event-driven TX backpressure — `SPSC_EVT_STREAM_TX_DRAINED` wakes
  per-stream waiters when the worker drains, replacing the polling
  loop the higher level had been using.

### Platform fix

`AIOPQUIC_GSO` env override + Darwin defaults: macOS has no
UDP GSO, so `do_not_use_gso=1` and `send_length_max=0` are the
defaults there. Linux gets the GSO send_length_max=65535 default.

### Picoquic pin

Advanced to upstream `2b1e14d5` + open PR #2097 ("Do not reschedule
already closed connection") as a build-time patch.

## v0.3.1 (2026-05-12)

Receiver-side dispatch perf. The 0.3.0 release fixed durability +
backpressure; 0.3.1 attacks the per-chunk Python dispatch cost that
was the sustained-rate wall on Ryzen WSL2 (~534 Mbps consumer cap).
0.3.1 moves the coalescing into Cython: when the picoquic worker
fires multiple stream_data callbacks for the same stream in quick
succession, `drain_rx` now drops the redundant no-data SPSC events
that the C-side per-stream byte ring already absorbed. Net result:
**87% fewer Python `_on_event` dispatches per session**, 10× lower
mp-loopback latency, ~3× higher sustained ceiling on the same box.

Sustained verification at this commit (Ryzen WSL2, mp-loopback,
30 s windows, paired with aiomoqt 0.9.3):

| Target | Delivered | avg lat (0.3.0 / 0.3.1) | Loss |
|---|---|---|---|
|  250 Mbps |  250 Mbps |  32 ms / **3.9 ms**  | 0 |
|  500 Mbps |  506 Mbps | 567 ms / **8 ms**    | 0 |
|  980 Mbps |  987 Mbps | 3938 ms / **65 ms**  | 0 |
| 1500 Mbps | 1.3-1.4 Gbps | — / ~150 ms       | 0 |

Cross-platform sanity on argo (Apple M4, 4P+6E, native macOS — no
WSL vSwitch overhead):

| Target | Tx | Rx | avg lat |
|---|---|---|---|
| 250 Mbps  | 253 Mbps  | 261 Mbps  | **0.5–0.9 ms** |
| 500 Mbps  | 506 Mbps  | 526 Mbps  | **1–2 ms**     |

Sub-millisecond MoQT delivery on native macOS at quarter-Gbps was
not previously visible — WSL2's UDP loopback floor (3.2 Gbps with
~1.5 ms native syscall cost) was masking it.

### Cython drain_rx coalescing (the big one)

`drain_rx` already pops ALL bytes available in `sc->rx` on the
FIRST event it sees for a stream this cycle. Subsequent SPSC
events for the same stream therefore find `avail == 0` and emit
events with `data=None` — pure dispatch waste that Python had to
walk through. 0.3.1 drops those no-data DATA events in Cython
before they reach Python. FIN events still emit unconditionally
(they carry end-of-stream regardless of data presence).

Common path: applies to both raw-QUIC `SPSC_EVT_STREAM_DATA` and
WT `SPSC_EVT_WT_STREAM_DATA`.

cProfile diff on 30 s aiomoqt subscriber @ 250 Mbps mp-loopback:

| metric                        |    0.3.0 |   0.3.1 | delta  |
|-------------------------------|---------:|--------:|-------:|
| total function calls          |   55.7 M |  31.4 M | -44%   |
| `_on_event` (aiopquic) calls  |  1,459 K |   186 K | -87%   |
| `_on_event` (aiomoqt) calls   |  1,459 K |   186 K | -87%   |
| `put_nowait` calls            |  1,567 K |   357 K | -77%   |
| `_on_event` self-time         |   3.09 s |  0.38 s | -88%   |

`drain_rx` is now spending its time on actual payload deliveries
instead of shuttling empty events. Receiver ceiling on Ryzen WSL2
moved from ~534 Mbps to ~1.4 Gbps (the new wall is single-Python-
process receive CPU, not dispatch).

### WT `setdefault(sid, asyncio.Queue())` allocation bug

`dict.setdefault(key, default)` evaluates its default every call,
even when the key is present. The four WT event dispatch sites
that used `self._stream_inbox.setdefault(sid, asyncio.Queue())`
were therefore allocating a fresh asyncio.Queue (plus its
internal asyncio.Lock) on every inbound WT event — 1.46 M wasted
allocations per 30 s session at 250 Mbps. After fix: 30.8 K
allocations (only the genuine new-stream cases).

Replaced with explicit `get` + create-if-None at 4 sites
(`receive_stream_data`, `_EVT_WT_STREAM_DATA`, `_EVT_WT_STREAM_FIN`,
`_EVT_WT_STREAM_RESET`). Raw QUIC path doesn't use this idiom — no
equivalent fix needed.

### `WT-Available-Protocols` plumbing (WebTransport subprotocols)

The WT CONNECT request constructed by aiopquic previously hardcoded
`NULL` for picoquic's `wt_available_protocols` argument, so clients
had no way to advertise subprotocols in the WebTransport handshake.
This broke MoQT-over-WT version negotiation per moq-transport-16 §3.1
("MOQT uses ALPN in QUIC and `WT-Available-Protocols` in WebTransport
to perform version negotiation"): moxygen-based relays could not see
the offered protocol set, fell back to legacy CLIENT_SETUP version-
array parsing, and rejected the connection. Discovered 2026-05-13
when basic adaptive_bench WT → mvfst (test.moqx.akaleapi.net) was
broken in 0.9.x but fine in 0.8.2 (qh3 silently masked the missing
header in the prior transport).

The fix:

- `WebTransportClient.__init__` accepts `wt_available_protocols:
  list[str] | None`. Stored on the session; opaque list of
  subprotocol identifiers — aiopquic does no interpretation.
- `connect_webtransport(...)` forwards the same kwarg.
- `WebTransportClient.open()` SF-list-formats the list per RFC 9651
  (each value double-quoted, comma-joined) before push_open, since
  picoquic copies the value verbatim into the QPACK literal — it
  does not do Structured Fields formatting on its side.
- `aiopquic_wt_open_params_t` (the SPSC payload) gets a
  `protocols_len: uint16_t`; bytes packed after the path block.
- C-side TX_WT_OPEN handler unpacks the protocols string and passes
  it to `picowt_connect` instead of NULL. Empty list still passes
  NULL (no header on the wire).

Layer-clean: aiopquic stays MoQT-agnostic. The string the caller
hands us goes on the wire verbatim. aiomoqt (0.9.3) is the one
that decides what to advertise based on its draft.

### `aiopquic.exceptions.StreamUnderflow` (layer cleanup)

`StreamChain` previously did `from aiomoqt.messages.base import
MOQTUnderflow` inline in `pull_*` helpers — a transport-layer
module importing from an upper-layer protocol library. New
`aiopquic.exceptions.StreamUnderflow` exception (same `(pos,
needed)` shape) replaces those raises. Aiomoqt 0.9.3 aliases
`MOQTUnderflow = StreamUnderflow` so existing `except` sites in
that layer keep working unchanged. No upward imports remain in
aiopquic.

### Compatibility

- `aiopquic.exceptions.StreamUnderflow` is the canonical exception
  raised by `StreamChain.pull_*` going forward. Callers that
  imported `MOQTUnderflow` from `aiomoqt.messages.base` still
  catch the same exception (alias).
- No ABI/protocol/wire change. Wire-level behaviour identical to 0.3.0.

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
