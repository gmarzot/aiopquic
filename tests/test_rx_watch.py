"""RX reader ownership, the lost-wake safety net, and the wake trace."""
import asyncio
import logging
import os
import ssl
import sys

import pytest

from aiopquic.asyncio import _rxwatch
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.dispatch import serve_dispatch
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


class Echo(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            self._quic.send_stream_data(event.stream_id, b"pong", end_stream=True)


class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies = asyncio.Queue()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            self.replies.put_nowait(event.stream_id)

    async def ping(self, timeout=3.0):
        sid = self._quic.get_next_available_stream_id()
        self._quic.send_stream_data(sid, b"ping", end_stream=True)
        assert await asyncio.wait_for(self.replies.get(), timeout) == sid


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE)


async def test_trace_orders_notify_drain_clear():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Echo)
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping()
        transport = server._engine._transport
        kinds = [kind for _, _, kind, _, _ in transport.wake_trace()]
        assert "notify" in kinds and "drain" in kinds and "clear" in kinds
        first_notify = kinds.index("notify")
        assert "drain" in kinds[first_notify:] and "clear" in kinds[first_notify:]
        counters = transport.counters
        assert counters["rx_notify_flips"] >= 1
        assert counters["rx_clears"] >= 1
        assert counters["rx_notify_write_errs"] == 0
    finally:
        server.close()


async def test_removed_reader_is_rescued(caplog):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Echo)
    transport = server._engine._transport
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping()
            asyncio.get_running_loop().remove_reader(transport.eventfd)
            with caplog.at_level(logging.WARNING, logger="aiopquic"):
                await client.ping(timeout=3.0)
                await client.ping(timeout=1.0)
        assert _rxwatch.rescues(transport) >= 1
        report = "\n".join(r.getMessage() for r in caplog.records)
        assert "rx wake lost" in report and "'reader': None" in report
    finally:
        server.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="reads the Linux eventfd counter")
async def test_consumed_wake_is_rescued(caplog):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Echo)
    engine = server._engine
    transport = engine._transport
    loop = asyncio.get_running_loop()
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping()
            # Park the reader, let a request queue (pending=1, wake written),
            # consume that wake, then restore the reader: a lost wake.
            loop.remove_reader(transport.eventfd)
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"ping", end_stream=True)
            for _ in range(200):
                if transport.rx_event_ring_count:
                    break
                await asyncio.sleep(0.005)
            assert transport.counters["rx_notify_pending_now"] == 1
            os.read(transport.eventfd, 8)
            loop.add_reader(transport.eventfd, engine.drain_and_route)
            with caplog.at_level(logging.WARNING, logger="aiopquic"):
                assert await asyncio.wait_for(client.replies.get(), 3.0) == sid
        assert _rxwatch.rescues(transport) >= 1
        report = "\n".join(r.getMessage() for r in caplog.records)
        assert "'fd_readable': False" in report
        assert "drain_and_route" in report
    finally:
        server.close()


async def test_dispatcher_detach_leaves_the_router_reader():
    port = next_port()
    server = await serve_dispatch("127.0.0.1", port, configuration=_server_cfg(),
                                  create_protocol=Echo, wt_path="/wt",
                                  wt_handler=lambda session: None)
    transport = server._transport
    try:
        server._wt_server._dispatcher.detach()
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping(timeout=1.0)
        history = [op for _, op, _ in _rxwatch._watches[transport].history]
        assert any(op.startswith("remove refused") for op in history)
        assert _rxwatch.rescues(transport) == 0
    finally:
        server.close()


async def test_dispatch_server_reader_is_rescued():
    port = next_port()
    server = await serve_dispatch("127.0.0.1", port, configuration=_server_cfg(),
                                  create_protocol=Echo, wt_path="/wt",
                                  wt_handler=lambda session: None)
    transport = server._transport
    try:
        asyncio.get_running_loop().remove_reader(transport.eventfd)
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping(timeout=3.0)
        assert _rxwatch.rescues(transport) >= 1
    finally:
        server.close()


async def test_webtransport_server_reader_is_rescued():
    port = next_port()
    accepted = asyncio.get_running_loop().create_future()

    def handler(session):
        if not accepted.done():
            accepted.set_result(session)

    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE)
    transport = server._transport
    try:
        asyncio.get_running_loop().remove_reader(transport.eventfd)
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE):
            await asyncio.wait_for(accepted, 3.0)
        assert _rxwatch.rescues(transport) >= 1
    finally:
        server.close()


async def test_client_reader_is_rescued():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Echo)
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=Client) as client:
            await client.ping()
            transport = client._quic._transport
            asyncio.get_running_loop().remove_reader(transport.eventfd)
            await client.ping(timeout=3.0)
            assert _rxwatch.rescues(transport) >= 1
    finally:
        server.close()
