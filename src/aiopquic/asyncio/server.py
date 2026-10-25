"""QUIC server — matches qh3.asyncio.server API.

Per-connection multiplexing: picoquic does packet→cnx demux in C; the
QuicEngine drains the shared SPSC ring and routes events to per-cnx
QuicConnection + protocol instances. The user's create_protocol factory
is invoked once per accepted connection (matching qh3's per-connection
protocol model).
"""

import asyncio
from typing import Callable

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicEngine
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio._rxwatch import clear_reader, set_reader


class QuicServer:
    """QUIC server that accepts incoming connections via picoquic."""

    def __init__(
        self,
        engine: QuicEngine,
        loop: asyncio.AbstractEventLoop,
    ):
        self._engine = engine
        self._loop = loop

    def close(self) -> None:
        """Stop the server and tear down all live connections."""
        if self._loop is not None and self._engine._transport is not None:
            clear_reader(self._loop, self._engine._transport, self._engine)
        self._engine.close()


async def serve(
    host: str,
    port: int,
    *,
    configuration: QuicConfiguration,
    create_protocol: Callable | None = None,
    stream_handler=None,
    retry: bool = False,
) -> QuicServer:
    """Start a QUIC server.

    create_protocol is called once per accepted connection with a fresh
    per-cnx QuicConnection. Falls back to the default
    QuicConnectionProtocol if not supplied. retry=True answers each new
    connection's first Initial with a Retry to validate the client's
    address.
    """
    if create_protocol is None:
        create_protocol = (
            lambda conn, stream_handler=None: QuicConnectionProtocol(
                conn, stream_handler=stream_handler))

    engine = QuicEngine(
        configuration=configuration,
        create_protocol=create_protocol,
        stream_handler=stream_handler,
    )
    engine._start_transport(port=port, retry=retry, host=host)

    loop = asyncio.get_event_loop()
    if engine.eventfd >= 0:
        set_reader(loop, engine._transport, engine.drain_and_route, engine)

    return QuicServer(engine, loop)
