"""congestion_control_options reaches picoquic's connections on raw QUIC and
WebTransport, and a transfer runs with it."""
import asyncio
import os

import pytest

from aiopquic._binding._transport import (
    TransportContext, _cc_options, _wt_cc_options,
)
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
OPTS = "Q0.02"
PAYLOAD = 1 << 20


@pytest.mark.parametrize("kwargs", [
    {"congestion_control_algorithm": None, "congestion_control_options": OPTS},
    {"congestion_control_options": 5},
    {"congestion_control_options": "Q\0"},
])
def test_configuration_rejects_bad_options(kwargs):
    with pytest.raises(ValueError):
        QuicConfiguration(**kwargs)


def test_start_rejects_options_without_algorithm():
    ctx = TransportContext()
    with pytest.raises(ValueError):
        ctx.start(alpn="h3", congestion_control_options=OPTS)
    assert not ctx.started


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


@pytest.mark.parametrize("opts", [OPTS, None])
async def test_quic_connection_carries_options(opts):
    port = next_port()
    servers = []

    def _make(conn, stream_handler=None):
        proto = _Sink(conn, stream_handler=stream_handler)
        servers.append(proto)
        return proto

    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                             congestion_control_options=opts)
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE,
                             congestion_control_options=opts)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_make)
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            assert _cc_options(client._quic._cnx_ptr) == opts
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"x" * PAYLOAD, end_stream=True)
            await _until(lambda: servers and servers[0].received == PAYLOAD)
            assert _cc_options(servers[0]._quic._cnx_ptr) == opts
    finally:
        server.close()


async def test_webtransport_session_carries_options():
    port = next_port()
    accepted = asyncio.get_running_loop().create_future()

    def handler(session):
        if not accepted.done():
            accepted.set_result(session)

    scfg = QuicConfiguration(is_client=False, congestion_control_options=OPTS)
    ccfg = QuicConfiguration(is_client=True, congestion_control_options=OPTS)
    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE,
                                      configuration=scfg)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE,
                                        configuration=ccfg) as session:
            peer = await asyncio.wait_for(accepted, 3.0)
            assert _wt_cc_options(session._state) == OPTS
            assert _wt_cc_options(peer._state) == OPTS
    finally:
        server.close()
