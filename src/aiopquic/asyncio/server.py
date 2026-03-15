"""QUIC server — matches qh3.asyncio.server API."""

import asyncio
from typing import Callable

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.asyncio.protocol import QuicConnectionProtocol


class QuicServer:
    """QUIC server that accepts incoming connections.

    Unlike qh3's QuicServer (which multiplexes over a single UDP socket),
    this uses picoquic's built-in server support via TransportContext.
    """

    def __init__(
        self,
        quic: QuicConnection,
        protocol: QuicConnectionProtocol,
        loop: asyncio.AbstractEventLoop,
    ):
        self._quic = quic
        self._protocol = protocol
        self._loop = loop

    def close(self) -> None:
        """Stop the server."""
        self._protocol._stop()
        self._quic.stop()


async def serve(
    host: str,
    port: int,
    *,
    configuration: QuicConfiguration,
    create_protocol: Callable | None = None,
    stream_handler=None,
) -> QuicServer:
    """Start a QUIC server.

    Usage:
        server = await serve('0.0.0.0', 4433, configuration=config)
        # ... server is running
        server.close()
    """
    quic = QuicConnection(configuration=configuration)
    quic._start_transport(port=port)

    if create_protocol is not None:
        protocol = create_protocol(quic, stream_handler=stream_handler)
    else:
        protocol = QuicConnectionProtocol(quic, stream_handler=stream_handler)

    loop = asyncio.get_event_loop()
    protocol._start(loop)

    return QuicServer(quic, protocol, loop)
