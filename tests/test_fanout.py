"""TransportContext.fanout() / fanout_datagram() over a DestSet: one call
writes the same bytes to streams (or datagrams) of several raw QUIC
connections and WebTransport sessions sharing one transport."""
import asyncio
import contextlib
import gc
import os
import time

import pytest

from aiopquic._binding._transport import (
    TransportContext,
    _fanout_fail_marks, _fanout_owed, _hold_tx_wake, _stream_ctx_refs,
    stream_ctx_create, stream_ctx_destroy, stream_ctx_mark_gone,
)
from aiopquic.fanout import (
    DestSet, FANOUT_EVENT_RING_FULL, FANOUT_GONE, FANOUT_QUIC_STREAM,
    FANOUT_RING_FULL, FANOUT_TOO_LARGE,
)
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.dispatch import serve_dispatch
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.webtransport import connect_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    DatagramFrameReceived, StreamDataReceived,
    WebTransportDatagramReceived, WebTransportNewStream,
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
ALPN = "hq-interop"

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


async def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _payloads(n, seed=1):
    out = []
    for i in range(n):
        size = 1 + (i * 7919 + seed * 104729) % 12000
        out.append(bytes((i + j) & 0xFF for j in range(size)))
    return out


class _Rx:
    """What one client received: bytes and FIN per stream, datagrams."""

    def __init__(self):
        self.streams = {}
        self.fins = set()
        self.dgrams = []

    def data(self, sid):
        return bytes(self.streams.get(sid, b""))


class _RawClient(QuicConnectionProtocol):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rx = _Rx()
        self.held = None    # a list: keep events, so the chunks hold credit

    def quic_event_received(self, ev):
        if isinstance(ev, StreamDataReceived):
            if self.held is not None:
                self.held.append(ev)
                return
            self._take(ev)
        elif isinstance(ev, DatagramFrameReceived):
            self.rx.dgrams.append(bytes(ev.data))

    def _take(self, ev):
        self.rx.streams.setdefault(ev.stream_id, bytearray()).extend(ev.data)
        if ev.end_stream:
            self.rx.fins.add(ev.stream_id)

    def release(self):
        held, self.held = self.held or [], None
        for ev in held:
            self._take(ev)


async def _wt_reader(wt, rx):
    tasks = []

    async def one(sid):
        async for ev in wt.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                rx.streams.setdefault(sid, bytearray()).extend(ev.data)
                if ev.end_stream:
                    rx.fins.add(sid)

    try:
        async for ev in wt.events():
            if isinstance(ev, WebTransportNewStream):
                tasks.append(asyncio.create_task(one(ev.stream_id)))
            elif isinstance(ev, WebTransportDatagramReceived):
                rx.dgrams.append(bytes(ev.data))
    finally:
        for t in tasks:
            t.cancel()


class _Peer:
    """One server-side connection or session and its client's receipts."""

    def __init__(self, kind, srv, rx, client):
        self.kind = kind        # "quic" or "wt"
        self.srv = srv          # server QuicConnection or WebTransportSession
        self.rx = rx
        self.client = client    # _RawClient or WebTransportClient

    async def open_stream(self, header=b"H"):
        """A server-initiated uni stream, written once so it has a context."""
        if self.kind == "quic":
            sid = self.srv.get_next_available_stream_id(is_unidirectional=True)
        else:
            sid = await self.srv.create_stream(bidir=False)
        self.srv.send_stream_data(sid, header)
        return sid


