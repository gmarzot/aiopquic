"""QUIC client — matches qh3.asyncio.client API."""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.asyncio.protocol import QuicConnectionProtocol


def _resolve_host(host: str, port: int | None = None) -> str:
    """An IP literal as is; a name resolves to its first IPv4 address,
    else its first IPv6 one."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    for family in (socket.AF_INET, socket.AF_INET6):
        for info in infos:
            if info[0] == family:
                return info[4][0]
    raise OSError(f"Cannot resolve {host}")


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
) -> AsyncGenerator[QuicConnectionProtocol, None]:
    """Connect to a QUIC server.

    Usage:
        async with connect('example.com', 443, configuration=config) as protocol:
            # protocol.quic_event_received() dispatches events
    """
    if configuration is None:
        configuration = QuicConfiguration(is_client=True)
    if configuration.server_name is None:
        configuration.server_name = host

    addr = _resolve_host(host, port)

    quic = QuicConnection(configuration=configuration)
    quic._start_transport(port=local_port)

    if create_protocol is not None:
        protocol = create_protocol(quic, stream_handler=stream_handler)
    else:
        protocol = QuicConnectionProtocol(quic, stream_handler=stream_handler)

    loop = asyncio.get_event_loop()
    protocol._start(loop)
    protocol.connect((addr, port))

    try:
        if wait_connected:
            await protocol.wait_connected()
        else:
            # Writes before the handshake need the connection to exist; with
            # a session ticket they go out as 0-RTT.
            await protocol.wait_created()
        yield protocol
    finally:
        try:
            await protocol.aclose()
        finally:
            protocol._stop()
            quic.stop()
