"""QUIC client — matches qh3.asyncio.client API."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.asyncio.protocol import QuicConnectionProtocol


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

    quic = QuicConnection(configuration=configuration)
    quic._start_transport(port=local_port)

    if create_protocol is not None:
        protocol = create_protocol(quic, stream_handler=stream_handler)
    else:
        protocol = QuicConnectionProtocol(quic, stream_handler=stream_handler)

    loop = asyncio.get_event_loop()
    protocol._start(loop)
    protocol.connect((host, port))

    try:
        if wait_connected:
            await protocol.wait_connected()
        yield protocol
    finally:
        protocol.close()
        protocol._stop()
        quic.stop()
