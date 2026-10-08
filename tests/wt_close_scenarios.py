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

    python tests/wt_close_scenarios.py directions PORT CERT KEY
        Each end holds an open uni and bidi stream; the client closes the
        session, then the server closes a second one. From both ends'
        qlogs: the closing end sent WT_SESSION_GONE frames, RESET_STREAM_AT
        on the streams it opened (WT §4.4; both ends negotiate
        reset_stream_at) and STOP_SENDING on those it receives on, and no
        end sent STOP_SENDING on a stream it only sends on or a reset on one
        it only receives on. Exits 0 when both hold.
"""
import asyncio
import glob
import json
import os
import re
import sys
import tempfile
import time

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.webtransport import (
    WT_SESSION_GONE, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
SNI = "test.example.com"  # the test certificate's name
CA_FILE = None            # set from the cert path in main()

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
    async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        transport_a.start(is_client=True, alpn="h3", ca_file=CA_FILE,
                          max_datagram_frame_size=64 * 1024)
        try:
            async with connect_webtransport(
                    "127.0.0.1", port, "/wt", sni=SNI, transport=transport_a):
                await asyncio.wait_for(a_streams.wait(), timeout=5.0)
                transport_a.stop()
                a_gone.set()
                await asyncio.wait_for(a_loaded.wait(), timeout=5.0)
        except Exception:
            pass
        if _queued_tx_bytes() <= cap:
            sys.exit(f"precondition: only {_queued_tx_bytes()} bytes queued")

        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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


def _frames_sent(path):
    """(frame_type, stream_id, error_code) of every RESET_STREAM,
    RESET_STREAM_AT and STOP_SENDING one endpoint sent."""
    with open(path) as f:
        text = f.read()
    # picoquic writes this transport parameter without its opening brace.
    text = re.sub(r'"version_negotiation": ("chosen": [^}]*})',
                  r'"version_negotiation": {\1', text)
    out = []
    for _t, _cat, name, data in json.loads(text)["traces"][0]["events"]:
        if name != "packet_sent":
            continue
        for fr in data.get("frames", []):
            if fr.get("frame_type") in (
                    "reset_stream", "reset_stream_at", "stop_sending"):
                out.append((fr["frame_type"], fr["stream_id"], fr["error_code"]))
    return out


def _wrong_direction(frames, is_client):
    """Frames the peer must answer with STREAM_STATE_ERROR: STOP_SENDING
    on our own uni stream (RFC 9000 §19.5), RESET_STREAM on the peer's
    (§19.4)."""
    bad = []
    for ftype, sid, code in frames:
        if (sid & 2) == 0:
            continue
        own = ((sid & 1) == 0) == is_client
        if (ftype == "stop_sending") == own:
            bad.append((ftype, sid, code))
    return bad


async def _hold_streams(session, sids):
    sids["uni"] = await session.create_stream(bidir=False)
    session.send_stream_data(sids["uni"], b"u" * 1000)
    sids["bidi"] = await session.create_stream(bidir=True)
    session.send_stream_data(sids["bidi"], b"b" * 100)


async def _count_data(session, seen):
    async def one(sid):
        async for ev in session.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived) and len(ev.data):
                seen.add(sid)

    async for ev in session.events():
        if isinstance(ev, WebTransportNewStream):
            asyncio.create_task(one(ev.stream_id))


async def _until(cond, what):
    for _ in range(500):
        if cond():
            return
        await asyncio.sleep(0.01)
    sys.exit(f"timed out waiting for {what}")


async def directions(port, cert, key):
    runs = []
    with tempfile.TemporaryDirectory() as root:
        sdir = os.path.join(root, "server-qlog")
        os.makedirs(sdir)
        srv = {}

        async def handler(session):
            srv.update(session=session, seen=set(), sids={})
            asyncio.create_task(_count_data(session, srv["seen"]))
            await _hold_streams(session, srv["sids"])

        server = await serve_webtransport(
            "127.0.0.1", port, "/wt", handler=handler,
            cert_file=cert, key_file=key,
            configuration=QuicConfiguration(is_client=False, qlog_dir=sdir))
        try:
            for closer in ("client", "server"):
                cdir = os.path.join(root, f"{closer}-close")
                os.makedirs(cdir)
                srv.clear()
                seen, sids = set(), {}
                async with connect_webtransport(
                        "127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE,
                        configuration=QuicConfiguration(qlog_dir=cdir)) as wt:
                    asyncio.create_task(_count_data(wt, seen))
                    await _hold_streams(wt, sids)
                    await _until(lambda: len(srv.get("sids", ())) == 2
                                 and len(srv["seen"]) == 2 and len(seen) == 2,
                                 "data on all four streams")
                    if closer == "client":
                        wt.close()
                    else:
                        srv["session"].close()
                        await wt.wait_closed(timeout=5.0)
                    # Loopback: the teardown packet leaves well within this.
                    await asyncio.sleep(0.3)
                [cpath] = glob.glob(os.path.join(cdir, "*.client.qlog"))
                cid = os.path.basename(cpath).split(".")[0]
                c_uni, s_uni = sids["uni"], srv["sids"]["uni"]
                c_bidi, s_bidi = sids["bidi"], srv["sids"]["bidi"]
                # The §6 frames each end must have sent; without them the
                # direction check below would pass vacuously.
                if closer == "client":
                    need = [("client", "reset_stream_at", c_uni),
                            ("client", "stop_sending", s_uni),
                            ("client", "reset_stream_at", c_bidi),
                            ("client", "stop_sending", c_bidi)]
                else:
                    need = [("server", "reset_stream_at", s_uni),
                            ("server", "stop_sending", c_uni),
                            ("server", "reset_stream_at", s_bidi),
                            ("server", "stop_sending", s_bidi),
                            ("client", "reset_stream_at", c_uni)]
                runs.append((closer, cpath,
                             os.path.join(sdir, f"{cid}.server.qlog"), need))
        finally:
            # The server's qlog of each connection is complete only now.
            server.close()

        errors = []
        for closer, cpath, spath, need in runs:
            sent = {"client": _frames_sent(cpath), "server": _frames_sent(spath)}
            for role, ftype, sid in need:
                if (ftype, sid, WT_SESSION_GONE) not in sent[role]:
                    errors.append(f"{closer} close: {role} sent no WT_SESSION_GONE "
                                  f"{ftype} on {sid}; sent {sent[role]}")
            for role in ("client", "server"):
                bad = _wrong_direction(sent[role], role == "client")
                if bad:
                    errors.append(f"{closer} close: {role} sent {bad}")
    if errors:
        sys.exit("\n".join(errors))


if __name__ == "__main__":
    mode, port, cert, key = sys.argv[1:5]
    CA_FILE = os.path.join(os.path.dirname(cert), "test-ca.crt")
    asyncio.run({"receive": receive, "unsent": unsent, "starve": starve,
                 "directions": directions}[mode](int(port), cert, key))
