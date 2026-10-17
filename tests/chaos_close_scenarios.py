"""Chaos-close stress, run in a fresh process.

test_chaos_close.py runs each mode in a subprocess under MALLOC_PERTURB_:
process-wide counters start at zero, and a native fault kills only the
child.

    python tests/chaos_close_scenarios.py MODE PORT CERT KEY SEED SECONDS

MODE is `quic` (raw QUIC) or `wt` (WebTransport). For SECONDS, concurrent
clients open connections against one server, and both ends apply a seeded
random mix of stream data, FIN, RESET_STREAM, STOP_SENDING and connection
or session close, with sub-millisecond gaps between them. Every connection
is bounded by a timeout. Exits 0 when nothing hung and every stream
context and chunk is released once all connections are gone; on failure
the reason goes to stderr.
"""
import asyncio
import gc
import json
import logging
import os
import random
import sys
import time

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    StopSendingReceived, StreamDataReceived, WebTransportNewStream,
    WebTransportStopSending, WebTransportStreamDataReceived,
)

SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"
PARALLEL = 8
CONN_TIMEOUT = 10.0
SETTLE_TIMEOUT = 20.0
MAX_WRITE = 64 * 1024
_BLOB = bytes(range(256)) * (MAX_WRITE // 256)

# Errors a racing call may legitimately raise once the peer or the
# connection is gone, or under backpressure.
_EXPECTED = (BufferError, ConnectionError, RuntimeError, ValueError)


def _fail(msg):
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def _live():
    c = TransportContext().counters
    return c["sc_alive_total"], c["chunks_alive_total"]


def _bidi(sid):
    return sid & 2 == 0


def _client_initiated(sid):
    return sid & 1 == 0


async def _gap(rng):
    r = rng.random()
    if r < 0.4:
        return
    if r < 0.8:
        await asyncio.sleep(0)
    else:
        await asyncio.sleep(rng.uniform(0, 0.003))


class _Stats:
    def __init__(self):
        self.n = {}

    def add(self, key):
        self.n[key] = self.n.get(key, 0) + 1


# ---------------------------------------------------------------- raw QUIC

def _quic_server_protocol(seed, stats):
    rng = random.Random(f"{seed}:server")

    class Server(QuicConnectionProtocol):
        def quic_event_received(self, event):
            q = self._quic
            try:
                if isinstance(event, StreamDataReceived):
                    sid = event.stream_id
                    r = rng.random()
                    if r < 0.30 and _bidi(sid):
                        q.send_stream_data(sid, _BLOB[:rng.randrange(1, MAX_WRITE)],
                                           end_stream=rng.random() < 0.3)
                        stats.add("s_data")
                    elif r < 0.40 and _bidi(sid):
                        q.reset_stream(sid, 0x21)
                        stats.add("s_reset")
                    elif r < 0.50 and not event.end_stream:
                        q.stop_stream(sid, 0x22)
                        stats.add("s_stop")
                    elif r < 0.53:
                        if _bidi(sid):
                            # Data and the close often leave in one packet.
                            q.send_stream_data(sid, _BLOB[:1024])
                        q.close(error_code=0x23)
                        stats.add("s_close")
                elif isinstance(event, StopSendingReceived):
                    if rng.random() < 0.7:
                        q.reset_stream(event.stream_id, 0x24)
                        stats.add("s_reset_on_stop")
            except _EXPECTED:
                stats.add("s_raised")

    return Server


async def _quic_client_one(rng, port, ca_file, stats):
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                            server_name=SNI, cafile=ca_file)
    async with connect("127.0.0.1", port, configuration=cfg) as client:
        q = client._quic
        sids = [q.get_next_available_stream_id(
                    is_unidirectional=rng.random() < 0.4)
                for _ in range(rng.randint(1, 8))]
        for _ in range(rng.randint(2, 24)):
            if q.closed:
                stats.add("c_saw_close")
                break
            sid = rng.choice(sids)
            r = rng.random()
            try:
                if r < 0.55:
                    q.send_stream_data(sid, _BLOB[:rng.randrange(1, MAX_WRITE)])
                    stats.add("c_data")
                elif r < 0.70:
                    q.send_stream_data(sid, b"", end_stream=True)
                    stats.add("c_fin")
                elif r < 0.82:
                    q.reset_stream(sid, 0x11)
                    stats.add("c_reset")
                elif r < 0.94 and _bidi(sid):
                    q.stop_stream(sid, 0x12)
                    stats.add("c_stop")
                elif r < 0.97:
                    q.close(error_code=0x13)
                    stats.add("c_close")
                    break
            except _EXPECTED:
                stats.add("c_raised")
            await _gap(rng)
        end = rng.random()
        if end < 0.3:
            await asyncio.sleep(rng.uniform(0, 0.01))
        elif end < 0.4:
            # Close behind a burst still in flight.
            try:
                q.send_stream_data(sids[0], _BLOB)
            except _EXPECTED:
                pass
            q.close(error_code=0x14)
        # else: leave through connect()'s close right away.


