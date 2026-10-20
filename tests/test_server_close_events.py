"""A server makes one protocol per connection, whichever side closes it.

picoquic reports a client's close to the server as application_close, then
close; the engine must not take the second for a new connection."""
import asyncio
import os

import pytest

import aiopquic.quic.connection as qc
from aiopquic.asyncio import connect
from aiopquic.asyncio.dispatch import serve_dispatch
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import ConnectionTerminated

from ._ports import next_port

CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE), reason="picoquic certs not found")


class _Recorder:
    """Protocols the server makes, and the close events its engine routes."""

    def __init__(self, monkeypatch):
        self.protocols = []
        self.closes = {}
        self.changed = asyncio.Event()
        route = qc.QuicEngine.route_event
        recorder = self

        def traced(engine, ev):
            if ev[0] in (qc._EVT_CLOSE, qc._EVT_APP_CLOSE):
                recorder.closes.setdefault(ev[5], []).append(ev[0])
            route(engine, ev)
            recorder.changed.set()

        monkeypatch.setattr(qc.QuicEngine, "route_event", traced)

    def protocol(self, *args, **kwargs):
        recorder = self

        class Server(QuicConnectionProtocol):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.terminated = 0
                self.cnx_ptr = self._quic._cnx_ptr  # cleared once the connection closes
                recorder.protocols.append(self)

            def quic_event_received(self, event):
                if isinstance(event, ConnectionTerminated):
                    self.terminated += 1

        return Server(*args, **kwargs)

    async def until(self, cond, timeout=10.0):
        async with asyncio.timeout(timeout):
            while not cond():
                self.changed.clear()
                await self.changed.wait()


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN], server_name=SNI, cafile=CA_FILE)


async def _serve(kind, port, rec):
    if kind == "quic":
        return await serve("127.0.0.1", port, configuration=_server_cfg(),
                           create_protocol=rec.protocol)
    return await serve_dispatch("127.0.0.1", port, configuration=_server_cfg(),
                                create_protocol=rec.protocol, wt_path="/wt",
                                wt_handler=lambda session: None)


@pytest.mark.parametrize("kind", ["quic", "dispatch"])
@pytest.mark.parametrize("closer", ["client", "server"])
async def test_one_server_protocol_per_connection(monkeypatch, kind, closer):
    rec = _Recorder(monkeypatch)
    port = next_port()
    server = await _serve(kind, port, rec)
    try:
        async with connect("127.0.0.1", port, configuration=_client_cfg()) as client:
            await rec.until(lambda: rec.protocols)
            sid = client._quic.get_next_available_stream_id()
            client._quic.send_stream_data(sid, b"x" * 1000, end_stream=True)
            if closer == "server":
                rec.protocols[0].close()
        ptr = rec.protocols[0].cnx_ptr
        # A client's close arrives as application_close, then close.
        want = 2 if closer == "client" else 1
        await rec.until(lambda: len(rec.closes.get(ptr, [])) >= want)
        assert len(rec.protocols) == 1
        assert rec.protocols[0].terminated == 1
    finally:
        server.close()
