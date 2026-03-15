"""Asyncio integration tests — QuicConnection + QuicConnectionProtocol."""

import asyncio
import os
import pytest

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection, stream_is_unidirectional
from aiopquic.quic.events import (
    HandshakeCompleted, StreamDataReceived, DatagramFrameReceived,
    StreamReset, ConnectionTerminated, ProtocolNegotiated,
)
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

_port_counter = 35567


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


def server_config(port):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def client_config():
    return QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"])


class TestQuicConnection:
    """Test QuicConnection event translation."""

    def test_stream_is_unidirectional(self):
        assert stream_is_unidirectional(2) is True
        assert stream_is_unidirectional(3) is True
        assert stream_is_unidirectional(0) is False
        assert stream_is_unidirectional(1) is False
        assert stream_is_unidirectional(4) is False

    def test_get_next_available_stream_id_client(self):
        cfg = QuicConfiguration(is_client=True)
        conn = QuicConnection(configuration=cfg)
        assert conn.get_next_available_stream_id() == 0
        assert conn.get_next_available_stream_id() == 4
        assert conn.get_next_available_stream_id(is_unidirectional=True) == 2
        assert conn.get_next_available_stream_id(is_unidirectional=True) == 6

    def test_get_next_available_stream_id_server(self):
        cfg = QuicConfiguration(is_client=False)
        conn = QuicConnection(configuration=cfg)
        assert conn.get_next_available_stream_id() == 1
        assert conn.get_next_available_stream_id() == 5
        assert conn.get_next_available_stream_id(is_unidirectional=True) == 3
        assert conn.get_next_available_stream_id(is_unidirectional=True) == 7

    def test_configuration_property(self):
        cfg = QuicConfiguration(is_client=True, alpn_protocols=["moq-chat"])
        conn = QuicConnection(configuration=cfg)
        assert conn.configuration is cfg
        assert conn.configuration.is_client is True
        assert conn.configuration.alpn_protocols == ["moq-chat"]


class TestAsyncConnect:
    """End-to-end asyncio tests using connect/serve."""

    @pytest.mark.asyncio
    async def test_connect_handshake(self):
        """Client connects, handshake completes."""
        port = next_port()
        server = await serve("127.0.0.1", port, configuration=server_config(port))

        events_received = []

        class ClientProtocol(QuicConnectionProtocol):
            def quic_event_received(self, event):
                events_received.append(event)

        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
                create_protocol=lambda quic, **kw: ClientProtocol(quic, **kw),
            ) as protocol:
                # Handshake completed (wait_connected=True by default)
                assert any(isinstance(e, HandshakeCompleted) for e in events_received)
                assert any(isinstance(e, ProtocolNegotiated) for e in events_received)
        finally:
            server.close()

    @pytest.mark.asyncio
    async def test_stream_data_exchange(self):
        """Client sends stream data, server receives it."""
        port = next_port()
        server_events = []

        class ServerProtocol(QuicConnectionProtocol):
            def quic_event_received(self, event):
                server_events.append(event)

        srv_cfg = server_config(port)
        srv_quic = QuicConnection(configuration=srv_cfg)
        srv_quic._start_transport(port=port)
        srv_protocol = ServerProtocol(srv_quic)
        loop = asyncio.get_event_loop()
        srv_protocol._start(loop)

        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
            ) as client:
                # Send data on a stream
                stream_id = client._quic.get_next_available_stream_id()
                client._quic.send_stream_data(stream_id, b"hello async", end_stream=True)

                # Wait for server to receive
                await asyncio.sleep(0.2)

                data_events = [e for e in server_events
                               if isinstance(e, StreamDataReceived)]
                assert len(data_events) > 0
                received = b"".join(e.data for e in data_events)
                assert received == b"hello async"
        finally:
            srv_protocol._stop()
            srv_quic.stop()

    @pytest.mark.asyncio
    async def test_datagram_exchange(self):
        """Bidirectional datagram exchange."""
        port = next_port()
        server_events = []

        class ServerProtocol(QuicConnectionProtocol):
            def quic_event_received(self, event):
                server_events.append(event)

        srv_cfg = server_config(port)
        srv_quic = QuicConnection(configuration=srv_cfg)
        srv_quic._start_transport(port=port)
        srv_protocol = ServerProtocol(srv_quic)
        loop = asyncio.get_event_loop()
        srv_protocol._start(loop)

        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
            ) as client:
                client._quic.send_datagram_frame(b"datagram via asyncio")
                await asyncio.sleep(0.2)

                dgram_events = [e for e in server_events
                                if isinstance(e, DatagramFrameReceived)]
                assert len(dgram_events) > 0
                assert dgram_events[0].data == b"datagram via asyncio"
        finally:
            srv_protocol._stop()
            srv_quic.stop()