async def run_quic(port, cert, key, seed, seconds, ca_file):
    stats = _Stats()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(cert, key)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=_quic_server_protocol(seed, stats))
    end = time.monotonic() + seconds
    counter = iter(range(1 << 62))

    async def worker():
        while time.monotonic() < end:
            i = next(counter)
            rng = random.Random(f"{seed}:{i}")
            try:
                await asyncio.wait_for(
                    _quic_client_one(rng, port, ca_file, stats), CONN_TIMEOUT)
                stats.add("conns")
            except asyncio.TimeoutError:
                _fail(f"connection {i} hung past {CONN_TIMEOUT}s")
            except (ConnectionError, OSError) as exc:
                stats.add(f"conn_error:{type(exc).__name__}")

    try:
        await asyncio.gather(*(worker() for _ in range(PARALLEL)))
        engine = server._engine
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while engine._connections and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if engine._connections:
            _fail(f"{len(engine._connections)} server connections never closed")
    finally:
        server.close()
    return stats


# ------------------------------------------------------------ WebTransport

async def _drain_stream(session, sid, on_data=None):
    gen = session.receive_stream_data(sid)
    try:
        async for ev in gen:
            if on_data is not None and isinstance(ev, WebTransportStreamDataReceived):
                if on_data(ev):
                    return
    finally:
        await gen.aclose()


def _wt_handler(seed, stats, tasks):
    rng = random.Random(f"{seed}:server")

    def act(session, ev):
        sid = ev.stream_id
        r = rng.random()
        try:
            if r < 0.30 and _bidi(sid) and not ev.end_stream:
                session.send_stream_data(sid, _BLOB[:rng.randrange(1, MAX_WRITE)],
                                         end_stream=rng.random() < 0.3)
                stats.add("s_data")
            elif r < 0.40 and _bidi(sid):
                session.reset_stream(sid, 0x21)
                stats.add("s_reset")
            elif r < 0.50 and not ev.end_stream:
                session.stop_stream(sid, 0x22)
                stats.add("s_stop")
                return True
            elif r < 0.53:
                session.close(0x23, b"chaos")
                stats.add("s_close")
                return True
        except _EXPECTED:
            stats.add("s_raised")
        return False

    async def events(session):
        gen = session.events()
        try:
            async for ev in gen:
                if isinstance(ev, WebTransportNewStream):
                    t = asyncio.ensure_future(_drain_stream(
                        session, ev.stream_id, lambda e: act(session, e)))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                elif isinstance(ev, WebTransportStopSending):
                    if rng.random() < 0.7:
                        try:
                            session.reset_stream(ev.stream_id, 0x24)
                            stats.add("s_reset_on_stop")
                        except _EXPECTED:
                            stats.add("s_raised")
        finally:
            await gen.aclose()

    def handler(session):
        t = asyncio.ensure_future(events(session))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    return handler


