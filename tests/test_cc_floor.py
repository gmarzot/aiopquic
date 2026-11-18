"""bbr1-floor and bbr-floor: bbr1 or bbr with a floor above the media rate,
capped below the bottleneck, released on RTT growth or an outage. Rates in
path_quality() are bytes per second."""
import asyncio
import os
import random
import ssl
import time

import pytest

from aiopquic._binding._transport import (
    _cc_floor_options, _cc_floor_queue_guard, _cc_floor_rate)
from aiopquic.asyncio import QuicConnectionProtocol, connect, serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
ALPN = "hq-interop"
ALGOS = ["bbr1-floor", "bbr-floor"]

MEDIA_BPS = 4_000_000
TICK = 0.01
ENGAGED, QUEUE_GUARD, LOSS_GUARD = 0x100, 0x200, 0x400

pytestmark = pytest.mark.skipif(not os.path.exists(CERT_FILE),
                                reason="picoquic certs not found")


@pytest.mark.parametrize("opts,expected", [
    ("", (0.0, 0)),
    ("K1.5", (1.5, 0)),
    ("R20000000", (0.0, 20_000_000)),
    ("K2R8000000", (2.0, 8_000_000)),
    ("T50000Q0.02K1.1", (1.1, 0)),
    ("zzK1.75zz", (1.75, 0)),
    ("K.5", (0.5, 0)),
    ("K1.2.3", (1.2, 0)),
    ("K", (0.0, 0)),
    ("Kx", (0.0, 0)),
    ("K0", (0.0, 0)),
    ("K99", (0.0, 0)),
    ("R", (0.0, 0)),
    ("R-5", (0.0, 0)),
    ("R" + "9" * 30, (0.0, 0)),
    ("R1000000000000", (0.0, 1_000_000_000_000)),
    ("R1000000000001", (0.0, 0)),
    ("K1.5R", (1.5, 0)),
])
def test_option_string(opts, expected):
    k, rate_bps = _cc_floor_options(opts)
    assert (k, rate_bps) == (pytest.approx(expected[0]), expected[1])


BIG = 10**12


@pytest.mark.parametrize("args,expected", [
    # media, k, loss, loss_min, bw_max
    ((0, 0.0, 0.0, 0.0, BIG), 0),
    # Headroom = loss / (1 - loss) + 5%, at most 100%.
    ((1000, 0.0, 0.0, 0.0, BIG), 1050),
    ((1000, 0.0, 0.2, 0.0, BIG), 1300),
    ((1000, 0.0, 0.45, 0.0, BIG), 1868),
    ((1000, 0.0, 0.9, 0.0, BIG), 2000),
    # K fixes the ratio.
    ((1000, 1.5, 0.2, 0.0, BIG), 1500),
    ((1000, 0.5, 0.0, 0.0, BIG), 500),
    # Capped at bw_max / (1 - loss_min) ...
    ((1000, 0.0, 0.2, 0.0, 1100), 1100),
    ((1000, 0.0, 0.2, 0.2, 1000), 1250),
    ((1000, 0.0, 0.2, 0.9, 1000), 1300),
    ((1000, 2.0, 0.0, 0.0, 1500), 1500),
    # ... but never below the media rate, or K x media for K < 1.
    ((1000, 0.0, 0.2, 0.0, 900), 1000),
    ((1000, 0.0, 0.2, 0.0, 0), 1000),
    ((1000, 0.5, 0.0, 0.0, 100), 500),
])
def test_floor_rate(args, expected):
    assert abs(_cc_floor_rate(*args) - expected) <= 1


@pytest.mark.parametrize("held,base,wmin,expected", [
    # 40 ms baseline: growth limit max(10 ms, 10 ms), cleared at 5 ms.
    (False, 40_000, 50_000, False),
    (False, 40_000, 50_001, True),
    (True, 40_000, 45_001, True),
    (True, 40_000, 45_000, False),
    (True, 40_000, 30_000, False),
    # 400 ms baseline: limit 100 ms.
    (False, 400_000, 490_000, False),
    (False, 400_000, 500_001, True),
    (True, 400_000, 449_000, False),
    # 1 ms baseline: the 10 ms minimum.
    (False, 1_000, 11_001, True),
    # No baseline or no window yet: unchanged.
    (True, 0, 500_000, True),
    (False, 0, 500_000, False),
    (True, 40_000, 2**64 - 1, True),
])
def test_queue_guard(held, base, wmin, expected):
    assert _cc_floor_queue_guard(held, base, wmin) is expected


