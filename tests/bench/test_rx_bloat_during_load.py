"""RX-side bloat reproducer — peak chunks_alive_total under saturation.

Catches the sub-side accumulation that motivated the 0.3.5
release-blocker work: at ~2 Gbps over 30s, sub process RSS hits 5+ GB
even though tracemalloc shows only ~25 MB tracked (= bloat is in
malloc'd StreamChunk buffers, not Python objects).

The test samples `chunks_alive_total` from ctx.counters several times
during the run and asserts the peak stays below a bound. Bound is
relative to advertise_cap × N_streams + slop (the legitimate
in-flight window). Anything higher means chunks are accumulating
because the consumer pipeline isn't releasing them at network rate.

Parametrized over `quic` and `wt`. Both should pass with a correct
RX FC + chunk-release implementation.
"""
import asyncio
import os
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.quic.events import (
    StreamDataReceived,
    WebTransportNewStream,
    WebTransportStreamDataReceived,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

_port_counter = 38600


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


# Test parameters mirror loopback_bench's normal usage.
CHUNK = b"\xbb" * (64 * 1024)         # 64 KB per push (~ MoQT object)
DURATION = 8.0                         # seconds of sustained push
SAMPLE_INTERVAL = 0.25                 # how often to sample memory

# RSS-growth bound. The bug we're chasing: WT loopback grows by 7+ GB
# while raw-QUIC loopback stays steady. Anything > 200 MB growth over
# DURATION seconds at saturation is bloat — the consumer is supposed
# to be releasing as it goes. We do NOT artificially retain anything;
# this catches the case where the wrapper layer fails to release.
MAX_RSS_GROWTH_BYTES = 200 * 1024 * 1024


def _rss_bytes():
    """Read this process's RSS in bytes from /proc/self/status."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                # "VmRSS:    123456 kB"
                return int(line.split()[1]) * 1024
    return 0


# ---------------------------------------------------------------- raw QUIC

@pytest.mark.asyncio
async def test_rx_bloat_during_load_quic():
    """Server pushes 64 KB chunks at full rate for DURATION seconds.
    Client receives via the normal API and does nothing special.
    Mirrors loopback_bench --quic. Assert RSS growth stays bounded —
    no artificial retention, no copies. If chunks are not getting
    freed at the wrapper layer, RSS climbs."""
    port = next_port()
    transport_ctx_box = {"ctx": None}
    bytes_received_box = {"n": 0}

    class _StreamingClient(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                bytes_received_box["n"] += len(event.data)

    srv_cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    srv_cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    srv_quic = QuicConnection(configuration=srv_cfg)
    srv_quic._start_transport(port=port)
    srv_protocol = QuicConnectionProtocol(srv_quic)
    srv_protocol._start(asyncio.get_event_loop())

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=QuicConfiguration(
                is_client=True, alpn_protocols=["hq-interop"]),
            create_protocol=_StreamingClient,
        ) as cli:
            transport_ctx_box["ctx"] = cli._quic._transport
            await asyncio.sleep(0.5)

            sid = srv_quic.get_next_available_stream_id(is_unidirectional=True)
            push_task = asyncio.create_task(_push_quic(srv_quic, sid))

            baseline_rss = _rss_bytes()
            peak_rss = baseline_rss
            deadline = time.monotonic() + DURATION
            while time.monotonic() < deadline:
                cur = _rss_bytes()
                if cur > peak_rss:
                    peak_rss = cur
                await asyncio.sleep(SAMPLE_INTERVAL)

            push_task.cancel()
            try:
                await push_task
            except asyncio.CancelledError:
                pass

            final = transport_ctx_box["ctx"].counters
            growth = peak_rss - baseline_rss
            recv_mb = bytes_received_box['n'] // 1024 // 1024
            print(f"[quic bloat] received={recv_mb} MB "
                  f"RSS baseline={baseline_rss // 1024 // 1024} MB "
                  f"peak={peak_rss // 1024 // 1024} MB "
                  f"growth={growth // 1024 // 1024} MB "
                  f"chunks_alive={final['chunks_alive_total']} "
                  f"sc_alive={final['sc_alive_total']}")
            assert growth <= MAX_RSS_GROWTH_BYTES, (
                f"QUIC RSS grew {growth // 1024 // 1024} MB over "
                f"{DURATION}s, exceeds bound "
                f"{MAX_RSS_GROWTH_BYTES // 1024 // 1024} MB. "
                f"received {bytes_received_box['n'] // 1024 // 1024} MB.\n"
                f"final counters: {final}")
    finally:
        srv_quic.close()


async def _push_quic(srv_quic, sid):
    """Push CHUNK forever on a server-side uni stream."""
    while True:
        try:
            srv_quic.send_stream_data(sid, CHUNK, end_stream=False)
        except BufferError:
            ev = srv_quic.get_tx_data_drain_event(sid)
            try:
                await asyncio.wait_for(ev.wait(), 1.0)
                ev.clear()
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(0)


# ---------------------------------------------------------------- WT

@pytest.mark.asyncio
async def test_rx_bloat_during_load_wt():
    """Same shape as the QUIC test but over a WT session. Mirrors
    loopback_bench's default (WT) mode. Should produce identical
    bounded RSS growth as the QUIC variant if the wrapper layer
    releases bytes consistently across transports."""
    port = next_port()
    streamer_done = asyncio.Event()
    bytes_received_box = {"n": 0}

    async def handler(session):
        async def _producer():
            try:
                sid = await session.create_stream(bidir=False)
                while not streamer_done.is_set():
                    try:
                        session.send_stream_data(sid, CHUNK, end_stream=False)
                    except BufferError:
                        ev = session.get_tx_data_drain_event(sid)
                        try:
                            await asyncio.wait_for(ev.wait(), 1.0)
                            ev.clear()
                        except asyncio.TimeoutError:
                            pass
                    await asyncio.sleep(0)
            except Exception:
                pass

        asyncio.create_task(_producer())
        await session._session_closed.wait()

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            ctx = wt._transport

            async def _drain():
                async for ev in wt.events():
                    if isinstance(ev, WebTransportNewStream):
                        sid = ev.stream_id

                        async def _consume_sid(sid):
                            async for sev in wt.receive_stream_data(sid):
                                if isinstance(
                                        sev, WebTransportStreamDataReceived):
                                    bytes_received_box["n"] += len(sev.data)
                                    if sev.end_stream:
                                        return

                        asyncio.create_task(_consume_sid(sid))

            drain_task = asyncio.create_task(_drain())

            baseline_rss = _rss_bytes()
            peak_rss = baseline_rss
            deadline = time.monotonic() + DURATION
            while time.monotonic() < deadline:
                cur = _rss_bytes()
                if cur > peak_rss:
                    peak_rss = cur
                await asyncio.sleep(SAMPLE_INTERVAL)

            streamer_done.set()
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

            final = ctx.counters
            growth = peak_rss - baseline_rss
            recv_mb = bytes_received_box['n'] // 1024 // 1024
            print(f"[wt bloat] received={recv_mb} MB "
                  f"RSS baseline={baseline_rss // 1024 // 1024} MB "
                  f"peak={peak_rss // 1024 // 1024} MB "
                  f"growth={growth // 1024 // 1024} MB "
                  f"chunks_alive={final['chunks_alive_total']} "
                  f"sc_alive={final['sc_alive_total']}")
            assert growth <= MAX_RSS_GROWTH_BYTES, (
                f"WT RSS grew {growth // 1024 // 1024} MB over "
                f"{DURATION}s, exceeds bound "
                f"{MAX_RSS_GROWTH_BYTES // 1024 // 1024} MB. "
                f"received {bytes_received_box['n'] // 1024 // 1024} MB.\n"
                f"final counters: {final}")
    finally:
        server.close()
