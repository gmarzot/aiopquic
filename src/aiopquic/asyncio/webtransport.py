"""WebTransport asyncio client.

Wraps aiopquic._binding._transport.WebTransportSessionState with an
async context-manager `connect_webtransport(host, port, path, ...)`
and a Python-level WebTransportClient that drains events from the
shared SPSC ring and routes them to per-session futures/queues.

Mirrors the shape of qh3's WebTransport API surface so aiomoqt can
swap backends with minimal call-site change.

Lifecycle:
    async with connect_webtransport(host, port, path,
                                     sni=..., ...) as wt:
        # session ready, MoQT setup can begin
        sid = await wt.create_stream(bidir=True)
        wt.send_stream_data(sid, b"...", end_stream=True)
        async for event in wt.events():
            ...
"""
from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from aiopquic._binding._transport import (
    TransportContext, WebTransportSessionState,
)
from aiopquic.quic.events import (
    WebTransportSessionReady, WebTransportSessionRefused,
    WebTransportSessionClosed, WebTransportSessionDraining,
    WebTransportStreamDataReceived, WebTransportStreamReset,
    WebTransportStopSending, WebTransportDatagramReceived,
    WebTransportNewStream,
)


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

# TX raw-QUIC stream events (we reuse them for WT stream sends —
# once a WT stream is created, sending is just QUIC stream send).
_TX_STREAM_DATA = 128
_TX_STREAM_FIN = 129


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


