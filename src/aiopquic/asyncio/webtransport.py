"""WebTransport asyncio API.

Three classes:
  WebTransportSession      — base; established-session API (create_stream,
                              send_stream_data, receive_stream_data,
                              events, close, drain, reset_stream, ...).
                              Holds .role ('client' or 'server') because
                              MoQT and other higher layers care.

  WebTransportClient(...)  — initiator subclass; adds open(). Used by
                              connect_webtransport(host, port, path).

  WebTransportServerSession(...) — acceptor subclass (Phase C.4 step 2/3);
                              constructed from an incoming H3 CONNECT.

The cdef class WebTransportSessionState (in _binding._transport) is the
C-side state holder; both subclasses share one. A process-wide
_DispatcherRegistry routes drained SPSC events to the right session by
the wt_session pointer.
"""
from __future__ import annotations

import asyncio
import socket
from collections import deque
from contextlib import asynccontextmanager, suppress
from typing import AsyncGenerator

from aiopquic._binding._transport import (
    TransportContext, WebTransportSessionState,
)
from aiopquic.quic.events import (
    WebTransportSessionRefused,
    WebTransportSessionClosed, WebTransportSessionDraining,
    WebTransportStreamDataReceived, WebTransportStreamReset,
    WebTransportStopSending, WebTransportDatagramReceived,
    WebTransportNewStream,
)


# Must match spsc_ring.h SPSC_EVT_STREAM_TX_DRAINED — shared with
# the raw-QUIC path. Fired by the picoquic worker when sc->tx drains
# after a Python writer was blocked, edge-trigger via tx_drain_pending.
_EVT_STREAM_TX_DRAINED = 15
_EVT_STREAM_DESTROY = 17
_EVT_WT_STREAM_DESTROY = 18

# Must match spsc_ring.h SPSC_EVT_WT_* values.
_EVT_WT_SESSION_READY = 64
_EVT_WT_SESSION_REFUSED = 65
_EVT_WT_SESSION_CLOSED = 66
_EVT_WT_SESSION_DRAINING = 67
_EVT_WT_STREAM_DATA = 68
_EVT_WT_STREAM_FIN = 69
_EVT_WT_STREAM_RESET = 70
_EVT_WT_STOP_SENDING = 71
_EVT_WT_DATAGRAM = 72
_EVT_WT_NEW_STREAM = 73
_EVT_WT_STREAM_CREATED = 74
_EVT_WT_NEW_SESSION = 75


class WebTransportError(Exception):
    """Raised when a WebTransport session fails."""


def _resolve_host(host: str) -> str:
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    infos = socket.getaddrinfo(host, None, socket.AF_INET)
    if not infos:
        raise OSError(f"Cannot resolve {host}")
    return infos[0][4][0]


# =====================================================================
# Base: established-session API. Role-agnostic except for the .role
# property (preserved because MoQT-level code makes role-specific
# choices: who sends ClientSetup vs ServerSetup, etc.).
# =====================================================================

