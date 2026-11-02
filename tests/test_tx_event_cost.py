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

from aiopquic._binding._transport import TransportContext

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


def test_liveness_walks_once_per_cnx_and_drops_deleted():
    port = next_port()
    server = start_server(port)
    client = TransportContext()
    try:
        client.start(port=0, alpn=ALPN, is_client=True, ca_file=CA_FILE)
        assert wait_for_ready(client)
        for _ in range(CNXS):
            client.create_client_connection("127.0.0.1", port, sni=SNI, alpn=ALPN)

        cnxs = set()

        def ready():
            cnxs.update(e[5] for e in server.drain_rx()
                        if e[0] in (SPSC_EVT_ALMOST_READY, SPSC_EVT_READY) and e[5])
            client.drain_rx()
            return len(cnxs) >= CNXS
        assert _wait(ready), f"{len(cnxs)} of {CNXS} server cnxs"

        c0 = server.counters
        for _ in range(4):
            _refresh(server, cnxs)
        assert _popped(server, c0["tx_event_ring_pops"] + 4 * CNXS)
        c1 = server.counters
        assert c1["tx_event_dropped_dead_cnx"] == c0["tx_event_dropped_dead_cnx"]
        assert c1["cnx_live_walks"] - c0["cnx_live_walks"] <= CNXS

        closed = set(sorted(cnxs)[:CNXS // 2])
        live = cnxs - closed
        for c in closed:
            server.push_tx_event(SPSC_EVT_TX_CLOSE, 0, cnx_ptr=c)
        server.wake_up()

        # picoquic deletes a server cnx once it disconnects; from then on
        # its events are dropped instead of answered.
        def all_dropped():
            dropped = server.counters["tx_event_dropped_dead_cnx"]
            _refresh(server, closed)
            return _wait(lambda: server.counters["tx_event_dropped_dead_cnx"]
                         - dropped >= len(closed), timeout=0.2)
        assert _wait(all_dropped, timeout=15.0), "closed cnxs still answered"

        c2 = server.counters
        _refresh(server, live)
        assert _popped(server, c2["tx_event_ring_pops"] + len(live))
        c3 = server.counters
        assert c3["tx_event_dropped_dead_cnx"] == c2["tx_event_dropped_dead_cnx"]
        assert c3["cnx_live_walks"] == c2["cnx_live_walks"]
    finally:
        client.stop()
        server.stop()
