"""An owed datagram MARK is re-posted when the TX event ring drains.

The first MARK submission is replaced by the ring-full result: the record
is committed but the worker was never told. Record admission, the drain
signal, the packet loop and the peer are real.
"""
import asyncio
import os

import pytest

from aiopquic.asyncio import QuicConnectionProtocol, connect, serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import DatagramFrameReceived, WebTransportDatagramReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"


class MarkRefusedOnce:
    """Proxy whose first MARK reports a full TX event ring."""

    def __init__(self, inner):
        self.inner = inner
        self.once = True

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def dgram_send(self, cnx, ring, data):
        if self.once:
            self.once = False
            assert self.inner.dgram_push(ring, data) > 0
            return 2
        return self.inner.dgram_send(cnx, ring, data)

    def push_dgram_ready(self, *args):
        if self.once:
            self.once = False
            return 1
        return self.inner.push_dgram_ready(*args)


def _config(client):
    q = QuicConfiguration(is_client=client, alpn_protocols=["dgram-mark"],
                          server_name=SNI, cafile=CA_FILE)
    if not client:
        q.load_cert_chain(CERT_FILE, KEY_FILE)
    return q


async def _owed_mark_goes_out_on_drain(sender, attr, inbox):
    transport = sender._transport
    original = getattr(sender, attr)
    setattr(sender, attr, MarkRefusedOnce(original))
    try:
        assert sender.send_datagram_frame(b"first") == 5
        assert sender._dgram_mark_owed
        # Any worker pass ends with the drained-ring check.
        transport.wake_up()
        assert await asyncio.wait_for(inbox.get(), 2) == b"first"
        assert not sender._dgram_mark_owed
        assert sender.send_datagram_frame(b"second") == 6
        assert await asyncio.wait_for(inbox.get(), 2) == b"second"
    finally:
        setattr(sender, attr, original)


async def test_quic_owed_mark_reposts_on_ring_drain():
    inbox = asyncio.Queue()

    class Receiver(QuicConnectionProtocol):
        def quic_event_received(self, ev):
            if isinstance(ev, DatagramFrameReceived):
                inbox.put_nowait(bytes(ev.data))

    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_config(False),
                         create_protocol=Receiver)
    try:
        async with connect("127.0.0.1", port, configuration=_config(True)) as c:
            await _owed_mark_goes_out_on_drain(c._quic, "_transport", inbox)
    finally:
        server.close()


async def test_wt_owed_mark_reposts_on_ring_drain():
    inbox = asyncio.Queue()

    async def handler(session):
        async for ev in session.events():
            if isinstance(ev, WebTransportDatagramReceived):
                inbox.put_nowait(bytes(ev.data))

    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler, cert_file=CERT_FILE,
        key_file=KEY_FILE, configuration=_config(False))
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        configuration=_config(True)) as wt:
            await _owed_mark_goes_out_on_drain(wt, "_state", inbox)
    finally:
        server.close()
