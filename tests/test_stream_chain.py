"""Stream chains over raw QUIC and WebTransport.

The binding tests assert on set_chain_applied, which counts the worker
accepting the call, as test_stream_priority does for priorities.

The scheduling tests read the sender's qlog, the order the scheduler sent
in: the receiver's shows arrival, which loss and retransmission reorder on a
loaded host. A priority 0 stream, written first, holds the connection while
every other stream is queued behind it, so the order of the other streams'
bytes is the scheduler's choice, whatever the host's speed. A run where the
blocker drained before the last write reached the worker is repeated.
"""

import asyncio
import contextlib
import functools
import glob
import json
import os
import time
import warnings

import pytest

from aiopquic._binding._transport import (
    TransportContext, _stream_ctx_refs, stream_ctx_create, stream_ctx_destroy,
)
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    StreamDataReceived, WebTransportNewStream, WebTransportStreamDataReceived,
)

try:
    from .test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_STREAM_DATA,
        SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_CLOSE, collect_stream_data,
        connect_client, drain_until, start_server,
    )
    from ._ports import next_port
except ImportError:      # loaded bare, outside the package
    from test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_STREAM_DATA,
        SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_CLOSE, collect_stream_data,
        connect_client, drain_until, start_server,
    )
    from _ports import next_port


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)

BOGUS_CNX = 0xDEADBEEF
ERR_INVALID_STREAM_ID = 0x40E      # PICOQUIC_ERROR_INVALID_STREAM_ID
ERR_STREAM_ALREADY_CLOSED = 0x40F  # PICOQUIC_ERROR_STREAM_ALREADY_CLOSED
ERR_DISCONNECTED = 0x414           # PICOQUIC_ERROR_DISCONNECTED
BLOCKER = b"\x00" * (2 * 1024 * 1024)
STREAM_BYTES = 64 * 1024


