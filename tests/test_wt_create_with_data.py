"""create_stream(initial=...): a WT stream's first bytes, FIN and priority
travel with the create and leave with the stream header."""
import asyncio
import glob
import json
import os

import pytest

from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import WebTransportNewStream, WebTransportStreamDataReceived

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

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


async def _server(got, qlog_dir=None):
    """Server that records each peer stream's bytes in got[sid] at FIN."""
    async def read(session, sid):
        buf = bytearray()
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                buf += ev.data
                if ev.end_stream:
                    got[sid] = bytes(buf)
                    return

    async def handler(session):
        async def recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    asyncio.create_task(read(session, ev.stream_id))
        asyncio.create_task(recv())

    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler, cert_file=CERT_FILE,
        key_file=KEY_FILE,
        configuration=QuicConfiguration(is_client=False, qlog_dir=qlog_dir))
    return port, server


async def _until(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.parametrize("bidir", [False, True])
async def test_initial_and_fin_arrive(bidir):
    got = {}
    port, server = await _server(got)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=bidir, initial=b"hello",
                                         end_stream=True)
            assert await _until(lambda: sid in got), got
            assert got[sid] == b"hello"
    finally:
        server.close()


async def test_initial_then_more():
    got = {}
    port, server = await _server(got)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False, initial=b"abc")
            wt.send_stream_data(sid, b"def")
            assert await _until(lambda: wt.tx_sent_bytes(sid) == 6)
            wt.send_stream_data(sid, b"", end_stream=True)
            assert await _until(lambda: sid in got), got
            assert got[sid] == b"abcdef"
    finally:
        server.close()


async def test_initial_leaves_with_header(tmp_path):
    got = {}
    port, server = await _server(got, qlog_dir=str(tmp_path))
    body = b"x" * 500
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False, initial=body,
                                         end_stream=True)
            assert await _until(lambda: sid in got), got
    finally:
        server.close()
    assert got[sid] == body
    paths = glob.glob(os.path.join(str(tmp_path), "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    packets = []
    for ev in events:
        if ev[2] != "packet_received":
            continue
        frames = [fr for fr in ev[3].get("frames") or []
                  if fr["frame_type"] == "stream" and fr["id"] == sid]
        if frames:
            packets.append(frames)
    assert packets, "no stream frames for the created stream"
    first = packets[0]
    assert min(fr["offset"] for fr in first) == 0
    end = max(fr["offset"] + fr["length"] for fr in first)
    assert end > len(body), first
    assert any(fr.get("fin") for fr in first), first


async def test_initial_larger_than_ring_rejected():
    port, server = await _server({})
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            with pytest.raises(ValueError):
                await wt.create_stream(
                    bidir=False, initial=b"x" * (wt.stream_ring_cap + 1))
            with pytest.raises(ValueError):
                await wt.create_stream(bidir=False, priority=256)
    finally:
        server.close()


async def test_priority_applied_with_create():
    got = {}
    port, server = await _server(got)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            before = wt._transport.counters["set_priority_applied"]
            sid = await wt.create_stream(bidir=False, initial=b"p",
                                         end_stream=True, priority=3)
            assert await _until(lambda: sid in got), got
            assert wt._transport.counters["set_priority_applied"] == before + 1
    finally:
        server.close()