class WebTransportClient:
    """Async WebTransport client over aiopquic.

    One client = one WT session. Multiple clients can share one
    TransportContext (the asyncio dispatcher routes events by the
    session pointer set by the C-side bridge in stream_ctx_ptr).
    """

    def __init__(self, transport: TransportContext,
                 host: str, port: int, path: str,
                 sni: str | None = None):
        self._transport = transport
        self._host = host
        self._port = port
        self._path = path
        self._sni = sni or host
        self._state = WebTransportSessionState(transport)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatcher_started = False
        # Session-level signals
        self._session_ready: asyncio.Future | None = None
        self._session_closed = asyncio.Event()
        self._session_close_event: WebTransportSessionClosed | None = None
        self._draining = False
        # Stream creation (single outstanding)
        self._pending_create: asyncio.Future | None = None
        # Per-stream incoming queues; entries are
        # WebTransportStreamDataReceived (data + end_stream) or
        # WebTransportStreamReset, in receive order.
        self._stream_inbox: dict[int, asyncio.Queue] = {}
        # Datagrams + general event stream for app-level fanout
        self._event_queue: asyncio.Queue = asyncio.Queue()

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

    # --- lifecycle -----------------------------------------------------

    async def open(self, timeout: float = 10.0) -> None:
        """Initiate the WT session — push TX_WT_OPEN, await SESSION_READY.

        Picoquic-thread does picowt_prepare_client_cnx + picowt_connect.
        """
        loop = asyncio.get_event_loop()
        self._loop = loop
        self._ensure_dispatcher()
        self._session_ready = loop.create_future()

        addr = _resolve_host(self._host)
        self._state.push_open(addr, self._port, self._path, self._sni)

        try:
            await asyncio.wait_for(self._session_ready, timeout=timeout)
        except asyncio.TimeoutError:
            raise WebTransportError(
                f"WT CONNECT timed out after {timeout}s"
            ) from None

    def _ensure_dispatcher(self) -> None:
        """Register the eventfd reader once per loop. Multiple
        WebTransportClient instances on the same loop share dispatch."""
        if self._dispatcher_started:
            return
        eventfd = self._transport.eventfd
        if eventfd >= 0 and self._loop is not None:
            # Use a class-level registry so a single add_reader serves
            # all WT clients on the same TransportContext + loop.
            registry = _get_dispatcher_registry()
            registry.attach(self._loop, self._transport, self)
        self._dispatcher_started = True

    # --- stream management --------------------------------------------

    async def create_stream(self, bidir: bool = True,
                              timeout: float = 5.0) -> int:
        """Open a new WT stream. Returns the assigned stream_id.

        Round-trips to the picoquic thread (picowt_create_local_stream
        allocates the id internally and pushes it back via
        WT_STREAM_CREATED). At codec rates (3-4 streams/sec/session)
        the round-trip cost is negligible."""
        if not self.session_ready:
            raise WebTransportError("session not ready")
        if self._pending_create is not None:
            # Serialize creates (single-outstanding).
            await asyncio.shield(self._pending_create)
        self._pending_create = self._loop.create_future()
        self._state.push_create_stream(bidir)
        try:
            sid = await asyncio.wait_for(self._pending_create,
                                           timeout=timeout)
        finally:
            self._pending_create = None
        if sid == 0:
            raise WebTransportError("WT stream create rejected")
        return sid

    def send_stream_data(self, stream_id: int, data: bytes,
                          end_stream: bool = False) -> None:
        """Send bytes on a WT stream. Synchronous from the caller's
        perspective (push to TX ring + wake_up). The picoquic thread
        does the wire work."""
        cnx_ptr = self._state.cnx_ptr
        if cnx_ptr == 0:
            raise WebTransportError("session not open")
        evt = _TX_STREAM_FIN if end_stream else _TX_STREAM_DATA
        self._transport.push_tx(evt, stream_id,
                                  data=data, cnx_ptr=cnx_ptr)
        self._transport.wake_up()

    def reset_stream(self, stream_id: int, error_code: int = 0) -> None:
        self._state.push_reset_stream(stream_id, error_code)

    async def receive_stream_data(self, stream_id: int):
        """Async-generator helper: yield WebTransportStreamDataReceived
        events for stream_id until FIN or reset."""
        q = self._stream_inbox.setdefault(stream_id, asyncio.Queue())
        while True:
            ev = await q.get()
            yield ev
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                return
            if isinstance(ev, WebTransportStreamReset):
                return

    # --- session close ------------------------------------------------

    def close(self, error_code: int = 0, reason: bytes = b"") -> None:
        """Send CLOSE_WEBTRANSPORT_SESSION capsule + close control stream."""
        if not self.session_closed:
            self._state.push_close(error_code, reason)

    def drain(self) -> None:
        self._state.push_drain()
        self._draining = True

    async def wait_closed(self) -> None:
        await self._session_closed.wait()

    # --- event fan-out ------------------------------------------------

    async def events(self) -> AsyncGenerator:
        """Yield non-stream events: SessionDraining, NewStream,
        Datagram, etc. Stream data is delivered via
        receive_stream_data(stream_id) on demand."""
        while not self._session_closed.is_set():
            ev = await self._event_queue.get()
            yield ev

    # --- internal: dispatcher hook ------------------------------------

    def _on_event(self, ev_tuple) -> None:
        """Called by the shared dispatcher for every drained event whose
        stream_ctx_ptr matches this session.

        ev_tuple: (event_type, stream_id, data, is_fin, error_code,
                    cnx_ptr, stream_ctx_ptr)."""
        evt_type, sid, data, _is_fin, error_code, _cnx_ptr, _ = ev_tuple
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
            self._event_queue.put_nowait(ev)
        elif evt_type == _EVT_WT_SESSION_DRAINING:
            self._draining = True
            self._event_queue.put_nowait(WebTransportSessionDraining())
        elif evt_type == _EVT_WT_STREAM_CREATED:
            if self._pending_create and not self._pending_create.done():
                if error_code != 0:
                    self._pending_create.set_result(0)
                else:
                    self._pending_create.set_result(sid)
        elif evt_type == _EVT_WT_STREAM_DATA:
            payload = data if data is not None else memoryview(b"")
            ev = WebTransportStreamDataReceived(
                stream_id=sid, data=payload, end_stream=False)
            q = self._stream_inbox.setdefault(sid, asyncio.Queue())
            q.put_nowait(ev)
        elif evt_type == _EVT_WT_STREAM_FIN:
            payload = data if data is not None else memoryview(b"")
            ev = WebTransportStreamDataReceived(
                stream_id=sid, data=payload, end_stream=True)
            q = self._stream_inbox.setdefault(sid, asyncio.Queue())
            q.put_nowait(ev)
        elif evt_type == _EVT_WT_STREAM_RESET:
            ev = WebTransportStreamReset(stream_id=sid,
                                            error_code=error_code)
            q = self._stream_inbox.setdefault(sid, asyncio.Queue())
            q.put_nowait(ev)
        elif evt_type == _EVT_WT_STOP_SENDING:
            self._event_queue.put_nowait(
                WebTransportStopSending(stream_id=sid,
                                          error_code=error_code))
        elif evt_type == _EVT_WT_DATAGRAM:
            payload = data if data is not None else memoryview(b"")
            self._event_queue.put_nowait(
                WebTransportDatagramReceived(data=payload))
        elif evt_type == _EVT_WT_NEW_STREAM:
            self._event_queue.put_nowait(
                WebTransportNewStream(stream_id=sid))


