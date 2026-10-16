"""Receiver per-stream flow control, read from the receiver's qlog.

  - One-shot streams that fit the handshake window cost no
    MAX_STREAM_DATA and no MAX_DATA.
  - No MAX_STREAM_DATA once a stream's FIN arrived (RFC 9000 §3.3).
  - A raw bidi stream we open gets application flow control on its
    first byte received: its window stays within the RX ring.
"""
import asyncio
import glob
import json
import os

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    StreamDataReceived, WebTransportNewStream,
    WebTransportStreamDataReceived,
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


def _sent_fc_frames(qlog_dir):
    """(MAX_DATA values, MAX_STREAM_DATA values) the qlog's endpoint sent."""
    paths = glob.glob(os.path.join(qlog_dir, "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    max_data, max_stream_data = [], []
    for ev in events:
        if ev[2] != "packet_sent":
            continue
        for fr in ev[3].get("frames") or []:
            if fr["frame_type"] == "max_data":
                max_data.append(fr["maximum"])
            elif fr["frame_type"] == "max_stream_data":
                max_stream_data.append(fr["maximum"])
    return max_data, max_stream_data


def _client_config(**kw):
    return QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"],
                             server_name=SNI, cafile=CA_FILE, **kw)


async def _run_raw(qlog_dir, n_streams, size, max_stream_data=None):
    port = next_port()
    got = {"fins": 0, "bytes": 0}
    done = asyncio.Event()

    class Sink(QuicConnectionProtocol):
        def quic_event_received(self, ev):
            if isinstance(ev, StreamDataReceived):
                got["bytes"] += len(ev.data)
                if ev.end_stream:
                    got["fins"] += 1
                    if got["fins"] == n_streams:
                        done.set()

    scfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"],
                             qlog_dir=qlog_dir)
    if max_stream_data is not None:
        scfg.max_stream_data = max_stream_data
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=Sink)
    try:
        async with connect("127.0.0.1", port, configuration=_client_config()) as cli:
            q = cli._quic
            for _ in range(n_streams):
                sid = q.get_next_available_stream_id(is_unidirectional=True)
                q.send_stream_data(sid, b"\xa5" * size, end_stream=True)
            await asyncio.wait_for(done.wait(), 30)
            # The qlog is complete once the connection is gone.
    finally:
        server.close()
    return got


async def _run_wt(qlog_dir, n_streams, size):
    port = next_port()
    got = {"fins": 0, "bytes": 0}
    done = asyncio.Event()
    tasks = set()

    async def drain(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                got["bytes"] += len(ev.data)
                if ev.end_stream:
                    got["fins"] += 1
                    if got["fins"] == n_streams:
                        done.set()

    async def handler(session):
        async for ev in session.events():
            if isinstance(ev, WebTransportNewStream):
                t = asyncio.create_task(drain(session, ev.stream_id))
                tasks.add(t)
                t.add_done_callback(tasks.discard)

    scfg = QuicConfiguration(is_client=False, qlog_dir=qlog_dir)
    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE,
                                      configuration=scfg)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE,
                                        configuration=QuicConfiguration()) as wt:
            for _ in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"\xa5" * size, end_stream=True)
            await asyncio.wait_for(done.wait(), 30)
    finally:
        for t in list(tasks):
            t.cancel()
        server.close()
    return got


RUNNERS = {"raw": _run_raw, "wt": _run_wt}


@pytest.mark.parametrize("stack", ["raw", "wt"])
@pytest.mark.parametrize("n,size", [(64, 256), (32, 60000)])
async def test_one_shot_streams_send_no_credit_frames(stack, n, size, tmp_path):
    got = await RUNNERS[stack](str(tmp_path), n, size)
    assert got == {"fins": n, "bytes": n * size}
    max_data, max_stream_data = _sent_fc_frames(str(tmp_path))
    assert max_stream_data == [], f"{len(max_stream_data)} MAX_STREAM_DATA"
    assert max_data == [], f"{len(max_data)} MAX_DATA"


