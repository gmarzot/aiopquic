"""Interop tests against public QUIC/H3 endpoints.

Tests aiopquic client against real-world servers running different
QUIC implementations for genetic diversity:
  - quic.nginx.org (nginx-quic, C)
  - cloudflare-quic.com (quiche, Rust)
  - www.google.com (Google QUIC, C++)
  - quic.aiortc.org (aioquic, Python)

Run with: pytest -m interop
Excluded from default test suite (needs network).
"""

import asyncio
import pytest
import socket

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    HandshakeCompleted, StreamDataReceived,
    ConnectionTerminated, ProtocolNegotiated,
)
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.client import connect


INTEROP_SERVERS = [
    ("quic.nginx.org", 443, "h3"),
    ("cloudflare-quic.com", 443, "h3"),
    ("www.google.com", 443, "h3"),
    ("quic.aiortc.org", 443, "h3"),
]


def _can_resolve(host):
    try:
        socket.getaddrinfo(host, 443, socket.AF_INET)
        return True
    except socket.gaierror:
        return False


def h3_client_config(host):
    return QuicConfiguration(
        is_client=True,
        alpn_protocols=["h3"],
        server_name=host,
    )


class CollectProtocol(QuicConnectionProtocol):
    def __init__(self, quic, **kwargs):
        super().__init__(quic, **kwargs)
        self.events = []

    def quic_event_received(self, event):
        self.events.append(event)


@pytest.mark.interop
class TestInteropHandshake:
    """QUIC handshake against diverse public implementations.

    This verifies TLS 1.3 handshake, version negotiation, and
    ALPN negotiation work with real servers — not just ourselves.

    Implementations tested:
      nginx-quic (C), quiche (Rust), Google QUIC (C++), aioquic (Python)
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host,port,alpn", INTEROP_SERVERS,
        ids=[s[0] for s in INTEROP_SERVERS],
    )
    async def test_handshake(self, host, port, alpn):
        if not _can_resolve(host):
            pytest.skip(f"Cannot resolve {host}")

        async with connect(
            host, port,
            configuration=h3_client_config(host),
            create_protocol=lambda q, **kw: CollectProtocol(q, **kw),
        ) as proto:
            handshake = [
                e for e in proto.events
                if isinstance(e, HandshakeCompleted)
            ]
            assert len(handshake) == 1, (
                f"No handshake with {host}"
            )
            negotiated = [
                e for e in proto.events
                if isinstance(e, ProtocolNegotiated)
            ]
            assert len(negotiated) == 1


@pytest.mark.interop
class TestInteropStream:
    """Raw QUIC stream data exchange with public servers.

    Sends a minimal request on a bidi stream and verifies
    we get stream data back. Tests the full data path:
    picoquic → SPSC ring → Python events.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host,port,alpn", INTEROP_SERVERS,
        ids=[s[0] for s in INTEROP_SERVERS],
    )
    async def test_stream_response(self, host, port, alpn):
        if not _can_resolve(host):
            pytest.skip(f"Cannot resolve {host}")

        async with connect(
            host, port,
            configuration=h3_client_config(host),
            create_protocol=lambda q, **kw: CollectProtocol(q, **kw),
        ) as proto:
            # Send raw bytes on a stream — servers may reject
            # this as malformed H3 but we should still get
            # either data or a stream reset/close back
            sid = proto._quic.get_next_available_stream_id()
            proto._quic.send_stream_data(
                sid, b'\x00\x00', end_stream=True,
            )

            # Wait for any response
            for _ in range(20):
                await asyncio.sleep(0.1)
                responses = [
                    e for e in proto.events
                    if isinstance(e, (
                        StreamDataReceived,
                        ConnectionTerminated,
                    ))
                ]
                if responses:
                    break

            # We got *something* back — the server processed
            # our connection and responded (even if error)
            assert len(responses) > 0, (
                f"No response from {host} after 2s"
            )
