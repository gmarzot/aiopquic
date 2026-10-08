"""One handler raising must not drop the rest of a drained batch."""
import asyncio
import os

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.dispatch import _DualRouter
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportServerSession, WebTransportStreamDataReceived, _Dispatcher,
    _EVT_WT_STREAM_DATA, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicEngine, _EVT_CNX_STACK
from aiopquic.quic.events import StreamDataReceived

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = pytest.mark.asyncio


class _Transport:
    def __init__(self, batch):
        # Any selectable fd serves as the dispatcher's wake-up (macOS has no eventfd).
        self.eventfd, self._wake_w = os.pipe()
        self._batch = batch

    def drain_rx(self):
        batch, self._batch = self._batch, []
        return batch

    def close(self):
        os.close(self.eventfd)
        os.close(self._wake_w)


def _ev(evt_type, stream_id, cnx_ptr, session_ptr, flag=False):
    return (evt_type, stream_id, b"x", flag, 0, cnx_ptr, session_ptr, 0)


class _Session:
    def __init__(self, raise_on=None):
        self.seen = []
        self._raise_on = raise_on

    def _on_event(self, ev):
        self.seen.append(ev[1])
        if ev[1] == self._raise_on:
            raise KeyError("audio")


async def test_wt_dispatcher_keeps_draining_after_a_raising_handler():
    bad, good = _Session(raise_on=11), _Session()
    tr = _Transport([_ev(_EVT_WT_STREAM_DATA, 11, 7, 1),
                     _ev(_EVT_WT_STREAM_DATA, 15, 7, 1),
                     _ev(_EVT_WT_STREAM_DATA, 19, 7, 2)])
    d = _Dispatcher(asyncio.get_running_loop(), tr)
    d._sessions[1], d._sessions[2] = bad, good
    try:
        d._drain()
    finally:
        d.detach()
        tr.close()
    assert bad.seen == [11, 15]
    assert good.seen == [19]


async def test_dual_router_keeps_draining_after_a_raising_handler():
    class _Router:
        def __init__(self, raise_first=False):
            self.seen = []
            self._raise_first = raise_first

        def route_event(self, ev):
            self.seen.append(ev[1])
            if self._raise_first and len(self.seen) == 1:
                raise KeyError("audio")

    wt, engine = _Router(raise_first=True), _Router()
    tr = _Transport([_ev(_EVT_WT_STREAM_DATA, 11, 0, 1),  # transport-level: WT
                     _ev(_EVT_CNX_STACK, 0, 9, 0, flag=False),
                     _ev(7, 15, 9, 0)])
    d = _DualRouter(asyncio.get_running_loop(), tr, engine, wt)
    try:
        d._drain()
    finally:
        asyncio.get_running_loop().remove_reader(tr.eventfd)
        tr.close()
    assert wt.seen == [11]
    assert engine.seen == [15]
    assert d._is_h3[9] is False


async def test_engine_keeps_draining_after_a_raising_connection():
    class _Conn:
        def __init__(self):
            self.raw = []

        def _enqueue_raw(self, *fields):
            self.raw.append(fields)

    class _Proto:
        def __init__(self, raise_):
            self.calls = 0
            self._raise = raise_

        def _process_events(self):
            self.calls += 1
            if self._raise:
                raise KeyError("audio")

    engine = QuicEngine(configuration=QuicConfiguration(is_client=False),
                        create_protocol=lambda *a, **kw: None)
    tr = _Transport([_ev(7, 11, 1, 0), _ev(7, 15, 2, 0)])
    engine._transport = tr
    engine._connections[1], engine._protocols[1] = _Conn(), _Proto(True)
    engine._connections[2], engine._protocols[2] = _Conn(), _Proto(False)
    try:
        engine.drain_and_route()
    finally:
        tr.close()
    assert len(engine._connections[2].raw) == 1
    assert engine._protocols[2].calls == 1


async def test_client_loop_keeps_going_after_a_raising_handler():
    class _Quic:
        _connected = True

        def __init__(self, events):
            self._events = list(events)

        def next_event(self):
            return self._events.pop(0) if self._events else None

    class _Proto(QuicConnectionProtocol):
        def __init__(self, quic):
            super().__init__(quic)
            self.seen = []

        def quic_event_received(self, event):
            self.seen.append(event.stream_id)
            if event.stream_id == 11:
                raise KeyError("audio")

    proto = _Proto(_Quic([
        StreamDataReceived(data=b"x", end_stream=False, stream_id=11),
        StreamDataReceived(data=b"y", end_stream=False, stream_id=15)]))
    proto._process_events()
    assert proto.seen == [11, 15]


class _RaisingOnce(WebTransportServerSession):
    """A session whose first stream-data event raises, as an application
    parser running inside _on_event would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raised = False

    def _on_event(self, ev):
        if ev[0] == _EVT_WT_STREAM_DATA and not self._raised:
            self._raised = True
            raise KeyError("audio")
        super()._on_event(ev)


async def test_wt_session_outlives_a_raising_handler():
    port = next_port()
    got = asyncio.get_running_loop().create_future()

    async def handler(session):
        async def _reader(sid):
            async for ev in session.receive_stream_data(sid):
                if isinstance(ev, WebTransportStreamDataReceived) and ev.data:
                    if not got.done():
                        got.set_result(bytes(ev.data))
                    return
        async for ev in session.events():
            if hasattr(ev, "stream_id") and type(ev).__name__ == "WebTransportNewStream":
                asyncio.create_task(_reader(ev.stream_id))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE,
        session_factory=lambda transport, state: _RaisingOnce(transport, state))
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt",
                                        sni=SNI, ca_file=CA_FILE) as wt:
            first = await wt.create_stream(bidir=False)
            wt.send_stream_data(first, b"a" * 100)
            await asyncio.sleep(0.2)
            second = await wt.create_stream(bidir=False)
            wt.send_stream_data(second, b"b" * 100)
            assert await asyncio.wait_for(got, 5.0) == b"b" * 100
    finally:
        server.close()


async def test_raw_server_outlives_a_raising_handler():
    got = asyncio.get_running_loop().create_future()

    class _Proto(QuicConnectionProtocol):
        raised = False

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                if not _Proto.raised:
                    _Proto.raised = True
                    raise KeyError("audio")
                if not got.done():
                    got.set_result(bytes(event.data))

    port = next_port()
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=cfg,
                         create_protocol=lambda quic, **kw: _Proto(quic, **kw))
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"],
                                 server_name=SNI, cafile=CA_FILE)
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"a" * 100)
            await asyncio.sleep(0.2)
            sid2 = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid2, b"b" * 100)
            assert await asyncio.wait_for(got, 5.0) == b"b" * 100
    finally:
        server.close()
