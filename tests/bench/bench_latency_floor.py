"""Latency-floor microbenches — measure transport overhead at operating
points where the per-stream byte ring stays near-empty (no bufferbloat).

Three patterns:

  1. test_bench_single_object_rtt
     Send one object, fully drain at consumer, measure t_built->t_drain.
     Repeat N times serially. Pure transport overhead per object — the
     absolute floor on what aiopquic can do for a request/reply pattern
     at zero queueing.

  2. test_bench_sub_saturation_sustained
     Send at a target Mbps with monotonic-clock pacing. Ring stays near-
     empty because we sleep between pushes. Measures end-to-end latency
     at typical media rates (50 / 100 / 500 Mbps) without saturation bias.

  3. test_bench_latency_vs_rate
     Sweep target rates over [10, 50, 100, 500, 1000, 2000] Mbps.
     For each, run for a short duration and report p50/p99 latency +
     achieved rate. Reveals the knee where bufferbloat kicks in.

Header per object: (seq:u64, t_built_ns:u64, magic:u64) = 24 B.
"""
import struct
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
)
SPSC_EVT_TX_MARK_ACTIVE = 134

from aiopquic._binding._transport import (
    stream_buf_push, stream_buf_used,
    stream_ctx_create, stream_ctx_destroy,
    stream_ctx_ensure_tx, stream_ctx_get_tx,
)


HEADER_FMT = "<QQQ"
HEADER_LEN = struct.calcsize(HEADER_FMT)
MAGIC = 0xDEADBEEFCAFEBABE
RING_CAPACITY = 1 << 20


def _build(seq, size, fill=0xBB):
    return struct.pack(HEADER_FMT, seq, time.monotonic_ns(), MAGIC) + \
           bytes([fill]) * (size - HEADER_LEN)


def _q(sorted_vals, q):
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * q)))
    return sorted_vals[idx]


def _drain_objects(server, sid, obj_size, rx_buf, latencies_ns,
                    counters):
    """Drain SPSC events, decode complete objects from rx_buf, append
    latency. counters is {'recv': int, 'bad_magic': int}."""
    for ev in server.drain_rx():
        if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                and ev[1] == sid and ev[2] is not None:
            payload = bytes(ev[2])
            if payload:
                rx_buf.extend(payload)
    while len(rx_buf) >= obj_size:
        seq, t_built, magic = struct.unpack_from(HEADER_FMT, rx_buf, 0)
        t_drain = time.monotonic_ns()
        if magic != MAGIC:
            counters['bad_magic'] += 1
        else:
            latencies_ns.append(t_drain - t_built)
        counters['recv'] += 1
        del rx_buf[:obj_size]


# ---------------------------------------------------------------------------
# 1. Single-object round-trip floor.
# ---------------------------------------------------------------------------
@pytest.mark.bench
@pytest.mark.parametrize("obj_size,n_iter", [
    (1024, 1000),
    (4096, 1000),
    (16384, 500),
], ids=["1K-1k", "4K-1k", "16K-500"])
def test_bench_single_object_rtt(big_ring_pair, obj_size, n_iter, capsys):
    """For each iteration: push one object, wait until it's drained at the
    consumer, then push the next. Zero queueing — single object always
    in flight. Measures the FLOOR on per-object latency."""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)
    try:
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        rx_buf = bytearray()
        latencies_ns = []
        counters = {'recv': 0, 'bad_magic': 0}

        t_start = time.monotonic()
        for seq in range(n_iter):
            obj = _build(seq, obj_size)
            # Spin until ring accepts (should be immediate since we wait
            # for drain between iterations).
            while True:
                if stream_buf_push(sb, obj) == len(obj):
                    break
                time.sleep(0.0)
            client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                           cnx_ptr=client_cnx, stream_ctx=sc)
            client.wake_up()

            # Drain until this seq has been received.
            target = seq + 1
            deadline = time.monotonic() + 1.0
            while counters['recv'] < target and time.monotonic() < deadline:
                _drain_objects(server, sid, obj_size, rx_buf,
                                latencies_ns, counters)
            assert counters['recv'] >= target, \
                f"seq {seq} not received within 1s"
        elapsed = time.monotonic() - t_start

        latencies_ns.sort()
        print(f"\n  --- single-object RTT  obj={obj_size}B  n={n_iter} ---")
        print(f"  total time: {elapsed:.2f}s  ({n_iter / elapsed:.0f} /s)")
        print(f"  latency_us: "
              f"min={latencies_ns[0]/1000:.0f}  "
              f"p50={_q(latencies_ns, 0.50)/1000:.0f}  "
              f"p90={_q(latencies_ns, 0.90)/1000:.0f}  "
              f"p99={_q(latencies_ns, 0.99)/1000:.0f}  "
              f"max={latencies_ns[-1]/1000:.0f}  "
              f"(n={len(latencies_ns)})")
        assert counters['bad_magic'] == 0
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)


