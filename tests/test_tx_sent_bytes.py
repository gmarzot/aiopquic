"""tx_sent_bytes(stream_id): bytes picoquic has taken from a stream's TX
ring into packets. A reading peer takes all of them; a peer that grants no
flow-control credit beyond its first window holds it below what was
written."""
import asyncio
import os
import time

import pytest

from aiopquic._binding._transport import TransportContext
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

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.path.exists(CERT_FILE),
                       reason="picoquic certs not found"),
]

N = 512 * 1024
WINDOW = 64 * 1024   # the non-reading peer's stream window


async def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        await asyncio.sleep(0.01)
    return cond()


async def _settled(read, quiet=0.2, timeout=5.0):
    """read() once it has stopped changing for `quiet` seconds."""
    deadline = time.monotonic() + timeout
    last, since = read(), time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        now = read()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet:
            break
    return last


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE)


def _silent_server(port, **kw):
    """A server whose events nobody drains: past its first window, no
    stream gets flow-control credit."""
    server = TransportContext()
    server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                 is_client=False, rx_data_ring_cap=WINDOW, **kw)
    return server


async def test_raw_reading_peer_takes_every_byte():
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg()) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id()
            assert quic.tx_sent_bytes(sid) == 0
            quic.send_stream_data(sid, b"x" * N)
            assert await _until(lambda: quic.tx_sent_bytes(sid) == N), (
                f"{quic.tx_sent_bytes(sid)} of {N} bytes sent")
    finally:
        server.close()


async def test_raw_peer_that_does_not_read_holds_it_below_n():
    port = next_port()
    server = _silent_server(port, alpn=ALPN)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg()) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id()
            quic.send_stream_data(sid, b"x" * N)
            sent = await _settled(lambda: quic.tx_sent_bytes(sid))
            assert 0 < sent < N, f"{sent} of {N} bytes sent"
    finally:
        server.stop()


async def test_wt_reading_peer_takes_every_byte():
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=lambda session: None,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            assert wt.tx_sent_bytes(sid) == 0
            wt.send_stream_data(sid, b"x" * N)
            assert await _until(lambda: wt.tx_sent_bytes(sid) == N), (
                f"{wt.tx_sent_bytes(sid)} of {N} bytes sent")
    finally:
        server.close()


async def test_wt_peer_that_does_not_read_holds_it_below_n():
    port = next_port()
    server = _silent_server(port, alpn="h3", wt_path="/wt",
                            max_datagram_frame_size=64 * 1024)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            wt.send_stream_data(sid, b"x" * N)
            sent = await _settled(lambda: wt.tx_sent_bytes(sid))
            assert 0 < sent < N, f"{sent} of {N} bytes sent"
    finally:
        server.stop()
