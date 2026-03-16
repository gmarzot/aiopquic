"""H3 integration tests — qh3 H3Connection on top of aiopquic QuicConnection."""

import asyncio
import os
import pytest

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.h3.connection import H3Connection, H3_ALPN
from aiopquic.h3.events import (
    DataReceived, HeadersReceived, WebTransportStreamDataReceived,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

_port_counter = 36567


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


def server_config():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def client_config():
    return QuicConfiguration(
        is_client=True, alpn_protocols=H3_ALPN,
    )


class TestH3Import:
    """Verify H3 re-export module works."""

    def test_h3_alpn(self):
        assert H3_ALPN == ["h3"]

    def test_h3_connection_class(self):
        assert H3Connection is not None
        assert callable(H3Connection)

    def test_h3_events(self):
        assert DataReceived is not None
        assert HeadersReceived is not None
        assert WebTransportStreamDataReceived is not None


class TestH3ApiCompatibility:
    """Verify our QuicConnection has the API surface H3Connection needs."""

    def test_quic_has_configuration_is_client(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert quic.configuration.is_client is True

    def test_quic_has_quic_logger(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert hasattr(quic, '_quic_logger')

    def test_quic_has_remote_max_datagram_frame_size(self):
        cfg = QuicConfiguration(
            is_client=True, max_datagram_frame_size=65536,
        )
        quic = QuicConnection(configuration=cfg)
        assert quic._remote_max_datagram_frame_size == 65536

    def test_quic_has_send_stream_data(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert callable(quic.send_stream_data)

    def test_quic_has_send_datagram_frame(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert callable(quic.send_datagram_frame)

    def test_quic_has_get_next_available_stream_id(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert callable(quic.get_next_available_stream_id)

    def test_quic_has_close(self):
        cfg = QuicConfiguration(is_client=True)
        quic = QuicConnection(configuration=cfg)
        assert callable(quic.close)


class TestH3AsyncIntegration:
    """End-to-end H3 over aiopquic transport."""

    @pytest.mark.asyncio
    async def test_h3_wraps_live_connection(self):
        """H3Connection wraps a connected QuicConnection."""
        port = next_port()
        server = await serve(
            "127.0.0.1", port, configuration=server_config(),
        )
        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
            ) as protocol:
                h3 = H3Connection(protocol._quic)
                assert h3._quic is protocol._quic
                assert h3._is_client is True
                assert h3._local_control_stream_id is not None
                assert h3._local_encoder_stream_id is not None
                assert h3._local_decoder_stream_id is not None
                assert h3._sent_settings is not None
        finally:
            server.close()

    @pytest.mark.asyncio
    async def test_h3_send_headers(self):
        """H3 client sends request headers."""
        port = next_port()
        server = await serve(
            "127.0.0.1", port, configuration=server_config(),
        )
        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
            ) as protocol:
                h3 = H3Connection(protocol._quic)
                stream_id = protocol._quic.get_next_available_stream_id()
                h3.send_headers(
                    stream_id=stream_id,
                    headers=[
                        (b":method", b"GET"),
                        (b":scheme", b"https"),
                        (b":authority", b"localhost"),
                        (b":path", b"/"),
                    ],
                )
                # send_headers is void; verify no crash
                assert stream_id >= 0
        finally:
            server.close()

    @pytest.mark.asyncio
    async def test_h3_send_data(self):
        """H3 client sends headers + body."""
        port = next_port()
        server = await serve(
            "127.0.0.1", port, configuration=server_config(),
        )
        try:
            async with connect(
                "127.0.0.1", port,
                configuration=client_config(),
            ) as protocol:
                h3 = H3Connection(protocol._quic)
                stream_id = protocol._quic.get_next_available_stream_id()
                h3.send_headers(
                    stream_id=stream_id,
                    headers=[
                        (b":method", b"POST"),
                        (b":scheme", b"https"),
                        (b":authority", b"localhost"),
                        (b":path", b"/upload"),
                        (b"content-type", b"application/octet-stream"),
                    ],
                )
                h3.send_data(
                    stream_id=stream_id,
                    data=b"hello from aiopquic h3",
                    end_stream=True,
                )
        finally:
            server.close()

    @pytest.mark.asyncio
    async def test_h3_webtransport_enabled(self):
        """H3 with WebTransport sends WT settings."""
        port = next_port()
        srv_cfg = server_config()
        srv_cfg.max_datagram_frame_size = 65536
        server = await serve(
            "127.0.0.1", port, configuration=srv_cfg,
        )
        try:
            cli_cfg = client_config()
            cli_cfg.max_datagram_frame_size = 65536
            async with connect(
                "127.0.0.1", port,
                configuration=cli_cfg,
            ) as protocol:
                h3 = H3Connection(
                    protocol._quic, enable_webtransport=True,
                )
                assert h3._enable_webtransport is True
                assert h3.sent_settings is not None
        finally:
            server.close()