@contextlib.asynccontextmanager
async def _rig(n_quic=2, n_wt=2, server_cfg=None, client_cfgs=None):
    """serve_dispatch on one transport; n_quic raw and n_wt WT clients."""
    srv_quic = []
    srv_wt = []

    def create_protocol(conn, stream_handler=None):
        srv_quic.append(conn)
        return QuicConnectionProtocol(conn, stream_handler=stream_handler)

    cfg = server_cfg or QuicConfiguration(is_client=False,
                                          alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    port = next_port()
    server = await serve_dispatch("127.0.0.1", port, configuration=cfg,
                                  create_protocol=create_protocol,
                                  wt_path="/wt", wt_handler=srv_wt.append)
    peers = []
    readers = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            for i in range(n_quic):
                ccfg = (client_cfgs or {}).get(i) or QuicConfiguration(
                    is_client=True, alpn_protocols=[ALPN],
                    server_name=SNI, cafile=CA_FILE)
                client = await stack.enter_async_context(connect(
                    "127.0.0.1", port, configuration=ccfg,
                    create_protocol=_RawClient))
                assert await _until(lambda: len(srv_quic) == i + 1
                                    and srv_quic[i]._cnx_ptr)
                peers.append(_Peer("quic", srv_quic[i], client.rx, client))
            for i in range(n_wt):
                wt = await stack.enter_async_context(connect_webtransport(
                    "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE))
                rx = _Rx()
                readers.append(asyncio.create_task(_wt_reader(wt, rx)))
                assert await _until(lambda: len(srv_wt) == i + 1
                                    and srv_wt[i].session_ready)
                peers.append(_Peer("wt", srv_wt[i], rx, wt))
            yield server.transports[0], peers
    finally:
        for r in readers:
            r.cancel()
        server.close()


async def _fanout_dests(transport, peers):
    ds = DestSet(transport)
    sids, idxs = [], []
    for p in peers:
        sid = await p.open_stream()
        sids.append(sid)
        idxs.append(ds.add(p.srv, sid))
        assert idxs[-1] is not None, p.kind
    assert len(set(idxs)) == len(idxs)
    return ds, sids, idxs


def _slot(index):
    return index & 0xFFFFFFFF


def _gone(idxs):
    return sorted((ix, FANOUT_GONE) for ix in idxs)


@pytest.mark.asyncio
async def test_fanout_bytes_match_per_stream_writes():
    async with _rig() as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        ref = [await p.open_stream() for p in peers]
        payloads = _payloads(40)
        calls0, writes0 = t.fanout_calls, t.fanout_writes
        for i, data in enumerate(payloads):
            last = i == len(payloads) - 1
            assert t.fanout(ds, data, last) is None
            for p, sid in zip(peers, ref):
                p.srv.send_stream_data(sid, data, end_stream=last)
            await asyncio.sleep(0.002)
        assert t.fanout_calls - calls0 == len(payloads)
        assert t.fanout_writes - writes0 == len(payloads) * len(peers)
        want = b"H" + b"".join(payloads)
        for p, sid, rsid in zip(peers, sids, ref):
            assert await _until(lambda: sid in p.rx.fins and rsid in p.rx.fins), (
                p.kind, len(p.rx.data(sid)), len(want))
            assert p.rx.data(sid) == p.rx.data(rsid) == want, p.kind
        # Past the FIN every destination is refused.
        assert sorted(t.fanout(ds, b"late")) == _gone(idxs)


@pytest.mark.asyncio
async def test_ring_full_refuses_only_that_destination():
    srv = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    srv.stream_ring_cap = 64 * 1024
    async with _rig(server_cfg=srv) as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        for p, sid in zip(peers, sids):
            assert await _until(lambda: p.rx.data(sid) == b"H"), p.kind
        await asyncio.sleep(0.05)
        # With the worker asleep, a full ring stays full.
        deadline = time.monotonic() + 2.0
        while not _hold_tx_wake(t):
            assert time.monotonic() < deadline, "worker never went idle"
            await asyncio.sleep(0.001)
        sc0 = peers[0].srv._stream_ctxs[sids[0]]
        fill = b"f" * (64 * 1024 - t.tx_data_ring_used(sc0))
        peers[0].srv.send_stream_data(sids[0], fill)
        chunk = bytes(range(256)) * 128
        refusals0 = t.fanout_refusals
        assert t.fanout(ds, chunk) == [(idxs[0], FANOUT_RING_FULL)]
        assert t.fanout_refusals - refusals0 == 1
        # Wake the server's worker with an inbound packet.
        cq = peers[1].client._quic
        cq.send_stream_data(cq.get_next_available_stream_id(), b"wake")
        assert t.fanout(ds, b"", True) is None
        for i, p in enumerate(peers):
            want = b"H" + (fill if i == 0 else chunk)
            assert await _until(lambda: sids[i] in p.rx.fins), (
                p.kind, len(p.rx.data(sids[i])))
            assert p.rx.data(sids[i]) == want, (i, p.kind)


@pytest.mark.asyncio
async def test_removed_destination_gets_nothing_more():
    async with _rig() as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        before, after = b"before" * 100, b"after" * 100
        assert t.fanout(ds, before) is None
        gone = 2                    # a WT destination
        assert ds.remove(idxs[gone]) is True
        assert len(ds) == len(peers) - 1
        assert ds.remove(idxs[gone]) is False
        assert t.fanout(ds, after, True) is None
        peers[gone].srv.send_stream_data(sids[gone], b"", end_stream=True)
        for i, p in enumerate(peers):
            assert await _until(lambda: sids[i] in p.rx.fins), p.kind
            want = b"H" + before + (b"" if i == gone else after)
            assert p.rx.data(sids[i]) == want, (i, p.kind)
        # A freed slot is reused under a new index; the old one is stale.
        sid = await peers[gone].open_stream()
        new = ds.add(peers[gone].srv, sid)
        assert _slot(new) == _slot(idxs[gone]) and new != idxs[gone]
        assert ds.remove(idxs[gone]) is False
        assert len(ds) == len(peers) and ds.stream_id(new) == sid
        with pytest.raises(IndexError):
            ds.stream_id(idxs[gone])


@pytest.mark.asyncio
async def test_reset_or_closed_destinations_are_refused():
    async with _rig() as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        assert t.fanout(ds, b"x" * 1000) is None
        # quic 0 and wt 2: stream reset; quic 1 and wt 3: closed.
        peers[0].srv.reset_stream(sids[0], 0x10)
        peers[2].srv.reset_stream(sids[2], 0x10)
        peers[1].client.close()
        peers[3].client.close()
        assert await _until(lambda: sorted(t.fanout(ds, b"y" * 1000) or [])
                            == _gone(idxs)), t.fanout(ds, b"z")
        for ix in idxs:
            assert ds.remove(ix) is True
        assert len(ds) == 0
        assert t.fanout(ds, b"w") is None
        for i in range(len(peers)):
            assert ds.add(peers[i].srv, sids[i]) is None, i
        assert len(ds) == 0


@pytest.mark.asyncio
async def test_datagram_fanout_reaches_every_destination():
    async with _rig() as (t, peers):
        ds = DestSet(t, datagram=True)
        idxs = [ds.add_datagram(p.srv) for p in peers]
        assert None not in idxs
        with pytest.raises(ValueError):
            ds.add(peers[0].srv, await peers[0].open_stream())
        sent = [b"dgram-%03d" % i + b"." * 200 for i in range(20)]
        calls0 = t.fanout_datagram_calls
        for d in sent:
            assert t.fanout_datagram(ds, d) is None
            await asyncio.sleep(0.005)
        assert t.fanout_datagram_calls - calls0 == len(sent)
        for p in peers:
            assert await _until(lambda: len(p.rx.dgrams) == len(sent)), (
                p.kind, len(p.rx.dgrams))
            assert p.rx.dgrams == sent, p.kind
        assert sorted(t.fanout_datagram(ds, b"x" * 70000)) == sorted(
            (ix, FANOUT_TOO_LARGE) for ix in idxs)
        with pytest.raises(ValueError):
            t.fanout_datagram(ds, b"")
        peers[1].client.close()
        peers[3].client.close()
        assert await _until(lambda: sorted(t.fanout_datagram(ds, b"q") or [])
                            == _gone([idxs[1], idxs[3]]))
        assert ds.add_datagram(peers[1].srv) is None
        assert ds.add_datagram(peers[3].srv) is None


@pytest.mark.asyncio
async def test_fin_by_another_writer_is_gone():
    async with _rig() as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        a, b = b"a" * 3000, b"b" * 2000
        assert t.fanout(ds, a) is None
        # Each stream's FIN comes from its own writer: tx_send_atomic on
        # raw QUIC, push_stream_data on WT.
        for p, sid in zip(peers, sids):
            p.srv.send_stream_data(sid, b, end_stream=True)
        writes0 = t.fanout_writes
        assert sorted(t.fanout(ds, b"late" * 100)) == _gone(idxs)
        assert sorted(t.fanout(ds, b"", True)) == _gone(idxs)
        assert t.fanout_writes == writes0
        for p, sid in zip(peers, sids):
            assert ds.add(p.srv, sid) is None, p.kind
        for p, sid in zip(peers, sids):
            assert await _until(lambda: sid in p.rx.fins), p.kind
        await asyncio.sleep(0.05)
        for p, sid in zip(peers, sids):
            assert p.rx.data(sid) == b"H" + a + b, p.kind


@pytest.mark.asyncio
async def test_add_by_connection():
    async with _rig() as (t, peers):
        ds = DestSet(t)
        for p in peers:
            assert ds.add(p.srv, 4 * 999 + 3) is None, p.kind
        assert len(ds) == 0
        async with _rig(n_quic=1, n_wt=1) as (t2, others):
            for o in others:
                sid = await o.open_stream()
                with pytest.raises(ValueError):
                    ds.add(o.srv, sid)
                with pytest.raises(ValueError):
                    DestSet(t, datagram=True).add_datagram(o.srv)
                assert DestSet(t2).add(o.srv, sid) is not None
        with pytest.raises(ValueError):
            ds.add_datagram(peers[0].srv)
        assert len(ds) == 0


@pytest.mark.asyncio
async def test_owed_marks_post_on_next_call():
    async with _rig() as (t, peers):
        ds, sids, idxs = await _fanout_dests(t, peers)
        for p, sid in zip(peers, sids):
            assert await _until(lambda: p.rx.data(sid) == b"H"), p.kind
        await asyncio.sleep(0.05)
        n = len(peers)
        # Every first mark fails, and so does the retry that ends the call.
        _fanout_fail_marks(t, 2 * n)
        data = b"owed" * 500
        assert t.fanout(ds, data) is None
        assert _fanout_fail_marks(t, 0) == 0
        assert _fanout_owed(t) == n
        await asyncio.sleep(0.1)
        for p, sid in zip(peers, sids):
            assert p.rx.data(sid) == b"H", p.kind
        # The next call posts them; the streams stay armed, so its own
        # commit posts none.
        assert t.fanout(ds, b"", True) is None
        assert _fanout_owed(t) == 0
        for p, sid in zip(peers, sids):
            assert await _until(lambda: sid in p.rx.fins), p.kind
            assert p.rx.data(sid) == b"H" + data, p.kind


# --- without a network: a transport that never starts ---------------------

def _dest(t, sc, sid=0, cap=4096, cnx=1):
    return (t, FANOUT_QUIC_STREAM, cnx, sid, sc, cap, None)


def _alive():
    return TransportContext().counters["sc_alive_total"]


def test_start_rotates():
    t = TransportContext()
    scs = [stream_ctx_create() for _ in range(3)]
    ds = DestSet(t)
    idxs = []
    for i, sc in enumerate(scs):
        idxs.append(ds._add(_dest(t, sc, sid=4 * i)))
        stream_ctx_mark_gone(sc)
        stream_ctx_destroy(sc)
    firsts = []
    for _ in range(5):
        assert ds.start == len(firsts) % 3
        refused = t.fanout(ds, b"x")
        assert sorted(refused) == _gone(idxs)
        firsts.append(_slot(refused[0][0]))
    assert firsts == [0, 1, 2, 0, 1]
    ds.remove(idxs[1])
    assert [r[0] for r in t.fanout(ds, b"x")] == [idxs[2], idxs[0]]


def test_dealloc_and_remove_return_stream_ctx_refs():
    gc.collect()
    base = _alive()
    t = TransportContext()
    scs = [stream_ctx_create() for _ in range(4)]
    ds = DestSet(t)
    idxs = []
    for sc in scs:
        idxs.append(ds._add(_dest(t, sc)))
        assert _stream_ctx_refs(sc) == 2
        stream_ctx_destroy(sc)
    assert _alive() == base + 4
    ds.remove(idxs[0])
    assert _alive() == base + 3
    del ds
    gc.collect()
    assert _alive() == base


def test_wrong_transport_or_kind_raises():
    t1, t2 = TransportContext(), TransportContext()
    sc = stream_ctx_create()
    try:
        with pytest.raises(ValueError):
            DestSet(t1)._add(_dest(t2, sc))
        with pytest.raises(ValueError):
            DestSet(t1).add(None, 0)
        with pytest.raises(ValueError):
            DestSet(t1).add(object(), 0)
        with pytest.raises(ValueError):
            DestSet(t1).add_datagram(object())
        with pytest.raises(ValueError):
            DestSet(t1, datagram=True).add(object(), 0)
        with pytest.raises(ValueError):
            DestSet(t1, datagram=True)._add(_dest(t1, sc))
        with pytest.raises(ValueError):
            t2.fanout(DestSet(t1), b"x")
        with pytest.raises(ValueError):
            t1.fanout_datagram(DestSet(t1), b"x")
        with pytest.raises(TypeError):
            DestSet(object())
    finally:
        stream_ctx_destroy(sc)


def test_too_large_and_event_ring_full_commit_nothing():
    t = TransportContext(tx_ring_cap=8)
    scs = [stream_ctx_create() for _ in range(12)]
    ds = DestSet(t)
    idxs = [ds._add(_dest(t, sc, sid=4 * i, cap=4096))
            for i, sc in enumerate(scs)]
    assert t.fanout(ds, b"x" * 5000) == [(ix, FANOUT_TOO_LARGE)
                                         for ix in idxs]
    # No worker drains the event ring: each first write needs a MARK.
    refused = t.fanout(ds, b"y" * 100)
    assert refused and all(code == FANOUT_EVENT_RING_FULL
                           for _, code in refused)
    took = [i for i in range(12) if idxs[i] not in dict(refused)]
    assert 1 <= len(took) <= 8, refused
    for i, sc in enumerate(scs):
        assert t.tx_data_ring_used(sc) == (100 if i in took else 0)
    # A full event ring refuses every destination, as tx_send_atomic does.
    assert sorted(t.fanout(ds, b"z" * 10)) == sorted(
        (ix, FANOUT_EVENT_RING_FULL) for ix in idxs)
    for i, sc in enumerate(scs):
        assert t.tx_data_ring_used(sc) == (100 if i in took else 0)


def test_stale_index_after_reuse():
    t = TransportContext()
    scs = [stream_ctx_create() for _ in range(3)]
    try:
        ds = DestSet(t)
        a = ds._add(_dest(t, scs[0], sid=0))
        b = ds._add(_dest(t, scs[1], sid=4))
        assert ds.remove(a) is True
        c = ds._add(_dest(t, scs[2], sid=8))
        assert _slot(c) == _slot(a) and c != a
        assert ds.remove(a) is False
        assert len(ds) == 2
        assert ds.stream_id(c) == 8 and ds.stream_id(b) == 4
        with pytest.raises(IndexError):
            ds.stream_id(a)
        assert _stream_ctx_refs(scs[2]) == 2
        stream_ctx_mark_gone(scs[2])
        assert t.fanout(ds, b"x") is not None
        assert (c, FANOUT_GONE) in t.fanout(ds, b"x")
        assert ds.remove(c) is True and ds.remove(c) is False
        assert ds.remove((1 << 32) | 7) is False
    finally:
        for sc in scs:
            stream_ctx_destroy(sc)


def test_gone_stream_is_not_added():
    t = TransportContext()
    sc = stream_ctx_create()
    try:
        ds = DestSet(t)
        assert t.tx_send_atomic(0, b"x", True, 1, sc, 4096) == 0
        refs = _stream_ctx_refs(sc)
        assert ds._add(_dest(t, sc)) is None
        sc2 = stream_ctx_create()
        stream_ctx_mark_gone(sc2)
        assert ds._add(_dest(t, sc2)) is None
        assert _stream_ctx_refs(sc2) == 1
        stream_ctx_destroy(sc2)
        assert len(ds) == 0 and _stream_ctx_refs(sc) == refs
    finally:
        stream_ctx_destroy(sc)


def test_owed_mark_is_not_a_refusal():
    t = TransportContext()
    sc = stream_ctx_create()
    try:
        ds = DestSet(t)
        ix = ds._add(_dest(t, sc))
        ring0 = t.tx_event_ring_count
        _fanout_fail_marks(t, 2)
        assert t.fanout(ds, b"x" * 100) is None
        assert t.tx_data_ring_used(sc) == 100
        assert _fanout_owed(t) == 1 and t.tx_event_ring_count == ring0
        assert _stream_ctx_refs(sc) == 3      # ours, the set's, the debt's
        # The next call posts the owed mark; its own commit needs none.
        assert t.fanout(ds, b"y" * 10) is None
        assert _fanout_owed(t) == 0 and t.tx_event_ring_count == ring0 + 1
        assert t.tx_data_ring_used(sc) == 110
        # A mark owed to a stream since gone is dropped with its reference.
        ds.remove(ix)
        sc2 = stream_ctx_create()
        ix2 = ds._add(_dest(t, sc2, sid=4))
        _fanout_fail_marks(t, 2)
        assert t.fanout(ds, b"z") is None
        assert _fanout_owed(t) == 1 and _stream_ctx_refs(sc2) == 3
        stream_ctx_mark_gone(sc2)
        assert t.fanout(ds, b"w") == [(ix2, FANOUT_GONE)]
        assert _fanout_owed(t) == 0 and _stream_ctx_refs(sc2) == 2
        assert t.tx_event_ring_count == ring0 + 1
        ds.remove(ix2)
        stream_ctx_destroy(sc2)
    finally:
        stream_ctx_destroy(sc)