def wait_counter(ctx, key, target, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = ctx.counters[key]
        if v >= target:
            return v
        time.sleep(0.01)
    return ctx.counters[key]


def applied(ctx):
    return ctx.counters['set_chain_applied']


class TestBinding:

    def test_picoquic_applies_it(self):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                before = applied(client)
                assert client.set_stream_chain(cnx_ptr, 0, 7, 3) == 0
                assert client.set_stream_chain(cnx_ptr, 0, 0, 0) == 0
                assert wait_counter(client, 'set_chain_applied',
                                    before + 2) == before + 2
                assert client.counters['set_chain_rejected'] == 0
                client.tx_send_stream(cnx_ptr, 0, b"chained")
                events = drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
                assert b"chained" in collect_stream_data(events, 0)
            finally:
                client.stop()
        finally:
            server.stop()

    def test_rejection_is_counted(self):
        """A server-initiated id on the client is refused by picoquic."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                assert client.set_stream_chain(cnx_ptr, 1, 7, 0) == 0
                assert wait_counter(client, 'set_chain_rejected', 1) == 1
                assert (client.counters['set_chain_last_err']
                        == ERR_INVALID_STREAM_ID)
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [None, 4])
    def test_first_write_carries_it(self, priority):
        """The chain rides the event that activates the stream, with the
        priority when there is one."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                before = applied(client)
                before_prio = client.counters['set_priority_applied']
                pushes = client.counters['tx_event_ring_pushes']
                client.tx_send_stream(
                    cnx_ptr, 0, b"first", chain=2**62, order=2**63,
                    priority=-1 if priority is None else priority)
                assert client.counters['tx_event_ring_pushes'] == pushes + 1
                assert wait_counter(client, 'set_chain_applied',
                                    before + 1) == before + 1
                assert (client.counters['set_priority_applied']
                        == before_prio + (priority is not None))
                events = drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
                assert b"first" in collect_stream_data(events, 0)
            finally:
                client.stop()
        finally:
            server.stop()

    def test_write_on_an_active_stream_applies_it(self):
        """A write that finds its stream still active posts no MARK_ACTIVE;
        its chain rides a SET_STREAM_CHAIN. A stream that drained before its
        second write is retried on the next one."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                for sid in range(0, 20, 4):
                    client.tx_send_stream(cnx_ptr, sid, b"x" * (2 << 20))
                    before = applied(client)
                    skipped = client.counters['tx_mark_active_skipped']
                    client.tx_send_stream(cnx_ptr, sid, b"more", chain=5,
                                          order=1)
                    active = (client.counters['tx_mark_active_skipped']
                              == skipped + 1)
                    assert wait_counter(client, 'set_chain_applied',
                                        before + 1) == before + 1
                    if active:
                        break
                else:
                    pytest.fail("every stream drained before its second write")
            finally:
                client.stop()
        finally:
            server.stop()

    def test_write_without_chain_applies_none(self):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                client.tx_send_stream(cnx_ptr, 0, b"plain")
                events = drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
                assert b"plain" in collect_stream_data(events, 0)
                assert applied(client) == 0
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("chain,order,exc", [
        (-1, 0, OverflowError), (2**64, 0, OverflowError),
        (1, -1, OverflowError), (None, 1, ValueError),
    ])
    def test_bad_arguments_are_refused(self, chain, order, exc):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                pushes = client.counters['tx_event_ring_pushes']
                with pytest.raises(exc):
                    client.tx_send_stream(cnx_ptr, 0, b"x", chain=chain,
                                          order=order)
                assert client.counters['tx_event_ring_pushes'] == pushes
            finally:
                client.stop()
        finally:
            server.stop()

    def test_unknown_cnx_is_dropped_not_applied(self):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                dropped = client.counters['tx_event_dropped_dead_cnx']
                assert client.set_stream_chain(BOGUS_CNX, 0, 1, 1) == 0
                assert wait_counter(client, 'tx_event_dropped_dead_cnx',
                                    dropped + 1) == dropped + 1
                assert applied(client) == 0
            finally:
                client.stop()
        finally:
            server.stop()

    def test_finished_stream_is_refused(self):
        """A stream whose FIN has left joins no chain."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                client.tx_send_stream(cnx_ptr, 0, b"done", end_stream=True)
                events = drain_until(server, SPSC_EVT_STREAM_FIN, timeout=5.0)
                assert b"done" in collect_stream_data(events, 0)
                before = applied(client)
                assert client.set_stream_chain(cnx_ptr, 0, 1, 0) == 0
                assert wait_counter(client, 'set_chain_rejected', 1) == 1
                assert (client.counters['set_chain_last_err']
                        == ERR_STREAM_ALREADY_CLOSED)
                assert applied(client) == before
            finally:
                client.stop()
        finally:
            server.stop()

    def test_closing_connection_refuses_it(self):
        """A chain set behind the connection's close is not applied."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                client.tx_send_stream(cnx_ptr, 0, b"x")
                drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
                before = applied(client)
                # The close posts no wake: the chain's wakes the worker for both.
                client.push_tx_event(SPSC_EVT_TX_CLOSE, 0, cnx_ptr=cnx_ptr)
                assert client.set_stream_chain(cnx_ptr, 0, 1, 0) == 0
                assert wait_counter(client, 'set_chain_rejected', 1) == 1
                assert client.counters['set_chain_last_err'] == ERR_DISCONNECTED
                assert applied(client) == before
            finally:
                client.stop()
        finally:
            server.stop()

    def test_queued_chain_holds_its_stream_context(self):
        """The event keeps the stream's context alive until the worker
        takes it, whatever its owner drops meanwhile."""
        ctx = TransportContext(tx_ring_cap=4)
        sc = stream_ctx_create()
        try:
            assert ctx.set_stream_chain(BOGUS_CNX, 0, 1, 1, sc) == 0
            assert _stream_ctx_refs(sc) == 2
            ctx.stop()
            assert _stream_ctx_refs(sc) == 1
        finally:
            stream_ctx_destroy(sc)

    def test_full_ring_returns_one_and_arms_the_drain(self):
        ctx = TransportContext(tx_ring_cap=2)
        posted = 0
        while ctx.set_stream_chain(BOGUS_CNX, 0, 1, 1) == 0:
            posted += 1
            assert posted <= 64, "ring never filled"
        assert posted == ctx.tx_event_ring_capacity
        arms = ctx.counters['tx_event_ring_arms']
        assert ctx.set_stream_chain(BOGUS_CNX, 0, 1, 1) == 1
        assert ctx.counters['tx_event_ring_arms'] > arms


# --- Scheduling -------------------------------------------------------------


def _sent_frames(qlog_dir, sids):
    """Stream frames on `sids` in the sender's qlog, in send order, or None
    while picoquic has not closed the qlog."""
    paths = glob.glob(os.path.join(qlog_dir, "*.qlog"))
    if len(paths) != 1:
        return None
    try:
        with open(paths[0]) as f:
            events = json.load(f)["traces"][0]["events"]
    except (OSError, ValueError):
        return None
    return [fr for ev in events if ev[2] == "packet_sent"
            for fr in ev[3].get("frames") or []
            if fr.get("frame_type") == "stream" and fr["id"] in sids]


async def _first_sends(qlog_dir, sids, size=STREAM_BYTES, timeout=10.0):
    """(stream id, new payload bytes) of each stream frame on `sids` that
    carries new payload, in send order, for streams of `size` payload
    bytes (or {stream id: size}). A WT stream's header, which h3zero queues
    apart from the payload, is not counted. Waits for the qlog to hold
    every byte."""
    sizes = size if isinstance(size, dict) else {sid: size for sid in sids}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        frames = _sent_frames(qlog_dir, sids)
        total = {sid: 0 for sid in sids}
        for fr in frames or []:
            total[fr["id"]] = max(total[fr["id"]], fr["offset"] + fr["length"])
        short = {sid: n for sid, n in total.items() if n < sizes[sid]}
        if frames is not None and not short:
            break
        assert loop.time() < deadline, (
            f"qlog in {qlog_dir} misses stream bytes: "
            f"{'no closed qlog' if frames is None else short}")
        await asyncio.sleep(0.05)
    sent = {sid: n - sizes[sid] for sid, n in total.items()}
    out = []
    for fr in frames:
        end = fr["offset"] + fr["length"]
        if end > sent[fr["id"]]:
            out.append((fr["id"], end - max(fr["offset"], sent[fr["id"]])))
            sent[fr["id"]] = end
    return out


def _spans(sends):
    """stream id -> (index of its first send, index of its last send)."""
    spans = {}
    for i, (sid, _) in enumerate(sends):
        first, _ = spans.get(sid, (i, i))
        spans[sid] = (first, i)
    return spans


def _sent_before(sends, index, sids):
    return sum(n for sid, n in sends[:index] if sid in sids)


def _check_chain_order(sends, chains):
    """Each chain's streams, listed in chain order, go one after the other."""
    spans = _spans(sends)
    for sids in chains:
        for a, b in zip(sids, sids[1:]):
            assert spans[a][1] < spans[b][0], (
                f"stream {b} started before stream {a} ended: {spans}")


def _check_sharing(sends, chains, size=STREAM_BYTES):
    """When one chain has sent everything, the others have sent at least a
    quarter of theirs."""
    spans = _spans(sends)
    for sids in chains:
        end = spans[sids[-1]][1]
        for others in chains:
            if others is sids:
                continue
            sent = _sent_before(sends, end, others)
            assert sent >= len(others) * size // 4, (
                f"chain {others} sent {sent} bytes while chain {sids} ran")


