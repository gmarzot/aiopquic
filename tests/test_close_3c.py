"""Session and connection close reach the wire, both directions.

draft-ietf-webtrans-http3 §6: a closing endpoint sends the close capsule
and FINs CONNECT, the peer answers with its own FIN, and a client with
nothing left on the connection closes it. The server's qlog is the
witness where the wire matters.
"""
import asyncio
import glob
import itertools
import json
import os
import re
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportError, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import ConnectionTerminated, HandshakeCompleted

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.path.exists(CA_FILE),
                       reason="picoquic certs not found"),
]


def _server_events(qlog_dir):
    """(ms, name, frame) for every stream-0 and close frame in the
    server's qlog, in arrival order."""
    [path] = glob.glob(os.path.join(qlog_dir, "*.server.qlog"))
    with open(path) as f:
        text = f.read()
    # picoquic writes this transport parameter without its opening brace.
    text = re.sub(r'"version_negotiation": ("chosen": [^}]*})',
                  r'"version_negotiation": {\1', text)
    events = json.loads(text)["traces"][0]["events"]
    t0 = events[0][0]
    out = []
    for t, _cat, name, data in events:
        if name not in ("packet_received", "packet_sent"):
            continue
        for fr in data.get("frames", []):
            ft = fr.get("frame_type")
            if (ft == "stream" and fr.get("id") == 0) or ft in (
                    "connection_close", "application_close"):
                out.append(((t - t0) / 1000.0, name, fr))
    return out


async def _serve(port, handler, qlog_dir=None):
    cfg = QuicConfiguration(is_client=False, qlog_dir=qlog_dir)
    return await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE, configuration=cfg)


def _connect(port, **kw):
    return connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                ca_file=CA_FILE, timeout=5.0, **kw)


async def test_client_exit_fins_connect_before_closing_the_connection(tmp_path):
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await _serve(port, handler, qlog_dir=str(tmp_path))
    try:
        async with _connect(port) as wt:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
    finally:
        server.close()
    await asyncio.sleep(0.2)
    events = _server_events(tmp_path)
    fin_at = next(ms for ms, name, fr in events
                  if name == "packet_received" and fr.get("fin"))
    close_at = next(ms for ms, name, fr in events
                    if name == "packet_received"
                    and fr["frame_type"] == "connection_close")
    assert fin_at < close_at, events
    assert close_at - fin_at < 1000, events


async def test_server_close_reaches_the_client_with_code_and_reason():
    port = next_port()

    async def handler(session):
        await asyncio.sleep(0.1)
        session.close(0x2a, b"bye")

    server = await _serve(port, handler)
    try:
        async with _connect(port) as wt:
            t0 = time.monotonic()
            await asyncio.wait_for(wt.wait_closed(), 5)
            ev = wt._session_close_event
            assert (ev.error_code, bytes(ev.reason)) == (0x2a, b"bye")
            await asyncio.wait_for(wt._cnx_closed.wait(), 1.0)
            assert time.monotonic() - t0 < 1.5
    finally:
        server.close()


async def test_client_close_reaches_the_server_with_code_and_reason():
    port = next_port()
    seen = {}

    async def handler(session):
        t0 = time.monotonic()
        await session.wait_closed()
        seen["ms"] = (time.monotonic() - t0) * 1000
        ev = session._session_close_event
        seen["code"] = (ev.error_code, bytes(ev.reason))

    server = await _serve(port, handler)
    try:
        async with _connect(port) as wt:
            await asyncio.sleep(0.1)
            t0 = time.monotonic()
            await wt.aclose(0x2a, b"bye")
            assert wt._cnx_closed.is_set()
            assert time.monotonic() - t0 < 1.0
        for _ in range(100):
            if "code" in seen:
                break
            await asyncio.sleep(0.01)
    finally:
        server.close()
    assert seen.get("code") == (0x2a, b"bye")
    assert seen["ms"] < 1000


