"""Connection accessors read worker snapshots, never picoquic memory.

picoquic's worker thread owns each connection and frees it. ALPN,
transport parameters, connection IDs, path quality, byte counters and the
datagram ceiling are read from copies the worker delivers as event
payloads, so they stay safe through close and after the transport stops.
"""
import asyncio
import os

import pytest

from aiopquic.asyncio import connect, serve_dispatch
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

ALPN = "hq-interop"
RAW_ALPN = "dual-raw"
WT_PATH = "/dual"

pytestmark = pytest.mark.asyncio


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                            max_datagram_frame_size=1200)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             max_datagram_frame_size=1200)


def _read_all(tr, ptr):
    tr.get_negotiated_alpn(ptr)
    tr.transport_parameters(ptr)
    tr.transport_parameters(ptr, local=True)
    tr.connection_ids(ptr)
    tr.path_quality(ptr)
    tr.cnx_data_counters(ptr)
    tr.datagram_payload_ceiling(ptr)


async def _read_until_cancelled(tr, ptr):
    while True:
        _read_all(tr, ptr)
        await asyncio.sleep(0)


async def _eventually(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_accessors_survive_close_and_transport_stop():
    # The crash this guards against: the worker frees the cnx (at close,
    # and with every cnx when the transport stops) while the asyncio side
    # still holds its pointer and reads through it.
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    try:
        for _ in range(20):
            async with connect("127.0.0.1", port,
                               configuration=_client_cfg()) as client:
                quic = client._quic
                tr, ptr = quic._transport, quic._cnx_ptr
                sid = quic.get_next_available_stream_id()
                quic.send_stream_data(sid, b"x" * 4096, end_stream=True)
                reader = asyncio.ensure_future(_read_until_cancelled(tr, ptr))
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            reader.cancel()
            for _ in range(200):
                _read_all(tr, ptr)
            assert tr.cnx_data_counters(0) == (0, 0)
            assert tr.path_quality(0) == {}
    finally:
        server.close()


async def test_handshake_values_at_ready_and_live_values_refresh():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg()) as client:
            quic = client._quic
            tr, ptr = quic._transport, quic._cnx_ptr
            # Carried with READY: present without waiting for a refresh.
            assert tr.get_negotiated_alpn(ptr) == ALPN
            assert quic.peer_transport_parameters[
                "max_datagram_frame_size"] == 1200
            assert quic.local_transport_parameters is not None
            assert quic.max_datagram_payload() == 1200
            assert quic.connection_ids["local"]

            before = quic.bytes_sent
            sid = quic.get_next_available_stream_id()
            quic.send_stream_data(sid, b"y" * 200_000, end_stream=True)
            await _eventually(lambda: quic.bytes_sent > before + 100_000)
            assert quic.path_quality()["bytes_sent"] > before
    finally:
        server.close()


class _EchoServer(QuicConnectionProtocol):
    alpns = []

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            q = self._quic
            _EchoServer.alpns.append(
                q._transport.get_negotiated_alpn(q._cnx_ptr))
            q.send_stream_data(event.stream_id, bytes(event.data),
                               end_stream=event.end_stream)


async def test_dispatched_connections_carry_their_stack_snapshot():
    # Under single-port dispatch the stack event carries the snapshot, so
    # both stacks know their ALPN from the first event on.
    port = next_port()
    wt_alpns = []
    wt_ready = asyncio.Event()
    _EchoServer.alpns = []

    async def wt_handler(session):
        wt_alpns.append(
            session._transport.get_negotiated_alpn(session.cnx_ptr))
        wt_ready.set()

    cfg = QuicConfiguration(is_client=False, alpn_protocols=[RAW_ALPN],
                            certificate_file=CERT_FILE,
                            private_key_file=KEY_FILE)
    server = await serve_dispatch(
        "127.0.0.1", port, configuration=cfg,
        create_protocol=lambda conn, stream_handler=None: _EchoServer(conn),
        wt_path=WT_PATH, wt_handler=wt_handler)
    try:
        raw_cfg = QuicConfiguration(is_client=True,
                                    alpn_protocols=[RAW_ALPN])
        async with connect("127.0.0.1", port, configuration=raw_cfg) as c:
            sid = c._quic.get_next_available_stream_id()
            c._quic.send_stream_data(sid, b"ping", end_stream=True)
            await _eventually(lambda: bool(_EchoServer.alpns))
        async with connect_webtransport("127.0.0.1", port, WT_PATH) as s:
            async with asyncio.timeout(10):
                await wt_ready.wait()
            # The WT client's snapshot rides its session READY.
            assert s.path_quality()
        assert _EchoServer.alpns[0] == RAW_ALPN
        assert wt_alpns == ["h3"]
    finally:
        server.close()