# ---------------------------------------------------------------------------
# 2. Sub-saturation sustained latency.
# ---------------------------------------------------------------------------
@pytest.mark.bench
@pytest.mark.parametrize("target_mbps,obj_size,duration_s", [
    (50,   4096, 3.0),
    (100,  4096, 3.0),
    (500,  4096, 3.0),
    (50,   1024, 3.0),
    (100,  1024, 3.0),
], ids=["50M-4K", "100M-4K", "500M-4K", "50M-1K", "100M-1K"])
def test_bench_sub_saturation_sustained(big_ring_pair, target_mbps,
                                          obj_size, duration_s, capsys):
    """Send at target_mbps using monotonic-clock pacing. Ring stays near-
    empty between pushes — this is the 'happy operating point'
    measurement: what does aiomoqt see at typical media rates?"""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)
    try:
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        rx_buf = bytearray()
        latencies_ns = []
        counters = {'recv': 0, 'bad_magic': 0}

        bytes_per_sec = target_mbps * 1_000_000 / 8
        period_ns = int(1e9 * obj_size / bytes_per_sec)

        ring_used_samples = []
        t_start = time.monotonic_ns()
        end_send_ns = t_start + int(duration_s * 1e9)
        sent = 0
        next_send_ns = t_start

        while time.monotonic_ns() < end_send_ns:
            now_ns = time.monotonic_ns()
            if now_ns >= next_send_ns:
                obj = _build(sent, obj_size)
                if stream_buf_push(sb, obj) == len(obj):
                    sent += 1
                    client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                                   cnx_ptr=client_cnx, stream_ctx=sc)
                    client.wake_up()
                    next_send_ns += period_ns
                    if sent % 256 == 0:
                        ring_used_samples.append(stream_buf_used(sb))
            _drain_objects(server, sid, obj_size, rx_buf,
                            latencies_ns, counters)

        # Drain remainder.
        deadline = time.monotonic() + 1.0
        while counters['recv'] < sent and time.monotonic() < deadline:
            _drain_objects(server, sid, obj_size, rx_buf,
                            latencies_ns, counters)
            time.sleep(0.0001)

        elapsed = (time.monotonic_ns() - t_start) / 1e9
        latencies_ns.sort()
        achieved_mbps = (counters['recv'] * obj_size * 8 / 1e6) / elapsed
        avg_ring = (sum(ring_used_samples) / len(ring_used_samples)
                     if ring_used_samples else 0)
        max_ring = max(ring_used_samples) if ring_used_samples else 0

        print(f"\n  --- sub-sat target={target_mbps}Mbps  obj={obj_size}B"
              f"  for {duration_s:.0f}s ---")
        print(f"  achieved: {achieved_mbps:.0f} Mbps "
              f"({counters['recv']} obj, "
              f"{counters['recv'] / elapsed:,.0f}/s)")
        print(f"  latency_us: "
              f"min={latencies_ns[0]/1000:.0f}  "
              f"p50={_q(latencies_ns, 0.50)/1000:.0f}  "
              f"p90={_q(latencies_ns, 0.90)/1000:.0f}  "
              f"p99={_q(latencies_ns, 0.99)/1000:.0f}  "
              f"max={latencies_ns[-1]/1000:.0f}")
        print(f"  ring_used: avg={avg_ring/1024:.1f}KB  "
              f"max={max_ring/1024:.1f}KB  cap={RING_CAPACITY/1024:.0f}KB")
        assert counters['bad_magic'] == 0
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)


