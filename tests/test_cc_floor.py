"""bbr1-floor: bbr1 with a floor at the media rate, released on a standing
queue or an outage. Rates in path_quality() are bytes per second."""
import asyncio
import os
import random
import ssl
import time

import pytest

from aiopquic._binding._transport import _cc_floor_options
from aiopquic.asyncio import QuicConnectionProtocol, connect, serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
ALPN = "hq-interop"
ALGO = "bbr1-floor"

MEDIA_BPS = 4_000_000
TICK = 0.01
ENGAGED, QUEUE_GUARD, LOSS_GUARD = 0x100, 0x200, 0x400

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


@pytest.mark.parametrize("opts,expected", [
    ("", (1.25, 0)),
    ("K1.5", (1.5, 0)),
    ("R20000000", (1.25, 20_000_000)),
    ("K2R8000000", (2.0, 8_000_000)),
    ("T50000Q0.02K1.1", (1.1, 0)),
    ("zzK1.75zz", (1.75, 0)),
    ("K.5", (0.5, 0)),
    ("K1.2.3", (1.2, 0)),
    ("K", (1.25, 0)),
    ("Kx", (1.25, 0)),
    ("K0", (1.25, 0)),
    ("K99", (1.25, 0)),
    ("R", (1.25, 0)),
    ("R-5", (1.25, 0)),
    ("R" + "9" * 30, (1.25, 0)),
    ("R1000000000000", (1.25, 1_000_000_000_000)),
    ("R1000000000001", (1.25, 0)),
    ("K1.5R", (1.5, 0)),
])
def test_option_string(opts, expected):
    k, rate_bps = _cc_floor_options(opts)
    assert (k, rate_bps) == (pytest.approx(expected[0]), expected[1])


def test_configuration_accepts_bbr1_floor():
    cfg = QuicConfiguration(congestion_control_algorithm=ALGO,
                            congestion_control_options="K1.5")
    assert cfg.congestion_control_algorithm == ALGO


class _Sink(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = 0

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += len(event.data)


class _Relay(asyncio.DatagramProtocol):
    """Client <-> server with a one-way delay; drops client packets at
    random while `loss` is set, and every packet while `down`."""

    def __init__(self, server_port, delay, loss=0.0):
        self.server_port = server_port
        self.delay = delay
        self.loss = loss
        self.down = False
        self.client = None
        self.rng = random.Random(7)

    def connection_made(self, transport):
        self.transport = transport
        self.loop = asyncio.get_running_loop()

    def datagram_received(self, data, addr):
        from_server = addr[1] == self.server_port
        if not from_server:
            self.client = addr
            if self.loss and self.rng.random() < self.loss:
                return
        if self.down:
            return
        dst = self.client if from_server else ("127.0.0.1", self.server_port)
        if dst is not None:
            self.loop.call_later(self.delay, self.transport.sendto, data, dst)


async def _session(algo, opts, delay=None, loss=0.0):
    """A server sink, an optional relay, and a client configuration."""
    port = next_port()
    sinks = []

    def make(conn, stream_handler=None):
        sinks.append(_Sink(conn, stream_handler=stream_handler))
        return sinks[-1]

    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg,
                         create_protocol=make)
    relay = None
    dial = port
    if delay is not None:
        dial = next_port()
        _, relay = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: _Relay(port, delay, loss), local_addr=("127.0.0.1", dial))
    ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             verify_mode=ssl.CERT_NONE,
                             congestion_control_algorithm=algo,
                             congestion_control_options=opts)
    return server, relay, dial, ccfg, sinks


def _close(server, relay):
    if relay is not None:
        relay.transport.close()
    server.close()


async def _send_media(quic, seconds, on_tick=None):
    """Writes MEDIA_BPS of stream data in TICK slices for `seconds`."""
    sid = quic.get_next_available_stream_id()
    chunk = b"m" * int(MEDIA_BPS / 8 * TICK)
    t0 = time.monotonic()
    sent = 0
    while (elapsed := time.monotonic() - t0) < seconds:
        while sent < (int(elapsed / TICK) + 1) * len(chunk):
            try:
                quic.send_stream_data(sid, chunk)
            except BufferError:
                break
            sent += len(chunk)
        if on_tick is not None:
            on_tick(elapsed)
        await asyncio.sleep(TICK)


async def test_loopback_transfer_reports_bbr1_state():
    server, relay, port, ccfg, sinks = await _session(ALGO, None)
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id()
            quic.send_stream_data(sid, b"y" * (1 << 20), end_stream=True)
            async with asyncio.timeout(10):
                while not (sinks and sinks[0].received == 1 << 20):
                    await asyncio.sleep(0.01)
            q = quic.path_quality()
            assert q["cc_state"] & 0xff in range(5), q
    finally:
        _close(server, relay)


async def test_estimate_tracks_an_app_limited_stream():
    server, relay, port, ccfg, sinks = await _session(ALGO, None)
    seen = []
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic

            def sample(elapsed):
                if elapsed > 2.5:
                    seen.append(quic.path_quality())

            await _send_media(quic, 4.0, sample)
    finally:
        _close(server, relay)
    q = seen[-1]
    estimate = q["cc_state"] >> 32
    floor = q["cc_param"] >> 32
    # Acked bytes carry QUIC framing on top of the stream bytes.
    assert 0.95 * MEDIA_BPS / 8 <= estimate <= 1.15 * MEDIA_BPS / 8, q
    assert q["cc_state"] & ENGAGED, q
    assert abs(floor - 1.25 * estimate) <= 1, q
    assert q["pacing_rate"] >= floor, q


async def _delivered_under_loss(algo, opts):
    """Stream bytes per second delivered over the last 3 s of 5 s at 25%
    random loss on a 40 ms path, and the client's last path_quality()."""
    server, relay, port, ccfg, sinks = await _session(algo, opts, 0.02, 0.25)
    marks = {}
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic

            def mark(elapsed):
                if "t2" not in marks and elapsed >= 2.0:
                    marks["t2"] = (time.monotonic(), sinks[0].received)

            await _send_media(quic, 5.0, mark)
            t2, rx2 = marks["t2"]
            rate = (sinks[0].received - rx2) / (time.monotonic() - t2)
            return rate, quic.path_quality()
    finally:
        _close(server, relay)


async def test_floor_holds_the_media_rate_under_loss():
    media = MEDIA_BPS / 8
    # bbr1 alone falls far behind, or the impairment proves nothing.
    plain, _ = await _delivered_under_loss("bbr1", None)
    assert plain < 0.6 * media, plain
    held, q = await _delivered_under_loss(ALGO, f"R{MEDIA_BPS}")
    assert held >= 0.85 * media, (held, q)
    assert q["cc_state"] & ENGAGED, q


async def test_outage_releases_the_floor_until_it_clears():
    server, relay, port, ccfg, sinks = await _session(
        ALGO, f"R{MEDIA_BPS}", 0.02)
    states = []
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic

            def tick(elapsed):
                relay.down = 1.0 <= elapsed < 2.0
                states.append((elapsed, quic.path_quality().get("cc_state", 0)))

            await _send_media(quic, 3.5, tick)
    finally:
        _close(server, relay)
    before = [s for t, s in states if 0.5 < t < 1.0]
    during = [s for t, s in states if 1.2 < t < 2.0]
    after = [s for t, s in states if t > 3.0]
    assert before and all(s & ENGAGED for s in before), states
    assert any(s & LOSS_GUARD and not s & ENGAGED for s in during), states
    assert after and after[-1] & ENGAGED, states
