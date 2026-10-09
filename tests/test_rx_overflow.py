"""A full RX event ring spills to the overflow and nothing is lost.

The client runs a 16-entry event ring and blocks its loop while the
server opens 200 streams, so the ring fills many times over. Every
stream still arrives with its FIN, in order, and the drop counter
stays at zero.
"""
import asyncio
import os
import threading
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportNewStream, WebTransportStreamDataReceived,
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import HandshakeCompleted, StreamDataReceived

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
N_STREAMS = 200
PAYLOAD = b"x" * 64
RING = 16

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.path.exists(CA_FILE),
                       reason="picoquic certs not found"),
]


def _stall_until_overflow(transport, limit=3.0) -> None:
    """Block the loop until the worker has spilled at least once."""
    deadline = time.monotonic() + limit
    while (transport.counters["rx_overflow_pushed"] == 0
           and time.monotonic() < deadline):
        time.sleep(0.02)
    time.sleep(0.1)


def _assert_spilled(counters) -> None:
    assert counters["rx_overflow_pushed"] > 0, counters
    assert counters["rx_overflow_max_depth"] > 0, counters
    assert counters["rx_event_drops"] == 0, counters


class _Burst(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            for _ in range(N_STREAMS):
                sid = self._quic.get_next_available_stream_id(
                    is_unidirectional=True)
                self._quic.send_stream_data(sid, PAYLOAD, end_stream=True)


class _Stalled(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fins: set[int] = set()
        self.data: dict[int, bytearray] = {}
        self._stalled = False

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted) and not self._stalled:
            self._stalled = True
            _stall_until_overflow(self._quic._transport)
        if isinstance(event, StreamDataReceived):
            buf = self.data.setdefault(event.stream_id, bytearray())
            assert event.stream_id not in self.fins, "data after FIN"
            buf.extend(event.data)
            if event.end_stream:
                self.fins.add(event.stream_id)


async def test_raw_quic_ring_overflow_loses_nothing():
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_Burst)
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                 server_name=SNI, cafile=CA_FILE,
                                 event_ring_capacity=RING)
        async with connect("127.0.0.1", port, configuration=ccfg,
                           create_protocol=_Stalled) as client:
            async with asyncio.timeout(10):
                while len(client.fins) < N_STREAMS:
                    await asyncio.sleep(0.02)
            assert all(bytes(client.data[s]) == PAYLOAD for s in client.fins)
            _assert_spilled(client._quic._transport.counters)
    finally:
        server.close()


def _wt_server_thread(port, ready, stop):
    """The server on its own loop, so a stalled client loop cannot hold
    back its stream opens."""
    async def main():
        async def handler(session):
            for _ in range(N_STREAMS):
                sid = await session.create_stream(bidir=False)
                session.send_stream_data(sid, PAYLOAD, end_stream=True)
            await session.wait_closed()

        server = await serve_webtransport(
            "127.0.0.1", port, "/wt", handler=handler,
            cert_file=CERT_FILE, key_file=KEY_FILE)
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.close()

    asyncio.run(main())


async def test_webtransport_ring_overflow_loses_nothing():
    port = next_port()
    ready, stop = threading.Event(), threading.Event()
    thread = threading.Thread(target=_wt_server_thread,
                              args=(port, ready, stop), daemon=True)
    thread.start()
    assert ready.wait(5), "server thread did not start"
    try:
        ccfg = QuicConfiguration(is_client=True, server_name=SNI,
                                 cafile=CA_FILE, event_ring_capacity=RING)
        async with connect_webtransport("127.0.0.1", port, "/wt",
                                        configuration=ccfg, sni=SNI,
                                        ca_file=CA_FILE, timeout=5.0) as wt:
            _stall_until_overflow(wt._transport)
            fins: set[int] = set()

            async def drain(sid):
                got = bytearray()
                async for ev in wt.receive_stream_data(sid):
                    if isinstance(ev, WebTransportStreamDataReceived):
                        got.extend(ev.data)
                assert bytes(got) == PAYLOAD
                fins.add(sid)

            tasks = []
            async with asyncio.timeout(10):
                async for ev in wt.events():
                    if isinstance(ev, WebTransportNewStream):
                        tasks.append(asyncio.ensure_future(drain(ev.stream_id)))
                        if len(tasks) == N_STREAMS:
                            break
                await asyncio.gather(*tasks)
            assert len(fins) == N_STREAMS
            _assert_spilled(wt._transport.counters)
    finally:
        stop.set()
        thread.join(5)
