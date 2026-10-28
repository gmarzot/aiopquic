"""TX wake gate: a write wakes the network thread only when it may be
asleep. The gate is checked against a worker held running or idle; live
bursts check that every write goes through it and is delivered; a write
after a quiet spell must reach the peer promptly, and a skipped wake is
counted as tx_wake_lost."""
import asyncio
import os
import time

import pytest

from aiopquic._binding._transport import _tx_wake_gate
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportNewStream, WebTransportStreamDataReceived,
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")

WRITES = 1000   # under the TX event ring's 2048 entries
CHUNK = b"x" * 64
QUIET = 0.3     # long enough for the network thread to sleep idle
PROMPT = 0.5    # a lost wake waits for the next packet or timer: seconds
ROUNDS = 10


async def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        await asyncio.sleep(0.01)
    return cond()


def test_gate_skips_wakes_while_worker_runs():
    assert _tx_wake_gate(0, WRITES) == (0, WRITES)


def test_gate_wakes_idle_worker_once():
    assert _tx_wake_gate(1, WRITES) == (1, WRITES - 1)


def _wakes(transport):
    c = transport.counters
    return c["wake_calls"], c["wake_skipped_coalesced"]


async def _burst(transport, send, sent):
    calls0, skipped0 = _wakes(transport)
    for _ in range(WRITES):
        send(CHUNK)
    total = WRITES * len(CHUNK)
    assert await _until(lambda: sent() == total), f"{sent()} of {total} bytes sent"
    calls1, skipped1 = _wakes(transport)
    gated = (calls1 + skipped1) - (calls0 + skipped0)
    assert gated >= WRITES, f"{gated} of {WRITES} writes went through the wake gate"


async def test_raw_writes_go_through_wake_gate():
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg)
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                 server_name=SNI, cafile=CA_FILE)
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id()
            await _burst(quic._transport,
                         lambda d: quic.send_stream_data(sid, d),
                         lambda: quic.tx_sent_bytes(sid))
    finally:
        server.close()


async def test_wt_writes_go_through_wake_gate():
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=lambda session: None,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            await _burst(wt._transport,
                         lambda d: wt.send_stream_data(sid, d),
                         lambda: wt.tx_sent_bytes(sid))
    finally:
        server.close()


class _Sink(QuicConnectionProtocol):
    got = {}

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            _Sink.got.setdefault(event.stream_id, bytearray()).extend(event.data)


async def _raw_server(port):
    _Sink.got = {}
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return await serve("127.0.0.1", port, configuration=cfg,
                       create_protocol=lambda conn, stream_handler=None: _Sink(conn))


def _raw_client(port):
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                            server_name=SNI, cafile=CA_FILE)
    return connect("127.0.0.1", port, configuration=cfg)


def _lost(*transports):
    return [t.counters["tx_wake_lost"] for t in transports]


async def test_raw_send_after_quiet_is_prompt():
    port = next_port()
    server = await _raw_server(port)
    try:
        async with _raw_client(port) as client:
            q = client._quic
            for i in range(ROUNDS):
                await asyncio.sleep(QUIET)
                sid = q.get_next_available_stream_id()
                want = b"q%d" % i
                q.send_stream_data(sid, want, end_stream=True)
                assert await _until(
                    lambda: bytes(_Sink.got.get(sid, b"")) == want, PROMPT), \
                    f"round {i}: not delivered within {PROMPT} s"
            assert _lost(q._transport, server._engine._transport) == [0, 0]
    finally:
        server.close()


async def test_wt_send_after_quiet_is_prompt():
    port = next_port()
    got = asyncio.Queue()

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
                got.put_nowait(bytes(data))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            for i in range(ROUNDS):
                await asyncio.sleep(QUIET)
                want = b"q%d" % i
                sid = await asyncio.wait_for(wt.create_stream(bidir=False), PROMPT)
                wt.send_stream_data(sid, want, end_stream=True)
                assert await asyncio.wait_for(got.get(), PROMPT) == want
            assert _lost(wt._transport, server._transport) == [0, 0]
    finally:
        server.close()


async def test_lost_wake_is_counted():
    port = next_port()
    server = await _raw_server(port)
    st = server._engine._transport
    try:
        async with _raw_client(port) as client:
            q = client._quic
            # A worker timer can end the sleep before the wake counts as lost.
            for _ in range(3):
                await asyncio.sleep(QUIET)
                st._tx_push_unwoken()
                await asyncio.sleep(0.1)
                sid = q.get_next_available_stream_id()
                q.send_stream_data(sid, b"rx", end_stream=True)
                assert await _until(lambda: bytes(_Sink.got.get(sid, b"")) == b"rx")
                if st.counters["tx_wake_lost"]:
                    break
            assert st.counters["tx_wake_lost"] == 1
            assert await _until(lambda: st.counters["tx_event_ring_count_now"] == 0)
    finally:
        server.close()
