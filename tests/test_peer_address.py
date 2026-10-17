"""peer_address() / local_address() on raw QUIC connections and
WebTransport sessions, read from worker snapshots."""
import asyncio
import os
import socket

from aiopquic._binding._transport import _format_sockaddr
from aiopquic.asyncio import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
WT_PATH = "/peer"

def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE)


async def test_raw_quic_peer_address_both_sides():
    port = next_port()
    seen = []

    def make_protocol(conn, stream_handler=None):
        # Earliest point the app sees a server connection.
        seen.append((conn.peer_address(), conn.local_address()))
        return QuicConnectionProtocol(conn, stream_handler=stream_handler)

    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=make_protocol)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg()) as client:
            quic = client._quic
            assert quic.peer_address() == ("127.0.0.1", port)
            local = quic.local_address()
            async with asyncio.timeout(5):
                while not seen:
                    await asyncio.sleep(0.01)
        srv_peer, srv_local = seen[0]
        assert srv_peer is not None and srv_peer[0] == "127.0.0.1"
        assert srv_peer[1] not in (0, port)
        assert local == srv_peer
        assert srv_local == ("127.0.0.1", port)
    finally:
        server.close()


async def test_webtransport_server_session_peer_address():
    port = next_port()
    seen = []
    got = asyncio.Event()

    async def handler(session):
        seen.append(session.peer_address())
        got.set()
        await session.wait_closed()

    server = await serve_webtransport(
        "127.0.0.1", port, WT_PATH, handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, WT_PATH,
                                       sni=SNI, ca_file=CA_FILE) as s:
            assert s.peer_address() == ("127.0.0.1", port)
            async with asyncio.timeout(10):
                await got.wait()
            assert seen[0] is not None and seen[0][0] == "127.0.0.1"
            assert seen[0][1] not in (0, port)
            assert s.local_address() == seen[0]
    finally:
        await server.aclose()


async def test_not_connected_is_none():
    conn = QuicConnection(configuration=_client_cfg())
    assert conn.peer_address() is None
    assert conn.local_address() is None


def test_format_sockaddr():
    v4 = socket.inet_pton(socket.AF_INET, "192.0.2.7") + bytes(12)
    assert _format_sockaddr(4, v4, 4433) == ("192.0.2.7", 4433)
    v6 = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
    assert _format_sockaddr(6, v6, 443) == ("2001:db8::1", 443)
    assert _format_sockaddr(6, socket.inet_pton(socket.AF_INET6, "::1"), 1) == ("::1", 1)
    mapped = socket.inet_pton(socket.AF_INET6, "::ffff:198.51.100.9")
    assert _format_sockaddr(6, mapped, 9) == ("198.51.100.9", 9)
    assert _format_sockaddr(0, bytes(16), 0) is None

