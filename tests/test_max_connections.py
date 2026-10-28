"""QuicConfiguration.max_connections caps the connections a server holds,
and TransportContext.set_max_connections lowers the cap while it runs; past
it picoquic refuses new connections."""
import asyncio
import contextlib
import os

import pytest

from aiopquic.asyncio import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportError, WebTransportNewStream, WebTransportStreamDataReceived,
    connect_webtransport, serve_webtransport,
)
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
WT_PATH = "/cap"
CAP = 2


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                            max_connections=CAP)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE, idle_timeout=3)


async def _refused(open_one) -> bool:
    try:
        async with asyncio.timeout(8):
            async with open_one():
                return False
    except Exception:
        return True


async def test_raw_quic_server_refuses_past_max_connections():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    try:
        async with contextlib.AsyncExitStack() as held:
            for _ in range(CAP):
                await held.enter_async_context(
                    connect("127.0.0.1", port, configuration=_client_cfg()))
            assert await _refused(lambda: connect(
                "127.0.0.1", port, configuration=_client_cfg()))
        # Room again once the held connections close.
        async with asyncio.timeout(10):
            while await _refused(lambda: connect(
                    "127.0.0.1", port, configuration=_client_cfg())):
                await asyncio.sleep(0.2)
    finally:
        server.close()


async def test_webtransport_server_refuses_past_max_connections():
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await serve_webtransport(
        "127.0.0.1", port, WT_PATH, handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE,
        configuration=QuicConfiguration(is_client=False,
                                        max_connections=CAP))

    def _open():
        return connect_webtransport("127.0.0.1", port, WT_PATH, sni=SNI,
                                    ca_file=CA_FILE)
    try:
        async with contextlib.AsyncExitStack() as held:
            for _ in range(CAP):
                await held.enter_async_context(_open())
            assert await _refused(_open)
    finally:
        await server.aclose()


class _Sink(QuicConnectionProtocol):
    got = bytearray()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            _Sink.got.extend(event.data)


async def _set_limit(transport, n):
    # Applied once the network thread has drained the TX event ring.
    assert transport.set_max_connections(n) == 0
    async with asyncio.timeout(5):
        while transport.counters["tx_event_ring_count_now"]:
            await asyncio.sleep(0.001)


async def _assert_refused(open_one, exc, match=None):
    with pytest.raises(exc, match=match):
        async with asyncio.timeout(5):
            async with open_one():
                pass


async def test_raw_quic_set_max_connections_refuses_new_keeps_existing():
    port = next_port()
    _Sink.got = bytearray()
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=cfg,
                         create_protocol=lambda conn, stream_handler=None: _Sink(conn))
    transport = server._engine._transport

    def _open():
        return connect("127.0.0.1", port, configuration=_client_cfg())
    try:
        with pytest.raises(ValueError):
            transport.set_max_connections(cfg.max_connections + 1)
        async with _open() as held:
            await _set_limit(transport, 0)
            await _assert_refused(_open, ConnectionError, "error_code=2")
            q = held._quic
            q.send_stream_data(q.get_next_available_stream_id(), b"kept",
                               end_stream=True)
            async with asyncio.timeout(5):
                while bytes(_Sink.got) != b"kept":
                    await asyncio.sleep(0.01)
        await _set_limit(transport, cfg.max_connections)
        async with _open():
            pass
    finally:
        server.close()


async def test_webtransport_set_max_connections_refuses_new_keeps_existing():
    port = next_port()
    got = asyncio.get_running_loop().create_future()

    async def handler(session):
        async for ev in session.events():
            if isinstance(ev, WebTransportNewStream):
                data = bytearray()
                reader = session.receive_stream_data(ev.stream_id)
                try:
                    async for chunk in reader:
                        if isinstance(chunk, WebTransportStreamDataReceived):
                            data.extend(chunk.data)
                            if chunk.end_stream:
                                break
                finally:
                    await reader.aclose()
                got.set_result(bytes(data))
                return

    server = await serve_webtransport(
        "127.0.0.1", port, WT_PATH, handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    transport = server._transport

    def _open():
        return connect_webtransport("127.0.0.1", port, WT_PATH, sni=SNI,
                                    ca_file=CA_FILE)
    try:
        async with _open() as held:
            await _set_limit(transport, 0)
            await _assert_refused(_open, WebTransportError)
            sid = await held.create_stream(bidir=False)
            held.send_stream_data(sid, b"kept", end_stream=True)
            assert await asyncio.wait_for(got, timeout=5) == b"kept"
        await _set_limit(transport, QuicConfiguration().max_connections)
        async with _open():
            pass
    finally:
        await server.aclose()


async def test_set_max_connections_ceiling_is_picoquics():
    # picoquic holds at least one connection, so a 0 at start allows 1.
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN], max_connections=0)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", next_port(), configuration=cfg)
    transport = server._engine._transport
    try:
        await _set_limit(transport, 0)
        await _set_limit(transport, 1)
        with pytest.raises(ValueError):
            transport.set_max_connections(2)
    finally:
        server.close()


def test_default_is_256():
    assert QuicConfiguration().max_connections == 256


@pytest.mark.parametrize("cap", [1, 4096])
def test_configurable(cap):
    assert QuicConfiguration(max_connections=cap).max_connections == cap