async def test_client_answers_a_server_close_with_its_fin(tmp_path):
    port = next_port()

    async def handler(session):
        await asyncio.sleep(0.1)
        session.close(0, b"")
        await session.wait_closed()

    server = await _serve(port, handler, qlog_dir=str(tmp_path))
    try:
        async with _connect(port) as wt:
            await asyncio.wait_for(wt.wait_closed(), 5)
            await asyncio.wait_for(wt._cnx_closed.wait(), 1.0)
        await asyncio.sleep(0.2)
    finally:
        server.close()
    await asyncio.sleep(0.2)
    events = _server_events(tmp_path)
    sent_fin = next(ms for ms, name, fr in events
                    if name == "packet_sent" and fr.get("fin"))
    got_fin = next(ms for ms, name, fr in events
                   if name == "packet_received" and fr.get("fin"))
    assert sent_fin < got_fin, events


async def test_server_aclose_closes_live_sessions():
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await _serve(port, handler)
    async with _connect(port) as wt:
        await asyncio.sleep(0.1)
        await server.aclose()
        await asyncio.wait_for(wt._cnx_closed.wait(), 1.0)
        assert wt.session_closed


async def test_closed_server_sessions_leave_the_dispatcher():
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await _serve(port, handler)
    try:
        for _ in range(5):
            async with _connect(port) as wt:
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert len(server._dispatcher._sessions) == 0
    finally:
        server.close()


async def test_aclose_is_bounded_when_the_peer_vanished():
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await _serve(port, handler)
    async with _connect(port) as wt:
        await asyncio.sleep(0.1)
        server.close()  # the transport goes away without any session close
        t0 = time.monotonic()
        await wt.aclose(0, b"", timeout=1.5)
        assert time.monotonic() - t0 < 3.0


async def test_a_refused_open_returns_promptly():
    port = next_port()
    t0 = time.monotonic()
    with pytest.raises(WebTransportError):
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE, timeout=1.0):
            pass
    assert time.monotonic() - t0 < 4.0


class _Terminations(QuicConnectionProtocol):
    """Server-side connections that completed a handshake, and those
    that saw their close; aborted attempts count for neither."""
    counter = itertools.count()
    completed: set = set()
    seen: set = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._n = next(_Terminations.counter)

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            _Terminations.completed.add(self._n)
        if isinstance(event, ConnectionTerminated):
            _Terminations.seen.add(self._n)


async def test_raw_connect_exit_delivers_the_close_every_time():
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_Terminations)
    _Terminations.completed = set()
    _Terminations.seen = set()
    try:
        for _ in range(20):
            ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                     server_name=SNI, cafile=CA_FILE)
            async with connect("127.0.0.1", port, configuration=ccfg):
                await asyncio.sleep(0.02)
        for _ in range(100):
            if _Terminations.completed <= _Terminations.seen and len(_Terminations.completed) >= 20:
                break
            await asyncio.sleep(0.01)
    finally:
        server.close()
    assert len(_Terminations.completed) >= 20
    assert _Terminations.completed <= _Terminations.seen


class _FullRingOnce:
    """Delegates to the real session state; the first close push finds a
    full TX ring."""

    def __init__(self, state):
        self._state = state
        self.close_calls = 0

    def push_close(self, error_code, reason):
        self.close_calls += 1
        if self.close_calls == 1:
            raise BufferError("TX ring full (WT_CLOSE)")
        self._state.push_close(error_code, reason)

    def __getattr__(self, name):
        return getattr(self._state, name)


async def test_close_on_a_full_tx_ring_is_retried_not_lost():
    port = next_port()
    seen = {}

    async def handler(session):
        await session.wait_closed()
        ev = session._session_close_event
        seen["code"] = (ev.error_code, bytes(ev.reason))

    server = await _serve(port, handler)
    try:
        async with _connect(port) as wt:
            await asyncio.sleep(0.1)
            proxy = _FullRingOnce(wt._state)
            wt._state = proxy
            wt.close(0x2a, b"bye")
            assert wt.session_closed
            assert proxy.close_calls == 1
            for _ in range(300):
                if "code" in seen:
                    break
                await asyncio.sleep(0.01)
            assert proxy.close_calls == 2
    finally:
        server.close()
    assert seen.get("code") == (0x2a, b"bye")