class WebTransportSession:
    """Established WebTransport session — common base for client and
    server. Subclasses provide the asymmetric setup."""

    def __init__(self, transport: TransportContext, role: str,
                 state: WebTransportSessionState | None = None):
        if role not in ("client", "server"):
            raise ValueError(f"role must be 'client' or 'server', got {role!r}")
        self._transport = transport
        self._role = role
        self._state = state or WebTransportSessionState(transport)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatcher_attached = False
        # Session-level signals
        self._session_ready: asyncio.Future | None = None
        self._session_closed = asyncio.Event()
        self._session_close_event: WebTransportSessionClosed | None = None
        self._draining = False
        # Stream creation: FIFO of pending futures. Each create_stream()
        # call appends a future; the WT_STREAM_CREATED handler popleft's
        # and resolves it. Picoquic processes the TX ring serially and
        # emits responses in the same order, so 1:1 pairing is correct.
        self._pending_creates: deque[asyncio.Future] = deque()
        # Per-stream incoming queues
        self._stream_inbox: dict[int, asyncio.Queue] = {}
        # Per-stream drain events. Set by _on_event when the picoquic
        # worker fires SPSC_EVT_STREAM_TX_DRAINED for a stream; awaited
        # by send_stream_data_drained / external callers via
        # get_tx_drain_event. Lazy-allocated per stream_id.
        self._stream_tx_drain_events: dict[int, asyncio.Event] = {}
        # Per-stream sc pointers (uintptr_t) the C side surfaces via
        # NEW_STREAM (peer-initiated) and STREAM_CREATED (we initiated).
        # send_stream_data() looks this up and passes the sc pointer
        # into Cython's push_stream_data. Removed on FIN/RESET/
        # STOP_SENDING/SESSION_CLOSED. The C side owns the underlying
        # aiopquic_stream_ctx_t lifetime via the per-stream link in
        # h3zero's path_callback_ctx; we only hold a borrowed integer.
        self._stream_tx_ctxs: dict[int, int] = {}
        # Datagrams + general event stream
        self._event_queue: asyncio.Queue = asyncio.Queue()

    # --- read-only state ----------------------------------------------

    @property
    def role(self) -> str:
        """'client' for the initiating side, 'server' for the
        accepting side. Higher layers (MoQT) use this to choose
        between client/server-specific protocol behavior."""
        return self._role

    @property
    def is_client(self) -> bool:
        return self._role == "client"

    @property
    def is_server(self) -> bool:
        return self._role == "server"

    @property
    def session_ready(self) -> bool:
        return self._session_ready is not None and self._session_ready.done()

    @property
    def session_closed(self) -> bool:
        return self._session_closed.is_set()

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def control_stream_id(self) -> int:
        return self._state.control_stream_id

    @property
    def cnx_ptr(self) -> int:
        return self._state.cnx_ptr

    # --- dispatcher attach (called by subclass setup paths) -----------

    def _attach_dispatcher(self) -> None:
        if self._dispatcher_attached:
            return
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        eventfd = self._transport.eventfd
        if eventfd >= 0:
            _get_dispatcher_registry().attach(
                self._loop, self._transport, self)
        self._dispatcher_attached = True

    # --- stream management --------------------------------------------

    async def create_stream(self, bidir: bool = True,
                              timeout: float = 5.0) -> int:
        """Open a new WT stream. Returns the assigned stream_id.

        Round-trips to the picoquic thread because picowt allocates
        the id internally. Multiple concurrent callers are safe:
        each appends a future to a FIFO and picoquic responds in
        the same order TX events were pushed."""
        if not self.session_ready:
            raise WebTransportError("session not ready")
        fut = self._loop.create_future()
        self._pending_creates.append(fut)
        self._state.push_create_stream(bidir)
        try:
            sid = await asyncio.wait_for(fut, timeout=timeout)
        except BaseException:
            try:
                self._pending_creates.remove(fut)
            except ValueError:
                pass
            raise
        if sid == 0:
            raise WebTransportError("WT stream create rejected")
        return sid

    def send_stream_data(self, stream_id: int, data: bytes,
                          end_stream: bool = False) -> None:
        """Send bytes on a WT stream.

        Writes into the stream's per-WT-stream sc->tx byte ring and
        marks the stream active. Picoquic pulls bytes at wire rate via
        prepare_to_send. All-or-nothing on backpressure: BufferError
        means no bytes committed and caller may retry the same buffer.
        """
        if not self.session_ready:
            raise WebTransportError("session not open")
        sc_ptr = self._stream_tx_ctxs.get(stream_id, 0)
        if sc_ptr == 0:
            raise WebTransportError(
                f"WT stream {stream_id} not available "
                "(never opened, closed, or reset)")
        self._state.push_stream_data(stream_id, sc_ptr, data, end_stream)

    def get_tx_drain_event(self, stream_id: int) -> asyncio.Event:
        """asyncio.Event signalled when the picoquic worker drains
        bytes from this stream's sc->tx after the ring was reported
        full. Caller pattern:
            event = wt.get_tx_drain_event(sid)
            event.clear()
            try:
                wt.send_stream_data(sid, data, end_stream)
            except BufferError:
                await event.wait()
                continue  # retry
        """
        event = self._stream_tx_drain_events.get(stream_id)
        if event is None:
            event = asyncio.Event()
            self._stream_tx_drain_events[stream_id] = event
        return event

    def stream_tx_buf_used(self, stream_id: int) -> int:
        """Per-stream sc->tx bytes-in-flight — the load-bearing
        backpressure signal in the pull model.

        Returns the count of bytes currently queued in this WT
        stream's sc->tx ring waiting for the picoquic worker to pull
        them onto the wire. Companion of arm_stream_tx_drain_pending +
        get_tx_drain_event: read used → if over budget, arm the
        per-stream drain-pending flag and await the per-stream event.
        Unknown stream_id returns 0.

        Conceptually `used` is how full the per-stream byte pipe is
        between Python (producer) and picoquic (consumer):
          - bytes you've pushed via send_stream_data but not yet on
            the wire
          - drained monotonically by the worker; wake via the
            per-stream sc->tx drain event
          - cap_minus_used = headroom before next push raises
            BufferError
          - used / wire_rate ≈ latency this queue adds above wire RTT
        """
        sc_ptr = self._stream_tx_ctxs.get(stream_id, 0)
        if not sc_ptr:
            return 0
        return self._transport.stream_tx_buf_used(sc_ptr)

    def arm_stream_tx_drain_pending(self, stream_id: int) -> None:
        """Arm the per-stream sc->tx_drain_pending flag so the next
        worker drain of this stream's sc->tx fires
        SPSC_EVT_STREAM_TX_DRAINED — even if sc->tx wasn't full.
        Pair with get_tx_drain_event(stream_id) for the canonical
        clear-arm-recheck-wait pattern against a byte budget below
        sc->tx full. No-op on unknown stream_id."""
        sc_ptr = self._stream_tx_ctxs.get(stream_id, 0)
        if not sc_ptr:
            return
        self._transport.arm_stream_tx_drain_pending(sc_ptr)

    def clear_stream_tx_drain_pending(self, stream_id: int) -> None:
        """Clear the per-stream sc->tx_drain_pending flag. Use when
        the producer observed the byte budget cleared between arm and
        wait (the race-recovery branch of clear-arm-recheck-wait).
        No-op on unknown stream_id."""
        sc_ptr = self._stream_tx_ctxs.get(stream_id, 0)
        if not sc_ptr:
            return
        self._transport.clear_stream_tx_drain_pending(sc_ptr)

    def path_quality(self) -> dict:
        """Snapshot of picoquic path-quality metrics for the underlying
        cnx. Returns a dict with cwnd (cwin), bytes_in_transit,
        smoothed rtt, pacing_rate, lost packet counts, bytes
        sent/received. The load-bearing CC + FC observability surface
        for the WT session's underlying QUIC path. Returns empty dict
        if the session is not yet open or the underlying cnx is gone.
        Mirrors aiopquic.QuicConnection.path_quality()."""
        cnx_ptr = self.cnx_ptr
        if not cnx_ptr:
            return {}
        return self._transport.path_quality(cnx_ptr)

    def tx_pressure(self, stream_id: int = 0) -> float:
        """TX-ring fill ratio in [0.0, 1.0], for backpressure-aware
        yielding from tight send loops. BufferError from
        send_stream_data is the hard signal; this is the soft
        companion. `stream_id` is reserved for future per-stream ring
        accounting; the SPSC TX ring is shared across all WT streams
        on this session today so the value is connection-global.
        """
        cap = self._transport.tx_capacity
        if not cap:
            return 0.0
        return self._transport.tx_count / cap

    async def send_stream_data_drained(self, stream_id: int, data: bytes,
                                         end_stream: bool = False,
                                         *,
                                         soft_yield_at: float = 0.5,
                                         hard_wait_at: float = 0.9) -> None:
        """Send with built-in TX-ring backpressure for WT.

        Composes send_stream_data + get_tx_drain_event + tx_pressure so
        every WT caller gets worker-thread-aware pacing without copying
        the heuristic. Mirrors aiopquic.QuicConnection.send_stream_data_drained.

        Layers:
          - hard ring-saturation guard: await the connection-global
            tx_event_ring_drain_event when `tx_pressure > hard_wait_at`
            (default 0.9). Uses the clear-arm-recheck-wait pattern so
            there is no lost wakeup if the worker drains the ring
            between our threshold check and the wait.
          - send: atomic push to sc->tx + MARK_ACTIVE under one GIL hold.
          - soft post-send yield: `await asyncio.sleep(0)` when
            `tx_pressure > soft_yield_at` (default 0.5).
          - BufferError retry: await whichever drain event the Cython
            side armed (sc->tx-full arms per-stream event; TX-ring-full
            arms connection-global event). asyncio.wait FIRST_COMPLETED
            on both wakes us on whichever fires.

        Application-level policies (byte budgets, fairness) belong
        above this layer. The canonical per-stream byte-budget
        primitive trio is:
          - stream_tx_buf_used(stream_id) — data-ring queue depth
            (where the payload actually lives in pull mode)
          - arm_stream_tx_drain_pending(stream_id) — request a wake
            on next sc->tx drain below sc->tx-full
          - get_tx_drain_event(stream_id) — the per-stream wake
        Pair these in a clear-arm-recheck-wait loop above the call
        to this helper.
        """
        sc_event = self.get_tx_drain_event(stream_id)
        ring_event = self._transport.tx_event_ring_drain_event
        while True:
            # Close-time guard: SESSION_CLOSED / per-stream-terminal
            # handlers set every drain event explicitly to unpark
            # waiters. Without this check the producer would observe
            # set() events, fall through, call send_stream_data on a
            # closed session, and propagate that failure into the
            # caller. Return cleanly instead.
            if self.session_closed:
                return
            # Connection-global ring pressure: tx_pressure reads the
            # SPSC TX event ring. Wait on the connection-global ring
            # event, NOT the per-stream sc->tx event (which is only
            # armed when sc->tx fills).
            if self.tx_pressure(stream_id) > hard_wait_at:
                ring_event.clear()
                self._transport.arm_tx_event_ring_drain_pending()
                if self.tx_pressure(stream_id) <= hard_wait_at:
                    # Raced — worker drained between checks.
                    self._transport.clear_tx_event_ring_drain_pending()
                    continue
                await ring_event.wait()
                continue
            # Clear both events BEFORE the send. If the worker fires
            # them during our send_stream_data call (race between the
            # Cython arming and us catching the exception), the set
            # will land AFTER our clear and be captured on the wait.
            # Clearing AFTER the BufferError loses that wakeup.
            sc_event.clear()
            ring_event.clear()
            try:
                self.send_stream_data(stream_id, data,
                                       end_stream=end_stream)
                if self.tx_pressure(stream_id) > soft_yield_at:
                    await asyncio.sleep(0)
                return
            except BufferError:
                # Cython side armed the appropriate signal:
                # - sc->tx full → per-stream sc->tx_drain_pending
                # - TX event ring full → connection-global tx_event_ring_drain_pending
                # Events were cleared BEFORE the call so no lost wakeup.
                sc_wait = asyncio.create_task(sc_event.wait())
                ring_wait = asyncio.create_task(ring_event.wait())
                done, pending = await asyncio.wait(
                    [sc_wait, ring_wait],
                    return_when=asyncio.FIRST_COMPLETED)
                # Cancellation hygiene: await the cancelled task under
                # suppress so the loop reaps it now instead of leaking
                # a pending Task until the next scheduler turn.
                for t in pending:
                    t.cancel()
                    with suppress(asyncio.CancelledError):
                        await t
                # Re-check close after the wait — the session-close
                # handler may have set our events to unpark us cleanly.
                if self.session_closed:
                    return

    def reset_stream(self, stream_id: int, error_code: int = 0) -> None:
        self._state.push_reset_stream(stream_id, error_code)

    def stop_stream(self, stream_id: int, error_code: int = 0) -> None:
        """Send STOP_SENDING on a WT stream (peer should reset)."""
        self._state.push_stop_sending(stream_id, error_code)

    def send_datagram_frame(self, data: bytes) -> None:
        """Send a WebTransport datagram. Not yet wired through the C
        bridge; raises NotImplementedError until the WT datagram TX
        path lands."""
        raise NotImplementedError("WT datagram TX not yet supported")

    async def receive_stream_data(self, stream_id: int):
        """Async-generator: yield WebTransportStreamDataReceived (and
        any final WebTransportStreamReset) for stream_id, then return
        on FIN or reset."""
        q = self._stream_inbox.get(stream_id)
        if q is None:
            self._stream_inbox[stream_id] = q = asyncio.Queue()
        while True:
            ev = await q.get()
            yield ev
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                return
            if isinstance(ev, WebTransportStreamReset):
                return

    # --- session close ------------------------------------------------

    def close(self, error_code: int = 0, reason: bytes = b"") -> None:
        if not self.session_closed:
            self._state.push_close(error_code, reason)

    def drain(self) -> None:
        self._state.push_drain()
        self._draining = True

    async def wait_closed(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._session_closed.wait()
        else:
            await asyncio.wait_for(self._session_closed.wait(),
                                     timeout=timeout)

    # --- event fan-out ------------------------------------------------

    async def events(self) -> AsyncGenerator:
        """Yield non-stream events: SessionDraining, NewStream,
        Datagram, etc. Stream data is delivered via
        receive_stream_data(stream_id) on demand."""
        while not self._session_closed.is_set():
            ev = await self._event_queue.get()
            yield ev

    # --- internal: dispatcher hook ------------------------------------

    def _drop_stream_tx(self, sid: int) -> None:
        """Wake any producer parked on this stream's tx drain event,
        then drop sc cache + event. Called from every per-stream
        terminal event (RESET, STOP_SENDING, STREAM_DESTROY,
        WT_STREAM_DESTROY). Without the wake the producer would
        deadlock — the popped Event is unreachable from this dict so
        no future STREAM_TX_DRAINED can set it. Idempotent / safe on
        unknown sid."""
        ev = self._stream_tx_drain_events.get(sid)
        if ev is not None:
            ev.set()
        self._stream_tx_ctxs.pop(sid, None)
        self._stream_tx_drain_events.pop(sid, None)

    def _on_event(self, ev_tuple) -> None:
        """Called by the dispatcher for every drained event whose
        stream_ctx_ptr matches this session.

        ev_tuple: (event_type, stream_id, data, is_fin, error_code,
                    cnx_ptr, stream_ctx_ptr, sc_ptr)."""
        evt_type, sid, data, _is_fin, error_code, _cnx_ptr, _, sc_ptr = (
            ev_tuple)
        if evt_type == _EVT_WT_SESSION_READY:
            if self._session_ready and not self._session_ready.done():
                self._session_ready.set_result(None)
        elif evt_type == _EVT_WT_SESSION_REFUSED:
            err = WebTransportSessionRefused(error_code=error_code)
            if self._session_ready and not self._session_ready.done():
                self._session_ready.set_exception(WebTransportError(
                    f"WT CONNECT refused (code={error_code})"))
            self._event_queue.put_nowait(err)
        elif evt_type == _EVT_WT_SESSION_CLOSED:
            reason = data if data is not None else memoryview(b"")
            ev = WebTransportSessionClosed(error_code=error_code,
                                              reason=reason)
            self._session_close_event = ev
            self._session_closed.set()
            if (self._session_ready
                    and not self._session_ready.done()):
                self._session_ready.set_exception(WebTransportError(
                    "WT session closed before READY"))
            # Fail any pending create_stream() callers; the session
            # is gone, no point making them wait for the timeout.
            while self._pending_creates:
                fut = self._pending_creates.popleft()
                if not fut.done():
                    fut.set_exception(WebTransportError(
                        "WT session closed"))
            # All borrowed sc pointers are invalidated by session close
            # (the C side will free links via LINK_RELEASE events as
            # picoquic retires the cnx). Drop our references now so
            # send_stream_data raises cleanly rather than handing a
            # soon-to-be-freed pointer back into Cython.
            self._stream_tx_ctxs.clear()
            # Wake any producer parked in send_stream_data_drained
            # awaiting per-stream sc_event or connection-global
            # ring_event. After close the worker stops invoking drain
            # callbacks for this session's streams, so without these
            # explicit sets the waiter would deadlock until process
            # exit. Producer wakes, loops, observes session_closed
            # and raises / returns cleanly.
            for tx_ev in self._stream_tx_drain_events.values():
                tx_ev.set()
            ring_ev = getattr(self._transport,
                                '_tx_event_ring_drain_event', None)
            if ring_ev is not None:
                ring_ev.set()
            self._event_queue.put_nowait(ev)
        elif evt_type == _EVT_WT_SESSION_DRAINING:
            self._draining = True
            self._event_queue.put_nowait(WebTransportSessionDraining())
        elif evt_type == _EVT_WT_STREAM_CREATED:
            # Stash the sc pointer the C side allocated for this
            # outbound stream so send_stream_data has it on first use.
            if sc_ptr and error_code == 0:
                self._stream_tx_ctxs[sid] = sc_ptr
            # Pair with the oldest pending create_stream() caller.
            # Skip already-cancelled futures (caller timed out / closed).
            while self._pending_creates:
                fut = self._pending_creates.popleft()
                if not fut.done():
                    fut.set_result(0 if error_code != 0 else sid)
                    break
        elif evt_type == _EVT_WT_STREAM_DATA:
            payload = data if data is not None else memoryview(b"")
            ev = WebTransportStreamDataReceived(
                stream_id=sid, data=payload, end_stream=False)
            q = self._stream_inbox.get(sid)
            if q is None:
                self._stream_inbox[sid] = q = asyncio.Queue()
            q.put_nowait(ev)
        elif evt_type == _EVT_WT_STREAM_FIN:
            payload = data if data is not None else memoryview(b"")
            ev = WebTransportStreamDataReceived(
                stream_id=sid, data=payload, end_stream=True)
            q = self._stream_inbox.get(sid)
            if q is None:
                self._stream_inbox[sid] = q = asyncio.Queue()
            q.put_nowait(ev)
            # Peer FINed the stream — no further reads, but we may
            # still be writing on a bidi until our own FIN goes out.
            # Don't drop tx_ctx here; that's owned by the writer side.
        elif evt_type == _EVT_WT_STREAM_RESET:
            ev = WebTransportStreamReset(stream_id=sid,
                                            error_code=error_code)
            q = self._stream_inbox.get(sid)
            if q is None:
                self._stream_inbox[sid] = q = asyncio.Queue()
            q.put_nowait(ev)
            # Peer reset → our tx side is also gone. Drop sc reference.
            self._drop_stream_tx(sid)
        elif evt_type == _EVT_WT_STOP_SENDING:
            self._event_queue.put_nowait(
                WebTransportStopSending(stream_id=sid,
                                          error_code=error_code))
            # Peer told us to stop sending — sc->tx is no longer useful.
            self._drop_stream_tx(sid)
        elif evt_type == _EVT_WT_DATAGRAM:
            payload = data if data is not None else memoryview(b"")
            self._event_queue.put_nowait(
                WebTransportDatagramReceived(data=payload))
        elif evt_type == _EVT_WT_NEW_STREAM:
            # Peer opened a new stream — for bidi we may want to reply,
            # so stash the sc pointer here too.
            if sc_ptr:
                self._stream_tx_ctxs[sid] = sc_ptr
            self._event_queue.put_nowait(
                WebTransportNewStream(stream_id=sid))
        elif evt_type == _EVT_STREAM_TX_DRAINED:
            # Picoquic worker drained bytes from this stream's sc->tx
            # and the edge-trigger CAS won. Wake any blocked writer.
            event = self._stream_tx_drain_events.get(sid)
            if event is None:
                event = asyncio.Event()
                self._stream_tx_drain_events[sid] = event
            event.set()
        elif evt_type == _EVT_STREAM_DESTROY:
            # Universal raw-QUIC STREAM_DESTROY — fired for raw-QUIC
            # streams that share this transport. WT data streams use
            # the _EVT_WT_STREAM_DESTROY path below.
            self._drop_stream_tx(sid)
            self._stream_inbox.pop(sid, None)
        elif evt_type == _EVT_WT_STREAM_DESTROY:
            # picohttp_callback_free fired for this WT data stream.
            # The link + sc are still alive at this point (LINK_RELEASE
            # follows in FIFO order and owns the actual free); we just
            # drop our cached sc pointer so _stream_tx_ctxs doesn't
            # accumulate stale entries. Do NOT pop _stream_inbox: a
            # consumer task may not have called receive_stream_data
            # yet — popping the queue races with consumers that hold
            # a deferred reference and would hang them. Inbox queues
            # for fully-drained streams are reclaimed at session
            # close.
            self._drop_stream_tx(sid)


# =====================================================================
# Initiator-side: subclass adds open().
# =====================================================================

class WebTransportClient(WebTransportSession):
    """Initiator-side WT session. Adds open() to drive the
    CONNECT handshake."""

    def __init__(self, transport: TransportContext,
                 host: str, port: int, path: str,
                 sni: str | None = None,
                 wt_available_protocols: list[str] | None = None):
        super().__init__(transport, role="client")
        self._host = host
        self._port = port
        self._path = path
        self._sni = sni or host
        # WT-Available-Protocols header sent in the H3 CONNECT request
        # (WebTransport spec §3.3). Generic list of subprotocol strings;
        # aiopquic does no interpretation. Higher layers (e.g. aiomoqt)
        # set this to advertise their version namespace (moqt-NN etc.).
        self._wt_protocols = wt_available_protocols or []

    async def open(self, timeout: float = 10.0) -> None:
        """Initiate the WT session — push TX_WT_OPEN, await
        SESSION_READY. Picoquic-thread does picowt_prepare_client_cnx
        + picowt_connect."""
        self._loop = asyncio.get_event_loop()
        self._attach_dispatcher()
        self._session_ready = self._loop.create_future()

        addr = _resolve_host(self._host)
        # WT-Available-Protocols is an HTTP Structured Field list of
        # strings (RFC 9651); each value is double-quoted, comma-joined.
        # picoquic puts the value into the QPACK literal verbatim — no
        # SF formatting on its side — so we have to produce the
        # canonical form here.
        if self._wt_protocols:
            protocols_str = ", ".join(f'"{p}"' for p in self._wt_protocols)
        else:
            protocols_str = ""
        self._state.push_open(addr, self._port, self._path, self._sni,
                              protocols_str)

        try:
            await asyncio.wait_for(self._session_ready, timeout=timeout)
        except asyncio.TimeoutError:
            raise WebTransportError(
                f"WT CONNECT timed out after {timeout}s"
            ) from None


# =====================================================================
# Acceptor-side: subclass added in Phase C.4 step 3 (server-side
# bridge in C must surface WT_NEW_SESSION events first).
# =====================================================================

class WebTransportServerSession(WebTransportSession):
    """Acceptor-side WT session. Constructed by the server's accept
    callback when a new H3 CONNECT arrives at a registered path.

    The C-side bridge has already allocated the underlying
    aiopquic_wt_session_t and registered the WT prefix; this Python
    object just wraps it and routes events from the rx_event_ring."""

    def __init__(self, transport: TransportContext,
                 state: WebTransportSessionState):
        super().__init__(transport, role="server", state=state)
        # Server side: session is already open at construction time
        # (the peer sent CONNECT and we accepted it).
        self._loop = asyncio.get_event_loop()
        self._attach_dispatcher()
        self._session_ready = self._loop.create_future()
        self._session_ready.set_result(None)


# =====================================================================
# Shared dispatcher: one add_reader per (loop, transport); routes
# every drained event to the matching session by stream_ctx_ptr.
# =====================================================================

class _Dispatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 transport: TransportContext):
        self._loop = loop
        self._transport = transport
        self._sessions: dict[int, WebTransportSession] = {}
        self._acceptor = None  # callable(session) -> None|coroutine
        loop.add_reader(transport.eventfd, self._drain)

    def add_session(self, session: WebTransportSession) -> None:
        self._sessions[session._state.session_ptr] = session

    def remove_session(self, session: WebTransportSession) -> None:
        self._sessions.pop(session._state.session_ptr, None)

    def set_acceptor(self, acceptor) -> None:
        """Install the server-side handler invoked once per
        EVT_WT_NEW_SESSION. Receives the new WebTransportServerSession."""
        self._acceptor = acceptor

    def _drain(self) -> None:
        events = self._transport.drain_rx()
        for ev in events:
            evt_type = ev[0]
            stream_ctx_ptr = ev[6]
            if evt_type == _EVT_WT_NEW_SESSION and self._acceptor is not None:
                self._spawn_server_session(ev)
                continue
            session = self._sessions.get(stream_ctx_ptr)
            if session is not None:
                session._on_event(ev)

    def set_session_factory(self, factory) -> None:
        """factory(transport, state) -> WebTransportSession; defaults
        to WebTransportServerSession when unset. Allows callers to
        substitute application-specific session subclasses (e.g.,
        aiomoqt's MOQTSessionWTServer)."""
        self._session_factory = factory

    def _spawn_server_session(self, ev) -> None:
        stream_ctx_ptr = ev[6]
        if stream_ctx_ptr in self._sessions:
            return  # already attached (re-entrancy guard)
        state = WebTransportSessionState(self._transport,
                                          session_ptr=stream_ctx_ptr)
        factory = getattr(self, '_session_factory', None)
        if factory is None:
            session = WebTransportServerSession(self._transport, state)
        else:
            session = factory(self._transport, state)
        self._sessions[stream_ctx_ptr] = session
        result = self._acceptor(session)
        if asyncio.iscoroutine(result):
            self._loop.create_task(result)

    def detach(self) -> None:
        try:
            self._loop.remove_reader(self._transport.eventfd)
        except Exception:
            pass


