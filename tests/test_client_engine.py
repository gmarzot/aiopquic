"""QuicClientEngine: raw QUIC client connections on one network thread."""
import asyncio
import contextlib
import os
import sys
import tempfile
import time

import pytest

from aiopquic.asyncio import QuicClientEngine, connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import _EVT_ALMOST_READY, _TX_CONNECT
from aiopquic.quic.events import ConnectionTerminated, StreamDataReceived

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


class Labelled(QuicConnectionProtocol):
    label = b"?"

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            self._quic.send_stream_data(event.stream_id, self.label,
                                        end_stream=True)


async def _labelled_server(label: bytes):
    port = next_port()
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    proto = type("Labelled" + label.decode(), (Labelled,), {"label": label})
    return port, await serve("127.0.0.1", port, configuration=cfg,
                             create_protocol=proto)


class GatedEngine(QuicClientEngine):
    """Holds routed events until release(), so a dial can be cancelled
    after its CONNECT was answered but before the reply is routed."""

    def __init__(self):
        super().__init__()
        self.held = []
        self.hold = True
        self.created = asyncio.Event()

    def route_event(self, ev):
        if not self.hold:
            return super().route_event(ev)
        ev = list(ev)
        if ev[2] is not None:
            ev[2] = bytes(ev[2])
        self.held.append(ev)
        if ev[0] == _EVT_ALMOST_READY:
            self.created.set()

    def release(self):
        self.hold = False
        for ev in self.held:
            super().route_event(ev)
        self.held.clear()


async def test_cancelled_dial_does_not_take_the_next_connection():
    port_a, server_a = await _labelled_server(b"A")
    port_b, server_b = await _labelled_server(b"B")
    engine = GatedEngine()
    second_cm = None
    try:
        first = asyncio.create_task(_open(port_a, engine).__aenter__())
        await asyncio.wait_for(engine.created.wait(), 5)
        first_ptr = next(ev[5] for ev in engine.held
                         if ev[0] == _EVT_ALMOST_READY)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        assert list(engine._pending) == [None]

        second_cm = _open(port_b, engine)
        second = asyncio.create_task(second_cm.__aenter__())
        while len(engine._pending) < 2:
            await asyncio.sleep(0)
        engine.release()
        client = await asyncio.wait_for(second, 5)
        assert client._quic._cnx_ptr != first_ptr
        assert await client.echo(b"to B") == b"B"
        async with asyncio.timeout(5):
            while first_ptr in engine._protocols:
                await asyncio.sleep(0.01)
        assert not engine._pending
        assert list(engine._protocols.values()) == [client]
    finally:
        if second_cm is not None:
            await second_cm.__aexit__(None, None, None)
        engine.close()
        server_a.close()
        server_b.close()


class NotCreated(Client):
    """Submits a CONNECT the worker cannot create a connection from."""

    def connect(self, addr):
        self._quic._transport.push_tx_event(_TX_CONNECT, 0, data=b"\0")
        self._quic._transport.wake_up()


async def test_failed_creation_fails_its_dial_and_not_the_next():
    port_a, server_a = await _labelled_server(b"A")
    engine = QuicClientEngine()
    try:
        async with _open(port_a, engine) as warm:
            assert await warm.echo(b"x") == b"A"

        async def failed():
            async with connect("127.0.0.1", port_a, configuration=_client_cfg(),
                               create_protocol=NotCreated, engine=engine):
                pass

        async def ok():
            async with _open(port_a, engine) as c:
                return await c.echo(b"y")

        started = time.monotonic()
        results = await asyncio.gather(failed(), ok(), return_exceptions=True)
        assert isinstance(results[0], ConnectionError)
        assert results[1] == b"A"
        assert time.monotonic() - started < 3
        assert not engine._pending
    finally:
        engine.close()
        server_a.close()


async def test_failed_creation_fails_a_standalone_dial():
    port_a, server_a = await _labelled_server(b"A")
    try:
        started = time.monotonic()
        with pytest.raises(ConnectionError):
            async with connect("127.0.0.1", port_a, configuration=_client_cfg(),
                               create_protocol=NotCreated):
                pass
        assert time.monotonic() - started < 3
    finally:
        server_a.close()


class Recorded(Client):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.terminated = []

    def quic_event_received(self, event):
        super().quic_event_received(event)
        if isinstance(event, ConnectionTerminated):
            self.terminated.append(event)


class RecordedEcho(Echo):
    seen: list = []

    def quic_event_received(self, event):
        super().quic_event_received(event)
        if isinstance(event, ConnectionTerminated):
            RecordedEcho.seen.append(self)


async def test_engine_close_gives_every_protocol_its_terminal_event():
    port, server = await _server()
    engine = QuicClientEngine()
    cms = [connect("127.0.0.1", port, configuration=_client_cfg(),
                   create_protocol=Recorded, engine=engine) for _ in range(2)]
    try:
        clients = [await cm.__aenter__() for cm in cms]
        for i, c in enumerate(clients):
            assert await c.echo(bytes([i])) == bytes([i])
        engine.close()
        for c in clients:
            assert len(c.terminated) == 1
            assert c.terminated[0].frame_type == 0
            await asyncio.wait_for(c.wait_closed(), 0.2)
        started = time.monotonic()
        for cm in reversed(cms):
            await cm.__aexit__(None, None, None)
        assert time.monotonic() - started < 0.5
    finally:
        engine.close()
        server.close()


async def test_engine_close_fails_a_dial_waiting_for_its_connection():
    port, server = await _server()
    engine = GatedEngine()
    try:
        dial = asyncio.create_task(_open(port, engine).__aenter__())
        await asyncio.wait_for(engine.created.wait(), 5)
        started = time.monotonic()
        engine.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(dial, 1)
        assert time.monotonic() - started < 1
        assert not engine._pending
    finally:
        engine.close()
        server.close()


async def test_server_close_gives_its_protocols_a_terminal_event():
    port = next_port()
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    RecordedEcho.seen = []
    server = await serve("127.0.0.1", port, configuration=cfg,
                         create_protocol=RecordedEcho)
    try:
        async with _open(port, None) as c:
            assert await c.echo(b"s") == b"s"
            server.close()
            assert len(RecordedEcho.seen) == 1
            assert RecordedEcho.seen[0]._closed.is_set()
    finally:
        server.close()


async def test_shared_engine_writes_the_session_ticket_file():
    port, server = await _server()
    engine = QuicClientEngine()
    try:
        with tempfile.TemporaryDirectory() as td:
            tickets = os.path.join(td, "tickets.bin")
            cfg = _client_cfg(session_ticket_file=tickets)
            async with connect("127.0.0.1", port, configuration=cfg,
                               create_protocol=Client, engine=engine) as c:
                assert await c.echo(b"t") == b"t"
            engine.close()
            assert os.path.getsize(tickets) > 0
    finally:
        engine.close()
        server.close()
