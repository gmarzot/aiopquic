"""Stream context reference scenarios, run in a fresh process.

test_stream_ctx_refs.py runs each in a subprocess under MALLOC_PERTURB_: a
touch of freed memory faults and kills only the child, and the process-wide
live stream context count starts at zero. Every event that carries a stream
context holds a reference to it, so the connection dropping its own
reference never frees a context an event still names.

    python tests/stream_ctx_scenarios.py close-then-data PORT CERT KEY
        APP_CLOSE then STREAM_DATA and STREAM_FIN for the same context in
        one drain_rx_callback batch; the handler drops the connection's
        reference on APP_CLOSE. Exits 0 when the stream's bytes arrive
        whole and no context is left.

    python tests/stream_ctx_scenarios.py close-then-data-split PORT CERT KEY
        The same with drain_rx, one event per call, the reference dropped
        between the calls.

    python tests/stream_ctx_scenarios.py undrained PORT CERT KEY
        Queue RX STREAM_DATA and TX reset, stop-sending and mark-active
        events that name a context nothing else holds, then free the
        transport. Exits 0 when the queued events kept it alive and the
        transport's teardown released it.

    python tests/stream_ctx_scenarios.py tx-ring-full PORT CERT KEY
        A reset pushed onto a full TX ring raises BufferError and returns
        its reference. Exits 0 when no context is left.

    python tests/stream_ctx_scenarios.py churn PORT CERT KEY
        Loopback connections that close mid-transfer from either side, with
        data, FIN, reset and stop-sending streams. Exits 0 when every
        context and chunk is released.
"""
import asyncio
import gc
import sys
import time

from aiopquic._binding._transport import (
    TransportContext, stream_buf_push, stream_ctx_create, stream_ctx_destroy,
    stream_ctx_ensure_rx, stream_ctx_get_rx,
)
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"
CA_FILE = None            # set from the cert path in __main__

SPSC_EVT_STREAM_DATA = 0
SPSC_EVT_STREAM_FIN = 1
SPSC_EVT_APP_CLOSE = 5
SPSC_EVT_TX_CNX_REFRESH = 147
SPSC_EVT_TX_STREAM_RESET = 132
SPSC_EVT_TX_STOP_SENDING = 133
SPSC_EVT_TX_MARK_ACTIVE = 134

CNX = 0x1000  # never dereferenced: no worker runs
PAYLOAD = bytes(range(256)) * 4


def _fail(msg):
    # Printed before teardown, which a regression may crash.
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def _counters():
    return TransportContext().counters


def _alive():
    return _counters()["sc_alive_total"]


def _sc_with_rx(data):
    sc = stream_ctx_create()
    stream_ctx_ensure_rx(sc, 4096)
    stream_buf_push(stream_ctx_get_rx(sc), data)
    return sc


def _check_released(what):
    gc.collect()
    c = _counters()
    if c["sc_alive_total"] != 0 or c["chunks_alive_total"] != 0:
        _fail(f"{what}: {c['sc_alive_total']} stream contexts and "
              f"{c['chunks_alive_total']} chunks alive")


def close_then_data(port, cert, key):
    t = TransportContext()
    sc = _sc_with_rx(PAYLOAD)
    t._test_push_rx(SPSC_EVT_APP_CLOSE, 0, cnx_ptr=CNX)
    t._test_push_rx(SPSC_EVT_STREAM_DATA, 0, cnx_ptr=CNX, stream_ctx=sc)
    t._test_push_rx(SPSC_EVT_STREAM_FIN, 0, cnx_ptr=CNX, stream_ctx=sc)
    got = []

    def handler(evt, sid, data, is_fin, err, cnx, stream_ctx, sc_ptr):
        if evt == SPSC_EVT_APP_CLOSE:
            stream_ctx_destroy(sc)  # the connection's reference
        elif data is not None:
            got.append(bytes(data))

    n = t.drain_rx_callback(handler)
    if n != 3:
        _fail(f"drained {n} events, expected 3")
    if b"".join(got) != PAYLOAD:
        _fail(f"received {len(b''.join(got))} of {len(PAYLOAD)} bytes")
    del t
    _check_released("after the drain")


