"""QUIC asyncio protocol — matches qh3.asyncio.protocol API.

Uses eventfd for zero-overhead notification from picoquic's network
thread to the asyncio event loop.
"""

import asyncio

from aiopquic.quic.connection import QuicConnection
from aiopquic.quic.events import (
    QuicEvent, HandshakeCompleted, ConnectionTerminated,
)


class QuicConnectionProtocol:
    """asyncio protocol for QUIC connections.

    Bridges QuicConnection (picoquic network thread) with the asyncio
    event loop using eventfd for RX notification.

    Subclass and override quic_event_received() to handle events.
    """

    def __init__(self, quic: QuicConnection,
                 stream_handler=None):
        self._quic = quic
        self._stream_handler = stream_handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = asyncio.Event()
        self._connected_waiter: asyncio.Future | None = None

    @property
    def _transport(self):
        """Access the underlying TransportContext (for compat)."""
        return self._quic._transport

    def _start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Register eventfd with the event loop."""
        self._loop = loop or asyncio.get_event_loop()
        eventfd = self._quic.eventfd
        if eventfd >= 0:
            self._loop.add_reader(eventfd, self._on_eventfd)

    def _stop(self) -> None:
        """Unregister eventfd from the event loop."""
        if self._loop is not None:
            eventfd = self._quic.eventfd
            if eventfd >= 0:
                try:
                    self._loop.remove_reader(eventfd)
                except Exception:
                    pass

    def _on_eventfd(self) -> None:
        """Called by asyncio when eventfd is readable (RX events ready)."""
        self._process_events()

    def _process_events(self) -> None:
        """Drain events from QuicConnection and dispatch."""
        event = self._quic.next_event()
        while event is not None:
            if isinstance(event, HandshakeCompleted):
                if self._connected_waiter and not self._connected_waiter.done():
                    self._connected_waiter.set_result(None)

            if isinstance(event, ConnectionTerminated):
                self._closed.set()

            self.quic_event_received(event)
            event = self._quic.next_event()

    def quic_event_received(self, event: QuicEvent) -> None:
        """Called for each QUIC event. Override in subclass."""
        pass

    def connect(self, addr: tuple[str, int]) -> None:
        """Initiate TLS handshake (client only)."""
        self._quic.connect(addr)

    def close(self) -> None:
        """Close the QUIC connection."""
        self._quic.close()

    async def wait_connected(self) -> None:
        """Wait for TLS handshake to complete."""
        if self._quic._connected:
            return
        if self._connected_waiter is None:
            self._connected_waiter = self._loop.create_future()
        await self._connected_waiter

    async def wait_closed(self) -> None:
        """Wait for connection to be fully closed."""
        await self._closed.wait()
        self._stop()
        self._quic.stop()
