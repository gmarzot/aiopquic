"""TX event path: MARK_ACTIVE only when a stream was parked, and an O(1)
liveness check for the cnx of each TX event.

A write to a stream picoquic still holds active posts no MARK_ACTIVE, and
the worker re-checks for bytes when it parks a stream: small writes and a
FIN racing the worker on several streams must all arrive, over raw QUIC
and WebTransport. Liveness walks picoquic's cnx list once per cnx, and a
cnx picoquic deleted is dropped again."""
import asyncio
import os
import random
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportNewStream, WebTransportStreamDataReceived,
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

try:
    from .test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_ALMOST_READY,
        SPSC_EVT_READY, SPSC_EVT_TX_CLOSE, start_server, wait_for_ready,
    )
    from ._ports import next_port
except ImportError:
    from test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_ALMOST_READY,
        SPSC_EVT_READY, SPSC_EVT_TX_CLOSE, start_server, wait_for_ready,
    )
    from _ports import next_port

from aiopquic._binding._transport import (
    TransportContext, _live_cnx_attach, _take_perflog,
)

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")

SPSC_EVT_TX_CNX_REFRESH = 147

STREAMS = 6
WRITES = 30
ROUNDS = 200
CNXS = 64


def _payload(sid, i):
    return bytes([(sid + i) % 251]) * (17 + (sid + i) % 23)


def _expected(sid):
    return b"".join(_payload(sid, i) for i in range(WRITES))


async def _until(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        await asyncio.sleep(0.005)
    return cond()


async def _race_writes(send, sids, rng):
    """Interleave small writes with yields so the worker parks streams
    between some writes and not others; the last write carries FIN."""
    for i in range(WRITES):
        for sid in sids:
            send(sid, _payload(sid, i), i == WRITES - 1)
        if rng.random() < 0.5:
            await asyncio.sleep(0 if rng.random() < 0.7 else 0.0005)


class _Sink(QuicConnectionProtocol):
    got = {}
    fin = set()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            _Sink.got.setdefault(event.stream_id, bytearray()).extend(event.data)
            if event.end_stream:
                _Sink.fin.add(event.stream_id)


async def test_raw_racing_writes_and_fin_all_arrive():
    _Sink.got, _Sink.fin = {}, set()
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=lambda conn, stream_handler=None: _Sink(conn))
    rng = random.Random(7)
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                 server_name=SNI, cafile=CA_FILE)
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic
            for _ in range(ROUNDS):
                sids = []
                for _ in range(STREAMS):
                    sid = quic.get_next_available_stream_id(is_unidirectional=True)
                    quic.send_stream_data(sid, b"")
                    sids.append(sid)
                await _race_writes(
                    lambda s, d, f: quic.send_stream_data(s, d, end_stream=f),
                    sids, rng)
                assert await _until(lambda: _Sink.fin.issuperset(sids)), (
                    f"FIN missing on {sorted(set(sids) - _Sink.fin)}")
                for sid in sids:
                    assert bytes(_Sink.got[sid]) == _expected(sid), f"stream {sid}"
            c = quic._transport.counters
            assert c["tx_mark_active_skipped"] > 0
    finally:
        server.close()


