# aiopquic → aiomoqt mating: validation + integration plan

aiopquic v0.1.0 shipped as a pure-QUIC drop-in for `qh3.quic`. Original PLAN.md ended at Phase 4 (asyncio integration); Phase 5 (HTTP/3) and Phase 6 (polish + interop) were abandoned when commit `270d3d6` removed the qh3 H3 re-export shim — H3/WT now belongs to consumers, not aiopquic.

This document picks up where PLAN.md stopped and defines the work to (a) re-validate aiopquic on its own against current upstream picoquic, and (b) integrate it as the QUIC backend for aiomoqt.

## Phase A — Pull picoquic upstream + revalidate aiopquic standalone

**Goal**: prove aiopquic is healthy on the latest upstream picoquic before changing anything else.

1. Bump vendored `third_party/picoquic` submodule from `6b1d3788` → upstream master HEAD (`060142ba` at time of writing, ~274 commits including WT/Chrome interop polish, capsule API, picotls 2026-04-23 update, RFC 9000 compliance fixes).
2. Rebuild via `build_picoquic.sh`. Investigate any compile errors in the Cython binding (struct layout drift, function signature changes) — these are the only meaningful ABI risks from a submodule bump.
3. Re-run the existing test suite:
   - `tests/test_spsc_ring.py` — ring correctness, atomics
   - `tests/test_transport.py` — Cython transport lifecycle
   - `tests/test_loopback.py` — full client↔server loopback
   - `tests/test_asyncio.py` — asyncio protocol bridge
   - `tests/test_interop.py` — already exists; verify still passing
4. Add real-public-endpoint coverage to `test_interop.py`:
   - picoquic's own `picoquicdemo` (spawn as subprocess; matches what their tests use)
   - `cloudflare-quic.com` (HTTP/0.9 over QUIC, no H3 needed)
   - Any other public QUIC test endpoints we can find that don't require H3
   The point isn't H3-functional testing — it's "does our handshake + raw streams + datagrams work against real-world stacks?"

**Deliverable**: green test run on upstream picoquic; documented baseline for aiomoqt integration.

## Phase B — Data-flow design pass: minimum-copy from picoquic to app

**Goal**: explicitly design the byte path so we know we're not silently introducing copies as we wire aiomoqt onto aiopquic.

### Current state (theoretical optimum: 2 copies per object — 1 RX, 1 TX)

**RX** path:
```
UDP → picoquic (decrypts, frames)                               (in C)
  → callback fn (bytes ptr valid only during callback)
  → C: memcpy into RX SPSC ring data_arena                       [COPY #1, unavoidable]
  → eventfd wake → asyncio
  → Cython: drain_rx() yields memoryview into data_arena
  → Python: QuicConnection event with memoryview payload
  → aiomoqt: Buffer(memoryview)
  → aiomoqt: parser slices memoryview for fields              (no copy if careful)
  → aiomoqt: SubscribedTrack.on_object(msg, size_bytes, ...) ← memoryview slice
  → app callback: consumes the slice                          (caller owns lifetime contract)
```
Total: 1 unavoidable copy. Critical: the memoryview must propagate through aiomoqt's Buffer/parser/Track API without materializing `bytes`.

**TX** path:
```
app: track.send(payload: bytes-like)
  → PublishedTrack: build object header into a pre-allocated send buffer  (no copy of payload)
  → aiomoqt: Buffer.write_bytes(payload) into ring entry slot              (no copy if zero-copy ring API)
  → SPSC TX ring entry written; wake_up
  → picoquic prepare_to_send callback fires
  → C: picoquic_provide_stream_data_buffer() returns pkt buffer
  → C: memcpy(pkt_buf, ring_entry.data, length)                            [COPY #1, unavoidable]
  → picoquic encrypts and sends
```
Total: 1 unavoidable copy.

### Where we'll likely lose this

1. **aiomoqt's `Buffer`** today wraps `bytes`/`bytearray`, not memoryview. Every parse path probably materializes `bytes(buf.pull_bytes(n))`. Fix: make `Buffer` accept memoryview, propagate slices.
2. **aiomoqt's track callback**: `on_object(msg, size_bytes, recv_time_ms)` — `msg.payload` is currently `bytes`. Change to memoryview with a "valid only inside this call" contract; for users that need ownership, document `bytes(msg.payload)` opt-in.
3. **TX path object construction**: `PublishedTrack` likely allocates a `bytes` per object, then frames into another buffer. Refactor to write headers + payload into a single pre-allocated bytearray that becomes the TX ring entry.
4. **Cython binding cost**: drain_rx() returns Python tuples — make sure the data field is a memoryview, not `bytes(...)`.

