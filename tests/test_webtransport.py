"""WebTransport loopback tests — client + server in one process.

Exercises CONNECT, bidi/uni stream creation, bidi data round-trip
(client→server AND server→client on a peer-opened bidi stream),
uni stream data, FIN, RESET, and graceful close.
"""
import asyncio
import os
import pytest

from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.events import (
    WebTransportNewStream, WebTransportStreamDataReceived,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

try:
    from ._ports import next_port
except ImportError:      # loaded bare, outside the package (bench helpers)
    from _ports import next_port


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


async def _drain_stream(session, stream_id, *, want=None, timeout=5.0):
    """Collect bytes from stream_id until FIN or `want` bytes received."""
    got = bytearray()
    async def _collect():
        async for ev in session.receive_stream_data(stream_id):
            if isinstance(ev, WebTransportStreamDataReceived):
                got.extend(ev.data)
                if want is not None and len(got) >= want:
                    return
                if ev.end_stream:
                    return
    await asyncio.wait_for(_collect(), timeout=timeout)
    return bytes(got)


@pytest.mark.asyncio
async def test_wt_session_open_close():
    """CONNECT round-trip + clean close."""
    port = next_port()
    accepted = asyncio.Event()

    async def handler(session):
        accepted.set()

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            assert wt.session_ready
            await asyncio.wait_for(accepted.wait(), timeout=2.0)
        # connect_webtransport closes on exit
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_bidi_client_to_server():
    """Client opens bidi WT stream and sends bytes; server receives."""
    port = next_port()
    server_got = asyncio.get_event_loop().create_future()

    async def handler(session):
        async def _recv():
            async for ev in session.events():
                # First NewStream surfaces the peer-opened bidi
                if isinstance(ev, WebTransportNewStream):
                    data = await _drain_stream(
                        session, ev.stream_id, want=5)
                    server_got.set_result(data)
                    return
        asyncio.create_task(_recv())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b"hello", end_stream=False)
            data = await asyncio.wait_for(server_got, timeout=5.0)
            assert data == b"hello"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_bidi_server_replies_on_peer_stream():
    """Server replies on a CLIENT-opened bidi WT stream — the path
    aiomoqt's MoQT control stream relies on. This is the bug
    aiomoqt's WT loopback exposed: server-side TX on a peer-opened
    bidi was never previously exercised.
    """
    port = next_port()

    async def handler(session):
        async def _echo():
            from aiopquic.asyncio.webtransport import WebTransportNewStream
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    sid = ev.stream_id
                    # Echo the first chunk back on the same bidi stream
                    async for sev in session.receive_stream_data(sid):
                        if isinstance(sev, WebTransportStreamDataReceived):
                            session.send_stream_data(
                                sid, b"reply:" + bytes(sev.data),
                                end_stream=False)
                            return
        asyncio.create_task(_echo())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b"ping", end_stream=False)
            data = await _drain_stream(wt, sid, want=len(b"reply:ping"),
                                          timeout=5.0)
            assert data == b"reply:ping"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_uni_client_to_server():
    """Client opens a uni WT stream, sends + FINs, server reads to FIN."""
    port = next_port()
    got = asyncio.get_event_loop().create_future()

    async def handler(session):
        async def _recv():
            from aiopquic.asyncio.webtransport import WebTransportNewStream
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    data = await _drain_stream(
                        session, ev.stream_id, timeout=5.0)
                    got.set_result(data)
                    return
        asyncio.create_task(_recv())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=False)
            wt.send_stream_data(sid, b"unidata", end_stream=True)
            data = await asyncio.wait_for(got, timeout=5.0)
            assert data == b"unidata"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_stream_tx_ctxs_drains_on_stream_close():
    """After N WT uni streams complete and picohttp_callback_free
    fires, the client's _stream_tx_ctxs dict should drain back to
    empty. Otherwise the dict leaks borrowed sc pointers that the
    C side has already freed via LINK_RELEASE.

    Parallel to test_sc_alive_returns_to_baseline_across_streams
    in tests/bench/test_rx_fc_counters.py (raw QUIC variant). The
    raw QUIC dict self-cleans via STREAM_DESTROY surfacing landed
    in 0.3.6 Step 3; this is the WT side."""
    port = next_port()
    n_streams = 50
    server_drained = asyncio.get_event_loop().create_future()
    drained_count = 0

    async def handler(session):
        async def _recv():
            nonlocal drained_count
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    await _drain_stream(
                        session, ev.stream_id, timeout=5.0)
                    drained_count += 1
                    if (drained_count >= n_streams
                            and not server_drained.done()):
                        server_drained.set_result(True)
                        return
        asyncio.create_task(_recv())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            for _ in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"x", end_stream=True)
            await asyncio.wait_for(server_drained, timeout=10.0)
            # Give picohttp_callback_free time to fire and the SPSC
            # WT_STREAM_DESTROY event to surface to the dispatcher.
            await asyncio.sleep(3.0)
            leaked = len(wt._stream_tx_ctxs)
            assert leaked == 0, (
                f"_stream_tx_ctxs leaked: {leaked} stale entries "
                f"after {n_streams} streams completed")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_sender_side_sc_returns_to_baseline_across_streams():
    """N WT uni streams (client-initiated, sender side) — after all
    complete and picohttp_callback_free + LINK_RELEASE fire, process-
    wide sc_alive_total must return to its starting value.

    Tier 1 verification: confirms patch 0004 (mark_active_stream NULL
    preserves app_stream_ctx) cured publisher-side memory accumulation
    under stream churn. Without 0004 the sender side's app_stream_ctx
    is zeroed mid-session so stream_released never fires on the
    sender, the link's sc ref is never dropped, and sc_alive_total
    grows linearly with stream count for the session's lifetime.

    Parallel to test_sc_alive_returns_to_baseline_across_streams in
    tests/bench/test_rx_fc_counters.py (raw-QUIC version)."""
    port = next_port()
    n_streams = 200
    server_drained = asyncio.get_event_loop().create_future()
    drained_count = 0

    async def handler(session):
        async def _recv():
            nonlocal drained_count
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    await _drain_stream(
                        session, ev.stream_id, timeout=5.0)
                    drained_count += 1
                    if (drained_count >= n_streams
                            and not server_drained.done()):
                        server_drained.set_result(True)
                        return
        asyncio.create_task(_recv())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            # Capture baseline AFTER session setup so control/CONNECT
            # streams are already accounted for.
            baseline = wt._transport.counters['sc_alive_total']

            for _ in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"x", end_stream=True)
            await asyncio.wait_for(server_drained, timeout=15.0)
            # Give picohttp_callback_free + LINK_RELEASE time to
            # propagate and StreamChunks to dealloc.
            await asyncio.sleep(3.0)

            final = wt._transport.counters['sc_alive_total']
            delta = final - baseline
            assert delta == 0, (
                f"sc_alive_total leaked across {n_streams} sender-side "
                f"streams: baseline={baseline} final={final} "
                f"(delta={delta})\n"
                f"counters: {wt._transport.counters}")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_datagram_client_to_server():
    """Client sends a WT datagram; server receives the payload alone
    (h3zero owns the RFC 9297 quarter-stream-id prefix)."""
    from aiopquic.quic.events import WebTransportDatagramReceived
    port = next_port()
    got: asyncio.Queue = asyncio.Queue()

    async def handler(session):
        async for ev in session.events():
            if isinstance(ev, WebTransportDatagramReceived):
                await got.put(bytes(ev.data))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            assert wt.send_datagram_frame(b"hello-datagram") == 14
            payload = await asyncio.wait_for(got.get(), timeout=5.0)
            assert payload == b"hello-datagram"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_datagram_oversize_is_refused():
    """A payload past the record cap can never drain, so it raises
    rather than queuing forever."""
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            with pytest.raises(ValueError):
                wt.send_datagram_frame(b"x" * 4096)
    finally:
        server.close()