def _check_round_robin(sends, sids):
    """Unchained streams at an even level interleave."""
    spans = _spans(sends)
    for a, b in zip(sids, sids[1:]):
        assert spans[b][0] < spans[a][1], (
            f"stream {b} waited for stream {a}: {spans}")


def _check_fifo(sends, sids):
    """Unchained streams at an odd level go one after the other."""
    spans = _spans(sends)
    for a, b in zip(sids, sids[1:]):
        assert spans[a][1] < spans[b][0], spans


# Streams as (priority, chain, order), opened in this order; chain None is
# unchained. Chain order runs against stream id, which round robin and FIFO
# by stream id would not follow.
TWO_CHAINS = [(8, 1, 2), (8, 2, 2), (8, 1, 1), (8, 2, 1), (8, 1, 0), (8, 2, 0)]
TWO_CHAINS_ODD = [(9, c, o) for _, c, o in TWO_CHAINS]
UNCHAINED = [(8, None, None)] * 4
UNCHAINED_ODD = [(9, None, None)] * 4


def _chains(sids, streams):
    """Stream ids of each chain, in chain order."""
    chains = {}
    for sid, (_, chain, order) in zip(sids, streams):
        if chain is not None:
            chains.setdefault(chain, []).append((order, sid))
    return [[sid for _, sid in sorted(c)] for _, c in sorted(chains.items())]


