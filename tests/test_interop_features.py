"""Retry, cipher suite, key update, QUIC v2, resumption, 0-RTT and IPv6 over loopback."""
import asyncio
import glob
import json
import os
import ssl

import pytest

from aiopquic._binding._transport import _ipv6_usable
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QUIC_V1, QUIC_V2, QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, HandshakeCompleted, StreamDataReceived,
)

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
ALPN = "hq-interop"
REPLY = 400 * 1024

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


class Server(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.end_stream:
            asyncio.get_running_loop().create_task(self._reply(event.stream_id))

    async def _reply(self, stream_id):
        data, offset = b"r" * REPLY, 0
        while offset < len(data):
            chunk = data[offset:offset + 65536]
            try:
                self._quic.send_stream_data(stream_id, chunk,
                                            end_stream=offset + len(chunk) >= len(data))
            except BufferError:
                await asyncio.sleep(0.002)
                continue
            offset += len(chunk)


class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handshake = None
        self.received = 0
        self.done = asyncio.get_running_loop().create_future()

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            self.handshake = event
        elif isinstance(event, StreamDataReceived):
            self.received += len(event.data)
            if event.end_stream and not self.done.done():
                self.done.set_result(None)
        elif isinstance(event, ConnectionTerminated) and not self.done.done():
            self.done.set_exception(ConnectionError(f"closed {event.error_code:#x}"))


def _server_cfg(**kw):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN], **kw)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg(**kw):
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE, **kw)


async def _fetch(host, port, cfg, *, early=False):
    """One request; returns the client's HandshakeCompleted."""
    async with connect(host, port, configuration=cfg, create_protocol=Client,
                       wait_connected=not early) as client:
        sid = client._quic.get_next_available_stream_id()
        client._quic.send_stream_data(sid, b"GET /x\r\n", end_stream=True)
        await asyncio.wait_for(client.done, 10)
        assert client.received == REPLY
        if client.handshake is None:
            await client.wait_connected()
        return client.handshake


def _qlog_events(qlog_dir):
    for path in glob.glob(os.path.join(qlog_dir, "*.client.qlog")):
        for event in json.load(open(path))["traces"][0]["events"]:
            yield event[2], event[3]


def _packets(qlog_dir, name="packet_sent"):
    """(packet_type, header) of each client packet event called name."""
    return [(data.get("packet_type"), data.get("header", {}))
            for ev, data in _qlog_events(qlog_dir) if ev == name]


async def test_retry(tmp_path):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(),
                         create_protocol=Server, retry=True)
    try:
        await _fetch("127.0.0.1", port, _client_cfg(qlog_dir=str(tmp_path)))
    finally:
        server.close()
    received = [kind for kind, _ in _packets(str(tmp_path), "packet_received")]
    assert "retry" in received


@pytest.mark.parametrize("server_suite,ok", [(0x1303, True), (0x1301, False)])
async def test_cipher_suite(server_suite, ok):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(cipher_suite=server_suite),
                         create_protocol=Server)
    try:
        if ok:
            await _fetch("127.0.0.1", port, _client_cfg(cipher_suite=0x1303))
        else:
            with pytest.raises(ConnectionError):
                await _fetch("127.0.0.1", port, _client_cfg(cipher_suite=0x1303))
    finally:
        server.close()


def test_cipher_suite_and_version_validation():
    with pytest.raises(ValueError):
        QuicConfiguration(cipher_suite=0x1304)
    with pytest.raises(ValueError):
        QuicConfiguration(supported_versions=[0xff00001d])


async def test_key_update(tmp_path):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(key_update_interval=64),
                         create_protocol=Server)
    try:
        await _fetch("127.0.0.1", port,
                     _client_cfg(key_update_interval=64, qlog_dir=str(tmp_path)))
    finally:
        server.close()
    phases = {h["key_phase"] for _, h in _packets(str(tmp_path)) if "key_phase" in h}
    assert phases >= {0, 1}


async def test_quic_v2(tmp_path):
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg(), create_protocol=Server)
    try:
        await _fetch("127.0.0.1", port, _client_cfg(supported_versions=[QUIC_V2, QUIC_V1],
                                                    qlog_dir=str(tmp_path)))
    finally:
        server.close()
    # Client Initial in v1; the server answers in v2 (compatible negotiation).
    sent = {h["version"] for _, h in _packets(str(tmp_path)) if h.get("version")}
    received = {h["version"] for _, h in _packets(str(tmp_path), "packet_received")
                if h.get("version")}
    assert f"{QUIC_V1:08x}" in sent and f"{QUIC_V2:08x}" in received


async def test_resumption_and_zero_rtt(tmp_path):
    port = next_port()
    tickets = str(tmp_path / "tickets.bin")
    server = await serve("127.0.0.1", port, configuration=_server_cfg(), create_protocol=Server)
    try:
        first = await _fetch("127.0.0.1", port, _client_cfg(session_ticket_file=tickets))
        assert not first.session_resumed
        assert os.path.getsize(tickets) > 0
        qlog = tmp_path / "q"
        qlog.mkdir()
        second = await _fetch("127.0.0.1", port, _client_cfg(session_ticket_file=tickets,
                                                             qlog_dir=str(qlog)), early=True)
        assert second.session_resumed
    finally:
        server.close()
    sent = [kind for kind, _ in _packets(str(qlog))]
    assert "0RTT" in sent


@pytest.mark.skipif(not _ipv6_usable(), reason="no IPv6 on this host")
async def test_ipv6():
    port = next_port()
    server = await serve("::", port, configuration=_server_cfg(), create_protocol=Server)
    try:
        await _fetch("::1", port, _client_cfg())
        await _fetch("127.0.0.1", port, _client_cfg())
    finally:
        server.close()