async def test_wt_racing_writes_and_fin_all_arrive():
    got = {}

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
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    rng = random.Random(11)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            for _ in range(ROUNDS // 2):
                sids = [await wt.create_stream(bidir=False) for _ in range(STREAMS)]
                await _race_writes(
                    lambda s, d, f: wt.send_stream_data(s, d, end_stream=f),
                    sids, rng)
                assert await _until(lambda: all(s in got for s in sids)), (
                    f"FIN missing on {[s for s in sids if s not in got]}")
                for sid in sids:
                    assert got[sid] == _expected(sid), f"stream {sid}"
            assert wt._transport.counters["tx_mark_active_skipped"] > 0
    finally:
        server.close()


def _refresh(ctx, cnxs):
    for c in cnxs:
        ctx.push_tx_event(SPSC_EVT_TX_CNX_REFRESH, 0, cnx_ptr=c)
    ctx.wake_up()


def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        time.sleep(0.02)
    return cond()


def _popped(ctx, target):
    return _wait(lambda: ctx.counters["tx_event_ring_pops"] >= target)


def _server_cnxs(server, client, port, n):
    """Open n connections from client; return the server's cnx pointers."""
    for _ in range(n):
        client.create_client_connection("127.0.0.1", port, sni=SNI, alpn=ALPN)
    cnxs = set()

    def ready():
        cnxs.update(e[5] for e in server.drain_rx()
                    if e[0] in (SPSC_EVT_ALMOST_READY, SPSC_EVT_READY) and e[5])
        client.drain_rx()
        return len(cnxs) >= n
    assert _wait(ready), f"{len(cnxs)} of {n} server cnxs"
    return cnxs


def _close_until_dropped(server, closed):
    """Close cnxs from the server; picoquic deletes each once it disconnects,
    and from then on its events are dropped instead of answered."""
    for c in closed:
        server.push_tx_event(SPSC_EVT_TX_CLOSE, 0, cnx_ptr=c)
    server.wake_up()

    def all_dropped():
        dropped = server.counters["tx_event_dropped_dead_cnx"]
        _refresh(server, closed)
        return _wait(lambda: server.counters["tx_event_dropped_dead_cnx"]
                     - dropped >= len(closed), timeout=0.2)
    return _wait(all_dropped, timeout=15.0)


def _refreshed(server, cnxs, times=1):
    """Refresh each cnx `times` times; return the counter deltas."""
    c0 = server.counters
    for _ in range(times):
        _refresh(server, cnxs)
    assert _popped(server, c0["tx_event_ring_pops"] + times * len(cnxs))
    c1 = server.counters
    return {k: c1[k] - c0[k] for k in ("cnx_live_walks", "tx_event_dropped_dead_cnx")}


@pytest.fixture
def pair():
    port = next_port()
    server = start_server(port)
    client = TransportContext()
    client.start(port=0, alpn=ALPN, is_client=True, ca_file=CA_FILE)
    assert wait_for_ready(client)
    try:
        yield server, client, port
    finally:
        client.stop()
        server.stop()


def test_liveness_walks_once_per_cnx_and_drops_deleted(pair):
    server, client, port = pair
    assert server.counters["live_cnxs_hooked"] == 1
    cnxs = _server_cnxs(server, client, port, CNXS)
    d = _refreshed(server, cnxs, times=4)
    assert d["tx_event_dropped_dead_cnx"] == 0
    assert d["cnx_live_walks"] <= CNXS

    closed = set(sorted(cnxs)[:CNXS // 2])
    live = cnxs - closed
    assert _close_until_dropped(server, closed), "closed cnxs still answered"
    assert _refreshed(server, live) == {"cnx_live_walks": 0,
                                        "tx_event_dropped_dead_cnx": 0}


def test_attach_leaves_a_set_perflog_alone(pair):
    server, client, port = pair
    _take_perflog(server)
    _live_cnx_attach(server)
    assert server.counters["live_cnxs_hooked"] == 0
    cnxs = _server_cnxs(server, client, port, 8)
    d = _refreshed(server, cnxs, times=3)
    assert d["cnx_live_walks"] >= 3 * len(cnxs)
    assert d["tx_event_dropped_dead_cnx"] == 0


def test_perflog_taken_after_attach_drops_the_set(pair):
    server, client, port = pair
    cnxs = _server_cnxs(server, client, port, 16)
    assert _refreshed(server, cnxs, times=2)["cnx_live_walks"] <= 16
    _take_perflog(server)
    d = _refreshed(server, cnxs)
    assert d["cnx_live_walks"] >= len(cnxs)
    assert d["tx_event_dropped_dead_cnx"] == 0
    assert server.counters["live_cnxs_hooked"] == 0
    closed = set(sorted(cnxs)[:8])
    assert _close_until_dropped(server, closed), "closed cnxs still answered"
    assert _refreshed(server, cnxs - closed)["tx_event_dropped_dead_cnx"] == 0
