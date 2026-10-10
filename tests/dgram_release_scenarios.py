"""Datagram ring release scenarios, run in a fresh process.

test_dgram_release.py runs each in a subprocess under MALLOC_PERTURB_: a
fault kills only the child, and the process-wide live-ring count starts at
zero. A MARK names its ring without holding a reference, so a release
must reach the worker behind every MARK queued before it.

    python tests/dgram_release_scenarios.py raw-queued PORT CERT KEY
        Queue a MARK for a live raw cnx without waking the worker, release
        the ring, then wake. Exits 0 when the worker processed the release
        after the MARK, left no table entry and freed the ring exactly
        once.

    python tests/dgram_release_scenarios.py wt-queued PORT CERT KEY
        Queue a WebTransport MARK without waking the worker, then close the
        session, which releases its ring. Exits 0 when the ring outlives
        the release, held by the session, and is freed with the session.

    python tests/dgram_release_scenarios.py raw-peer-close PORT CERT KEY
        The server echoes every datagram; a client sends datagrams and
        closes the connection on the first echo, 20 times. Exits 0 when
        every cycle ends and no ring is left.

    python tests/dgram_release_scenarios.py wt-peer-close PORT CERT KEY
        A client sends datagrams until the server closes the session, then
        sends its first datagram on a fresh session and closes it at once,
        20 times each. Exits 0 when every cycle ends and no ring is left.
"""
import asyncio
import gc
import os
import sys
import time

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import DatagramFrameReceived

SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"
CA_FILE = None            # set from the cert path in __main__

SPSC_EVT_READY = 6
SPSC_EVT_ALMOST_READY = 7
SPSC_EVT_TX_MARK_DATAGRAM_READY = 146
SPSC_EVT_TX_MARK_WT_DATAGRAM_READY = 148

CYCLES = 20
BURST = 16
PAYLOAD = b"d" * 1000


def _fail(msg):
    # Printed before teardown, which a regression may crash.
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def _alive(transport):
    return transport.counters["dgram_rings_alive_total"]


def _wait(cond, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.005)
    _fail(f"timed out waiting for {what}")


async def _await(cond, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.005)
    _fail(f"timed out waiting for {what}")


def _raw_connect(client, port):
    client.create_client_connection("127.0.0.1", port, sni=SNI, alpn=ALPN)
    cnx = 0
    ready = False
    deadline = time.monotonic() + 5.0
    while not ready and time.monotonic() < deadline:
        for ev in client.drain_rx():
            if ev[0] == SPSC_EVT_ALMOST_READY and ev[5]:
                cnx = ev[5]
            elif ev[0] == SPSC_EVT_READY and ev[5]:
                ready = True
        time.sleep(0.01)
    if not (cnx and ready):
        _fail("raw handshake did not complete")
    return cnx


def raw_queued(port, cert, key):
    server = TransportContext()
    server.start(port=port, cert_file=cert, key_file=key, alpn=ALPN,
                 is_client=False, max_datagram_frame_size=1200)
    client = TransportContext()
    client.start(port=0, alpn=ALPN, is_client=True, ca_file=CA_FILE,
                 max_datagram_frame_size=1200)
    try:
        cnx = _raw_connect(client, port)
        _wait(lambda: client.tx_event_ring_count == 0, "an idle TX ring")
        base = _alive(client)
        ring = client.dgram_ring_create(4096, 1200)
        # Not woken: the worker reaches the MARK only after the release.
        client.push_tx_event(SPSC_EVT_TX_MARK_DATAGRAM_READY, 0,
                             cnx_ptr=cnx, stream_ctx=ring)
        client.dgram_ring_release(ring, cnx)
        client.wake_up()
        _wait(lambda: client.tx_event_ring_count == 0, "the worker to drain")
        c = client.counters
        if c["dgram_table_live"] != 0:
            _fail("the worker registered a ring after its release: "
                  f"{c['dgram_table_live']} table entries")
        if c["dgram_ring_release_processed"] != 1:
            _fail(f"worker processed {c['dgram_ring_release_processed']} "
                  "releases, expected 1")
        if _alive(client) != base:
            _fail(f"{_alive(client) - base} rings alive after the release")
    finally:
        client.stop()
        server.stop()
    if _alive(client) != base:
        _fail(f"{_alive(client) - base} rings alive after stop")