async def _wt_client_one(rng, port, ca_file, stats):
    readers = []
    try:
        async with connect_webtransport("127.0.0.1", port, "/chaos", sni=SNI,
                                        ca_file=ca_file) as wt:
            sids = []
            for _ in range(rng.randint(1, 6)):
                bidir = rng.random() < 0.6
                sid = await wt.create_stream(bidir=bidir)
                sids.append(sid)
                if bidir:
                    readers.append(asyncio.ensure_future(_drain_stream(wt, sid)))
            for _ in range(rng.randint(2, 20)):
                if wt.session_closed:
                    stats.add("c_saw_close")
                    break
                sid = rng.choice(sids)
                r = rng.random()
                try:
                    if r < 0.55:
                        wt.send_stream_data(sid, _BLOB[:rng.randrange(1, MAX_WRITE)])
                        stats.add("c_data")
                    elif r < 0.70:
                        wt.send_stream_data(sid, b"", end_stream=True)
                        stats.add("c_fin")
                    elif r < 0.82:
                        wt.reset_stream(sid, 0x11)
                        stats.add("c_reset")
                    elif r < 0.94 and _bidi(sid):
                        wt.stop_stream(sid, 0x12)
                        stats.add("c_stop")
                    elif r < 0.97:
                        wt.close(0x13, b"chaos")
                        stats.add("c_close")
                        break
                except _EXPECTED:
                    stats.add("c_raised")
                await _gap(rng)
            if rng.random() < 0.3:
                await asyncio.sleep(rng.uniform(0, 0.01))
    finally:
        for t in readers:
            t.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def run_wt(port, cert, key, seed, seconds, ca_file):
    stats = _Stats()
    tasks = set()
    server = await serve_webtransport(
        "127.0.0.1", port, "/chaos", handler=_wt_handler(seed, stats, tasks),
        cert_file=cert, key_file=key)
    end = time.monotonic() + seconds
    counter = iter(range(1 << 62))

    async def worker():
        while time.monotonic() < end:
            i = next(counter)
            rng = random.Random(f"{seed}:{i}")
            try:
                await asyncio.wait_for(
                    _wt_client_one(rng, port, ca_file, stats), CONN_TIMEOUT)
                stats.add("conns")
            except asyncio.TimeoutError:
                _fail(f"session {i} hung past {CONN_TIMEOUT}s")
            except (ConnectionError, OSError) as exc:
                stats.add(f"conn_error:{type(exc).__name__}")

    try:
        await asyncio.gather(*(worker() for _ in range(PARALLEL)))
        # Server sessions end when their client's connection does.
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if tasks:
            _fail(f"{len(tasks)} server session tasks never finished")
    finally:
        await server.aclose()
    return stats


async def main(mode, port, cert, key, seed, seconds):
    ca_file = os.path.join(os.path.dirname(cert), "test-ca.crt")
    base = _live()
    run = run_quic if mode == "quic" else run_wt
    stats = await run(port, cert, key, seed, seconds, ca_file)
    # Released once the drain or the worker's teardown handles the last event.
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while _live() != base and time.monotonic() < deadline:
        gc.collect()
        await asyncio.sleep(0.05)
    live = _live()
    if live != base:
        _fail(f"{live[0] - base[0]} stream contexts and {live[1] - base[1]} "
              f"chunks alive after {stats.n.get('conns', 0)} connections")
    if not stats.n.get("conns"):
        _fail("no connection completed")
    return stats


if __name__ == "__main__":
    mode, port, cert, key = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    seed, seconds = int(sys.argv[5]), float(sys.argv[6])
    if mode not in ("quic", "wt"):
        _fail(f"unknown mode {mode}")
    # Resets and closes are the point here; keep stderr for failures.
    logging.getLogger("aiopquic").setLevel(logging.ERROR)
    stats = asyncio.run(main(mode, port, cert, key, seed, seconds))
    print(json.dumps(dict(sorted(stats.n.items()))), flush=True)
