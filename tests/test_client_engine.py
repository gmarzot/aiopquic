"""QuicClientEngine: raw QUIC client connections on one network thread."""
import asyncio
import os
import sys

import pytest

from aiopquic.asyncio import QuicClientEngine, connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"


class Echo(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self._quic.send_stream_data(event.stream_id, bytes(event.data),
                                        end_stream=event.end_stream)


class Client(QuicConnectionProtocol):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.received = bytearray()
        self.finished = asyncio.Event()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += bytes(event.data)
            if event.end_stream:
                self.finished.set()

    async def echo(self, payload: bytes) -> bytes:
        self.received.clear()
        self.finished.clear()
        sid = self._quic.get_next_available_stream_id()
        self._quic.send_stream_data(sid, payload, end_stream=True)
        await asyncio.wait_for(self.finished.wait(), 10)
        return bytes(self.received)


def _client_cfg(**kw):
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE, idle_timeout=5, **kw)


async def _server():
    port = next_port()
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return port, await serve("127.0.0.1", port, configuration=cfg, create_protocol=Echo)


def _open(port, engine):
    return connect("127.0.0.1", port, configuration=_client_cfg(),
                   create_protocol=Client, engine=engine)


def _thread_count() -> int:
    return len(os.listdir("/proc/self/task"))


async def test_connections_keep_their_own_data():
    port, server = await _server()
    engine = QuicClientEngine()

    async def one(i):
        async with _open(port, engine) as c:
            payload = bytes([i]) * (20_000 + i)
            assert await c.echo(payload) == payload

    try:
        await asyncio.gather(*(one(i) for i in range(16)))
    finally:
        engine.close()
        server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="counts /proc threads")
async def test_one_thread_serves_every_connection():
    port, server = await _server()
    engine = QuicClientEngine()
    held = []
    try:
        held.append(_open(port, engine))
        await held[0].__aenter__()
        threads = _thread_count()
        for _ in range(7):
            held.append(_open(port, engine))
            await held[-1].__aenter__()
        assert _thread_count() == threads
    finally:
        for cm in reversed(held):
            await cm.__aexit__(None, None, None)
        engine.close()
        server.close()


async def test_closing_one_leaves_the_others_and_new_ones_work():
    port, server = await _server()
    engine = QuicClientEngine()
    try:
        async with _open(port, engine) as kept:
            async with _open(port, engine) as gone:
                assert await gone.echo(b"a") == b"a"
            assert await kept.echo(b"b") == b"b"
            async with _open(port, engine) as late:
                assert await late.echo(b"c") == b"c"
            assert await kept.echo(b"d") == b"d"
    finally:
        engine.close()
        server.close()


async def test_failed_handshake_does_not_shift_the_next_connection():
    port, server = await _server()
    engine = QuicClientEngine()
    wrong = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                              server_name=SNI, cafile=CA_FILE, idle_timeout=1)
    try:
        with pytest.raises(ConnectionError):
            async with asyncio.timeout(8):
                async with connect("127.0.0.1", next_port(), configuration=wrong,
                                   create_protocol=Client, engine=engine):
                    pass
        async with _open(port, engine) as c:
            assert await c.echo(b"after") == b"after"
    finally:
        engine.close()
        server.close()


async def test_engine_close_ends_open_connections():
    port, server = await _server()
    engine = QuicClientEngine()
    try:
        async with _open(port, engine) as c:
            assert await c.echo(b"x") == b"x"
            engine.close()
            assert c._quic.closed
    finally:
        engine.close()
        server.close()


async def test_ring_capacity_reaches_the_transport():
    port, server = await _server()
    engine = QuicClientEngine(ring_capacity=1 << 17)
    try:
        async with _open(port, engine) as c:
            assert await c.echo(b"y") == b"y"
            assert c._quic._transport.tx_event_ring_capacity >= 1 << 17
    finally:
        engine.close()
        server.close()


def _local_port(client) -> int:
    return client._quic.local_address()[1]


async def test_connections_take_their_own_source_ports():
    port, server = await _server()
    engine = QuicClientEngine(sockets=4)
    held = []
    try:
        for i in range(4):
            cm = _open(port, engine)
            client = await cm.__aenter__()
            held.append((cm, client))
            payload = bytes([i]) * 50_000
            assert await client.echo(payload) == payload
        ports = {_local_port(c) for _, c in held}
        assert len(ports) == (4 if sys.platform == "linux" else 1)
    finally:
        for cm, _ in reversed(held):
            await cm.__aexit__(None, None, None)
        engine.close()
        server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="the socket pool is Linux only")
async def test_more_connections_than_sockets_share_them():
    port, server = await _server()
    engine = QuicClientEngine(sockets=3)
    held = []
    try:
        for i in range(7):
            cm = _open(port, engine)
            client = await cm.__aenter__()
            held.append((cm, client))
            assert await client.echo(bytes([i])) == bytes([i])
        assert len({_local_port(c) for _, c in held}) == 3
        for i, (_, client) in enumerate(held):
            assert await client.echo(bytes([i + 100])) == bytes([i + 100])
    finally:
        for cm, _ in reversed(held):
            await cm.__aexit__(None, None, None)
        engine.close()
        server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="counts /proc threads")
async def test_socket_pool_adds_no_thread():
    port, server = await _server()
    engine = QuicClientEngine(sockets=8)
    held = []
    try:
        held.append(_open(port, engine))
        await held[0].__aenter__()
        threads = _thread_count()
        for _ in range(7):
            held.append(_open(port, engine))
            await held[-1].__aenter__()
        assert _thread_count() == threads
    finally:
        for cm in reversed(held):
            await cm.__aexit__(None, None, None)
        engine.close()
        server.close()


async def test_socket_pool_carries_large_transfers_at_once():
    port, server = await _server()
    engine = QuicClientEngine(sockets=4)

    async def one(i):
        async with _open(port, engine) as c:
            payload = bytes([i]) * 4_000_000
            assert await c.echo(payload) == payload

    try:
        await asyncio.gather(*(one(i) for i in range(4)))
    finally:
        engine.close()
        server.close()


async def test_socket_pool_closes_with_connections_open():
    port, server = await _server()
    engine = QuicClientEngine(sockets=4)
    try:
        async with _open(port, engine) as first, _open(port, engine) as second:
            assert await first.echo(b"a") == b"a"
            assert await second.echo(b"b") == b"b"
            engine.close()
            assert first._quic.closed and second._quic.closed
    finally:
        engine.close()
        server.close()


async def test_socket_pool_leaves_no_wake_lost():
    port, server = await _server()
    engine = QuicClientEngine(sockets=4)
    try:
        async with _open(port, engine) as c:
            for i in range(50):
                assert await c.echo(bytes([i])) == bytes([i])
                await asyncio.sleep(0.01)
            assert c._quic._transport.counters["tx_wake_lost"] == 0
    finally:
        engine.close()
        server.close()
