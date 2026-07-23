"""Single-port dual-stack dispatch: raw QUIC + h3/WebTransport on one
UDP port (serve_dispatch), each connection routed by negotiated ALPN.

The acceptance case: two clients against the SAME port at the SAME
time — one raw QUIC (byte-echo under a private ALPN), one WebTransport
(CONNECT at the registered path) — both stacks fully live.
"""
import asyncio
import ssl
import zlib

import pytest

from aiopquic.asyncio import connect, serve_dispatch
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.webtransport import connect_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from .conftest import counted_pad, _free_port

RAW_ALPN = "dual-raw"
WT_PATH = "/dual"

pytestmark = pytest.mark.asyncio


class _EchoServer(QuicConnectionProtocol):
    """Raw-side test protocol: echo every stream chunk, FIN on FIN."""

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self._quic.send_stream_data(
                event.stream_id, bytes(event.data),
                end_stream=event.end_stream)
            self.transmit()


class _EchoClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = bytearray()
        self.done = asyncio.get_event_loop().create_future()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += event.data
            if event.end_stream and not self.done.done():
                self.done.set_result(True)


async def _serve_dual(port, cert_paths, wt_accepted):
    async def wt_handler(session):
        wt_accepted.set()

    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[RAW_ALPN],
        certificate_file=cert_paths["cert"],
        private_key_file=cert_paths["key"])
    return await serve_dispatch(
        "127.0.0.1", port, configuration=cfg,
        create_protocol=lambda conn, stream_handler=None: _EchoServer(conn),
        wt_path=WT_PATH, wt_handler=wt_handler)


async def _raw_roundtrip(port, payload=256 * 1024):
    cfg = QuicConfiguration(
        is_client=True, alpn_protocols=[RAW_ALPN],
        verify_mode=ssl.CERT_NONE)
    pad = counted_pad(payload)
    async with connect("127.0.0.1", port, configuration=cfg,
                       create_protocol=_EchoClient) as client:
        sid = client._quic.get_next_available_stream_id()
        client._quic.send_stream_data(sid, pad, end_stream=True)
        client.transmit()
        async with asyncio.timeout(30):
            await client.done
        assert zlib.crc32(bytes(client.received)) == zlib.crc32(pad)


async def _wt_session(port, wt_accepted):
    async with connect_webtransport("127.0.0.1", port, WT_PATH):
        async with asyncio.timeout(10):
            await wt_accepted.wait()


async def test_raw_quic_on_shared_port(cert_paths):
    port = _free_port()
    wt_accepted = asyncio.Event()
    server = await _serve_dual(port, cert_paths, wt_accepted)
    try:
        await _raw_roundtrip(port)
    finally:
        server.close()


async def test_webtransport_on_shared_port(cert_paths):
    port = _free_port()
    wt_accepted = asyncio.Event()
    server = await _serve_dual(port, cert_paths, wt_accepted)
    try:
        await _wt_session(port, wt_accepted)
    finally:
        server.close()


async def test_concurrent_raw_and_wt_clients(cert_paths):
    # The acceptance case: both transports live on ONE port at once.
    port = _free_port()
    wt_accepted = asyncio.Event()
    server = await _serve_dual(port, cert_paths, wt_accepted)
    try:
        async with asyncio.timeout(60):
            await asyncio.gather(
                _raw_roundtrip(port),
                _wt_session(port, wt_accepted),
            )
        assert wt_accepted.is_set()
    finally:
        server.close()
