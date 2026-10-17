"""Loss recovery across a total outage, through an in-process UDP relay.

The relay drops every datagram in both directions for a while. A sender
with data in flight must keep probing through the outage and recover
when the link returns, rather than going silent until the idle timeout.
"""
import asyncio
import os
import ssl
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import ConnectionTerminated, StreamDataReceived

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
ALPN = "hq-interop"
IDLE_TIMEOUT = 8.0

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


class Relay(asyncio.DatagramProtocol):
    """Forwards between one client and the server; drops all while down."""

    def __init__(self, server_port: int):
        self.server_port = server_port
        self.client = None
        self.down = False
        self.dropped = {"client": 0, "server": 0}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        from_server = addr[1] == self.server_port
        if not from_server:
            self.client = addr
        if self.down:
            self.dropped["server" if from_server else "client"] += 1
            return
        dst = self.client if from_server else ("127.0.0.1", self.server_port)
        if dst is not None:
            self.transport.sendto(data, dst)


class Server(QuicConnectionProtocol):
    """Answers a request stream with `reply_size` bytes and FIN."""

    reply_size = 2

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            asyncio.get_running_loop().create_task(self._reply(event.stream_id))

    async def _reply(self, stream_id):
        data = b"r" * self.reply_size
        offset, chunk = 0, 64 * 1024
        while offset < len(data):
            part = data[offset:offset + chunk]
            fin = offset + len(part) >= len(data)
            try:
                self._quic.send_stream_data(stream_id, part, end_stream=fin)
            except BufferError:
                await asyncio.sleep(0.005)
                continue
            offset += len(part)


class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = 0
        self.done = asyncio.get_running_loop().create_future()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += len(event.data)
            if event.end_stream and not self.done.done():
                self.done.set_result(time.monotonic())
        elif isinstance(event, ConnectionTerminated) and not self.done.done():
            self.done.set_exception(
                ConnectionError(f"closed ({event.error_code:#x})"))


async def _setup(reply_size: int):
    server_port, relay_port = next_port(), next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                             idle_timeout=IDLE_TIMEOUT)
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    protocol = type("Server", (Server,), {"reply_size": reply_size})
    server = await serve("127.0.0.1", server_port, configuration=scfg,
                         create_protocol=protocol)
    _, relay = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: Relay(server_port), local_addr=("127.0.0.1", relay_port))
    ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE,
                             idle_timeout=IDLE_TIMEOUT)
    return server, relay, relay_port, ccfg


async def test_client_tail_loss_recovers():
    """A request lost in an outage is retransmitted once the link returns."""
    server, relay, relay_port, ccfg = await _setup(reply_size=2)
    try:
        async with connect("127.0.0.1", relay_port, configuration=ccfg,
                           create_protocol=Client) as client:
            await asyncio.sleep(0.3)
            relay.down = True
            sid = client._quic.get_next_available_stream_id()
            t0 = time.monotonic()
            client._quic.send_stream_data(sid, b"g" * 1300, end_stream=True)
            await asyncio.sleep(0.8)
            relay.down = False
            done = await asyncio.wait_for(client.done, IDLE_TIMEOUT + 2)
        assert done - t0 < 3.0, f"reply after {done - t0:.2f} s"
        assert relay.dropped["client"] >= 2, relay.dropped
    finally:
        relay.transport.close()
        server.close()


async def test_server_blackhole_recovers():
    """A sender mid-transfer keeps probing through an outage and finishes."""
    server, relay, relay_port, ccfg = await _setup(reply_size=8 * 1024 * 1024)
    try:
        async with connect("127.0.0.1", relay_port, configuration=ccfg,
                           create_protocol=Client) as client:
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"GET", end_stream=True)
            while client.received < 256 * 1024:
                await asyncio.sleep(0.005)
            relay.down = True
            await asyncio.sleep(1.5)
            relay.down = False
            await asyncio.wait_for(client.done, 6.0)
        assert client.received == 8 * 1024 * 1024
        assert relay.dropped["server"] >= 3, relay.dropped
    finally:
        relay.transport.close()
        server.close()
