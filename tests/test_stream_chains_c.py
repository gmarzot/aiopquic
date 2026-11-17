"""Stream chains on stock picoquic: a chain's active stream hands the chain on
when it drains, ends or resets, so a stalled or reset stream does not hold
the rest of its chain; a stream takes the latest chain set on it; a chain
keeps its turn at a round robin level; a session's streams leave their
chains when it closes; and a stream beyond the peer's stream limit does
not hold the streams that can send."""

import asyncio
import contextlib

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.events import (
    StreamDataReceived, StreamReset, WebTransportNewStream,
    WebTransportStreamDataReceived,
)

try:
    from .test_stream_chain import (
        ALPN, BLOCKER, CA_FILE, CERT_FILE, KEY_FILE, SENDERS, SNI,
        _chains, _check_chain_order, _check_queued, _check_sharing,
        _client_cfg, _first_sends, _queued, _sender_log, _server_cfg, _spans,
        _until, next_port,
    )
except ImportError:      # loaded bare, outside the package
    from test_stream_chain import (
        ALPN, BLOCKER, CA_FILE, CERT_FILE, KEY_FILE, SENDERS, SNI,
        _chains, _check_chain_order, _check_queued, _check_sharing,
        _client_cfg, _first_sends, _queued, _sender_log, _server_cfg, _spans,
        _until, next_port,
    )

pytestmark = pytest.mark.asyncio

PART = b"\x01" * 4096
BULK = PART * 128
SMALL = 256


async def _quic_pair(tmp_path, max_streams_uni=None):
    """A raw QUIC server recording ended streams, in order, and reset
    streams, and its port."""
    ended, reset = [], set()

    class _Receiver(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived) and event.end_stream:
                ended.append(event.stream_id)
            elif isinstance(event, StreamReset):
                reset.add(event.stream_id)

    cfg = _server_cfg(ALPN)
    if max_streams_uni is not None:
        cfg.max_streams_uni = max_streams_uni
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=cfg,
                         create_protocol=_Receiver)
    return server, port, ended, reset


def _uni(quic):
    return quic.get_next_available_stream_id(is_unidirectional=True)


@contextlib.asynccontextmanager
async def _quic_sender(port, qlog_dir):
    """A raw QUIC client connection whose qlog holds every packet it sends."""
    with _sender_log():
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN, qlog_dir)) as client:
            yield client._quic


def _check_turns(sends, chains, most=3):
    """While every chain has streams to start, none starts more than `most`
    in a row."""
    left = {sid: i for i, sids in enumerate(chains) for sid in sids}
    unstarted = [len(sids) for sids in chains]
    prev, run = None, 0
    for sid, _ in sends:
        chain = left.pop(sid, None)
        if chain is None:
            continue
        if 0 in unstarted:
            return
        run = run + 1 if chain == prev else 1
        prev = chain
        unstarted[chain] -= 1
        assert run <= most, f"chain {chains[chain]} started {run} in a row"


@pytest.mark.parametrize("level", [8, 9])
@_queued
async def test_quic_stalled_stream_does_not_hold_its_chain(tmp_path, level):
    server, port, ended, _ = await _quic_pair(tmp_path)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN)) as client:
            quic = client._quic
            # Queue both behind a blocker, so b is held when a drains.
            blocker = _uni(quic)
            quic.send_stream_data(blocker, BLOCKER, end_stream=True,
                                  priority=0)
            a = quic.get_next_available_stream_id(is_unidirectional=True)
            b = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(a, PART, priority=level, chain=1, order=0)
            quic.send_stream_data(b, PART, end_stream=True, priority=level,
                                  chain=1, order=1)
            await _check_queued(quic, blocker)
            assert await _until(lambda: b in ended), ended
            assert a not in ended
            quic.send_stream_data(a, PART, end_stream=True)
            assert await _until(lambda: a in ended), ended
            c = quic._transport.counters
            assert c['chain_promotes'] >= 1, c
    finally:
        server.close()


@_queued
async def test_quic_reset_stream_hands_its_chain_on(tmp_path):
    server, port, ended, reset = await _quic_pair(tmp_path)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN)) as client:
            quic = client._quic
            blocker = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(blocker, BLOCKER, end_stream=True,
                                  priority=0)
            a = quic.get_next_available_stream_id(is_unidirectional=True)
            b = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(a, PART * 64, priority=8, chain=1, order=0)
            quic.send_stream_data(b, PART, end_stream=True, priority=8,
                                  chain=1, order=1)
            await _check_queued(quic, blocker)
            quic.reset_stream(a, 7)
            assert await _until(lambda: b in ended), (ended, reset)
            c = quic._transport.counters
            assert c['chain_holds'] >= 1, c
    finally:
        server.close()


