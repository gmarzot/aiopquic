"""packet_train_mode reaches picoquic on raw QUIC and WebTransport, and a
transfer runs with it."""
import asyncio
import os

import pytest

from aiopquic._binding._transport import _packet_train_mode, _wt_packet_train_mode
from aiopquic.asyncio import QuicConnectionProtocol, connect, serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
PAYLOAD = 1 << 20


def test_configuration_default_off():
    assert QuicConfiguration().packet_train_mode is False


class _Sink(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = 0

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += len(event.data)


async def _until(cond, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("trains", [True, False])
async def test_quic_connection_carries_mode(trains):
    port = next_port()
    servers = []

    def _make(conn, stream_handler=None):
        proto = _Sink(conn, stream_handler=stream_handler)
        servers.append(proto)
        return proto

    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                             packet_train_mode=trains)
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE,
                             packet_train_mode=trains)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_make)
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            assert _packet_train_mode(client._quic._cnx_ptr) == int(trains)
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"x" * PAYLOAD, end_stream=True)
            await _until(lambda: servers and servers[0].received == PAYLOAD)
            assert _packet_train_mode(servers[0]._quic._cnx_ptr) == int(trains)
    finally:
        server.close()


async def test_webtransport_session_carries_mode():
    port = next_port()
    accepted = asyncio.get_running_loop().create_future()

    def handler(session):
        if not accepted.done():
            accepted.set_result(session)

    scfg = QuicConfiguration(is_client=False, packet_train_mode=True)
    ccfg = QuicConfiguration(is_client=True, packet_train_mode=True)
    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE,
                                      configuration=scfg)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE,
                                        configuration=ccfg) as session:
            peer = await asyncio.wait_for(accepted, 3.0)
            assert _wt_packet_train_mode(session._state) == 1
            assert _wt_packet_train_mode(peer._state) == 1
    finally:
        server.close()