# ---------------------------------------------------------------------------
# 3. Latency-vs-rate sweep — finds the knee.
# ---------------------------------------------------------------------------
@pytest.mark.bench
def test_bench_latency_vs_rate(big_ring_pair, capsys):
    """Sweep target rates; report p50/p99 latency + ring fill at each.
    Reveals the bandwidth-vs-latency tradeoff and where bufferbloat
    starts. obj_size fixed at 4 KB (typical media object)."""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0
    obj_size = 4096
    duration_s = 2.0
    # Cap below loopback line rate — above ~2 Gbps the bench's pacing
    # backlog hits the peer's MAX_STREAM_DATA window (peer sees us as
    # overshooting their advertised flow control). The C-side ring
    # overflow guard fires loudly; not a transport bug, just bench
    # overshoot. The interesting range is below the knee anyway.
    targets_mbps = [10, 50, 100, 250, 500, 1000, 1500, 2000]

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)

    print(f"\n  --- latency-vs-rate sweep  obj={obj_size}B  "
          f"per-step={duration_s}s ---")
    print(f"  {'target':>8}  {'achieved':>9}  {'objs/s':>10}  "
          f"{'p50':>5}  {'p90':>5}  {'p99':>5}  {'ring_avg':>9}")

    try:
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        for target_mbps in targets_mbps:
            rx_buf = bytearray()
            latencies_ns = []
            counters = {'recv': 0, 'bad_magic': 0}
            ring_samples = []

            bytes_per_sec = target_mbps * 1_000_000 / 8
            period_ns = int(1e9 * obj_size / bytes_per_sec)

            t_start = time.monotonic_ns()
            end_send_ns = t_start + int(duration_s * 1e9)
            sent = 0
            next_send_ns = t_start

            while time.monotonic_ns() < end_send_ns:
                now_ns = time.monotonic_ns()
                if now_ns >= next_send_ns:
                    obj = _build(sent, obj_size)
                    accepted = stream_buf_push(sb, obj)
                    if accepted == len(obj):
                        sent += 1
                        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                                       cnx_ptr=client_cnx, stream_ctx=sc)
                        client.wake_up()
                        next_send_ns += period_ns
                        if sent % 128 == 0:
                            ring_samples.append(stream_buf_used(sb))
                _drain_objects(server, sid, obj_size, rx_buf,
                                latencies_ns, counters)

            # Drain.
            deadline = time.monotonic() + 0.5
            while counters['recv'] < sent and time.monotonic() < deadline:
                _drain_objects(server, sid, obj_size, rx_buf,
                                latencies_ns, counters)

            elapsed = (time.monotonic_ns() - t_start) / 1e9
            latencies_ns.sort()
            achieved = (counters['recv'] * obj_size * 8 / 1e6) / elapsed
            avg_ring = ((sum(ring_samples) / len(ring_samples))
                         if ring_samples else 0)
            print(f"  {target_mbps:>5}M    {achieved:>6.0f}M  "
                  f"{counters['recv'] / elapsed:>9,.0f}  "
                  f"{_q(latencies_ns, 0.50)/1000:>4.0f}u  "
                  f"{_q(latencies_ns, 0.90)/1000:>4.0f}u  "
                  f"{_q(latencies_ns, 0.99)/1000:>4.0f}u  "
                  f"{avg_ring/1024:>6.0f}KB")
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)
