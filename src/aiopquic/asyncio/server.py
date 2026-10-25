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
from aiopquic.asyncio._workers import shared_port_args, worker_count


class QuicServer:
    """QUIC server that accepts incoming connections via picoquic.

    With several workers, each engine owns its own picoquic context,
    network thread and connections; they share one port.
    """

    def __init__(
        self,
        engines: QuicEngine | list[QuicEngine],
        loop: asyncio.AbstractEventLoop,
    ):
        self._engines = (list(engines) if isinstance(engines, (list, tuple))
                         else [engines])
        self._engine = self._engines[0]
        self._loop = loop

    @property
    def transports(self) -> list:
        """Each worker's TransportContext."""
        return [engine._transport for engine in self._engines]

    def close(self) -> None:
        """Stop the server and tear down all live connections."""
        for engine in self._engines:
            if self._loop is not None and engine.eventfd >= 0:
                try:
                    self._loop.remove_reader(engine.eventfd)
                except Exception:
                    pass
            engine.close()


async def serve(
    host: str,
    port: int,
    *,
    configuration: QuicConfiguration,
    create_protocol: Callable | None = None,
    stream_handler=None,
    retry: bool = False,
    workers: int = 1,
) -> QuicServer:
    """Start a QUIC server.

    create_protocol is called once per accepted connection with a fresh
    per-cnx QuicConnection. Falls back to the default
    QuicConnectionProtocol if not supplied. retry=True answers each new
    connection's first Initial with a Retry to validate the client's
    address. workers > 1 (Linux) runs that many picoquic contexts on one
    port; the kernel spreads connections across them.
    """
    if create_protocol is None:
        create_protocol = (
            lambda conn, stream_handler=None: QuicConnectionProtocol(
                conn, stream_handler=stream_handler))

    workers = worker_count(workers, port)
    shared = shared_port_args(workers)
    engines = []
    try:
        for _ in range(workers):
            engine = QuicEngine(
                configuration=configuration,
                create_protocol=create_protocol,
                stream_handler=stream_handler,
            )
            engine._start_transport(port=port, retry=retry, host=host, **shared)
            engines.append(engine)
    except BaseException:
        for engine in engines:
            engine.close()
        raise

    loop = asyncio.get_event_loop()
    for engine in engines:
        if engine.eventfd >= 0:
            loop.add_reader(engine.eventfd, engine.drain_and_route)

    return QuicServer(engines, loop)
