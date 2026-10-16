"""Reset and stopped streams release their contexts mid-connection.

picoquic detaches a stream's app context when we reset it or stop
reading it; without a re-attach the stream is never reported released,
and its stream context (raw QUIC) or h3zero stream, link and stream
context (WebTransport) stay until the connection or session ends.
"""
import asyncio
import gc
import os
import time

import pytest

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportSession, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    StopSendingReceived, StreamReset,
    WebTransportNewStream, WebTransportStopSending, WebTransportStreamReset,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)

N = 20
PAYLOAD = b"r" * (64 * 1024)


async def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _server_config():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config():
    return QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"],
                             server_name=SNI, cafile=CA_FILE)


@pytest.mark.asyncio
async def test_raw_reset_churn_releases_stream_contexts():
    resets = []

    class Server(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamReset):
                resets.append(event.stream_id)

    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_config(),
                         create_protocol=Server)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            for _ in range(N):
                sid = quic.get_next_available_stream_id(is_unidirectional=True)
                quic.send_stream_data(sid, PAYLOAD)
                quic.reset_stream(sid, 0x10)
            assert await _until(lambda: len(resets) == N), resets
            assert await _until(lambda: not quic._stream_ctxs), (
                f"{len(quic._stream_ctxs)} of {N} reset streams kept their "
                f"stream context")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_raw_stop_sending_releases_stream_contexts():
    class Server(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StopSendingReceived):
                self._quic.reset_stream(event.stream_id, 0x11)

    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_config(),
                         create_protocol=Server)
    try:
        async with connect("127.0.0.1", port,
                           configuration=_client_config()) as client:
            quic = client._quic
            for _ in range(N):
                sid = quic.get_next_available_stream_id()
                quic.send_stream_data(sid, b"x", end_stream=True)
                quic.stop_stream(sid, 0x10)
            assert await _until(lambda: not quic._stream_ctxs), (
                f"{len(quic._stream_ctxs)} of {N} stopped streams kept their "
                f"stream context")
    finally:
        server.close()


async def _wt_churn(stop):
    """N client streams reset, or stopped once the server has answered on
    them (the server resets back); the client's links and the process-wide
    stream contexts must return to baseline."""
    async def handler(session):
        async def _recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    if stop:
                        session.send_stream_data(ev.stream_id, b"y")
                    asyncio.create_task(_drain(session, ev.stream_id))
                elif isinstance(ev, WebTransportStopSending):
                    session.reset_stream(ev.stream_id, 0x11)
        asyncio.create_task(_recv())

    async def _drain(session, sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamReset):
                return

    port = next_port()
    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as wt:
            gc.collect()
            baseline = wt._transport.counters["sc_alive_total"]
            for _ in range(N):
                sid = await wt.create_stream(bidir=stop)
                if stop:
                    wt.send_stream_data(sid, b"x", end_stream=True)
                    reader = wt.receive_stream_data(sid)
                    await asyncio.wait_for(reader.__anext__(), timeout=5.0)
                    await reader.aclose()
                    wt.stop_stream(sid, 0x10)
                else:
                    wt.send_stream_data(sid, PAYLOAD)
                    wt.reset_stream(sid, 0x10)

            def settled():
                gc.collect()
                c = wt._transport.counters
                return (c["sc_destroy_wt_link_callback_free"] >= N
                        and c["sc_alive_total"] == baseline)
            assert await _until(settled), (
                f"{N} streams, baseline sc_alive_total={baseline}: "
                f"{wt._transport.counters}")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_reset_churn_releases_streams():
    await _wt_churn(stop=False)


@pytest.mark.asyncio
async def test_wt_stop_sending_releases_streams():
    await _wt_churn(stop=True)


@pytest.mark.asyncio
async def test_wt_data_after_close_is_dropped():
    from aiopquic.asyncio.webtransport import (
        _EVT_WT_STREAM_DATA, _EVT_WT_STREAM_FIN, _EVT_WT_STREAM_RESET,
    )
    session = WebTransportSession(TransportContext(), "client")
    session._session_closed.set()
    for evt, sid in ((_EVT_WT_STREAM_DATA, 4), (_EVT_WT_STREAM_FIN, 8),
                     (_EVT_WT_STREAM_RESET, 12)):
        session._on_event((evt, sid, memoryview(b"late"), 0, 0, 0, 0, 0))
    assert session._stream_inbox == {}
