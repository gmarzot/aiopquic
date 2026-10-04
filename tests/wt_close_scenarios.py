"""WebTransport session-close scenarios, run in a fresh process.

test_webtransport.py runs each in a subprocess: process-wide counters start
at zero, and a fault kills only the child.

    python tests/wt_close_scenarios.py receive PORT CERT KEY
        Close a session while the peer is still sending, 20 times. Run
        with MALLOC_PERTURB_ so a read of a freed stream context faults
        instead of returning stale bytes. Exits 0 when every cycle ends.

    python tests/wt_close_scenarios.py unsent PORT CERT KEY
        Close a session with bytes still queued unsent. Exits 0 when the
        process-wide queued-bytes total is back to its starting value.

    python tests/wt_close_scenarios.py starve PORT CERT KEY
        A peer vanishes without closing while the server holds more than
        the TX budget queued for it. Exits 0 when the server still opens
        a stream to a second peer.
"""
import asyncio
import sys
import time

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.events import (
    WebTransportNewStream, WebTransportStreamDataReceived,
)

CYCLES = 20
CHUNK = b"x" * 16384
BURST = b"y" * (2 << 20)


def _queued_tx_bytes():
    c = TransportContext().counters
    return (c['tx_data_bytes_pushed_total'] - c['tx_data_bytes_pulled_total']
            - c['tx_data_bytes_discarded_total'])


async def _send_burst(session):
    # One push, no FIN: the worker keeps transmitting while the client's
    # loop is blocked, and all of it is delivered before the client
    # closes, so nothing is left unsent on a peer-closed session.
    try:
        sid = await session.create_stream(bidir=False)
        session.send_stream_data(sid, BURST)
    except Exception:
        return


async def _receive_some(wt):
    # Close the reader explicitly: a suspended generator would keep the
    # last chunk, and with it a reference to the stream's sc.
    async for ev in wt.events():
        if isinstance(ev, WebTransportNewStream):
            reader = wt.receive_stream_data(ev.stream_id)
            got = 0
            try:
                async for data_ev in reader:
                    if isinstance(data_ev, WebTransportStreamDataReceived):
                        got += len(data_ev.data)
                        if got >= 4 * len(CHUNK):
                            return
            finally:
                await reader.aclose()


async def _close_while_receiving(port):
    async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
        await _receive_some(wt)
        # Block the loop so data events queue undrained, close, and keep
        # blocking while the worker runs the session cleanup. The
        # dispatcher then drains events whose streams were just released.
        time.sleep(0.05)
        wt.close()
        time.sleep(0.02)
        await asyncio.sleep(0.05)


async def receive(port, cert, key):
    async def handler(session):
        asyncio.create_task(_send_burst(session))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=cert, key_file=key)
    try:
        for _ in range(CYCLES):
            await asyncio.wait_for(_close_while_receiving(port), timeout=10.0)
    finally:
        server.close()


async def unsent(port, cert, key):
    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=cert, key_file=key)
    try:
        before = _queued_tx_bytes()
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=False)
            chunk = b"q" * 65536
            for _ in range(4096):
                try:
                    wt.send_stream_data(sid, chunk)
                except BufferError:
                    break
            else:
                sys.exit("stream ring never filled")
            if _queued_tx_bytes() <= before:
                sys.exit("nothing was left queued to credit")
        for _ in range(200):
            if _queued_tx_bytes() == before:
                return
            await asyncio.sleep(0.01)
        sys.exit(f"{_queued_tx_bytes() - before} bytes still counted as "
                 f"queued after the session closed")
    finally:
        server.close()


async def starve(port, cert, key):
    cap = 4 * 1024 * 1024
    a_streams = asyncio.Event()
    a_gone = asyncio.Event()
    a_loaded = asyncio.Event()
    sessions = []

    async def load_vanished_peer(session):
        # Streams first, bytes after the peer is gone: nothing is ACKed,
        # so the bytes stay queued for the life of the connection.
        try:
            sids = [await session.create_stream(bidir=False)
                    for _ in range(2)]
            a_streams.set()
            await a_gone.wait()
            for sid in sids:
                session.send_stream_data(sid, b"z" * (3 << 20))
        finally:
            a_loaded.set()

    async def open_one(session):
        try:
            sid = await session.create_stream(bidir=False)
            session.send_stream_data(sid, b"ok", end_stream=True)
        except Exception:
            return

    async def handler(session):
        sessions.append(session)
        task = load_vanished_peer if len(sessions) == 1 else open_one
        asyncio.create_task(task(session))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=cert, key_file=key)
    try:
        transport_a = TransportContext()
        transport_a.start(is_client=True, alpn="h3",
                          max_datagram_frame_size=64 * 1024)
        try:
            async with connect_webtransport(
                    "127.0.0.1", port, "/wt", transport=transport_a):
                await asyncio.wait_for(a_streams.wait(), timeout=5.0)
                transport_a.stop()
                a_gone.set()
                await asyncio.wait_for(a_loaded.wait(), timeout=5.0)
        except Exception:
            pass
        if _queued_tx_bytes() <= cap:
            sys.exit(f"precondition: only {_queued_tx_bytes()} bytes queued")

        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            try:
                await asyncio.wait_for(_receive_new_stream(wt), timeout=3.0)
            except asyncio.TimeoutError:
                sys.exit("no stream to the second peer: stream creation "
                         "parked behind the vanished peer's bytes")
    finally:
        server.close()


async def _receive_new_stream(wt):
    async for ev in wt.events():
        if isinstance(ev, WebTransportNewStream):
            return


if __name__ == "__main__":
    mode, port, cert, key = sys.argv[1:5]
    asyncio.run({"receive": receive, "unsent": unsent, "starve": starve}[mode](
        int(port), cert, key))
