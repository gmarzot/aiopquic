"""A close reaches the application once, says whether the peer closed at the
application or the transport level, and carries the peer's reason phrase."""
import asyncio
import logging
import os

import pytest

from aiopquic.asyncio import QuicConnectionProtocol, connect, serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import _TX_CLOSE
from aiopquic.quic.events import ConnectionTerminated, HandshakeCompleted

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
CONNECTION_REFUSED = 0x2
H3_NO_ERROR = 0x100


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE, idle_timeout=5)


class _Recorder(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.terminated = []

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self.terminated.append(event)


async def _until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def test_peer_application_close_is_one_event_with_its_reason():
    port = next_port()

    class Closer(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, HandshakeCompleted):
                self._quic.close(error_code=5, reason_phrase="bye")

    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Closer)
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=_Recorder) as client:
            await _until(lambda: client.terminated)
            # picoquic's final close callback follows the application close.
            await client.wait_closed()
            await asyncio.sleep(0.3)
            assert client.terminated == [ConnectionTerminated(
                error_code=5, frame_type=None, reason_phrase="bye")]
    finally:
        server.close()


async def test_our_close_reason_reaches_the_peer():
    port = next_port()
    servers = []

    def _make(conn, stream_handler=None):
        proto = _Recorder(conn, stream_handler=stream_handler)
        servers.append(proto)
        return proto

    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=_make)
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=_Recorder) as client:
            await _until(lambda: servers)
            client._quic.close(error_code=9, reason_phrase="client-bye")
            await _until(lambda: servers[0].terminated)
            assert servers[0].terminated == [ConnectionTerminated(
                error_code=9, frame_type=None, reason_phrase="client-bye")]
            # Our own close also ends in exactly one event.
            await _until(lambda: client.terminated)
            await asyncio.sleep(0.3)
            assert len(client.terminated) == 1
    finally:
        server.close()


async def test_refused_connection_is_a_transport_close():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    transport = server._engine._transport
    try:
        assert transport.set_max_connections(0) == 0
        await _until(lambda: not transport.counters["tx_event_ring_count_now"])
        async with connect("127.0.0.1", port, configuration=_client_cfg(),
                           create_protocol=_Recorder,
                           wait_connected=False) as client:
            await _until(lambda: client.terminated)
            await asyncio.sleep(0.3)
            assert [(t.error_code, t.frame_type) for t in client.terminated] == [
                (CONNECTION_REFUSED, 0)]
    finally:
        server.close()


async def _wt_session_closed(handler, client_cfg=None, timeout=5.0):
    """The client's WebTransportSessionClosed after the server handler runs."""
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport(
                "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                configuration=client_cfg) as wt:
            await wt.wait_closed(timeout=timeout)
            return wt._session_close_event
    finally:
        server.close()


async def test_wt_session_close_is_not_a_connection_close():
    async def handler(session):
        await asyncio.sleep(0.1)
        session.close(7, b"session-bye")

    ev = await _wt_session_closed(handler)
    assert (ev.error_code, bytes(ev.reason), ev.connection_closed,
            ev.frame_type) == (7, b"session-bye", False, None)


async def test_wt_quic_application_close_carries_kind_and_reason():
    async def handler(session):
        await asyncio.sleep(0.1)
        session._transport.push_tx_event(
            _TX_CLOSE, 0, data=b"h3-bye", error_code=H3_NO_ERROR,
            cnx_ptr=session.cnx_ptr)
        session._transport.wake_up()

    ev = await _wt_session_closed(handler)
    assert (ev.error_code, bytes(ev.reason), ev.connection_closed,
            ev.frame_type) == (H3_NO_ERROR, b"h3-bye", True, None)


async def test_wt_idle_timeout_is_a_transport_close():
    cfg = QuicConfiguration(is_client=True, idle_timeout=1)
    ev = await _wt_session_closed(lambda session: None, client_cfg=cfg,
                                  timeout=10.0)
    assert (ev.error_code, ev.connection_closed, ev.frame_type) == (0, True, 0)


def _aiopquic_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("aiopquic") and r.levelno >= logging.WARNING]


async def test_quic_closes_log_no_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="aiopquic")
    await test_peer_application_close_is_one_event_with_its_reason()
    await test_our_close_reason_reaches_the_peer()
    await test_refused_connection_is_a_transport_close()
    assert any(r.getMessage() == "connection closed (app): error=5"
               for r in caplog.records)
    assert any(r.getMessage() == "connection closed (transport): error=2"
               for r in caplog.records)
    assert _aiopquic_warnings(caplog) == []


async def test_wt_closes_log_no_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="aiopquic")
    await test_wt_session_close_is_not_a_connection_close()
    await test_wt_quic_application_close_carries_kind_and_reason()
    assert _aiopquic_warnings(caplog) == []
