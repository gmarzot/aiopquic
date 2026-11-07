"""QUIC client — matches qh3.asyncio.client API."""

import asyncio
import copy
import ipaddress
import socket
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import (
    QuicConnection, QuicEngine, _EVT_ALMOST_READY, _EVT_APP_CLOSE, _EVT_CLOSE,
)
from aiopquic.asyncio._rxwatch import clear_reader, set_reader
from aiopquic.asyncio.protocol import QuicConnectionProtocol


async def _resolve_host(host: str, port: int | None = None) -> str:
    """An IP literal as is; a name resolves, without blocking the event
    loop, to its first IPv4 address, else its first IPv6 one."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_DGRAM)
    for family in (socket.AF_INET, socket.AF_INET6):
        for info in infos:
            if info[0] == family:
                return info[4][0]
    raise OSError(f"Cannot resolve {host}")


class QuicClientEngine(QuicEngine):
    """Client connections sharing one picoquic thread and RX ring.

    Pass it to connect(engine=...), from one event loop. The first
    connection's configuration sets the transport-wide settings, so give
    every connection the same one. close() ends any open connections and
    stops the thread.

    `sockets` above 1 gives connections that many source ports in turn, on
    Linux, so a relay hashing them to worker threads spreads them. Otherwise
    they share one socket.
    """

    def __init__(self, *, ring_capacity: int | None = None,
                 max_connections: int = 4096, sockets: int = 0):
        super().__init__(configuration=None, create_protocol=None)
        self._ring_capacity = ring_capacity
        self._max_connections = max_connections
        self._sockets = sockets
        self._pending: deque[QuicConnectionProtocol] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_started(self, configuration: QuicConfiguration,
                        port: int = 0) -> None:
        if self._transport is not None:
            return
        cfg = copy.copy(configuration)
        if self._ring_capacity is not None:
            cfg.event_ring_capacity = self._ring_capacity
        cfg.max_connections = self._max_connections
        self._configuration = cfg
        self._start_transport(port=port, client_sockets=self._sockets)
        self._loop = asyncio.get_running_loop()
        set_reader(self._loop, self._transport, self.drain_and_route, self)

    def route_event(self, ev) -> None:
        evt_type, cnx_ptr = ev[0], ev[5]
        protocol = self._protocols.get(cnx_ptr)
        if protocol is None:
            # The worker creates connections in connect() order and pushes
            # ALMOST_READY for each before any other event of it.
            if (cnx_ptr == 0 or evt_type != _EVT_ALMOST_READY
                    or not self._pending):
                return
            protocol = self._pending.popleft()
            self._protocols[cnx_ptr] = protocol
            self._connections[cnx_ptr] = protocol._quic
        protocol._quic._handle_raw_event(*ev)
        protocol._process_events()
        if evt_type in (_EVT_CLOSE, _EVT_APP_CLOSE):
            self._connections.pop(cnx_ptr, None)
            self._protocols.pop(cnx_ptr, None)

    def close(self) -> None:
        if self._transport is not None:
            clear_reader(self._loop, self._transport, self)
        super().close()


@asynccontextmanager
async def connect(
    host: str,
    port: int,
    *,
    configuration: QuicConfiguration | None = None,
    create_protocol: Callable | None = None,
    stream_handler=None,
    wait_connected: bool = True,
    local_port: int = 0,
    engine: QuicClientEngine | None = None,
) -> AsyncGenerator[QuicConnectionProtocol, None]:
    """Connect to a QUIC server.

    Usage:
        async with connect('example.com', 443, configuration=config) as protocol:
            # protocol.quic_event_received() dispatches events

    `engine` runs the connection on a shared QuicClientEngine, which
    outlives it, instead of a transport of its own.
    """
    if configuration is None:
        configuration = QuicConfiguration(is_client=True)
    if configuration.server_name is None:
        configuration.server_name = host

    addr = await _resolve_host(host, port)

    if engine is None:
        quic = QuicConnection(configuration=configuration)
        quic._start_transport(port=local_port)
    else:
        engine._ensure_started(configuration, port=local_port)
        quic = QuicConnection(configuration=configuration, engine=engine)

    if create_protocol is not None:
        protocol = create_protocol(quic, stream_handler=stream_handler)
    else:
        protocol = QuicConnectionProtocol(quic, stream_handler=stream_handler)

    loop = asyncio.get_event_loop()
    if engine is None:
        protocol._start(loop)
    else:
        protocol._loop = loop
        engine._pending.append(protocol)

    try:
        protocol.connect((addr, port))
        if engine is not None:
            await protocol.wait_created()
        if wait_connected:
            await protocol.wait_connected()
        else:
            # Writes before the handshake need the connection to exist; with
            # a session ticket they go out as 0-RTT.
            await protocol.wait_created()
        yield protocol
    finally:
        if engine is not None and protocol in engine._pending:
            # Never created: a stale entry would take the next connection's cnx.
            engine._pending.remove(protocol)
        try:
            await protocol.aclose()
        finally:
            if engine is None:
                protocol._stop()
            quic.stop()
