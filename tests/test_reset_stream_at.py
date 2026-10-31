"""RESET_STREAM_AT (draft-ietf-quic-reliable-stream-reset) on raw QUIC and
WebTransport: a reset keeps the first reliable_size bytes of its stream,
which MoQT uses to deliver a reset stream's header.
"""
import asyncio
import glob
import json
import os
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    StreamDataReceived, StreamReset,
    WebTransportNewStream, WebTransportStreamDataReceived, WebTransportStreamReset,
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

HEADERS = (b"\x05hdr", b"\x04a-longer-stream-header")
PAYLOAD = b"p" * (64 * 1024)


async def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _wire_resets(qlog_dir):
    """{stream_id: frame type} of the resets the qlog's endpoint received.
    picoquic's qlog writes final_size as reliable_size, so sizes are not
    read from it."""
    paths = glob.glob(os.path.join(str(qlog_dir), "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    seen = {}
    for ev in events:
        if ev[2] != "packet_received":
            continue
        for fr in ev[3].get("frames") or []:
            if fr["frame_type"] in ("reset_stream", "reset_stream_at"):
                seen[fr.get("id", fr.get("stream_id"))] = fr["frame_type"]
    return seen


def _server_config(qlog_dir, **kw):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"],
                            qlog_dir=str(qlog_dir), **kw)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config():
    return QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"],
                             server_name=SNI, cafile=CA_FILE)


async def _raw_server(qlog_dir, **kw):
    """Server recording, per stream, the bytes and reset code received."""
    data, resets, conns = {}, {}, []

    class Server(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            conns.append(self._quic)

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                data[event.stream_id] = (data.get(event.stream_id, b"")
                                         + bytes(event.data))
            elif isinstance(event, StreamReset):
                resets[event.stream_id] = event.error_code

    port = next_port()
    server = await serve("127.0.0.1", port,
                         configuration=_server_config(qlog_dir, **kw),
                         create_protocol=Server)
    return port, server, data, resets, conns


@pytest.mark.asyncio
async def test_raw_reset_keeps_its_header(tmp_path):
    port, server, data, resets, conns = await _raw_server(tmp_path)
    sent = {}
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            for header in HEADERS:
                sid = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(sid, header + PAYLOAD)
                quic.reset_stream(sid, 0x10, reliable_size=len(header))
                sent[sid] = header
            assert await _until(lambda: len(resets) == len(sent)), resets
            assert quic.reset_stream_at_negotiated()
            assert conns[0].reset_stream_at_negotiated()
    finally:
        server.close()
    for sid, header in sent.items():
        assert data.get(sid, b"")[:len(header)] == header, (sid, data.get(sid))
        assert resets[sid] == 0x10
    assert _wire_resets(tmp_path) == {sid: "reset_stream_at" for sid in sent}


@pytest.mark.asyncio
async def test_raw_reset_waits_until_its_header_is_sent(tmp_path):
    """A stream past the peer's MAX_STREAMS sends nothing yet: its reset is
    held until the header goes out, and nothing past the header is sent."""
    port, server, data, resets, _ = await _raw_server(tmp_path,
                                                      max_streams_uni=1)
    header = HEADERS[1]
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            first = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(first, b"a")
            assert await _until(lambda: first in data)
            held = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(held, header + PAYLOAD)
            quic.reset_stream(held, 0x11, reliable_size=len(header))
            await asyncio.sleep(0.3)
            assert held not in data and held not in resets
            quic.send_stream_data(first, b"", end_stream=True)
            assert await _until(lambda: held in resets), resets
    finally:
        server.close()
    assert data[held] == header
    assert resets[held] == 0x11
    assert _wire_resets(tmp_path)[held] == "reset_stream_at"


@pytest.mark.asyncio
async def test_raw_reliable_size_capped_at_bytes_written(tmp_path):
    port, server, data, resets, _ = await _raw_server(tmp_path)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            for bad in (-1, 1 << 32):
                with pytest.raises(ValueError):
                    quic.reset_stream(sid, 0x12, reliable_size=bad)
            quic.send_stream_data(sid, b"short")
            quic.reset_stream(sid, 0x12, reliable_size=1000)
            assert await _until(lambda: sid in resets), resets
    finally:
        server.close()
    assert data[sid] == b"short"
    assert resets[sid] == 0x12
    assert _wire_resets(tmp_path) == {sid: "reset_stream_at"}


@pytest.mark.asyncio
async def test_raw_reset_falls_back_when_not_negotiated(tmp_path):
    port, server, data, resets, conns = await _raw_server(
        tmp_path, reset_stream_at=False)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(sid, HEADERS[0] + PAYLOAD)
            quic.reset_stream(sid, 0x13, reliable_size=len(HEADERS[0]))
            assert await _until(lambda: sid in resets), resets
            assert not quic.reset_stream_at_negotiated()
            assert not conns[0].reset_stream_at_negotiated()
    finally:
        server.close()
    assert resets[sid] == 0x13
    assert _wire_resets(tmp_path) == {sid: "reset_stream"}


@pytest.mark.asyncio
async def test_raw_close_with_a_held_reset(tmp_path):
    port, server, data, resets, _ = await _raw_server(tmp_path,
                                                      max_streams_uni=1)
    try:
        for _ in range(3):
            async with connect("127.0.0.1", port,
                               configuration=_client_config()) as client:
                quic = client._quic
                first = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(first, b"a")
                held = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(held, HEADERS[0] + PAYLOAD)
                quic.reset_stream(held, 0x14, reliable_size=len(HEADERS[0]))
                await asyncio.sleep(0.05)
                quic.close()
                await asyncio.sleep(0.05)
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(sid, HEADERS[0])
            quic.reset_stream(sid, 0x15, reliable_size=len(HEADERS[0]))
            assert await _until(lambda: 0x15 in resets.values()), resets
    finally:
        server.close()


async def _wt_server(tmp_path, on_session, **kw):
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=on_session, cert_file=CERT_FILE,
        key_file=KEY_FILE,
        configuration=QuicConfiguration(is_client=False,
                                        qlog_dir=str(tmp_path), **kw))
    return port, server