def _credit_for_fin_first_streams(qlog_dir):
    """MAX_STREAM_DATA frames sent for streams whose first STREAM frame
    received carried FIN, and how many such streams there were."""
    paths = glob.glob(os.path.join(qlog_dir, "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    first_fin, credited = {}, []
    for ev in events:
        for fr in ev[3].get("frames") or []:
            if ev[2] == "packet_received" and fr["frame_type"] == "stream":
                first_fin.setdefault(fr["id"], bool(fr.get("fin")))
            elif (ev[2] == "packet_sent"
                    and fr["frame_type"] == "max_stream_data"):
                credited.append(fr["stream_id"])
    fin_first = {sid for sid, fin in first_fin.items() if fin}
    return [sid for sid in credited if sid in fin_first], len(fin_first)


async def test_raw_no_stream_credit_after_fin(tmp_path):
    # A window below the ring takes a first-touch grant, but not on a
    # stream whose first bytes carry FIN.
    got = await _run_raw(str(tmp_path), 64, 256, max_stream_data=10_000_000)
    assert got == {"fins": 64, "bytes": 64 * 256}
    credited, fin_first = _credit_for_fin_first_streams(str(tmp_path))
    assert fin_first > 0
    assert credited == [], f"MAX_STREAM_DATA after FIN on streams {credited}"


def _stream_window_lead(qlog_dir, stream_id):
    """Largest MAX_STREAM_DATA the endpoint sent for stream_id, less the
    stream bytes it had received by then."""
    paths = glob.glob(os.path.join(qlog_dir, "*.qlog"))
    assert len(paths) == 1, paths
    with open(paths[0]) as f:
        events = json.load(f)["traces"][0]["events"]
    received, lead = 0, 0
    for ev in events:
        for fr in ev[3].get("frames") or []:
            if fr.get("id", fr.get("stream_id")) != stream_id:
                continue
            if ev[2] == "packet_received" and fr["frame_type"] == "stream":
                received = max(received, fr["offset"] + fr["length"])
            elif (ev[2] == "packet_sent"
                    and fr["frame_type"] == "max_stream_data"):
                lead = max(lead, fr["maximum"] - received)
    return lead


async def test_raw_local_bidi_window_stays_within_ring(tmp_path):
    """A bidi stream we open takes application flow control on its first
    byte received: the window we grant never runs more than the RX ring
    ahead of what arrived. Left to picoquic, it grows geometrically."""
    ring = 1024 * 1024
    answer = 8 * ring
    port = next_port()

    class Srv(QuicConnectionProtocol):
        answered = False

        def quic_event_received(self, ev):
            if isinstance(ev, StreamDataReceived) and not self.answered:
                self.answered = True
                asyncio.ensure_future(self._answer(ev.stream_id))

        async def _answer(self, sid):
            chunk = b"\x33" * (256 * 1024)
            for _ in range(answer // len(chunk)):
                await self._quic.send_stream_data_drained(sid, chunk)
            await self._quic.send_stream_data_drained(sid, b"", end_stream=True)

    got = {"bytes": 0}
    done = asyncio.Event()

    class Cli(QuicConnectionProtocol):
        def quic_event_received(self, ev):
            if isinstance(ev, StreamDataReceived):
                got["bytes"] += len(ev.data)
                if ev.end_stream:
                    done.set()

    scfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=Srv)
    try:
        ccfg = _client_config(max_stream_data=ring, qlog_dir=str(tmp_path))
        async with connect("127.0.0.1", port, configuration=ccfg,
                           create_protocol=Cli) as cli:
            sid = cli._quic.get_next_available_stream_id()
            cli._quic.send_stream_data(sid, b"GET")
            await asyncio.wait_for(done.wait(), 30)
            overflow = cli._quic._transport.counters["rx_byte_ring_overflow"]
    finally:
        server.close()
    assert got["bytes"] == answer
    assert overflow == 0
    lead = _stream_window_lead(str(tmp_path), sid)
    assert lead <= ring, f"window ran {lead} B ahead of data received"
