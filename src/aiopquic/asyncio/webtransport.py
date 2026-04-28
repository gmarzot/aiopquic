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
from contextlib import asynccontextmanager
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
        # Stream creation (single outstanding)
        self._pending_create: asyncio.Future | None = None
        # Per-stream incoming queues
        self._stream_inbox: dict[int, asyncio.Queue] = {}
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
        the id internally. Codec rates (3-4 streams/sec/session)
        make this round-trip cost negligible."""
        if not self.session_ready:
            raise WebTransportError("session not ready")
        if self._pending_create is not None:
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
        """Send bytes on a WT stream. Synchronous (push + wake).
        cdef class encapsulates the cnx pointer + transport plumbing."""
        if not self.session_ready:
            raise WebTransportError("session not open")
        self._state.push_stream_data(stream_id, data, end_stream)

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

    def _on_event(self, ev_tuple) -> None:
        """Called by the dispatcher for every drained event whose
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
# Initiator-side: subclass adds open().
# =====================================================================

class WebTransportClient(WebTransportSession):
    """Initiator-side WT session. Adds open() to drive the
    CONNECT handshake."""

    def __init__(self, transport: TransportContext,
                 host: str, port: int, path: str,
                 sni: str | None = None):
        super().__init__(transport, role="client")
        self._host = host
        self._port = port
        self._path = path
        self._sni = sni or host

    async def open(self, timeout: float = 10.0) -> None:
        """Initiate the WT session — push TX_WT_OPEN, await
        SESSION_READY. Picoquic-thread does picowt_prepare_client_cnx
        + picowt_connect."""
        self._loop = asyncio.get_event_loop()
        self._attach_dispatcher()
        self._session_ready = self._loop.create_future()

        addr = _resolve_host(self._host)
        self._state.push_open(addr, self._port, self._path, self._sni)

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
    object just wraps it and routes events from the rx_ring."""

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

    def _spawn_server_session(self, ev) -> None:
        stream_ctx_ptr = ev[6]
        if stream_ctx_ptr in self._sessions:
            return  # already attached (re-entrancy guard)
        state = WebTransportSessionState(self._transport,
                                          session_ptr=stream_ctx_ptr)
        session = WebTransportServerSession(self._transport, state)
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
    """
    own_transport = transport is None
    if own_transport:
        transport = TransportContext()
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
) -> WebTransportServer:
    """Start a WebTransport server listening on (host, port) at path.

    handler(session) is invoked once per accepted CONNECT, with a
    WebTransportServerSession. May be a sync function or coroutine.

    `transport` defaults to a fresh TransportContext started in server
    mode (alpn='h3', wt_path=path); pass an existing one to share
    rings/threading.
    """
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
    return WebTransportServer(transport, dispatcher, own_transport)