class _DispatcherRegistry:
    """Process-wide registry keyed by (loop_id, transport_id).
    Lazy-created; one add_reader per pair."""
    def __init__(self):
        self._dispatchers: dict[tuple[int, int], _Dispatcher] = {}

    def attach(self, loop: asyncio.AbstractEventLoop,
                transport: TransportContext,
                session: WebTransportSession) -> None:
        key = (id(loop), id(transport))
        d = self._dispatchers.get(key)
        if d is None:
            d = _Dispatcher(loop, transport)
            self._dispatchers[key] = d
        d.add_session(session)

    def attach_acceptor(self, loop: asyncio.AbstractEventLoop,
                         transport: TransportContext,
                         acceptor) -> "_Dispatcher":
        """Bind a server-side accept handler to (loop, transport).
        Returns the underlying dispatcher so callers can detach."""
        key = (id(loop), id(transport))
        d = self._dispatchers.get(key)
        if d is None:
            d = _Dispatcher(loop, transport)
            self._dispatchers[key] = d
        d.set_acceptor(acceptor)
        return d

    def detach(self, loop: asyncio.AbstractEventLoop,
                transport: TransportContext,
                session: WebTransportSession) -> None:
        key = (id(loop), id(transport))
        d = self._dispatchers.get(key)
        if d is None:
            return
        d.remove_session(session)
        if not d._sessions:
            d.detach()
            del self._dispatchers[key]