def _wt_collector(data, resets):
    async def on_session(session):
        async def _recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    asyncio.create_task(
                        _collect(session, ev.stream_id, data, resets))
        asyncio.create_task(_recv())
    return on_session


async def _collect(session, sid, data, resets):
    async for ev in session.receive_stream_data(sid):
        if isinstance(ev, WebTransportStreamDataReceived):
            data[sid] = data.get(sid, b"") + bytes(ev.data)
        elif isinstance(ev, WebTransportStreamReset):
            resets[sid] = ev.error_code
            return


@pytest.mark.asyncio
async def test_wt_reset_keeps_its_header(tmp_path):
    data, resets, sessions = {}, {}, []

    async def on_session(session):
        sessions.append(session)

        async def _recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    asyncio.create_task(
                        _collect(session, ev.stream_id, data, resets))
        asyncio.create_task(_recv())

    port, server = await _wt_server(tmp_path, on_session)
    sent = {}
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            for bidir, header in zip((False, True), HEADERS):
                sid = await wt.create_stream(bidir=bidir)
                wt.send_stream_data(sid, header + PAYLOAD)
                wt.reset_stream(sid, 7, reliable_size=len(header))
                sent[sid] = header
            assert await _until(lambda: len(resets) == len(sent)), resets
            assert wt.reset_stream_at_negotiated()
            assert sessions[0].reset_stream_at_negotiated()
    finally:
        server.close()
    for sid, header in sent.items():
        assert data.get(sid, b"")[:len(header)] == header, (sid, data.get(sid))
        assert resets[sid] == 7
    assert _wire_resets(tmp_path) == {sid: "reset_stream_at" for sid in sent}


@pytest.mark.asyncio
async def test_wt_reset_waits_until_its_header_is_sent(tmp_path):
    """h3zero holds three uni streams per side, so a limit of seven leaves
    four for WT data and the fifth waits for credit. picoquic extends the
    limit once more than half of it has closed: all four must finish."""
    data, resets = {}, {}
    port, server = await _wt_server(tmp_path, _wt_collector(data, resets),
                                    max_streams_uni=7)
    header = HEADERS[1]
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            fill = []
            for _ in range(4):
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"a")
                fill.append(sid)
            assert await _until(lambda: all(sid in data for sid in fill))
            held = await wt.create_stream(bidir=False)
            wt.send_stream_data(held, header + PAYLOAD)
            wt.reset_stream(held, 10, reliable_size=len(header))
            await asyncio.sleep(0.3)
            assert held not in data and held not in resets
            for sid in fill:
                wt.send_stream_data(sid, b"", end_stream=True)
            assert await _until(lambda: held in resets), resets
    finally:
        server.close()
    assert data[held] == header
    assert resets[held] == 10
    assert _wire_resets(tmp_path)[held] == "reset_stream_at"


@pytest.mark.asyncio
async def test_wt_server_resets_keep_their_header(tmp_path):
    """Server side: a stream it opened (WT header ahead of the data) and its
    half of a client-opened bidi (no WT header)."""
    data, resets = {}, {}
    uni_header, bidi_header = HEADERS

    async def reply(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                session.send_stream_data(sid, bidi_header + PAYLOAD)
                session.reset_stream(sid, 9, reliable_size=len(bidi_header))
                return

    async def on_session(session):
        async def _run():
            uni = await session.create_stream(bidir=False)
            session.send_stream_data(uni, uni_header + PAYLOAD)
            session.reset_stream(uni, 8, reliable_size=len(uni_header))
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    asyncio.create_task(reply(session, ev.stream_id))
        asyncio.create_task(_run())

    port, server = await _wt_server(tmp_path, on_session)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            async def _recv():
                async for ev in wt.events():
                    if isinstance(ev, WebTransportNewStream):
                        asyncio.create_task(
                            _collect(wt, ev.stream_id, data, resets))
            recv = asyncio.create_task(_recv())
            bidi = await wt.create_stream(bidir=True)
            collect = asyncio.create_task(_collect(wt, bidi, data, resets))
            wt.send_stream_data(bidi, b"go")
            assert await _until(lambda: len(resets) == 2), resets
            recv.cancel()
            collect.cancel()
    finally:
        server.close()
    codes = {code: sid for sid, code in resets.items()}
    assert data[codes[8]][:len(uni_header)] == uni_header
    assert data[codes[9]][:len(bidi_header)] == bidi_header