### Lifetime contract (the hard part)

Memoryview into the SPSC ring's data_arena is only valid while the ring entry exists. As the consumer reads through the ring, old entries' regions get reclaimed. So:

- App callback semantics: "the memoryview you receive is valid until you return from this call." Same contract qh3 already exposes.
- If the app needs to retain bytes, document `bytes(view)` as the explicit copy point.
- Internally aiomoqt MUST NOT cache memoryviews into a deque or queue without copying — the ring will overwrite them.

### Investigation tasks (before/during Phase D)

- [ ] Audit `aiomoqt/utils/buffer.py` (or wherever Buffer lives) — does it accept memoryview?
- [ ] Audit `aiomoqt/messages/*.py` — does deserialize hold references to source bytes, or does it copy?
- [ ] Audit `SubscribedTrack.on_object` callback — what's the type contract for `msg.payload`?
- [ ] Audit `PublishedTrack.publish_object()` (or equivalent) — how many copies on TX?
- [ ] Audit `aiopquic._binding._transport.drain_rx()` — does it return memoryview or `bytes`?

Each audit informs whether the qh3 → aiopquic swap is straight or requires aiomoqt-side cleanup.

## Phase B-1 — Replace re_buf with a chained-memoryview deque parser

**Goal**: Eliminate the per-stream 32MB pre-allocated `re_buf` accumulator and the dual-state-machine that maintains it. Replace with an O(unparsed-bytes) deque of memoryviews that the parser walks directly. Naturally falls out of "propagate memoryview from aiopquic without materializing bytes."

### Why
Current design (`aiomoqt/protocol.py:_process_data_stream`) keeps a 32MB pre-allocated `re_buf: Buffer` per subgroup stream. Every QUIC chunk that doesn't complete the in-flight message is `data_slice`d and `push_bytes`d into `re_buf`. After a successful parse, the next message processing happens against `re_buf` until drained, then flips back to `msg_buf` mode. The `cur_pos` / `msg_len` / `needed` / `have` state spans both buffers.

Failure modes observed:
- **Memory**: 50 subscribers × 32MB committed = 1.6GB virtual just for accumulators.
- **Throughput**: every chunk-boundary message gets memcpy'd through `push_bytes`. Hot path.
- **Correctness suspected**: under sustained load we see framer desync (PARSE EXCEPTION with garbage varints, ObjectStatus values pulled from payload bytes, ext_len absurd values). The dual-state seam at `_process_data_stream:484-509` is the most fragile spot in the file. Strong candidate for being the desync source.

### Design

```python
class StreamChain:
    """Per-stream chained-memoryview reader.

    Producer (data-stream task) appends arriving memoryview chunks
    via .extend(view). Parser consumes from the front via pull_*
    primitives. When a parse succeeds, consumed-from-front memoryviews
    are dropped, releasing the underlying SPSC ring entries in aiopquic.
    """
    _chunks: deque[memoryview]    # unconsumed chunks, in arrival order
    _read_pos: int                # byte offset into _chunks[0]
    _total_unread: int            # cached len() across the deque

    def extend(self, view: memoryview) -> None: ...      # producer
    def total(self) -> int: ...                          # for underflow checks
    def pull_uint_var(self) -> int: ...                  # walk if needed
    def pull_bytes(self, n: int) -> memoryview: ...      # contiguous if possible
    def commit(self) -> None: ...                        # confirm parse; drop consumed
    def rollback(self) -> None: ...                      # revert to last commit
```

**Parser primitive contract:**
- `pull_uint_var()` reads 1-8 bytes; almost always within `_chunks[0]`. If a varint genuinely spans a chunk boundary, copy 8 bytes onto a stack buffer and decode there. Rare path.
- `pull_bytes(n)` returns a memoryview slice. If `n` fits within `_chunks[0]` from `_read_pos`, return zero-copy. Otherwise return a flat memoryview backed by a one-time concatenation. The size-hint matters: object payloads can be large (KB-MB). For payloads we hold the chained view; if a downstream consumer truly needs contiguous bytes, it calls `bytes(view)` itself.
- `commit()` advances `_read_pos`; drops any fully-consumed memoryviews from the deque front (releases SPSC ring slots).
- `rollback()` restores `_read_pos` to the last commit. Used on `MOQTUnderflow`: parser reverts, waits for next chunk to arrive in deque, retries.

