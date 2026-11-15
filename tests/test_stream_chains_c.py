"""Stream chains on stock picoquic: a chain's active stream hands the chain on
when it drains, ends or resets, so a stalled or reset stream does not hold
the rest of its chain."""

import asyncio

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
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, _client_cfg, _server_cfg,
        _until, next_port,
    )
except ImportError:      # loaded bare, outside the package
    from test_stream_chain import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, _client_cfg, _server_cfg,
        _until, next_port,
    )

pytestmark = pytest.mark.asyncio

PART = b"\x01" * 4096


async def _quic_pair(tmp_path):
    """A raw QUIC server recording ended and reset streams, and its port."""
    ended, reset = set(), set()

    class _Receiver(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived) and event.end_stream:
                ended.add(event.stream_id)
            elif isinstance(event, StreamReset):
                reset.add(event.stream_id)

    port = next_port()
    server = await serve("127.0.0.1", port,
                         configuration=_server_cfg(str(tmp_path), ALPN),
                         create_protocol=_Receiver)
    return server, port, ended, reset


@pytest.mark.parametrize("level", [8, 9])
async def test_quic_stalled_stream_does_not_hold_its_chain(tmp_path, level):
    server, port, ended, _ = await _quic_pair(tmp_path)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN)) as client:
            quic = client._quic
            a = quic.get_next_available_stream_id(is_unidirectional=True)
            b = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(a, PART, priority=level, chain=1, order=0)
            quic.send_stream_data(b, PART, end_stream=True, priority=level,
                                  chain=1, order=1)
            assert await _until(lambda: b in ended), ended
            assert a not in ended
            quic.send_stream_data(a, PART, end_stream=True)
            assert await _until(lambda: a in ended), ended
            c = quic._transport.counters
            assert c['chain_promotes'] >= 1, c
    finally:
        server.close()


async def test_quic_reset_stream_hands_its_chain_on(tmp_path):
    server, port, ended, reset = await _quic_pair(tmp_path)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_cfg(ALPN)) as client:
            quic = client._quic
            blocker = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(blocker, b"\x00" * (2 * 1024 * 1024),
                                  end_stream=True, priority=0)
            a = quic.get_next_available_stream_id(is_unidirectional=True)
            b = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(a, PART * 64, priority=8, chain=1, order=0)
            quic.send_stream_data(b, PART, end_stream=True, priority=8,
                                  chain=1, order=1)
            quic.reset_stream(a, 7)
            assert await _until(lambda: b in ended), (ended, reset)
            c = quic._transport.counters
            assert c['chain_holds'] >= 1, c
    finally:
        server.close()


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
        key_file=KEY_FILE, configuration=_server_cfg(str(tmp_path)))
    try:
        async with connect_webtransport(
                "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                configuration=_client_cfg("h3")) as wt:
            a = await wt.create_stream(bidir=False, initial=PART, priority=8,
                                       chain=1, order=0)
            b = await wt.create_stream(bidir=False, initial=PART,
                                       end_stream=True, priority=8,
                                       chain=1, order=1)
            assert await _until(lambda: b in ended), ended
            assert a not in ended
            wt.send_stream_data(a, PART, end_stream=True)
            assert await _until(lambda: a in ended), ended
            c = wt._transport.counters
            assert c['chain_promotes'] >= 1, c
    finally:
        server.close()