def close_then_data_split(port, cert, key):
    t = TransportContext()
    sc = _sc_with_rx(PAYLOAD)
    t._test_push_rx(SPSC_EVT_APP_CLOSE, 0, cnx_ptr=CNX)
    t._test_push_rx(SPSC_EVT_STREAM_DATA, 0, cnx_ptr=CNX, stream_ctx=sc)
    first = t.drain_rx(max_events=1)
    if [ev[0] for ev in first] != [SPSC_EVT_APP_CLOSE]:
        _fail(f"first batch {first}")
    stream_ctx_destroy(sc)
    second = t.drain_rx(max_events=1)
    if len(second) != 1 or bytes(second[0][2]) != PAYLOAD:
        _fail(f"second batch {second}")
    del first, second, t
    _check_released("after the drains")


def undrained(port, cert, key):
    t = TransportContext()
    sc = _sc_with_rx(PAYLOAD)
    t._test_push_rx(SPSC_EVT_STREAM_DATA, 0, cnx_ptr=CNX, stream_ctx=sc)
    for evt in (SPSC_EVT_TX_STREAM_RESET, SPSC_EVT_TX_STOP_SENDING,
                SPSC_EVT_TX_MARK_ACTIVE):
        t.push_tx_event(evt, 0, cnx_ptr=CNX, stream_ctx=sc)
    stream_ctx_destroy(sc)
    if _alive() != 1:
        _fail(f"{_alive()} stream contexts alive while events name one")
    t.stop()
    if _alive() != 1:
        _fail("stop() released the context an RX event still names")
    del t
    _check_released("after the transport was freed")


def tx_ring_full(port, cert, key):
    t = TransportContext(tx_ring_cap=16)
    sc = stream_ctx_create()
    try:
        while True:
            t.push_tx_event(SPSC_EVT_TX_CNX_REFRESH, 0, cnx_ptr=CNX)
    except BufferError:
        pass
    try:
        t.push_tx_event(SPSC_EVT_TX_STREAM_RESET, 0, cnx_ptr=CNX,
                        stream_ctx=sc)
        _fail("a push onto a full TX ring succeeded")
    except BufferError:
        pass
    stream_ctx_destroy(sc)
    if _alive() != 0:
        _fail("the failed push kept its reference")
    del t
    _check_released("after the transport was freed")


async def churn(port, cert, key):
    cycles = 30
    payload = b"c" * (256 * 1024)

    class Server(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if not isinstance(event, StreamDataReceived):
                return
            if event.stream_id == 0 and self._abort:
                self._quic.close(error_code=0x10)
                return
            if event.end_stream and event.stream_id % 4 == 0:
                self._quic.send_stream_data(event.stream_id, b"ok",
                                            end_stream=True)

    async def one(i):
        mode = i % 3
        Server._abort = mode == 1
        cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                server_name=SNI, cafile=CA_FILE)
        async with connect("127.0.0.1", port, configuration=cfg) as client:
            quic = client._quic
            sids = [quic.get_next_available_stream_id() for _ in range(4)]
            for sid in sids:
                quic.send_stream_data(sid, payload[:16 * 1024])
            quic.send_stream_data(sids[0], b"", end_stream=True)
            quic.send_stream_data(sids[1], b"", end_stream=True)
            quic.reset_stream(sids[2], 0x11)
            quic.stop_stream(sids[3], 0x12)
            uni = quic.get_next_available_stream_id(is_unidirectional=True)
            quic.send_stream_data(uni, payload, end_stream=True)
            if mode == 2:
                return  # close with data in flight
            if mode == 0:
                await asyncio.sleep(0.05)
                return
            deadline = time.monotonic() + 5.0
            while not quic._closed and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if not quic._closed:
                _fail(f"cycle {i}: the server's close never arrived")

    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(cert, key)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=Server)
    Server._abort = False
    try:
        for i in range(cycles):
            await asyncio.wait_for(one(i), timeout=10.0)
        engine = server._engine
        deadline = time.monotonic() + 10.0
        while engine._connections and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if engine._connections:
            _fail(f"{len(engine._connections)} server connections never "
                  "closed")
    finally:
        server.close()
    _check_released(f"after {cycles} connections")


SCENARIOS = {
    "close-then-data": close_then_data,
    "close-then-data-split": close_then_data_split,
    "undrained": undrained,
    "tx-ring-full": tx_ring_full,
    "churn": churn,
}


if __name__ == "__main__":
    import inspect
    import os
    mode, port, cert, key = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    CA_FILE = os.path.join(os.path.dirname(cert), "test-ca.crt")
    fn = SCENARIOS[mode]
    if inspect.iscoroutinefunction(fn):
        asyncio.run(fn(port, cert, key))
    else:
        fn(port, cert, key)
    print("OK", flush=True)
