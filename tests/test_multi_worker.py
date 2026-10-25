"""Servers running several picoquic workers on one port (Linux SO_REUSEPORT)."""
import asyncio
import os
import ssl
import sys

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve, worker_count
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, DatagramFrameReceived, HandshakeCompleted,
    StreamDataReceived,
)

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
ALPN = "hq-interop"
REPLY = 64 * 1024

pytestmark = [
    pytest.mark.skipif(not os.path.exists(CERT_FILE),
                       reason="picoquic certs not found"),
    pytest.mark.skipif(not sys.platform.startswith("linux"),
                       reason="SO_REUSEPORT load balancing is Linux-only"),
]


class Server(QuicConnectionProtocol):
    """Replies REPLY bytes to each finished stream; echoes datagrams."""

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            asyncio.get_running_loop().create_task(self._reply(event.stream_id))
        elif isinstance(event, DatagramFrameReceived):
            self._quic.send_datagram_frame(bytes(event.data))

    async def _reply(self, stream_id):
        data, offset = b"r" * REPLY, 0
        while offset < len(data):
            chunk = data[offset:offset + 65536]
            try:
                self._quic.send_stream_data(
                    stream_id, chunk, end_stream=offset + len(chunk) >= len(data))
            except BufferError:
                await asyncio.sleep(0.002)
                continue
            offset += len(chunk)


class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handshake = None
        self.pending = {}
        self.datagram = asyncio.get_running_loop().create_future()

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            self.handshake = event
        elif isinstance(event, StreamDataReceived):
            got, fut = self.pending.get(event.stream_id, (0, None))
            got += len(event.data)
            self.pending[event.stream_id] = (got, fut)
            if event.end_stream and fut is not None and not fut.done():
                fut.set_result(got)
        elif isinstance(event, DatagramFrameReceived):
            if not self.datagram.done():
                self.datagram.set_result(bytes(event.data))
        elif isinstance(event, ConnectionTerminated):
            error = ConnectionError(f"closed {event.error_code:#x}")
            for _, fut in self.pending.values():
                if fut is not None and not fut.done():
                    fut.set_exception(error)

    async def request(self, timeout=10):
        sid = self._quic.get_next_available_stream_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending[sid] = (0, fut)
        self._quic.send_stream_data(sid, b"GET /x\r\n", end_stream=True)
        assert await asyncio.wait_for(fut, timeout) == REPLY


def _server_cfg(**kw):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN], **kw)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg(**kw):
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE, **kw)


def _recording(seen):
    """create_protocol that notes which worker accepted each connection."""
    def factory(conn, stream_handler=None):
        seen.append(id(conn._transport))
        return Server(conn, stream_handler=stream_handler)
    return factory


async def _fetch(port, cfg=None, *, datagram=False):
    async with connect("127.0.0.1", port, configuration=cfg or _client_cfg(),
                       create_protocol=Client) as client:
        await client.request()
        if datagram:
            client._quic.send_datagram_frame(b"ping")
            assert await asyncio.wait_for(client.datagram, 5) == b"ping"
        return client.handshake


async def test_connections_spread_across_workers():
    port, seen = next_port(), []
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=_recording(seen), workers=2)
    try:
        assert len(server.transports) == 2
        for _ in range(24):
            await _fetch(port, datagram=True)
        assert set(seen) == {id(t) for t in server.transports}
    finally:
        server.close()


async def test_concurrent_clients_on_four_workers():
    port, seen = next_port(), []
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=_recording(seen), workers=4)
    try:
        await asyncio.gather(*(_fetch(port) for _ in range(32)))
        assert len(seen) == 32
        assert len(set(seen)) >= 3
    finally:
        server.close()


async def test_ticket_resumes_on_another_worker(tmp_path):
    port, seen = next_port(), []
    tickets = str(tmp_path / "tickets.bin")
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=_recording(seen), workers=2)
    try:
        first = await _fetch(port, _client_cfg(session_ticket_file=tickets))
        assert not first.session_resumed
        for _ in range(20):
            handshake = await _fetch(port, _client_cfg(session_ticket_file=tickets))
            if seen[-1] != seen[0]:
                assert handshake.session_resumed
                break
        else:
            pytest.fail("no connection reached the second worker")
    finally:
        server.close()


async def test_retry_with_workers():
    port, seen = next_port(), []
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=_recording(seen), retry=True, workers=2)
    try:
        for _ in range(12):
            await _fetch(port)
        assert len(set(seen)) == 2
    finally:
        server.close()


async def test_single_worker_is_the_default():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Server)
    try:
        assert len(server.transports) == 1
        await _fetch(port)
    finally:
        server.close()


def test_worker_count_validation(monkeypatch):
    with pytest.raises(ValueError):
        worker_count(0, 4433)
    with pytest.raises(ValueError):
        worker_count(2, 0)
    assert worker_count(3, 4433) == 3
    monkeypatch.setattr(sys, "platform", "darwin")
    assert worker_count(3, 4433) == 1


def _fds_and_threads():
    return len(os.listdir("/proc/self/fd")), len(os.listdir("/proc/self/task"))


async def test_start_stop_releases_fds_and_threads():
    await _fetch_once_with(workers=1)
    before = _fds_and_threads()
    for _ in range(3):
        await _fetch_once_with(workers=3)
    for _ in range(40):
        if _fds_and_threads() <= before:
            break
        await asyncio.sleep(0.05)
    assert _fds_and_threads() <= before


async def _fetch_once_with(workers):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Server, workers=workers)
    try:
        await _fetch(port)
    finally:
        server.close()


class _RebindingProxy:
    """UDP relay for one client whose server-facing source port can change
    mid-connection, as a NAT rebinding does."""

    def __init__(self, server_addr):
        self.server_addr = server_addr
        self.client_addr = None
        self.down = self.up = None

    async def start(self):
        proxy = self

        class Down(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                proxy.client_addr = addr
                proxy.up.sendto(data, proxy.server_addr)

        self.down, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Down, local_addr=("127.0.0.1", 0))
        await self.rebind()
        return self.down.get_extra_info("sockname")[1]

    async def rebind(self):
        proxy = self

        class Up(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if proxy.client_addr is not None:
                    proxy.down.sendto(data, proxy.client_addr)

        old = self.up
        self.up, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Up, local_addr=("127.0.0.1", 0))
        if old is not None:
            old.close()

    def close(self):
        for t in (self.up, self.down):
            if t is not None:
                t.close()


async def test_rebinding_survives_or_closes_fast():
    """A new 4-tuple may hash to another worker. That worker's stateless
    reset (shared seed) closes the connection promptly; it must never hang."""
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Server, workers=2)
    outcomes = []
    try:
        for _ in range(6):
            proxy = _RebindingProxy(("127.0.0.1", port))
            proxy_port = await proxy.start()
            try:
                async with connect("127.0.0.1", proxy_port,
                                   configuration=_client_cfg(idle_timeout=30),
                                   create_protocol=Client) as client:
                    await client.request()
                    await proxy.rebind()
                    try:
                        await client.request(timeout=5)
                        outcomes.append("survived")
                    except ConnectionError:
                        outcomes.append("closed")
            finally:
                proxy.close()
        assert len(outcomes) == 6
    finally:
        server.close()