async def _wait_counter(transport, key, target, timeout=2.0):
    """Wait for a worker counter to reach `target`; return its value."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        v = transport.counters[key]
        if v >= target:
            return v
        await asyncio.sleep(0.01)
    return transport.counters[key]


@pytest.mark.asyncio
async def test_wt_set_stream_priority_reaches_picoquic():
    """picoquic accepts a priority set on an open WT stream.

    The WT priority handler is dispatched ahead of the raw stale-cnx
    guard, so raw-side counting covers none of it; this is the only
    coverage that path has.
    """
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b"hello", end_stream=False)
            tx = wt._transport
            before = tx.counters['set_priority_applied']

            assert wt.set_stream_priority(sid, 2) == 0
            assert await _wait_counter(
                tx, 'set_priority_applied', before + 1) == before + 1
            assert tx.counters['set_priority_rejected'] == 0

            # Still usable: re-prioritising mid-stream is the point.
            assert wt.set_stream_priority(sid, 200) == 0
            assert await _wait_counter(
                tx, 'set_priority_applied', before + 2) == before + 2
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_set_stream_priority_on_closed_session_raises():
    """A closed session raises rather than reporting a posted priority.

    Matches send_datagram_frame; stop_stream is a silent no-op instead.
    Note leaving the context manager does not itself mark the session
    closed — both guards key on _session_closed.
    """
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=True)
            assert wt.set_stream_priority(sid, 2) == 0
            wt._session_closed.set()
            with pytest.raises(ConnectionError):
                wt.set_stream_priority(sid, 2)
    finally:
        server.close()
