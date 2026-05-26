# Wake Protocol — TX/RX Sequence Diagrams

Authoritative reference for the wake/event sequences across the
producer (Python) → Cython → SPSC event ring → picoquic worker
pipeline, and the parallel per-stream `sc->tx` / `sc->rx` byte ring
data path.

Code reference: **aiopquic perf-0.3.5** / **aiomoqt perf-0.9.5**
(refreshed 2026-05-25 to align with the two-ring / event-vs-data
model and to document the QUIC-vs-WT asymmetries surfaced during
the RX-FC investigation).

---

## Naming evolution & design intent

These are the design directions the codebase is migrating toward.
The names in the diagrams below reflect *current* code; the
forthcoming renames / refactors are listed here so the doc is the
single source of truth for "what does this ring carry, really."

1. **`ctx->tx_ring` → `ctx->tx_event_ring`** and **`ctx->rx_ring` →
   `ctx->rx_event_ring`**. The current names are ambiguous — they
   suggest "TX data" and "RX data" but they are event channels with
   only incidental payload-carrying. Rename clarifies role.
2. **Datagrams will adopt the stream-like `sc->rx` / `sc->tx`
   pattern.** Today datagrams piggy-back on the event ring via the
   inline-payload form of `spsc_ring_push`. This is the *only* path
   (besides a handful of WT control events) that pushes bytes
   through the event ring. Giving datagrams a dedicated per-session
   byte ring keeps event entries fixed-size and small.
3. **aiopquic does not expose WT control interfaces.** The WT
   control stream (capsules: CLOSE_SESSION, DRAIN_SESSION, etc.)
   surfaces only as session-level *events* to the aiopquic asyncio
   API. WT control-byte framing lives inside aiopquic's
   `_binding/c/h3wt_callback.h`; callers see typed events.