# =====================================================================
# Shared dispatcher: one add_reader per (loop, transport); routes
# every drained event to the matching WebTransportClient by
# stream_ctx_ptr (the wt_session_t* set in h3wt_callback.h).
# =====================================================================

class _Dispatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 transport: TransportContext):
        self._loop = loop
        self._transport = transport
        self._clients: dict[int, WebTransportClient] = {}
        loop.add_reader(transport.eventfd, self._drain)

    def add_client(self, client: WebTransportClient) -> None:
        self._clients[client._state.session_ptr] = client

    def remove_client(self, client: WebTransportClient) -> None:
        self._clients.pop(client._state.session_ptr, None)

    def _drain(self) -> None:
        events = self._transport.drain_rx()
        for ev in events:
            stream_ctx_ptr = ev[6]
            client = self._clients.get(stream_ctx_ptr)
            if client is not None:
                client._on_event(ev)
            # else: raw-QUIC event or no matching session — drop on
            # the floor for now. Future: route raw-QUIC events to
            # QuicConnectionProtocol if it's also using this transport.

    def detach(self) -> None:
        try:
            self._loop.remove_reader(self._transport.eventfd)
        except Exception:
            pass


class _DispatcherRegistry:
    """Process-wide registry of dispatchers keyed by (loop_id,
    transport_id). Lazy-created; one add_reader per pair."""
    def __init__(self):
        self._dispatchers: dict[tuple[int, int], _Dispatcher] = {}

    def attach(self, loop: asyncio.AbstractEventLoop,
                transport: TransportContext,
                client: WebTransportClient) -> None:
        key = (id(loop), id(transport))
        d = self._dispatchers.get(key)
        if d is None:
            d = _Dispatcher(loop, transport)
            self._dispatchers[key] = d
        d.add_client(client)

    def detach(self, loop: asyncio.AbstractEventLoop,
                transport: TransportContext,
                client: WebTransportClient) -> None:
        key = (id(loop), id(transport))
        d = self._dispatchers.get(key)
        if d is None:
            return
        d.remove_client(client)
        if not d._clients:
            d.detach()
            del self._dispatchers[key]


_REGISTRY: _DispatcherRegistry | None = None


def _get_dispatcher_registry() -> _DispatcherRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _DispatcherRegistry()
    return _REGISTRY


# =====================================================================
# Public async context-manager entry point.
# =====================================================================

@asynccontextmanager
async def connect_webtransport(
        host: str, port: int, path: str,
        *, sni: str | None = None,
        transport: TransportContext | None = None,
        timeout: float = 10.0,
) -> AsyncGenerator[WebTransportClient, None]:
    """Open a WebTransport session.

    Usage:
        async with connect_webtransport(host, port, '/moq-relay') as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b'...')
            ...

    `transport` defaults to a fresh TransportContext started in
    client mode; pass an existing one to share rings/threading
    across multiple sessions.
    """
    own_transport = transport is None
    if own_transport:
        transport = TransportContext()
        # WT requires h3 ALPN at the QUIC layer + datagram support.
        # picowt_prepare_client_cnx will create a per-session cnx with
        # h3zero_callback set; this start() just brings up the
        # picoquic_quic_t + network thread.
        transport.start(is_client=True, alpn="h3",
                          max_datagram_frame_size=64 * 1024)

    client = WebTransportClient(transport, host, port, path, sni=sni)
    try:
        await client.open(timeout=timeout)
        yield client
    finally:
        loop = asyncio.get_event_loop()
        if not client.session_closed:
            client.close(0, b"")
        _get_dispatcher_registry().detach(loop, transport, client)
        if own_transport:
            try:
                transport.stop()
            except Exception:
                pass