_REGISTRY: _DispatcherRegistry | None = None


def _get_dispatcher_registry() -> _DispatcherRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _DispatcherRegistry()
    return _REGISTRY


# =====================================================================
# Public client-side entry point.
# =====================================================================

@asynccontextmanager
async def connect_webtransport(
        host: str, port: int, path: str,
        *, sni: str | None = None,
        wt_available_protocols: list[str] | None = None,
        transport: TransportContext | None = None,
        timeout: float = 10.0,
) -> AsyncGenerator[WebTransportClient, None]:
    """Open a WebTransport session.

    Usage:
        async with connect_webtransport(host, port, '/moq-relay') as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b'...')
            ...

    `transport` defaults to a fresh TransportContext started in client
    mode (alpn='h3', max_datagram_frame_size=64KB); pass an existing
    one to share rings/threading across multiple sessions.

    Empty path "" is normalized to "/" — HTTP/3 root request semantics
    (RFC 9114 §4.3.1). Picoquic's path-match is exact, so registering
    "" on the server side and connecting with "" both fail to route
    against a peer that uses the literal "/". Normalizing here keeps
    consumers from having to think about it.
    """
    if path == "":
        path = "/"
    own_transport = transport is None
    if own_transport:
        transport = TransportContext()
        transport.start(is_client=True, alpn="h3",
                          max_datagram_frame_size=64 * 1024)

    client = WebTransportClient(transport, host, port, path, sni=sni,
                                  wt_available_protocols=wt_available_protocols)
    try:
        await client.open(timeout=timeout)
        yield client
    finally:
        loop = asyncio.get_event_loop()
        if not client.session_closed:
            client.close(0, b"")
            try:
                await asyncio.wait_for(client.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        _get_dispatcher_registry().detach(loop, transport, client)
        if own_transport:
            try:
                transport.stop()
            except Exception:
                pass


# =====================================================================
# Public server-side entry point.
# =====================================================================

class WebTransportServer:
    """Server handle returned by serve_webtransport. close() tears
    down the engine and detaches the dispatcher."""

    def __init__(self, transport: TransportContext, dispatcher,
                 own_transport: bool):
        self._transport = transport
        self._dispatcher = dispatcher
        self._own_transport = own_transport

    def close(self) -> None:
        try:
            self._dispatcher.set_acceptor(None)
            self._dispatcher.detach()
        except Exception:
            pass
        if self._own_transport:
            try:
                self._transport.stop()
            except Exception:
                pass


async def serve_webtransport(
        host: str, port: int, path: str,
        *, handler,
        cert_file: str, key_file: str,
        transport: TransportContext | None = None,
        session_factory=None,
) -> WebTransportServer:
    """Start a WebTransport server listening on (host, port) at path.

    handler(session) is invoked once per accepted CONNECT.
    session_factory(transport, state) constructs each session;
    defaults to WebTransportServerSession.

    Empty path "" is normalized to "/" — picoquic's path table is
    exact-match (no default route), and HTTP/3 clients send `:path: /`
    for root requests (RFC 9114 §4.3.1). Without normalization, a
    server registered with "" never matches a root-path CONNECT.
    """
    if path == "":
        path = "/"
    own_transport = transport is None
    if own_transport:
        transport = TransportContext()
        transport.start(
            port=port,
            cert_file=cert_file, key_file=key_file,
            alpn="h3",
            is_client=False,
            max_datagram_frame_size=64 * 1024,
            wt_path=path,
        )

    loop = asyncio.get_event_loop()
    dispatcher = _get_dispatcher_registry().attach_acceptor(
        loop, transport, handler)
    if session_factory is not None:
        dispatcher.set_session_factory(session_factory)
    return WebTransportServer(transport, dispatcher, own_transport)