4. **Two event rings are essential.** One ring per direction
   prevents RX and TX flows from interfering. Their backpressure
   models can be kept simple precisely because they don't carry
   data — see the [Event vs data backpressure](#event-vs-data-backpressure)
   table.

---

## The two-ring model — events vs data

Each `(asyncio thread) ↔ (picoquic worker thread)` direction crosses
the GIL boundary over **two independent rings**, each with distinct
purpose, sizing, and backpressure model.

| Ring | Class | Granularity | Cap | Carries |
|------|-------|-------------|-----|---------|
| `ctx->tx_ring` (→ `tx_event_ring`) | **EVENT-flow** | fixed `spsc_entry_t` (~64 B) | thousands of entries | wake codes, FC grant requests, stream lifecycle events, MARK_ACTIVE, TX_DRAINED, *legacy*: occasional small inline payloads (datagrams, WT control) |
| `ctx->rx_ring` (→ `rx_event_ring`) | **EVENT-flow** | same | same | RX wake codes, STREAM_DATA notifications (payload-less; bytes already in sc->rx), STREAM_FIN, STREAM_DESTROY, TX_DRAINED replies |
| `sc->tx` | **DATA-flow** | raw byte ring | MB-sized (default 4 MB) | actual TX stream payload bytes, one ring per stream |
| `sc->rx` | **DATA-flow** | raw byte ring | MB-sized (default 4 MB) | actual RX stream payload bytes, one ring per stream |
| (WT only) `s->bridge->rx_ring` | **EVENT-flow** | same as `ctx->rx_ring` | same | WT-specific RX events; bytes for data streams still live in the per-stream `sc->rx` |

### Event vs data backpressure

| Concern | Event ring | Data ring (`sc->rx` / `sc->tx`) |
|---|---|---|
| Overflow severity | **HIGH** — lost event = lost wake = silent hang | Recoverable: RX overflow is a peer FC violation (spec'd close); TX overflow surfaces as BufferError + drain wait |
| Sizing rule of thumb | Entries are tiny; size to never reasonably overflow under burst | Sized to advertised flow-control window |
| Backpressure mechanism | CAS-armed `tx_ring_drain_pending` + `TX_RING_DRAINED` reply event | RX: `picoquic_open_flow_control(grant)` driven by `buf_free - bytes_pending_release`. TX: `tx_drain_pending` flag + `STREAM_TX_DRAINED` reply |
| Drop policy | Counter + re-arm + retry; drops are *bugs* | RX overflow = close connection; TX overflow = block producer |

---

## The four datapaths

A connection has four logical flows. Each uses BOTH ring types — the
event ring to signal, the data ring (or sc->rx/sc->tx) to carry
payload. Steady-state behavior should fit on one diagram per flow.

### RX peer → app (per stream)

```
peer STREAM frame
  → picoquic reassembles in-order bytes                  [worker thread]
  → callback delivers (bytes, length)
  → DATA: stream_buf_push(sc->rx, bytes, length)         [sc->rx fills]
  → EVENT: spsc_ring_push(rx_ring, STREAM_DATA)          [event-only]
  → wake-fd                                              [→ asyncio]
  → drain_rx pops event                                  [asyncio thread]
  → DATA: stream_buf_pop(sc->rx, ...)                    [sc->rx drains]
  → wrap StreamChunk + memoryview
  → consumer touches memoryview                          [pending_release ↓]
  → EVENT: spsc_ring_push(tx_ring, OPEN_FLOW_CONTROL)    [event-only]
  → wake worker                                          [→ worker]
  → DATA: picoquic_open_flow_control(grant) → wire MAX_STREAM_DATA
```

### TX app → peer (per stream)

```
producer calls send_stream_data(bytes)                   [asyncio thread]
  → DATA: stream_buf_push(sc->tx, bytes, length)         [sc->tx fills]
  → EVENT: spsc_ring_push(tx_ring, MARK_ACTIVE)          [event-only]
  → wake worker                                          [→ worker]
  → worker calls picoquic_mark_active_stream
  → prepare_to_send fires
  → DATA: stream_buf_pop(sc->tx) → into picoquic frame   [sc->tx drains]
  → wire STREAM frame
  → if sc->tx was full at push: tx_drain_pending=1, producer waits
  → on drain: EVENT spsc_ring_push(rx_ring, TX_DRAINED)  [event-only]
  → wake asyncio                                         [→ asyncio]
  → producer's sc_event fires, retry succeeds
```

### RX flow control (we tell peer how much we can take)

```
first-touch:  picoquic_open_flow_control(cnx, sid, buf_free_post_push)
              (called inline in the stream_data callback on worker)

ongoing:      drain_rx pops sc->rx bytes
              → consumer touches → pending_release ↓
              → EVENT push OPEN_FLOW_CONTROL
              → worker: grant = buf_free(sc->rx) - bytes_pending_release
              → picoquic_open_flow_control(cnx, sid, grant)
              → picoquic emits MAX_STREAM_DATA (only if advancing — monotonic)
```

### TX flow control (peer tells us how much we can send)

```
internal to picoquic. Observable signal:
  prepare_to_send budget == 0 → sc->tx fills → producer blocks
                                              (via tx_drain_pending)
  peer MAX_STREAM_DATA arrives → prepare_to_send budget > 0
                                → sc->tx drains → TX_DRAINED → producer wakes
```

---

## WT vs Raw QUIC — where the paths diverge

WT layers H3 capsule framing on top of QUIC streams. The wire-level
QUIC FC and the SPSC event mechanics are *identical*; the
differences are at the framing boundary and at first-touch.

| Stage | Raw QUIC | WT |
|---|---|---|
| Wire→app entry | direct `aiopquic_stream_cb` ([callback.h](src/aiopquic/_binding/c/callback.h)) | through **picohttp / h3zero capsule parser** → `aiopquic_wt_path_callback` ([h3wt_callback.h](src/aiopquic/_binding/c/h3wt_callback.h)) |
| Stream identity | picoquic `stream_id` | `(wt_session, wt_link)` wrapper around `stream_id` |
| First-touch FC mode | not switched; `initial_max_stream_data` from transport params applies | **`picoquic_set_app_flow_control(cnx, sid, 1)`** — switches to app-controlled FC |
| Event ring used | `ctx->rx_ring` directly | **`s->bridge->rx_ring`** — separate bridge-scoped ring |
| Stream-create signal | inline first stream_data event | dedicated `WT_NEW_STREAM` event with BORROWED `sc` |
| Stream destroy | `SPSC_EVT_STREAM_DESTROY` (raw QUIC pure-receiver only) | `wt_link_destroy` via different teardown path |
| Control plane | none — application owns out-of-band signalling | dedicated WT control stream + capsule framing (CLOSE/DRAIN) |
| Datagrams | future: dedicated per-session byte ring | future: same |

**Known asymmetries under investigation** (2026-05-25):

- **RX bloat**: identical workload at saturation produces bounded
  RSS on raw QUIC and unbounded RSS on WT. Suspected: capsule-layer
  buffering inside picohttp/h3zero before bytes reach `sc->rx`, OR
  `s->bridge->rx_ring` accounting separate from `ctx->rx_ring`
  visibility.
- **2-process vs in-process loopback**: WT throughput drops ~40%
  and seizes after ~17 s in 2-process mode but is steady-state
  stable in in-process loopback. Not the same bug as the bloat.

These belong to the [Case 5–6 RX](#case-5--rx-sub-saturation-steady-state)
flow but currently have no diagnostic-quality counter signature —
adding instrumentation is gated on `ctx.dump_counters()` (see below).

---

## Counter dump API

`TransportContext.dump_counters()` exposes the per-cnx and per-stream
counters as a Python dict suitable for test assertions and
SIGUSR2-style runtime introspection. See implementation notes
below the existing counter reference table; the C-side fields are
the same ones called out in the per-case "Counter signature" blocks.

---

## Participants

```
+--------------------------+  Python / asyncio loop
|  App producer            |   (aiomoqt MOQTSession.stream_write_drain,
|                          |    aiopquic send_stream_data_drained)
+------------+-------------+
             |
             v  send_stream_data() — atomic GIL-held push
+--------------------------+  Cython, still on asyncio thread
|  TransportContext        |
|   push_tx() / sc send    |
+------------+-------------+
             |
             | (a) per-stream pull model:
             |     sc->tx byte ring        (stream_ctx.h)         [DATA]
             | (b) push model / TX events:
             |     ctx->tx_ring SPSC       (callback.h)           [EVENT]
             v
+--------------------------+  picoquic worker thread
|  picoquic packet_loop_v3 |   prepare_to_send callback,
|                          |   aiopquic_stream_cb
+------------+-------------+
             |
             | wire send                rx_ring SPSC + wake-fd    [EVENT]
             |                          sc->rx byte ring          [DATA]
             |                          rx_consumed / open_flow_control
             v
        (peer / loopback)
```

Notation: `[file:line]` references the implementing site. Counter
fields live on `aiopquic_ctx_t` (cnx-global, [callback.h:130+]) and
`aiopquic_stream_ctx_t` (per-stream, [stream_ctx.h:70+]). `_ns` fields
hold the most-recent `CLOCK_MONOTONIC` ns timestamp.

---

## Counter / log reference

| Counter                           | Owner       | Incremented at                                                            |
|----------------------------------- |------------ |-------------------------------------------------------------------------- |
| cnt_tx_ring_pushes                 | ctx         | Python push into ctx->tx_ring [_transport.pyx]                            |
| cnt_tx_ring_pops                   | ctx         | worker pop in prepare_to_send / WT routing [callback.h:595, h3wt_callback.h] |
| cnt_tx_ring_arms                   | ctx         | `aiopquic_arm_tx_ring_drain_pending` [callback.h:324-329]                 |
| cnt_tx_ring_fires                  | ctx         | `aiopquic_maybe_fire_tx_ring_drained` push OK [callback.h:371-374]        |
| cnt_tx_ring_fire_dropped           | ctx         | fire CAS won, rx_ring push failed, re-armed [callback.h:375-386]          |
| last_tx_ring_arm_ns / fire_ns      | ctx         | timestamp on the matching event                                            |
| cnt_wake_calls                     | ctx         | this caller WILL invoke picoquic_wake_up [callback.h:307-308]             |
| cnt_wake_skipped_coalesced         | ctx         | already pending, coalesced [callback.h:309-311]                           |
| cnt_prepare_to_send_empty          | ctx         | sc->tx empty / picoquic offered length==0 [callback.h:527-534]            |
| worker_prepare_to_send_calls       | ctx         | every prepare_to_send invocation [callback.h:506-507]                     |
| worker_prepare_to_send_pulled_bytes| ctx         | bytes popped from sc->tx into picoquic [callback.h:524-526]               |
| worker_rx_event_drops              | ctx         | rx_ring full at push time [callback.h:733-744]                            |
| worker_rx_byte_ring_overflow       | ctx         | peer overran sc->rx (FLOW_CONTROL_ERROR) [callback.h:683-702]             |
| sc->cnt_drain_arms                 | per-stream  | sc->tx full at send_data, or explicit arm [stream_ctx.h:75, :199, :95]    |
| sc->cnt_drain_fires                | per-stream  | worker fired SPSC_EVT_STREAM_TX_DRAINED [callback.h:562-564]              |
| sc->cnt_drain_dropped              | per-stream  | CAS won, rx_ring push of STREAM_TX_DRAINED failed [callback.h:565-573]    |
| sc->last_drain_arm_ns / fire_ns    | per-stream  | timestamps                                                                |

Opt-in stderr lines (worker thread, AIOPQUIC_RX_LOG=1):

```
[aiopquic_rx] EVENT RING FULL: drop stream=<sid> evt=<n> (rx_ring entries=<k>)
[aiopquic_rx] stream=<sid> RX ring overflow: pushed <p> of <len> ... Peer flow-control violation.
```

Picoquic textlog signatures (worker, AIOPQUIC_TEXTLOG_FILE=...):
NewReno CC collapse looks like `cwin: 3072` (= 2 × MSS minimum) with
RTT trace `rtt: 55µs → 499ms` and `nb_ret: 118 → 136 → ...`, followed
by `Too many retransmits, abandon path` every ~1.128s. This is
*upstream* of the wake protocol — it's what the protocol *cannot*
fix by itself.

---

## aiomoqt overlay

aiomoqt is the **typical caller** of the aiopquic TX path covered
below. `MOQTSession.stream_write_drain`
([aiomoqt/aiomoqt/protocol.py:1231-1344](aiomoqt/aiomoqt/protocol.py#L1231-L1344))
wraps `send_stream_data` and adds **two MoQT-level backpressure
layers** above the dual-event mechanism shared with aiopquic's own
`send_stream_data_drained`. The wake primitives (sc_event,
ring_event, FIRST_COMPLETED on BufferError) are identical to
aiopquic's — only the *threshold* logic differs.

```
+------------------------------------------------+
|  MoQT writers (track.py subgroup/fetch/control)|
|    next_objects_bytes_batch (live encoder)     |
+------+-----------------------------------------+
       |  per-object: 1-N calls
       v
+------------------------------------------------+
|  MOQTSession.stream_write_drain                |
|   Path A: byte-budget        (tx_max_inflight_bytes)
|     arm_stream_tx_drain_pending → await sc_event
|   Path B: hard ring-pressure (tx_pressure > 0.9)
|     arm_tx_ring_drain_pending  → await ring_event
|   Send: send_stream_data (atomic)
|   On BufferError: dual-event FIRST_COMPLETED
+------+-----------------------------------------+
       |
       v
+------------------------------------------------+
|  aiopquic QuicConnection.send_stream_data      |
|   atomic: push event → ctx->tx_ring            |
|            push bytes → sc->tx                 |
|            MARK_ACTIVE + wake_up               |
|   raises BufferError if either ring is full;   |
|   Cython side has already armed the matching   |
|   *_drain_pending flag before returning        |
+------------------------------------------------+
```

**Callers in [aiomoqt/aiomoqt/track.py](aiomoqt/aiomoqt/track.py):**
subgroup writer paths at lines 387, 401, 779, 807 — all of MoQT's
data-bearing streams flow through `stream_write_drain`. Control and
session helpers (CLIENT_SETUP, SUBSCRIBE, FETCH, GOAWAY) use the
session's bidi control stream and route through the same path.

**Path A — byte-budget (currently dormant)**
([protocol.py:1267, 1281-1290](aiomoqt/aiomoqt/protocol.py#L1281-L1290)):
intent is per-stream latency cap — block once `sc->tx->used` exceeds
`tx_max_inflight_bytes` (default 2 MB ≈ ~10 ms at 1.6 Gbps).
Implementation arms `sc->tx_drain_pending` via
`arm_stream_tx_drain_pending` and waits on `sc_event`. **Status:**
the gate metric `tx_pending_bytes(stream_id)` currently returns the
connection-global SPSC aggregate (always ~0 in pull-model raw QUIC)
per the D4 surgical revert in
[connection.py](aiopquic/src/aiopquic/quic/connection.py) — so the
branch is wired correctly but the metric never trips. Fix: switch
`tx_pending_bytes` to return per-stream `sc->tx->used` when
`stream_id` is given.

**Path B — hard ring-pressure**
([protocol.py:1293-1301](aiomoqt/aiomoqt/protocol.py#L1293-L1301)):
when `tx_pressure(stream_id) > 0.9` (= SPSC TX event ring is >90%
full), clear `ring_event`, arm `tx_ring_drain_pending`, re-check,
and wait. Same clear-arm-recheck-wait pattern as aiopquic's
`send_stream_data_drained`.

**`next_objects_bytes_batch` (shipping in aiomoqt 0.9.5)** — the
live-encoder convenience that lets a producer hand multiple
contiguous objects to a single `stream_write_drain` call, reducing
GIL turnover. Not a new wake mechanism; just a coalescer above
this protocol. See [project_session_level_coalescing.md](/home/gmarzot/.claude/projects/-home-gmarzot-Projects-moq-aiomoqt/memory/project_session_level_coalescing.md)
for the deferred session-level auto-coalescing follow-on (0.10.x).

**aiomoqt-specific guarantee:** `stream_write_drain` is the **only**
sanctioned data-path entry. MoQT track writers must not bypass it to
call `send_stream_data` directly — doing so loses the Path-A/B
threshold logic and turns the writer into a tight loop that can
starve the worker thread. The session-writable check at
[protocol.py:1269-1270](aiomoqt/aiomoqt/protocol.py#L1269-L1270)
also depends on going through this entry.

---

## Case 1 — TX sub-saturation, steady state

Producer rate < worker drain rate. sc->tx never fills, SPSC TX ring
stays under high-water. No events armed; the wake-coalescing flag in
`aiopquic_tx_wake_set_pending` ([callback.h:304-313]) deduplicates
worker wakeups.

```
Producer        Cython send_data        ctx->tx_ring       sc->tx        picoquic worker
   |                  |                       |                |                |
   |--push(data)----->|                       |                |                |
   |                  |  push event ----------+                |                |
   |                  |  push bytes -------------------------->|                |
   |                  |  tx_wake_set_pending (0→1)             |                |
   |                  |  picoquic_wake_up ---------------------------->         |
   |<-return OK-------|                       |                |                |
   |                  |                       |                |  pop event     |
   |                  |                       |<---------------+----- pop bytes |
   |                  |                       |                |  send on wire  |
   |                  |                       |                |                |
   | (next push)      |                       |                |                |
   |--push(data)----->|                       |                |                |
   |                  |  push event           |                |                |
   |                  |  tx_wake_set_pending (already=1)       |                |
   |                  |  → cnt_wake_skipped_coalesced++        |                |
   |<-return OK-------|                       |                |                |
```

**Source:**
- Producer entry: [aiomoqt/aiomoqt/protocol.py:1311-1322](aiomoqt/aiomoqt/protocol.py#L1311-L1322)
- Atomic push: [aiopquic/src/aiopquic/_binding/c/stream_ctx.h:174-211](aiopquic/src/aiopquic/_binding/c/stream_ctx.h#L174-L211)
- Worker pop: [aiopquic/src/aiopquic/_binding/c/callback.h:506-577](aiopquic/src/aiopquic/_binding/c/callback.h#L506-L577)
- Wake coalesce: [callback.h:304-313](aiopquic/src/aiopquic/_binding/c/callback.h#L304-L313)

**Counter signature (steady-state sub-sat):**
```
cnt_tx_ring_pushes ≈ cnt_tx_ring_pops    (lag of at most a few)
cnt_tx_ring_arms = 0
cnt_tx_ring_fires = 0
sc->cnt_drain_arms = 0
sc->cnt_drain_fires = 0
cnt_wake_calls << cnt_tx_ring_pushes     (most pushes coalesced)
cnt_wake_skipped_coalesced ≈ cnt_tx_ring_pushes - cnt_wake_calls
worker_prepare_to_send_pulled_bytes > 0 and growing
cnt_prepare_to_send_empty present but small (worker faster than producer)
```

---

## Case 2 — TX saturation, sc->tx full (per-stream byte ring)

Producer outruns the per-stream byte ring (`sc->tx` capacity =
QuicConfiguration.tx_stream_ring_cap, default ~4 MB). `send_data`
pre-checks `free_bytes < len`, atomically arms `sc->tx_drain_pending`
and returns 0 → Cython surfaces as `BufferError`.

```
Producer       Cython send_data        sc->tx        picoquic worker
   |                |                     |               |
   |--push(LARGE)-->|                     |               |
   |                | free_bytes < len    |               |
   |                | sc->cnt_drain_arms++                |
   |                | last_drain_arm_ns ← now             |
   |                | tx_drain_pending = 1                |
   |                | return 0 → BufferError              |
   |<-BufferError---|                     |               |
   |                |                     |  pop bytes    |
   | sc_wait task ──┐                     |               |
   | ring_wait task ┤  asyncio.wait FIRST_COMPLETED       |
   |                |  prepare_to_send: CAS tx_drain_pending 1→0
   |                |  push SPSC_EVT_STREAM_TX_DRAINED into rx_ring
   |                |  sc->cnt_drain_fires++; last_drain_fire_ns ← now
   |                |  notify_rx → wake-fd
   |                | asyncio drains rx_ring → drain_rx_callback
   |                | sees STREAM_TX_DRAINED → sc_event.set()
   |<-sc_event------|                     |               |
   | retry push --->|                     |               |
```

**Race-free guarantee:** producer **clears `sc_event` BEFORE** the
`send_stream_data` call ([protocol.py:1307-1308](aiomoqt/aiomoqt/protocol.py#L1307-L1308)).
If the worker drains and fires between our clear and our wait, the
set lands after the clear and we capture it on the wait. Clearing
**after** BufferError loses that wakeup.

**Source:**
- Pre-check + arm: [stream_ctx.h:188-202](aiopquic/src/aiopquic/_binding/c/stream_ctx.h#L188-L202)
- Worker CAS-clear + fire: [callback.h:551-575](aiopquic/src/aiopquic/_binding/c/callback.h#L551-L575)
- Producer dual-event wait: [protocol.py:1323-1335](aiomoqt/aiomoqt/protocol.py#L1323-L1335),
  [connection.py:547-558](aiopquic/src/aiopquic/quic/connection.py#L547-L558),
  [webtransport.py:315-326](aiopquic/src/aiopquic/asyncio/webtransport.py#L315-L326)

**Counter signature (sc->tx saturated, healthy drain):**
```
sc->cnt_drain_arms  > 0
sc->cnt_drain_fires ≈ sc->cnt_drain_arms     (no lost wakes)
sc->cnt_drain_dropped = 0
last_drain_fire_ns > last_drain_arm_ns       (wake came after arm)
cnt_tx_ring_arms = 0                         (different ring path)
worker_prepare_to_send_pulled_bytes growing
```

**Stuck signature (per-stream wake lost):**
```
sc->cnt_drain_arms > sc->cnt_drain_fires
last_drain_arm_ns > last_drain_fire_ns       (arm is more recent)
worker_prepare_to_send_calls growing while pulled_bytes stuck → check
  Case 4 / Case 8.
```

---

## Case 3 — TX saturation, SPSC event ring full (push model)

The `ctx->tx_ring` is the connection-global SPSC ring carrying
discrete TX events (control TX, WT commands, legacy push-model
stream data). In the pull-model raw-QUIC path, stream payload bytes
do **not** go through this ring — they go through `sc->tx`. But TX
events still flow through `tx_ring`, and producer code paths that
arm `tx_ring_drain_pending` (e.g. high-pressure soft guards in the
two `send_stream_data_drained` wrappers) use this signal.

```
Producer        Cython push_tx          ctx->tx_ring     picoquic worker
   |                  |                       |                 |
   |--tx_pressure---->|                       |                 |
   |  > 0.9?          |                       |                 |
   |  ring_event.clear()                      |                 |
   |  arm_tx_ring_drain_pending()             |                 |
   |  cnt_tx_ring_arms++; last_tx_ring_arm_ns ← now             |
   |  recheck tx_pressure                     |                 |
   |  > 0.9? → await ring_event               |                 |
   |                  |                       |  pop event      |
   |                  |                       |  cnt_tx_ring_pops++
   |                  |                       |  maybe_fire_tx_ring_drained
   |                  |                       |  load tx_ring_drain_pending == 1
   |                  |                       |  count ≤ low_water? yes
   |                  |                       |  CAS 1→0 win
   |                  |                       |  push SPSC_EVT_TX_RING_DRAINED into rx_ring
   |                  |                       |  cnt_tx_ring_fires++
   |                  |                       |  notify_rx → wake-fd
   |  asyncio drain_rx → ring_event.set()     |                 |
   |<-ring_event------|                       |                 |
   |  retry           |                       |                 |
```

**Source:**
- Producer clear-arm-recheck-wait: [connection.py:527-534](aiopquic/src/aiopquic/quic/connection.py#L527-L534),
  [webtransport.py:293-301](aiopquic/src/aiopquic/asyncio/webtransport.py#L293-L301),
  [protocol.py:1293-1301](aiomoqt/aiomoqt/protocol.py#L1293-L1301)
- Worker maybe_fire: [callback.h:355-387](aiopquic/src/aiopquic/_binding/c/callback.h#L355-L387)
- Threshold: `ctx->tx_ring_low_water` (default 50% of `tx_ring_cap`,
  [callback.h:63](aiopquic/src/aiopquic/_binding/c/callback.h#L63))

**Counter signature (ring-saturation, healthy):**
```
cnt_tx_ring_arms > 0
cnt_tx_ring_fires ≈ cnt_tx_ring_arms
cnt_tx_ring_fire_dropped = 0
last_tx_ring_fire_ns > last_tx_ring_arm_ns
cnt_tx_ring_pops growing alongside fires
```

**Stuck signature (ring-event wake lost):**
```
cnt_tx_ring_arms > cnt_tx_ring_fires + cnt_tx_ring_fire_dropped
last_tx_ring_arm_ns > last_tx_ring_fire_ns
```

**Note on D4 partial revert:** `QuicConnection.tx_pending_bytes()` now
returns the connection-global SPSC aggregate (always ~0 in pull
model). The Path B byte-budget branch in
`stream_write_drain` ([protocol.py:1281-1290](aiomoqt/aiomoqt/protocol.py#L1281-L1290))
therefore arms the per-stream `sc->tx_drain_pending` via
`arm_stream_tx_drain_pending` but on a metric that is currently
inert. Documented for completeness; the branch is dormant until
`tx_pending_bytes` is wired to per-stream `sc->tx->used`.

---

## Case 4 — TX dual-event BufferError (both rings can fail)

`send_stream_data` is atomic: it pushes the TX event into the SPSC
ring AND pushes payload bytes into `sc->tx` under one GIL hold.
Either step can fail:

- `sc->tx` full → Cython arms `sc->tx_drain_pending` (Case 2 path)
- `ctx->tx_ring` full → Cython arms `ctx->tx_ring_drain_pending` (Case 3 path)

The producer can't know which fired, so it waits on **both** with
`asyncio.wait(FIRST_COMPLETED)`. The race the clear-before-send
guard prevents:

```
                T0        T1        T2          T3              T4
Producer:    sc_event.clear()
             ring_event.clear()
             send_stream_data() ──────────────────────► raises BufferError
                          (worker drained between push attempts)
                          worker fires SPSC_EVT_STREAM_TX_DRAINED
                                              sc_event.set()
                                                              await wait(...)
                                                              ←── set seen

Lost-wake variant (NOT what we do):
             send_stream_data() ─► BufferError
                          worker fires set ►
                                  sc_event.set()
                                              sc_event.clear()  ← WAKE LOST
                                                              await wait(...)  forever
```

By clearing **before** the send, any set that lands during the send
attempt remains observable on the subsequent wait.

```
Producer        Cython           sc->tx    ctx->tx_ring   worker
   |               |               |            |             |
   | sc_event.clear()                                          |
   | ring_event.clear()                                        |
   |--push(data)-->|               |            |             |
   |               | try push event into tx_ring → FULL       |
   |               | arm_tx_ring_drain_pending                |
   |               |  OR                        |             |
   |               | push event OK, push bytes into sc->tx → FULL
   |               | arm sc->tx_drain_pending                 |
   |<-BufferError--|               |            |             |
   | sc_wait, ring_wait = create_task each                    |
   | asyncio.wait(FIRST_COMPLETED)                            |
   |               |               |            | pop / drain |
   |               |               |            | fire either:|
   |               |               |            | STREAM_TX_DRAINED → sc_event.set
   |               |               |            | TX_RING_DRAINED   → ring_event.set
   |←──────────────first one wins; pending task cancelled────|
   | retry push    |               |            |             |
```

**Source:** [protocol.py:1311-1335](aiomoqt/aiomoqt/protocol.py#L1311-L1335),
[connection.py:540-558](aiopquic/src/aiopquic/quic/connection.py#L540-L558),
[webtransport.py:307-326](aiopquic/src/aiopquic/asyncio/webtransport.py#L307-L326)

**Counter signature (productive saturation, both signals firing):**
```
sc->cnt_drain_arms  > 0   sc->cnt_drain_fires  ≈ arms
cnt_tx_ring_arms    > 0   cnt_tx_ring_fires    ≈ arms
both last_*_fire_ns recent (within a few ms of dump time)
worker_prepare_to_send_pulled_bytes growing steadily
```

**Note:** A successful sub-saturation flow should not show
`cnt_tx_ring_arms > 0` (because hard threshold 0.9 is never
crossed). Seeing both counters at zero with throughput rolling
means everything is in the pure happy path of Case 1.

---

## Case 5 — RX sub-saturation, steady state

Peer sends, picoquic delivers stream_data callback on worker thread
(`aiopquic_stream_cb`). First-touch creates the per-stream wrapper
and opts into application flow control. Subsequent calls push bytes
into `sc->rx`, then push a (payload-less) SPSC event into `rx_ring`
to notify asyncio. **Credit replenishment goes back through the TX
event ring** — see Case 6 for the steady-state loop.

```
peer / picoquic worker      sc->rx     rx_ring     asyncio    ctx->tx_ring    worker (later)
   |                          |          |            |            |              |
   | stream_data cb           |          |            |            |              |
   | (first touch) create sc                                                       |
   |              open_flow_control(buf_free)  ── direct: worker is in picoquic ctx
   |              rx_credit_store(advertise_cap)                                   |
   | push bytes ───────────► |          |            |            |              |
   | spsc_ring_push(evt)─────────────► |            |            |              |
   | notify_rx → wake-fd      |          |            |            |              |
   |                          |          | wake-fd readable                       |
   |                          |          | drain_rx_callback                       |
   |                          |          | pop event ►                            |
   |                          | pop bytes ◄─                                       |
   |                          |          | rx_consumed_add(n)                     |
   |                          |          | push SPSC_EVT_TX_OPEN_FLOW_CONTROL ──►|
   |                          |          | dispatch StreamDataReceived            |
   |                          |          |            |            | pop TX evt ►|
   |                          |          |            |            | picoquic_open_flow_control(n)
   |◄── MAX_STREAM_DATA on wire ─────────────────────────────────────────────────|
```

**Why first-touch is different from steady-state:** worker calls
`picoquic_open_flow_control(buf_free)` synchronously inline in the
stream_data callback because the worker is already in picoquic
context. All *subsequent* credit advances happen on the asyncio
thread (which is not in picoquic context), so they have to
round-trip through the TX event ring to a worker-thread call.

**Source:**
- Worker push + first-touch FC (synchronous):
  [callback.h:638-724](aiopquic/src/aiopquic/_binding/c/callback.h#L638-L724)
- Consumer drain + credit advance via TX event ring: see
  `_drain_and_convert` and `push_open_flow_control` in
  [_transport.pyx:1009, 1072-1092](aiopquic/src/aiopquic/_binding/_transport.pyx)
- Worker-side TX_OPEN_FLOW_CONTROL handler:
  [callback.h:905-926](aiopquic/src/aiopquic/_binding/c/callback.h#L905-L926)
- Wake-fd coalesce: [callback.h:397-409](aiopquic/src/aiopquic/_binding/c/callback.h#L397-L409)

**Counter signature:**
```
worker_rx_event_drops = 0
worker_rx_byte_ring_overflow = 0
rx_notify_pending toggles 0↔1 fast (not visible in counters; visible
  in throughput stability)
cnt_tx_ring_pushes growing from credit traffic + any TX data
```

---

## Case 6 — RX backpressure (consumer slower than peer)

Python consumer is slower than the peer's send rate. `sc->rx` fills
toward its physical cap (= advertised window). The worker has
**already** issued the initial `picoquic_open_flow_control` grant at
first-touch; subsequent credit comes from the **consumer** via
`SPSC_EVT_TX_OPEN_FLOW_CONTROL` once it advances `rx_consumed`.

```
peer        worker stream_data cb           sc->rx            consumer
 |                |                           |                   |
 |--bytes (1)--->|                            |                   |
 |               | push → sc->rx              |                   |
 |               | push event → rx_ring                            |
 |               | notify_rx                                        |
 |--bytes (2)--->|                            |                   |
 |               | push → sc->rx              |                   |
 |               | (rx_consumed unchanged → no extension fired)   |
 |               |                            |  drain_rx pops    |
 |               |                            | rx_consumed += n  |
 |               |                            | push SPSC_EVT_TX_OPEN_FLOW_CONTROL
 |               |                            |    with n bytes as credit
 |               |  worker pops TX event                            |
 |               |  picoquic_open_flow_control(n) ► peer advances  |
 |<-WINDOW UP----| MAX_STREAM_DATA frame      |                   |
```

If consumer falls *far* behind, peer's send window exhausts (peer
becomes flow-control-blocked at MAX_STREAM_DATA). Producer stalls
upstream until consumer drains. This is the **intended** end-to-end
backpressure path — no policy is needed in aiopquic beyond honoring
the consumer's drain rate.

**Source:**
- First-touch grant: [callback.h:713-717](aiopquic/src/aiopquic/_binding/c/callback.h#L713-L717)
- Threshold const: `AIOPQUIC_RX_FC_THRESHOLD_DIV` ([callback.h]) — 1/4
  the advertised window; reserved for future worker-side hysteresis
- Consumer credit: drained via `SPSC_EVT_TX_OPEN_FLOW_CONTROL` from
  the asyncio drain path (see `_drain_and_convert` in _transport.pyx)

**Counter signature (FC-throttled peer):**
```
worker_rx_event_drops = 0
worker_rx_byte_ring_overflow = 0
worker_prepare_to_send_calls high (worker still active sending ACKs/FC)
worker_prepare_to_send_pulled_bytes flat (no app TX pending)
peer-side: stalled at the granted MAX_STREAM_DATA
```

---

## Case 7 — RX event ring overflow

`rx_ring` is full when the worker tries to push an event into it.

**Relative likelihood at current sizing:** `rx_ring` capacity is
~262144 entries; per-stream `sc->rx` is ~4 MB. At typical
stream_data event size (~1.2 KB payload), one full `sc->rx`
corresponds to ~3000 events. So `sc->rx` fills **far before**
`rx_ring` does — the more common backpressure path is the peer
stalling at MAX_STREAM_DATA (Case 6), not the rx event ring
overrunning. Case 7 becomes relevant if (a) the rx_ring is sized
much smaller, (b) many concurrent streams share the rx_ring while
each consumer is slow, or (c) wake-event traffic (STREAM_TX_DRAINED,
TX_RING_DRAINED) is heavy alongside data.

**For stream_data events** — bytes are pushed into `sc->rx`
*before* the event push attempt, so when the event push fails the
bytes still live in `sc->rx`. Any later event for the same stream
triggers a drain pass that pops them. Net data loss = 0 *unless* no
later event ever arrives (very short stream).

**For wake events** (STREAM_TX_DRAINED, TX_RING_DRAINED) — these
are the path the producer relies on to wake from BufferError. A
silent drop here would deadlock the producer. The fire-side code
**re-arms `_drain_pending`** instead of accepting the drop, so the
next worker pop retries (see Case 8 for the TX_RING_DRAINED
variant; the STREAM_TX_DRAINED variant is at
[callback.h:565-573](aiopquic/src/aiopquic/_binding/c/callback.h#L565-L573)).

```
worker stream_data cb               sc->rx            ctx->rx_ring
       |                              |                    |
       | push bytes ─────────────────>|  OK                |
       | spsc_ring_push(event) ──────────────────────────►| FULL → ret != 0
       | worker_rx_event_drops++                          |
       | worker_rx_event_drops_stream_data++              |
       | (if AIOPQUIC_RX_LOG=1, stderr line; throttled to 100)
```

**Source:** [callback.h:723-744](aiopquic/src/aiopquic/_binding/c/callback.h#L723-L744)

**Counter signature (event ring overrun, bytes safe):**
```
worker_rx_event_drops > 0
worker_rx_event_drops_stream_data > 0
worker_rx_byte_ring_overflow = 0   (bytes still fit in sc->rx)
stderr (if AIOPQUIC_RX_LOG=1):
  [aiopquic_rx] EVENT RING FULL: drop stream=N evt=4 (rx_ring entries=K)
```

**Distinct from spec violation:**
`worker_rx_byte_ring_overflow > 0` means the peer overran our
advertised window — FLOW_CONTROL_ERROR territory; the connection
returns -1 from the callback and picoquic closes.

---

## Case 8 — TX_RING_DRAINED fire dropped (rx_ring full at fire time)

The wake protocol's most subtle path. Worker pops a TX event,
sees `tx_ring_drain_pending == 1` AND count ≤ low_water, **wins the
CAS-clear**, then tries to push `SPSC_EVT_TX_RING_DRAINED` into
`rx_ring` — and the rx_ring is full. The CAS is already committed;
without re-arm, the producer's wake is lost.

```
worker (pop loop)              ctx->tx_ring_drain_pending      rx_ring
       |                                  |                       |
       | spsc_ring_pop(tx_ring) → entry   |                       |
       | cnt_tx_ring_pops++               |                       |
       | maybe_fire_tx_ring_drained():    |                       |
       |   load pending == 1              |                       |
       |   count ≤ low_water? yes         |                       |
       |   CAS 1 → 0  ── WIN ────────────>|                       |
       |   spsc_ring_push(TX_RING_DRAINED) ─────────────────────►| FULL
       |   cnt_tx_ring_fire_dropped++     |                       |
       |   re-arm: pending = 1 ──────────►|                       |
       |   (next maybe_fire retries when rx_ring has room)        |
```

Re-arm guarantees the producer's wake eventually fires: pending=1
persists until the next CAS-clear succeeds AND rx_ring push
succeeds. The matching STREAM_TX_DRAINED variant is at
[callback.h:560-573](aiopquic/src/aiopquic/_binding/c/callback.h#L560-L573).

**Source:** [callback.h:371-386](aiopquic/src/aiopquic/_binding/c/callback.h#L371-L386)

**Counter signature (rx_ring under producer-saturation pressure):**
```
cnt_tx_ring_fire_dropped > 0
cnt_tx_ring_arms > 0
worker_rx_event_drops > 0  OR  cnt_tx_ring_fire_dropped tracks
  closely with general rx_ring congestion
last_tx_ring_arm_ns may exceed last_tx_ring_fire_ns transiently;
  re-arm should close the gap on the next worker iteration
```

If `cnt_tx_ring_fire_dropped` grows monotonically while
`cnt_tx_ring_fires` stays flat, rx_ring is chronically full and
the consumer is the wall, not the producer.

---

## Reading a SIGUSR2 dump — quick triage

```
  cnt_drain_arms  >  cnt_drain_fires + cnt_drain_dropped ?
     YES → per-stream wake lost (Case 2 / Case 4 race) — bug
     NO  → check next

  cnt_tx_ring_arms > cnt_tx_ring_fires + cnt_tx_ring_fire_dropped ?
     YES → ring-event wake lost (Case 3 / Case 4 race) — bug
     NO  → check next

  worker_rx_event_drops > 0 ?
     YES → rx_ring saturation (Case 7); bytes safe in sc->rx;
           consumer is the wall
     NO  → check next

  worker_prepare_to_send_calls growing AND pulled_bytes flat ?
     YES → worker is being called but no bytes; sc->tx is empty.
           Producer-side or upstream picoquic CC (NewReno collapse,
           etc.) — wake protocol is not the cause
     NO  → next

  cnt_prepare_to_send_empty growing ?
     YES → worker faster than producer (sub-sat regime)
```

Cross-reference `last_*_arm_ns` with `last_*_fire_ns`: if `arm_ns`
is more recent than `fire_ns`, the most recent arm is still
outstanding. The dump's wall-clock is in the SIGUSR2 header; the
per-counter ns values share the same CLOCK_MONOTONIC base.

---

## Out of scope

This document is the *wake protocol* — what events fire, when, and
how the producer waits. It does **not** cover:

- Picoquic congestion control (NewReno collapse, BBR behavior under
  GIL-induced RTT spikes). Today's loopback freezes at high rate
  are CC-layer, upstream of everything here.
- MoQT-layer dispatch/parse cost (the Cython-migration opportunity
  tracked in [project_aiopquic_aiomoqt_layering.md](/home/gmarzot/.claude/projects/-home-gmarzot-Projects-moq-aiomoqt/memory/project_aiopquic_aiomoqt_layering.md)).
- RX flow-control credit math: covered above in [The four datapaths](#the-four-datapaths)
  and [Case 5–6](#case-5--rx-sub-saturation-steady-state). The
  asymmetries flagged under [WT vs Raw QUIC](#wt-vs-raw-quic--where-the-paths-diverge)
  are active investigation items, not steady-state behavior.

When counters show the wake protocol is clean but throughput is
stuck, the cause is in one of those upstream layers.
