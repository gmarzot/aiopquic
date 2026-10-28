"""TX wake gate: a write wakes the network thread only when it may be
asleep. The gate is checked against a worker held running or idle; live
bursts check that every write goes through it and is delivered."""
import asyncio
import os
import time

import pytest

from aiopquic._binding._transport import _tx_wake_gate
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration

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