**Replaces in `_process_data_stream`:**
- The `re_buf` Buffer.
- The `if available < needed: ... elif cur_pos == msg_len: ...` triage.
- `data_slice` + `push_bytes` accumulation.
- Both `msg_buf` and `re_buf` modes — there's only one mode now.

```python
async def _process_data_stream(self, stream_id: int) -> None:
    chain = StreamChain()
    while True:
        try:
            async with asyncio.timeout(MOQT_IDLE_STREAM_TIMEOUT):
                msg_view = await self._stream_queues[stream_id].get()
            if msg_view is None:
                return
            chain.extend(msg_view)
        except asyncio.TimeoutError:
            raise

        while chain.total() > 0:
            try:
                msg_obj = self._moqt_handle_data_stream_chain(stream_id, chain)
            except MOQTUnderflow:
                chain.rollback()
                break  # wait for more data
            except MOQTStreamReject as e:
                self._reject_stream(stream_id, e.error_code, e.reason)
                return
            except Exception as e:
                # ... existing parse-error stream-abandon path
                return
            chain.commit()
            # dispatch msg_obj as today
```

**Memory profile**: `chain` holds only what the producer queued and the parser hasn't consumed. Backpressure is naturally observable as `chain.total()`. No fixed pre-allocation per stream.

### Steps

1. Implement `StreamChain` (target file: `aiomoqt/utils/buffer.py` or new `aiomoqt/utils/stream_chain.py`).
2. Audit which `Buffer.pull_*` methods aiomoqt's parser actually uses; add equivalent methods on `StreamChain` that work zero-copy when possible.
3. Convert `MOQTUnderflow` to be parser-internal: parser raises after a partial read; `StreamChain.rollback()` undoes any pull_* that happened in this attempt. Implementation: parser ops mutate a *tentative* `_read_pos`; commit/rollback applies or discards.
4. Refactor `_moqt_handle_data_stream` to take `StreamChain` instead of `Buffer`. (Internal API; no public breakage.)
5. Refactor `_process_data_stream` per the sketch above.
6. Tests:
   - Unit: chunk a serialized SubgroupHeader+ObjectHeader sequence into 1-byte slices; assert parser still produces identical messages.
   - Stress: replay a captured high-load packet trace through both old and new code paths; assert byte-for-byte output equality.
   - Memory: 100-stream loopback; verify resident set stays bounded with chain vs old `re_buf`.
7. Once validated, delete the `re_buf` code path entirely. Single mode going forward.

### Risk

- Parser primitives that today read multi-byte ints by reading from a single contiguous buffer need to handle the rare cross-chunk case. Most likely sites: `pull_uint_var`, `pull_bytes(n)` with large n. A single helper `_contiguous(n: int) -> memoryview` that copies once when needed contains the complexity.
- `MOQTUnderflow` semantics change: old code used `e.needed - msg_len` to compute "how many more bytes." New design just retries when more arrives in the deque. Simpler, but check that no callers depend on `e.needed` for sizing decisions (sniff before refactor).

### Deliverable

- Net code reduction in `_process_data_stream` (the dual-buffer state machine becomes a single clean loop).
- Bounded per-stream memory (drops the 32MB ceiling).
- Zero-copy data path from picoquic → aiopquic SPSC ring → aiomoqt parser → application callback.
- Plausible fix for the framer-desync bug observed under load (since the most likely source — the `re_buf` / `msg_buf` seam — no longer exists).

## Phase C — aiomoqt-aiopquic interface mapping

**Goal**: define the surface aiomoqt binds against and confirm aiopquic provides it.

aiomoqt's two transport modes:

### Raw QUIC (`MOQTClient(use_quic=True)`, scheme `moqt://`)

- Today: imports `qh3.quic.connection.QuicConnection`, `qh3.quic.configuration.QuicConfiguration`, `qh3.asyncio.client.connect`, `qh3.quic.events.*`.
- Swap: replace `qh3.quic` with `aiopquic.quic`; replace `qh3.asyncio` with `aiopquic.asyncio`.
- **Risk surface**: any qh3 API aiomoqt uses that aiopquic doesn't yet expose. Build a compatibility check list during Phase B audit.

### H3/WebTransport (`MOQTClient(use_quic=False)`, scheme `https://`)