@_queued
async def test_wt_stalled_stream_does_not_hold_its_chain(tmp_path):
    ended = set()

    async def read(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                ended.add(sid)
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
        async with connect_webtransport(
                "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                configuration=_client_cfg("h3")) as wt:
            blocker = await wt.create_stream(bidir=False, initial=BLOCKER,
                                             end_stream=True, priority=0)
            a = await wt.create_stream(bidir=False, initial=PART, priority=8,
                                       chain=1, order=0)
            b = await wt.create_stream(bidir=False, initial=PART,
                                       end_stream=True, priority=8,
                                       chain=1, order=1)
            await _check_queued(wt, blocker)
            assert await _until(lambda: b in ended), ended
            assert a not in ended
            wt.send_stream_data(a, PART, end_stream=True)
            assert await _until(lambda: a in ended), ended
            c = wt._transport.counters
            assert c['chain_promotes'] >= 1, c
    finally:
        server.close()


@_queued
async def test_quic_held_stream_takes_a_new_chain(tmp_path):
    """A stream held in its chain since its first write moves to the chain
    set on it later."""
    server, port, ended, _ = await _quic_pair(tmp_path)
    try:
        async with _quic_sender(port, str(tmp_path)) as quic:
            blocker = _uni(quic)
            quic.send_stream_data(blocker, BLOCKER, end_stream=True,
                                  priority=0)
            a, b = _uni(quic), _uni(quic)
            quic.send_stream_data(a, BULK, end_stream=True, priority=8,
                                  chain=1, order=0)
            quic.send_stream_data(b, PART, end_stream=True, priority=8,
                                  chain=1, order=1)
            assert quic.set_stream_chain(b, 2, 0) == 0
            await _check_queued(quic, blocker)
            assert await _until(lambda: a in ended and b in ended), ended
            c = quic._transport.counters
            assert c['set_chain_applied'] == 3, c
            assert c['set_chain_rejected'] == 0, c
    finally:
        server.close()
    spans = _spans(await _first_sends(str(tmp_path), [a, b],
                                      size={a: len(BULK), b: len(PART)}))
    assert spans[b][1] < spans[a][1], spans


@_queued
async def test_quic_first_write_chain_wins_over_an_earlier_set(tmp_path):
    """The chain of a stream's first write replaces one set before it."""
    server, port, ended, _ = await _quic_pair(tmp_path)
    try:
        async with _quic_sender(port, str(tmp_path)) as quic:
            blocker = _uni(quic)
            quic.send_stream_data(blocker, BLOCKER, end_stream=True,
                                  priority=0)
            a, b = _uni(quic), _uni(quic)
            quic.send_stream_data(a, BULK, end_stream=True, priority=8,
                                  chain=1, order=0)
            assert quic.set_stream_chain(b, 1, 1) == 0
            quic.send_stream_data(b, PART, end_stream=True, priority=8,
                                  chain=2, order=0)
            await _check_queued(quic, blocker)
            assert await _until(lambda: a in ended and b in ended), ended
    finally:
        server.close()
    spans = _spans(await _first_sends(str(tmp_path), [a, b],
                                      size={a: len(BULK), b: len(PART)}))
    assert spans[b][1] < spans[a][1], spans


@pytest.mark.parametrize("transport", sorted(SENDERS))
@_queued
async def test_chains_of_small_streams_share_a_level(tmp_path, transport):
    """Two chains of one-packet streams at a round robin level, the second
    opened after the first, take turns."""
    streams = [(8, 1, i) for i in range(12)] + [(8, 2, i) for i in range(12)]
    sids = await SENDERS[transport](str(tmp_path), streams, size=SMALL)
    sends = await _first_sends(str(tmp_path), sids, size=SMALL)
    chains = _chains(sids, streams)
    _check_chain_order(sends, chains)
    _check_sharing(sends, chains, size=SMALL)
    _check_turns(sends, chains)


async def test_wt_session_close_takes_its_streams_out(tmp_path):
    """A closing session's streams leave their chains before the
    connection goes."""
    ended = set()

    async def read(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived) and ev.end_stream:
                ended.add(sid)
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
        async with connect_webtransport(
                "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                configuration=_client_cfg("h3")) as wt:
            await wt.create_stream(bidir=False, initial=PART, priority=8,
                                   chain=1, order=0)
            b = await wt.create_stream(bidir=False, initial=PART,
                                       end_stream=True, priority=8,
                                       chain=1, order=1)
            assert await _until(lambda: b in ended), ended
            transport = wt._transport
        c = transport.counters
        assert c['chain_drops'] == 0, c
    finally:
        server.close()


@_queued
async def test_quic_stream_beyond_the_peer_limit_does_not_hold_its_chain(
        tmp_path):
    """Streams that rank first in their chain but exceed the peer's stream
    limit wait for the ones that can send."""
    limit = 4
    server, port, ended, _ = await _quic_pair(tmp_path, max_streams_uni=limit)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN)) as client:
            quic = client._quic
            blocker = quic.get_next_available_stream_id()
            quic.send_stream_data(blocker, BLOCKER, end_stream=True,
                                  priority=0)
            sids = [_uni(quic) for _ in range(2 * limit)]
            for i, sid in enumerate(sids):
                quic.send_stream_data(sid, PART, end_stream=True, priority=8,
                                      chain=1, order=100 - i)
            await _check_queued(quic, blocker)
            assert await _until(lambda: set(sids) <= set(ended), 10.0), ended
    finally:
        server.close()
