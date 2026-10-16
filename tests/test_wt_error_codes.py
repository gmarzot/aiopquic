"""WebTransport stream error codes (draft-ietf-webtrans-http3 §4.4).

Application codes are 32-bit; on the wire they are remapped into the
WT_APPLICATION_ERROR range, skipping the reserved 0x1f * N + 0x21
codepoints, and mapped back on receipt.
"""
import asyncio
import glob
import json
import os

import pytest

from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    WebTransportNewStream, WebTransportStopSending,
    WebTransportStreamDataReceived, WebTransportStreamReset,
)

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

FIRST = 0x52e4a40fa8db
LAST = 0x52e5ac983162
CODES = [0, 0x1d, 0x1e, 0x1f, 0x11223344, 0xffffffff]


def to_h3(n):
    return FIRST + n + n // 0x1e


def test_mapping_reference():
    assert to_h3(0xffffffff) == LAST
    for n in range(0, 2000):
        assert (to_h3(n) - 0x21) % 0x1f != 0


def _received_codes(qlog_dir, frame_types):
    paths = glob.glob(os.path.join(qlog_dir, "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    codes = {}
    for ev in events:
        if ev[2] != "packet_received":
            continue
        for fr in ev[3].get("frames") or []:
            if fr["frame_type"] in frame_types:
                codes[fr.get("id", fr.get("stream_id"))] = fr["error_code"]
    return codes


async def _session(tmp_path, on_stream):
    """Server with a qlog; on_stream(session, sid) runs per new stream."""
    async def handler(session):
        async def _recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    asyncio.create_task(on_stream(session, ev.stream_id))
                elif isinstance(ev, WebTransportStopSending):
                    stops[ev.stream_id] = ev.error_code
        asyncio.create_task(_recv())

    stops = {}
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler, cert_file=CERT_FILE,
        key_file=KEY_FILE,
        configuration=QuicConfiguration(is_client=False, qlog_dir=str(tmp_path)))
    return port, server, stops


async def _until(pred, timeout=10.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_reset_codes_mapped_on_the_wire(tmp_path):
    seen, resets = set(), {}

    async def on_stream(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                seen.add(sid)
            elif isinstance(ev, WebTransportStreamReset):
                resets[sid] = ev.error_code
                return

    port, server, _ = await _session(tmp_path, on_stream)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sent = {}
            for code in CODES:
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"x")
                assert await _until(lambda: sid in seen)
                wt.reset_stream(sid, code)
                sent[sid] = code
            assert await _until(lambda: len(resets) == len(CODES)), resets
    finally:
        server.close()
    assert resets == sent
    wire = _received_codes(str(tmp_path), ("reset_stream", "reset_stream_at"))
    assert wire == {sid: to_h3(code) for sid, code in sent.items()}


async def test_stop_sending_codes_mapped_on_the_wire(tmp_path):
    seen = set()

    async def on_stream(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                seen.add(sid)
                return

    port, server, stops = await _session(tmp_path, on_stream)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            sent = {}
            for code in CODES:
                sid = await wt.create_stream(bidir=True)
                wt.send_stream_data(sid, b"x")
                assert await _until(lambda: sid in seen)
                wt.stop_stream(sid, code)
                sent[sid] = code
            assert await _until(lambda: len(stops) == len(CODES)), stops
    finally:
        server.close()
    assert stops == sent
    wire = _received_codes(str(tmp_path), ("stop_sending",))
    assert wire == {sid: to_h3(code) for sid, code in sent.items()}