- Today: aiomoqt wraps qh3's `H3Connection` over qh3's `QuicConnection` and routes WT bidi/uni streams through `WebTransportStreamDataReceived` events (Phase 2 routing fix from v0.8.2). Carries the `_h3_bind_wt_bidi` workaround for qh3's client-bidi state-tracking gap.
- Three options for aiopquic:
  1. **picoquic's webtransport.c via aiopquic** (RECOMMENDED) — bind `pico_webtransport.h` (128-line public API) + h3zero callback dispatch into Cython; expose as `aiopquic.webtransport`. picoquic's WT is fully merged into upstream master with active polish: Chrome compatibility (`13f99955`), capsule API, WT-Protocol header handling, wildcard CONNECT path matching, authority extraction. ~200-300 LOC of Cython binding. Eliminates the qh3 client-bidi workaround.
  2. **qh3.h3 atop aiopquic.quic** — keep qh3's pure-Python H3 framing; just give it aiopquic's QuicConnection underneath. Lighter binding work but H3 frame decode (varints, capsules) stays in Python on the hot path.
  3. **Pure-Python H3+WT in aiomoqt** — write our own. Lots of work, no upside.

**Why option 1 is now primary** (this reverses the original ranking, which was risk-averse from when we assumed aiopquic-WT meant reimplementation):
- The H3 frame parse hot path moves from Python to C. Same parsing class — varints, frame headers, extension length fields — that produced the v0.8.2 framer-desync bug under load.
- The Phase 2 WT routing fix and `_h3_bind_wt_bidi` shim from v0.8.2 become moot — picoquic already routes bidi state correctly.
- aiomoqt's `_h3_handle_event` / `_h3_handle_wt_stream` / `_strip_wt_header` apparatus deletes; `MOQTSession` collapses to "open a stream, read/write MoQT frames" with the same code path for both `use_quic=True` and `use_quic=False`.
- We have freedom on the aiomoqt side, so the simplification is realizable.

**Implementation sketch for option 1**:
- Cython `aiopquic._binding._h3.pyx` (or similar) wraps:
  - `picowt_prepare_client_cnx` / `picowt_set_control_stream` / `picowt_connect`
  - `picowt_create_local_stream` (bidi/uni) / `picowt_reset_stream`
  - Capsule send: `picowt_send_close_session_message`, `picowt_send_drain_session_message`
  - h3zero callback dispatch: route `picohttp_callback_post_data` / `_post_fin` / `_provide_data` / `_post_datagram` / etc. into the existing SPSC RX ring, tagged with WT stream IDs
- Python `aiopquic.webtransport.WebTransportSession` exposes an asyncio-friendly API: `open_bidi_stream()`, `open_uni_stream()`, `close()`, `drain()`, `recv_datagram()`, `send_datagram()`.
- aiomoqt swaps its `_h3_handle_*` paths for direct calls into `WebTransportSession`. Same MoQT framing code reads from a Stream object whether the underlying transport is raw QUIC or WT.

Fall back to option 2 only if a specific blocker emerges in the Cython binding (e.g., callback semantics that don't fit the SPSC model).

### Validation milestone

A "two-line swap" probe inside aiomoqt:
```python
# aiomoqt/client.py
from aiopquic.quic.connection import QuicConnection      # was: qh3.quic.connection
from aiopquic.quic.configuration import QuicConfiguration  # was: qh3.quic.configuration
```
Run aiomoqt's loopback test. Whatever breaks is the integration shopping list.

## Phase D — Integration commits on `gmarzot-aiomoqt-support`

Once Phase C identifies the gap, land changes on the `gmarzot-aiomoqt-support` branch:
- API parity fixes on aiopquic side (event class names, method signatures, missing fields)
- Buffer/memoryview plumbing if Phase B audit found unnecessary copies
- Any picoquic-side workarounds (analogous to the qh3 `_h3_bind_wt_bidi` shim from v0.8.2)

Ship aiopquic v0.2.0 once aiomoqt loopback passes against it.

## Phase E — aiomoqt swap branch

Open a `pivot-to-aiopquic` branch on aiomoqt itself. Run the full v0.8.2 regression matrix against aiopquic. Document any behavior deltas. Re-run the load-stress scenarios that revealed the framer desync — if it's gone, that's a major win for the pivot. If it's still there, it's a clue about where the bug actually lives (since the code paths are now different).

## Out of scope here

- QMUX (picoquic PR #2087) — track upstream; if it lands during this work we re-evaluate. Probably won't ship in time.
- macOS/BSD support — aiopquic is currently Linux-only (eventfd dependency). Document; punt.
- Pure-Python H3 framing (qh3.h3 reuse) — was originally the recommended path; demoted to fallback after we confirmed picoquic ships a complete Chrome-validated WT implementation behind `pico_webtransport.h`. The native binding is now Phase C option 1.