async def wt_queued(port, cert, key):
    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=cert, key_file=key)
    transport = TransportContext()
    transport.start(is_client=True, alpn="h3", ca_file=CA_FILE,
                    max_datagram_frame_size=64 * 1024)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        transport=transport) as wt:
            await _await(lambda: transport.tx_event_ring_count == 0,
                         "an idle TX ring")
            base = _alive(transport)
            ring = transport.dgram_ring_create(4096, 1200)
            wt._dgram_ring = ring  # released by the session's close
            ptr = wt._state.session_ptr
            # Not woken: close() wakes the worker after the release.
            transport.push_tx_event(SPSC_EVT_TX_MARK_WT_DATAGRAM_READY, 0,
                                    error_code=ring, cnx_ptr=ptr,
                                    stream_ctx=ptr)
            wt.close()
            await _await(lambda: transport.tx_event_ring_count == 0,
                         "the worker to drain")
            if _alive(transport) != base + 1:
                _fail("the ring was freed while a queued MARK named it")
        del wt
        gc.collect()
        # The session struct holds the last reference until it is freed.
        await _await(lambda: _alive(transport) == base,
                     "the session to free its ring")
    finally:
        server.close()
        transport.stop()


def _datagram_cfg(**kw):
    return QuicConfiguration(alpn_protocols=[ALPN],
                             max_datagram_frame_size=1200, **kw)


async def raw_peer_close(port, cert, key):
    # The server echoes each datagram from its event handler, which the
    # engine runs as it routes the batch: an echo's MARK and the release
    # for the client's close in the same batch are microseconds apart.
    class _Echo(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, DatagramFrameReceived):
                try:
                    self._quic.send_datagram_frame(bytes(event.data))
                except ConnectionError:
                    pass

    class _CloseOnEcho(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, DatagramFrameReceived):
                self._quic.close()

    scfg = _datagram_cfg(is_client=False)
    scfg.load_cert_chain(cert, key)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_Echo)
    base = TransportContext().counters["dgram_rings_alive_total"]
    try:
        for _ in range(CYCLES):
            ccfg = _datagram_cfg(is_client=True, server_name=SNI,
                                 cafile=CA_FILE)
            async with connect("127.0.0.1", port, configuration=ccfg,
                               create_protocol=_CloseOnEcho) as client:
                quic = client._quic
                deadline = time.monotonic() + 5.0
                while not quic.closed and time.monotonic() < deadline:
                    for _ in range(BURST):
                        try:
                            quic.send_datagram_frame(PAYLOAD)
                        except ConnectionError:
                            break
                    await asyncio.sleep(0)
                if not quic.closed:
                    _fail("no echo came back")
        # The server releases a connection's ring when it routes the close.
        await asyncio.sleep(0.2)
    finally:
        server.close()
    gc.collect()
    left = TransportContext().counters["dgram_rings_alive_total"] - base
    if left:
        _fail(f"{left} rings alive after {CYCLES} cycles")


async def wt_peer_close(port, cert, key):
    async def handler(session):
        async for _ in session.events():
            session.close()
            return

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=cert, key_file=key)
    # A session struct drops its ring reference when the worker frees it,
    # so the client transport runs until the sessions are gone.
    transport = TransportContext()
    transport.start(is_client=True, alpn="h3", ca_file=CA_FILE,
                    max_datagram_frame_size=64 * 1024)
    base = _alive(transport)
    try:
        for _ in range(CYCLES):
            async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                            transport=transport) as wt:
                deadline = time.monotonic() + 5.0
                while not wt.session_closed and time.monotonic() < deadline:
                    for _ in range(BURST):
                        try:
                            wt.send_datagram_frame(PAYLOAD)
                        except ConnectionError:
                            break
                    await asyncio.sleep(0)
                if not wt.session_closed:
                    _fail("the server never closed the session")
            # Only the session's first MARK hands it a reference.
            async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                            transport=transport) as wt:
                wt.send_datagram_frame(PAYLOAD)
                wt.close()
            del wt
            gc.collect()
        await _await(lambda: _alive(transport) == base,
                     "every session to free its ring")
    finally:
        server.close()
        transport.stop()


if __name__ == "__main__":
    mode, port, cert, key = sys.argv[1:5]
    CA_FILE = os.path.join(os.path.dirname(cert), "test-ca.crt")
    if mode == "raw-queued":
        raw_queued(int(port), cert, key)
    else:
        asyncio.run({"wt-queued": wt_queued, "raw-peer-close": raw_peer_close,
                     "wt-peer-close": wt_peer_close}[mode](
                         int(port), cert, key))