def _client_cfg(alpn, qlog_dir=None):
    return QuicConfiguration(is_client=True, alpn_protocols=[alpn],
                             server_name=SNI, cafile=CA_FILE,
                             congestion_control_algorithm="newreno",
                             qlog_dir=qlog_dir)


def _server_cfg(alpn=None):
    """Raw QUIC server configuration for `alpn`, else WebTransport's."""
    if alpn is None:
        return QuicConfiguration(is_client=False)
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[alpn])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


@contextlib.contextmanager
def _sender_log():
    """Transports started inside log every packet: picoquic stops logging a
    connection's packets after it has sent 100, unless a textlog raises
    the log level."""
    old = os.environ.get("AIOPQUIC_TEXTLOG_FILE")
    os.environ["AIOPQUIC_TEXTLOG_FILE"] = os.devnull
    try:
        yield
    finally:
        if old is None:
            del os.environ["AIOPQUIC_TEXTLOG_FILE"]
        else:
            os.environ["AIOPQUIC_TEXTLOG_FILE"] = old


class _Unqueued(Exception):
    """The blocker drained before every write reached the worker."""


async def _check_queued(conn, blocker):
    """Every write reached the worker while the blocker still had bytes to
    send, so no other stream had sent yet; else raises _Unqueued."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while conn.tx_event_ring_fill() != 0:
        assert loop.time() < deadline, "TX events not drained"
        await asyncio.sleep(0.0002)
    if conn.tx_data_ring_used(blocker) == 0:
        raise _Unqueued


def _queued(test, runs=5):
    """Runs `test`, each time in a fresh directory, until a run queues every
    write behind its blocker; fails after `runs` that do not."""
    @functools.wraps(test)
    async def run(tmp_path, **kwargs):
        for i in range(runs):
            path = tmp_path / f"run{i}"
            path.mkdir()
            try:
                return await test(path, **kwargs)
            except _Unqueued:
                warnings.warn(f"{test.__name__}: the blocker drained before "
                              f"every write was queued; run {i + 1} discarded")
        pytest.fail(f"the blocker drained before every write was queued, "
                    f"{runs} runs")
    return run


async def _until(pred, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def _send_quic(qlog_dir, streams, size=STREAM_BYTES):
    """Send the blocker, then `size` bytes on each stream, over raw QUIC.
    Returns the stream ids, in the order of `streams`."""
    done = set()

    class _Receiver(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived) and event.end_stream:
                done.add(event.stream_id)

    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(ALPN),
                         create_protocol=_Receiver)
    try:
        with _sender_log():
            async with connect("127.0.0.1", port,
                               configuration=_client_cfg(ALPN, qlog_dir)) as client:
                quic = client._quic
                blocker = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(blocker, BLOCKER, end_stream=True, priority=0)
                sids = []
                for priority, chain, order in streams:
                    sid = quic.get_next_available_stream_id(is_unidirectional=True)
                    quic.send_stream_data(sid, bytes(size), end_stream=True,
                                          priority=priority, chain=chain,
                                          order=order)
                    sids.append(sid)
                await _check_queued(quic, blocker)
                assert await _until(lambda: done >= {blocker, *sids}), done
    finally:
        server.close()
    return sids


async def _send_wt(qlog_dir, streams, size=STREAM_BYTES):
    """As _send_quic, over WebTransport, with the bytes riding the creates."""
    done = set()

    async def read(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                done.add(sid)
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
        key_file=KEY_FILE, configuration=_server_cfg())
    try:
        with _sender_log():
            async with connect_webtransport(
                    "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                    configuration=_client_cfg("h3", qlog_dir)) as wt:
                blocker = await wt.create_stream(bidir=False, initial=BLOCKER,
                                                 end_stream=True, priority=0)
                # Concurrent creates post back to back, ids in this order.
                sids = await asyncio.gather(*(
                    wt.create_stream(bidir=False, initial=bytes(size),
                                     end_stream=True, priority=priority,
                                     chain=chain, order=order)
                    for priority, chain, order in streams))
                assert sids == sorted(sids), sids
                await _check_queued(wt, blocker)
                assert await _until(lambda: done >= {blocker, *sids}), done
    finally:
        server.close()
    return sids


SENDERS = {"quic": _send_quic, "wt": _send_wt}


@pytest.mark.parametrize("transport", sorted(SENDERS))
@_queued
async def test_chains_share_and_keep_order(tmp_path, transport):
    """Round robin level: two chains take turns, each in chain order."""
    sids = await SENDERS[transport](str(tmp_path), TWO_CHAINS)
    sends = await _first_sends(str(tmp_path), sids)
    chains = _chains(sids, TWO_CHAINS)
    _check_chain_order(sends, chains)
    _check_sharing(sends, chains)


@pytest.mark.parametrize("transport", sorted(SENDERS))
@_queued
async def test_chains_keep_order_at_fifo_level(tmp_path, transport):
    """FIFO level: each chain in chain order; heads go by stream id, so no sharing."""
    sids = await SENDERS[transport](str(tmp_path), TWO_CHAINS_ODD)
    _check_chain_order(await _first_sends(str(tmp_path), sids),
                       _chains(sids, TWO_CHAINS_ODD))


@pytest.mark.parametrize("transport", sorted(SENDERS))
@_queued
async def test_unchained_round_robin(tmp_path, transport):
    sids = await SENDERS[transport](str(tmp_path), UNCHAINED)
    _check_round_robin(await _first_sends(str(tmp_path), sids), sids)


@pytest.mark.parametrize("transport", sorted(SENDERS))
@_queued
async def test_unchained_fifo(tmp_path, transport):
    sids = await SENDERS[transport](str(tmp_path), UNCHAINED_ODD)
    _check_fifo(await _first_sends(str(tmp_path), sids), sids)


@_queued
async def test_quic_connection_set_stream_chain(tmp_path):
    """QuicConnection.set_stream_chain on open streams before their data."""
    qlog_dir = str(tmp_path)
    done = set()

    class _Receiver(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived) and event.end_stream:
                done.add(event.stream_id)

    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(ALPN),
                         create_protocol=_Receiver)
    try:
        with _sender_log():
            async with connect("127.0.0.1", port,
                               configuration=_client_cfg(ALPN, qlog_dir)) as client:
                quic = client._quic
                blocker = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(blocker, BLOCKER, end_stream=True, priority=0)
                sids = [quic.get_next_available_stream_id(is_unidirectional=True)
                        for _ in range(4)]
                # Chain order the reverse of stream id order.
                for i, sid in enumerate(sids):
                    assert quic.set_stream_priority(sid, 9) == 0
                    assert quic.set_stream_chain(sid, 1, len(sids) - i) == 0
                for sid in sids:
                    quic.send_stream_data(sid, bytes(STREAM_BYTES), end_stream=True)
                await _check_queued(quic, blocker)
                assert await _until(lambda: done >= {blocker, *sids}), done
                assert client._quic._transport.counters['set_chain_rejected'] == 0
    finally:
        server.close()
    _check_fifo(await _first_sends(qlog_dir, sids), sids[::-1])


@_queued
async def test_wt_set_stream_chain(tmp_path):
    """WebTransportSession.set_stream_chain moves an open stream into a chain."""
    qlog_dir = str(tmp_path)
    done = set()

    async def read(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                done.add(sid)
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
        key_file=KEY_FILE, configuration=_server_cfg())
    try:
        with _sender_log():
            async with connect_webtransport(
                    "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                    configuration=_client_cfg("h3", qlog_dir)) as wt:
                blocker = await wt.create_stream(bidir=False, initial=BLOCKER,
                                                 end_stream=True, priority=0)
                sids = [await wt.create_stream(bidir=False, priority=9)
                        for _ in range(4)]
                for i, sid in enumerate(sids):
                    assert wt.set_stream_chain(sid, 1, len(sids) - i) == 0
                for sid in sids:
                    wt.send_stream_data(sid, bytes(STREAM_BYTES), end_stream=True)
                await _check_queued(wt, blocker)
                assert await _until(lambda: done >= {blocker, *sids}), done
                assert wt._transport.counters['set_chain_rejected'] == 0
    finally:
        server.close()
    _check_fifo(await _first_sends(qlog_dir, sids), sids[::-1])
