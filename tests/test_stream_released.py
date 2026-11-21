"""stream_released(): a stream we opened stays unreleased until the peer
acknowledges its data and FIN (or its reset), raw QUIC and WebTransport.

An in-process UDP relay drops every datagram while down, so no ACK can
arrive and the stream must read unreleased; once the link returns the
sender retransmits and the stream is released.
"""
import asyncio
import os
import ssl
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from .test_loss_recovery import Relay

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
OUTAGE = 0.5
DEADLINE = 8.0
UNKNOWN_ID = 4 * 100_000 + 2

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


async def _until(pred, timeout=DEADLINE):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.01)
    return True


async def _relay(server_port):
    relay_port = next_port()
    _, relay = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: Relay(server_port), local_addr=("127.0.0.1", relay_port))
    return relay, relay_port


class _Sink(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fins = set()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            self.fins.add(event.stream_id)


async def _quic_setup():
    server_port = next_port()
    servers = []

    def make(*args, **kwargs):
        p = _Sink(*args, **kwargs)
        servers.append(p)
        return p

    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", server_port, configuration=scfg,
                         create_protocol=make)
    relay, relay_port = await _relay(server_port)
    ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE)
    return server, servers, relay, relay_port, ccfg


async def test_quic_released_after_fin_acked():
    server, servers, relay, relay_port, ccfg = await _quic_setup()
    try:
        async with connect("127.0.0.1", relay_port,
                           configuration=ccfg) as client:
            quic = client._quic
            assert quic.stream_released(UNKNOWN_ID)
            relay.down = True
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(sid, b"x" * 4000, end_stream=True)
            await asyncio.sleep(OUTAGE)
            assert not quic.stream_released(sid)
            assert not servers or sid not in servers[0].fins
            relay.down = False
            assert await _until(lambda: quic.stream_released(sid))
            assert sid in servers[0].fins
            assert relay.dropped["client"] >= 1, relay.dropped
    finally:
        relay.transport.close()
        server.close()


async def test_quic_released_after_reset_acked():
    server, servers, relay, relay_port, ccfg = await _quic_setup()
    try:
        async with connect("127.0.0.1", relay_port,
                           configuration=ccfg) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(sid, b"x" * 100)
            relay.down = True
            quic.reset_stream(sid, 7)
            await asyncio.sleep(OUTAGE)
            assert not quic.stream_released(sid)
            relay.down = False
            assert await _until(lambda: quic.stream_released(sid))
    finally:
        relay.transport.close()
        server.close()


async def test_quic_released_after_close():
    server, servers, relay, relay_port, ccfg = await _quic_setup()
    try:
        async with connect("127.0.0.1", relay_port,
                           configuration=ccfg) as client:
            quic = client._quic
            relay.down = True
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(sid, b"x" * 100, end_stream=True)
            await asyncio.sleep(0.1)
            assert not quic.stream_released(sid)
            client.close()
            assert await _until(lambda: quic.closed)
            assert quic.stream_released(sid)
            assert quic.stream_released(UNKNOWN_ID)
    finally:
        relay.transport.close()
        server.close()


async def _wt_setup(sessions):
    server_port = next_port()

    async def handler(session):
        sessions.append(session)
        await session._session_closed.wait()

    server = await serve_webtransport(
        "127.0.0.1", server_port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    relay, relay_port = await _relay(server_port)
    return server, relay, relay_port


async def test_wt_released_after_fin_acked():
    sessions = []
    server, relay, relay_port = await _wt_setup(sessions)
    try:
        async with connect_webtransport("127.0.0.1", relay_port, "/wt",
                                        sni=SNI, ca_file=CA_FILE) as wt:
            assert wt.stream_released(UNKNOWN_ID)
            sid = await wt.create_stream(bidir=False)
            assert not wt.stream_released(sid)
            relay.down = True
            wt.send_stream_data(sid, b"x" * 4000, end_stream=True)
            await asyncio.sleep(OUTAGE)
            assert not wt.stream_released(sid)
            relay.down = False
            assert await _until(lambda: wt.stream_released(sid))
            assert not wt.session_closed
            assert relay.dropped["client"] >= 1, relay.dropped
    finally:
        relay.transport.close()
        server.close()


async def test_wt_released_at_reset_and_close():
    sessions = []
    server, relay, relay_port = await _wt_setup(sessions)
    try:
        async with connect_webtransport("127.0.0.1", relay_port, "/wt",
                                        sni=SNI, ca_file=CA_FILE) as wt:
            reset_sid = await wt.create_stream(bidir=False)
            open_sid = await wt.create_stream(bidir=False)
            relay.down = True
            wt.send_stream_data(reset_sid, b"x" * 100)
            wt.send_stream_data(open_sid, b"x" * 100, end_stream=True)
            await asyncio.sleep(0.1)
            assert not wt.stream_released(reset_sid)
            wt.reset_stream(reset_sid, 7)
            assert wt.stream_released(reset_sid)
            assert not wt.stream_released(open_sid)
            relay.down = False
        assert wt.session_closed
        assert wt.stream_released(open_sid)
        assert wt.stream_released(UNKNOWN_ID)
    finally:
        relay.transport.close()
        server.close()