@pytest.mark.parametrize("algo", ALGOS)
def test_configuration_accepts_floor(algo):
    cfg = QuicConfiguration(congestion_control_algorithm=algo,
                            congestion_control_options="K1.5")
    assert cfg.congestion_control_algorithm == algo


class _Sink(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = 0

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.received += len(event.data)


class _Relay(asyncio.DatagramProtocol):
    """Client <-> server with a one-way `delay`; drops client packets at
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


async def _fresh_quality(quic):
    """path_quality() as of now: a call returns the previous snapshot and
    asks for the next."""
    quic.path_quality()
    await asyncio.sleep(0.05)
    return quic.path_quality()


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


@pytest.mark.parametrize("algo,opts", [
    ("bbr1-floor", None),
    ("bbr-floor", None),
    ("bbr1-floor", "K1.5R8000000"),
    ("bbr-floor", "K1.5R8000000"),
])
async def test_loopback_transfer_reports_inner_state(algo, opts):
    server, relay, port, ccfg, sinks = await _session(algo, opts)
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic
            sid = quic.get_next_available_stream_id()
            quic.send_stream_data(sid, b"y" * (1 << 20), end_stream=True)
            async with asyncio.timeout(10):
                while not (sinks and sinks[0].received == 1 << 20):
                    await asyncio.sleep(0.01)
            q = await _fresh_quality(quic)
            # bbr1: Startup..ProbeRTT (0-4); bbr: Startup..ProbeRTT (0-6).
            assert q["cc_state"] & 0xff in range(7), q
            if opts is not None:
                assert q["cc_state"] >> 32 == 1_000_000, q
                # K x media rate at most, capped by the delivery rate seen.
                assert 1_000_000 <= q["cc_param"] >> 32 <= 1_500_000, q
    finally:
        _close(server, relay)


@pytest.mark.parametrize("algo", ALGOS)
async def test_estimate_tracks_an_app_limited_stream(algo):
    server, relay, port, ccfg, sinks = await _session(algo, None)
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
    # No loss: 5% headroom.
    assert estimate <= floor <= 1.1 * estimate, q
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
            return rate, await _fresh_quality(quic)
    finally:
        _close(server, relay)


async def test_plain_bbr1_falls_behind_under_loss():
    # Or the impairment in the floor test proves nothing.
    plain, _ = await _delivered_under_loss("bbr1", None)
    assert plain < 0.6 * MEDIA_BPS / 8, plain


@pytest.mark.parametrize("algo", ALGOS)
@pytest.mark.parametrize("k", [None, 1.3])
async def test_floor_holds_the_media_rate_under_loss(algo, k):
    media = MEDIA_BPS / 8
    opts = f"R{MEDIA_BPS}" if k is None else f"K{k}R{MEDIA_BPS}"
    held, q = await _delivered_under_loss(algo, opts)
    assert held >= 0.85 * media, (held, q)
    assert q["cc_state"] & ENGAGED, q
    floor = q["cc_param"] >> 32
    if k is None:
        # 25% loss: 1 + 1/3 + 5%; the cap is not binding.
        assert 1.3 * media <= floor <= 1.5 * media, q
    else:
        assert 1.2 * media <= floor <= k * media + 1, q


@pytest.mark.parametrize("algo", ALGOS)
async def test_rtt_growth_releases_the_floor_until_it_settles(algo):
    server, relay, port, ccfg, sinks = await _session(
        algo, f"R{MEDIA_BPS}", 0.02)
    states = []
    try:
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic

            def tick(elapsed):
                relay.delay = 0.08 if 1.5 <= elapsed < 3.5 else 0.02
                states.append((elapsed, quic.path_quality().get("cc_state", 0)))

            await _send_media(quic, 5.5, tick)
    finally:
        _close(server, relay)
    before = [s for t, s in states if 1.0 < t < 1.5]
    during = [s for t, s in states if 2.5 < t < 3.5]
    after = [s for t, s in states if t > 5.0]
    assert before and all(s & ENGAGED for s in before), states
    assert any(s & QUEUE_GUARD and not s & ENGAGED for s in during), states
    assert after and after[-1] & ENGAGED and not after[-1] & QUEUE_GUARD, states


@pytest.mark.parametrize("algo", ALGOS)
async def test_outage_releases_the_floor_until_it_clears(algo):
    server, relay, port, ccfg, sinks = await _session(
        algo, f"R{MEDIA_BPS}", 0.02)
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
