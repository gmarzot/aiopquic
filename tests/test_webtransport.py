"""WebTransport loopback tests — client + server in one process.

Exercises CONNECT, bidi/uni stream creation, bidi data round-trip
(client→server AND server→client on a peer-opened bidi stream),
uni stream data, FIN, RESET, and graceful close.
"""
import asyncio
import gc
import os
import pytest

from aiopquic._binding._transport import tx_data_bytes_queued
from aiopquic.asyncio.webtransport import (
    WebTransportError, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.events import (
    WebTransportStreamReset, WebTransportStopSending,
    WebTransportNewStream, WebTransportStreamDataReceived,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name

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
        # A suspended reader keeps its last chunk, and with it the sc.
        reader = session.receive_stream_data(stream_id)
        try:
            async for ev in reader:
                if isinstance(ev, WebTransportStreamDataReceived):
                    got.extend(ev.data)
                    if want is not None and len(got) >= want:
                        return
                    if ev.end_stream:
                        return
        finally:
            await reader.aclose()
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            # Process-wide count, read after session setup. Collect earlier
            # tests' garbage first: chunks and stopped transports hold sc refs.
            gc.collect()
            baseline = wt._transport.counters['sc_alive_total']

            for _ in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                wt.send_stream_data(sid, b"x", end_stream=True)
            await asyncio.wait_for(server_drained, timeout=15.0)
            # Give picohttp_callback_free + LINK_RELEASE time to
            # propagate and StreamChunks to dealloc.
            await asyncio.sleep(3.0)

            gc.collect()
            c = wt._transport.counters
            assert (c['sc_create_wt_link'],
                    c['sc_destroy_wt_link_callback_free']) == (
                        n_streams, n_streams), f"counters: {c}"
            final = c['sc_alive_total']
            delta = final - baseline
            assert delta == 0, (
                f"sc_alive_total leaked across {n_streams} sender-side "
                f"streams: baseline={baseline} final={final} "
                f"(delta={delta})\n"
                f"counters: {c}")
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
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
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=True)
            assert wt.set_stream_priority(sid, 2) == 0
            wt._session_closed.set()
            with pytest.raises(ConnectionError):
                wt.set_stream_priority(sid, 2)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_registry_does_not_leak_entries_across_sessions():
    """Serving then closing must retire the dispatcher entry.

    The registry keys on (id(loop), id(transport)) and id() is unique
    only among live objects, so a retained entry keeps a dead pair
    addressable by a later one that lands on the same addresses.
    """
    from aiopquic.asyncio.webtransport import _get_dispatcher_registry

    reg = _get_dispatcher_registry()
    baseline = len(reg._dispatchers)

    async def handler(session):
        pass

    for _ in range(3):
        port = next_port()
        server = await serve_webtransport(
            "127.0.0.1", port, "/wt",
            handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
        try:
            async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
                assert wt.session_ready
        finally:
            server.close()

    assert len(reg._dispatchers) == baseline, (
        f"registry grew from {baseline} to {len(reg._dispatchers)} "
        f"over 3 serve/close cycles")


@pytest.mark.asyncio
async def test_wt_close_resets_streams_with_session_gone():
    """The peer sees WT_SESSION_GONE on this session's streams.

    draft-ietf-webtrans-http3 §6: on termination the endpoint MUST reset
    the send side and abort reading on the receive side of every stream in
    the session, using WT_SESSION_GONE — so the peer can tell session
    teardown from an ordinary stream reset. picowt_deregister would reset
    with code 0 and never stop reading; WT_SESSION_GONE is ours to send.
    """
    from aiopquic.asyncio.webtransport import WT_SESSION_GONE

    port = next_port()
    reset_codes = []
    saw_stream = asyncio.get_event_loop().create_future()

    async def handler(session):
        async def _watch():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    if not saw_stream.done():
                        saw_stream.set_result(ev.stream_id)
                    asyncio.create_task(_watch_stream(session, ev.stream_id))

        async def _watch_stream(session, sid):
            async for sev in session.receive_stream_data(sid):
                if isinstance(sev, WebTransportStreamReset):
                    reset_codes.append(sev.error_code)
                    return
        asyncio.create_task(_watch())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=True)
            wt.send_stream_data(sid, b"hello", end_stream=False)
            await asyncio.wait_for(saw_stream, timeout=5.0)
        # Leaving the context closes the session; the reset rides out with it.
        for _ in range(500):
            if reset_codes:
                break
            await asyncio.sleep(0.01)
    finally:
        server.close()

    assert reset_codes, "peer saw no stream reset after session close"
    assert reset_codes[0] == WT_SESSION_GONE, (
        f"expected WT_SESSION_GONE ({WT_SESSION_GONE:#x}), "
        f"got {reset_codes[0]:#x}")


def _run_close_scenario(mode, **env):
    import subprocess
    import sys
    script = os.path.join(os.path.dirname(__file__), "wt_close_scenarios.py")
    return subprocess.run(
        [sys.executable, script, mode, str(next_port()), CERT_FILE, KEY_FILE],
        env=dict(os.environ, **env),
        capture_output=True, text=True, timeout=180)


def test_wt_close_credits_unsent_bytes():
    """Bytes queued unsent when a session closes are credited at close.

    The process-wide queued total gates stream creation on every
    connection in the process, so it cannot wait for the stream's final
    free, which follows the stream's LINK_RELEASE being drained. Runs in a
    fresh process so the total starts at zero.
    """
    proc = _run_close_scenario("unsent")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


def test_wt_close_while_receiving_survives_poisoned_frees():
    """Closing a session while the peer sends must not touch freed memory.

    A stream's link and sc must outlive the data events still queued for
    it, or drain_rx copies out of a freed ring. MALLOC_PERTURB_ poisons
    freed memory so such a read faults instead of returning stale bytes.
    """
    proc = _run_close_scenario("receive", MALLOC_PERTURB_="165")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


def test_vanished_peer_does_not_stall_other_sessions():
    """A peer that vanishes without closing leaves its bytes queued until
    its connection times out. The server must still open streams to other
    peers: the TX budget is per connection, not per process.
    """
    proc = _run_close_scenario("starve")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


def test_wt_close_resets_and_stops_by_direction():
    """§6 teardown frames go only where the stream has that direction.

    STOP_SENDING on a stream we only send on (RFC 9000 §19.5), or
    RESET_STREAM on one we only receive on (§19.4), is a connection error
    at the peer. A picoquic peer ignores the first when the RESET just
    before it already retired the stream, so the check reads both ends'
    qlogs.
    """
    proc = _run_close_scenario("directions")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


@pytest.mark.asyncio
async def test_wt_stop_sending_on_own_uni_is_dropped():
    """An app STOP_SENDING on our own uni stream is dropped and counted.

    The stream is receive-only at the peer, where STOP_SENDING on it is a
    connection error (RFC 9000 §19.5): a picoquic server closes the
    connection, and the session with it.
    """
    port = next_port()
    srv = {}
    seen = set()
    stops = []
    both_seen = asyncio.Event()

    async def handler(session):
        srv["session"] = session

        async def _read(sid):
            async for ev in session.receive_stream_data(sid):
                if isinstance(ev, WebTransportStreamDataReceived) and ev.data:
                    seen.add(sid)
                    if len(seen) == 2:
                        both_seen.set()

        async for ev in session.events():
            if isinstance(ev, WebTransportNewStream):
                asyncio.create_task(_read(ev.stream_id))
            elif isinstance(ev, WebTransportStopSending):
                stops.append((ev.stream_id, ev.error_code))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            uni = await wt.create_stream(bidir=False)
            wt.send_stream_data(uni, b"x" * 100)
            bidi = await wt.create_stream(bidir=True)
            wt.send_stream_data(bidi, b"y")
            await asyncio.wait_for(both_seen.wait(), timeout=5.0)
            before = wt._transport.counters.get(
                'tx_wrong_direction_dropped', 0)

            wt.stop_stream(uni, 0x10)
            wt.stop_stream(bidi, 0x10)

            try:
                await srv["session"].wait_closed(timeout=0.5)
                closed = True
            except asyncio.TimeoutError:
                closed = False
            assert not closed, (
                "peer closed the session: STOP_SENDING reached a stream "
                "that is receive-only there")
            dropped = wt._transport.counters.get(
                'tx_wrong_direction_dropped', 0) - before
            assert dropped == 1, f"expected 1 dropped request, got {dropped}"
            # Ids only: a received STOP_SENDING's code is not exposed by
            # picoquic's public API, so it surfaces as 0.
            assert [sid for sid, _ in stops] == [bidi], (
                f"peer saw STOP_SENDING {stops}, expected only bidi {bidi}")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_reset_stream_by_direction():
    """reset_stream reaches the peer on our own uni stream. On the peer's
    uni stream, which has no send side here, it is dropped and counted."""
    port = next_port()
    srv = {}
    resets = []
    data_seen = asyncio.Event()
    peer_uni = asyncio.get_event_loop().create_future()

    async def handler(session):
        srv["session"] = session
        sid = await session.create_stream(bidir=False)
        session.send_stream_data(sid, b"s" * 100)

        async def _read(sid):
            async for ev in session.receive_stream_data(sid):
                if isinstance(ev, WebTransportStreamDataReceived) and ev.data:
                    data_seen.set()
                elif isinstance(ev, WebTransportStreamReset):
                    resets.append((sid, ev.error_code))
                    return

        async for ev in session.events():
            if isinstance(ev, WebTransportNewStream):
                asyncio.create_task(_read(ev.stream_id))

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            async def _watch():
                async for ev in wt.events():
                    if (isinstance(ev, WebTransportNewStream)
                            and not peer_uni.done()):
                        peer_uni.set_result(ev.stream_id)
            watcher = asyncio.create_task(_watch())

            uni = await wt.create_stream(bidir=False)
            wt.send_stream_data(uni, b"x" * 100)
            await asyncio.wait_for(data_seen.wait(), timeout=5.0)
            s_uni = await asyncio.wait_for(peer_uni, timeout=5.0)
            before = wt._transport.counters.get(
                'tx_wrong_direction_dropped', 0)

            wt.reset_stream(uni, 0x10)
            wt.reset_stream(s_uni, 0x10)

            for _ in range(200):
                if resets:
                    break
                await asyncio.sleep(0.01)
            watcher.cancel()
            assert resets == [(uni, 0x10)], f"peer saw resets {resets}"
            dropped = wt._transport.counters.get(
                'tx_wrong_direction_dropped', 0) - before
            assert dropped == 1, f"expected 1 dropped request, got {dropped}"
            assert not srv["session"].session_closed
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_reset_releases_queued_bytes():
    """A reset stream sends nothing more, so its queued bytes must leave
    the session's TX backlog and the process total. Otherwise they count
    against the session's TX budget until the session closes."""
    port = next_port()

    async def handler(session):
        async for _ in session.events():
            pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            wt.send_stream_data(sid, b"y" * 1000)
            await asyncio.sleep(0.2)
            # The peer vanishes: nothing more is acked, so the rest queues.
            server.close()
            for _ in range(64):
                try:
                    wt.send_stream_data(sid, b"x" * 65536)
                except BufferError:
                    break
            await asyncio.sleep(0.3)
            stranded = wt._tx_backlog()
            assert stranded > 1 << 20, (
                f"only {stranded} B queued; the test needs a backlog")
            queued = tx_data_bytes_queued()
            producer = asyncio.create_task(
                wt.send_stream_data_drained(sid, b"z" * (1 << 20)))
            await asyncio.sleep(0.1)
            assert not producer.done(), "writer did not park on the full ring"

            wt.reset_stream(sid, 0x10)

            # The parked writer wakes, and later writes fail fast.
            with pytest.raises(WebTransportError):
                await asyncio.wait_for(producer, timeout=2.0)
            with pytest.raises(WebTransportError):
                wt.send_stream_data(sid, b"w")
            for _ in range(200):
                if (wt._tx_backlog() == 0
                        and tx_data_bytes_queued() <= queued - stranded):
                    break
                await asyncio.sleep(0.01)
            assert wt._tx_backlog() == 0, (
                f"session backlog {wt._tx_backlog()} B after the reset")
            assert tx_data_bytes_queued() <= queued - stranded, (
                f"process total {tx_data_bytes_queued()} B, expected at most "
                f"{queued - stranded} B")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_write_after_peer_teardown_raises_connection_error():
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            wt.close()
            with pytest.raises(ConnectionError):
                wt.send_stream_data(sid, b"x")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_write_larger_than_the_stream_ring_raises():
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI, ca_file=CA_FILE) as wt:
            sid = await wt.create_stream(bidir=False)
            with pytest.raises(ValueError):
                wt.send_stream_data(sid, b"x" * (wt.stream_ring_cap + 1))
            wt.send_stream_data(sid, b"y" * 1024)
    finally:
        server.close()
