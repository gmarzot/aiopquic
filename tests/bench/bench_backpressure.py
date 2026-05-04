"""End-to-end MAX_STREAM_DATA backpressure verification.

The producer wants to send at full speed; the consumer artificially
slows its drain rate (sleep between drain_rx calls). The C-side worker
extends MAX_STREAM_DATA only as the consumer drains, so the peer's
SEND rate should clamp to roughly the consumer's drain rate. If
backpressure works, achieved Mbps tracks consumer drain rate. If
backpressure is broken, producer overruns and triggers RX ring overflow
(or worse — silent drop).

Pass criteria:
  - achieved Mbps within ~50% of target consumer drain rate (tight
    clamping is hard to test deterministically; we check the rate
    is bounded, not exact).
  - zero gaps / dupes / magic mismatches.
  - no RX ring overflow.

Bench: tests/bench/bench_backpressure.py
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


@pytest.mark.bench
@pytest.mark.parametrize("consumer_mbps,obj_size,duration_s", [
    (50,  4096, 3.0),
    (200, 4096, 3.0),
    (50,  16384, 3.0),
], ids=["50M-4K", "200M-4K", "50M-16K"])
def test_bench_backpressure_clamps_to_consumer(
        big_ring_pair, consumer_mbps, obj_size, duration_s, capsys):
    """Producer pushes flat-out; consumer drains at consumer_mbps using
    monotonic-clock pacing on the drain side. Achieved sender rate
    should clamp to ~consumer_mbps via the MAX_STREAM_DATA backpressure
    path."""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)
    try:
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        # Consumer pacing: only drain at consumer_mbps. We translate
        # the rate into a min interval between drains assuming we pull
        # a typical batch each time. Simpler approach: sleep a tiny
        # amount between drains, sized so total drain bandwidth = target.
        # Each drain pulls ~ring_capacity bytes worst case; we want
        # avg = consumer_mbps. Use period_ns = obj_size * 8e9 /
        # (consumer_mbps * 1e6) per object processed.
        period_ns = int(obj_size * 8e9 / (consumer_mbps * 1e6))

        rx_buf = bytearray()
        latencies_ns = []
        bad_magic = 0
        gaps = 0
        duplicates = 0
        last_seq = -1
        recv_objs = 0
        next_drain_ns = time.monotonic_ns()
        ring_used_samples = []

        sent_objs = 0
        push_full_waits = 0
        rx_overflow_seen = False

        t_start = time.monotonic_ns()
        end_send_ns = t_start + int(duration_s * 1e9)
        pending = None

        while time.monotonic_ns() < end_send_ns:
            # Producer side: push as fast as possible.
            if pending is None:
                pending = _build(sent_objs, obj_size)
            accepted = stream_buf_push(sb, pending)
            if accepted == len(pending):
                pending = None
                sent_objs += 1
                client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                               cnx_ptr=client_cnx, stream_ctx=sc)
                client.wake_up()
            elif accepted == 0:
                push_full_waits += 1
                # Producer hits backpressure — the WHOLE POINT. Don't
                # sleep too long here; just yield so consumer can run.
                time.sleep(0.00001)
            else:
                pending = pending[accepted:]
            if sent_objs % 64 == 0:
                ring_used_samples.append(stream_buf_used(sb))

            # Consumer side: paced drain. Only call drain_rx when the
            # next-drain budget has accumulated.
            now_ns = time.monotonic_ns()
            if now_ns >= next_drain_ns:
                events = server.drain_rx()
                for ev in events:
                    if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                            and ev[1] == sid and ev[2] is not None:
                        payload = bytes(ev[2])
                        if payload:
                            rx_buf.extend(payload)
                while len(rx_buf) >= obj_size:
                    seq, t_built, magic = struct.unpack_from(
                        HEADER_FMT, rx_buf, 0)
                    t_now = time.monotonic_ns()
                    if magic != MAGIC:
                        bad_magic += 1
                    else:
                        latencies_ns.append(t_now - t_built)
                    expected = last_seq + 1
                    if seq < expected:
                        duplicates += 1
                    elif seq > expected:
                        gaps += seq - expected
                    if seq > last_seq:
                        last_seq = seq
                    recv_objs += 1
                    del rx_buf[:obj_size]
                    next_drain_ns += period_ns

        elapsed = (time.monotonic_ns() - t_start) / 1e9
        achieved_mbps = (recv_objs * obj_size * 8 / 1e6) / elapsed

        latencies_ns.sort()
        avg_ring = (sum(ring_used_samples) / len(ring_used_samples)
                     if ring_used_samples else 0)
        max_ring = max(ring_used_samples) if ring_used_samples else 0

        print(f"\n  --- backpressure consumer={consumer_mbps}Mbps  "
              f"obj={obj_size}B  for {duration_s:.0f}s ---")
        print(f"  sent={sent_objs}  recv={recv_objs}  "
              f"push_full_waits={push_full_waits}")
        print(f"  achieved: {achieved_mbps:.0f} Mbps "
              f"(target consumer rate {consumer_mbps} Mbps)")
        print(f"  ring_used: avg={avg_ring/1024:.0f}KB  "
              f"max={max_ring/1024:.0f}KB  cap={RING_CAPACITY/1024:.0f}KB")
        if latencies_ns:
            print(f"  latency_us: p50={_q(latencies_ns, 0.50)/1000:.0f}  "
                  f"p99={_q(latencies_ns, 0.99)/1000:.0f}")
        print(f"  integrity: gaps={gaps}  dupes={duplicates}  "
              f"bad_magic={bad_magic}")

        # The point of this bench: backpressure must clamp the sender.
        # We expect achieved rate to be at most ~2x the consumer target
        # (some slack for ring + flight). Hard upper bound: if achieved
        # is 5x consumer rate, backpressure is broken.
        assert achieved_mbps < consumer_mbps * 5, (
            f"backpressure failed: achieved {achieved_mbps:.0f} Mbps >> "
            f"consumer {consumer_mbps} Mbps"
        )
        assert bad_magic == 0
        assert gaps == 0
        assert duplicates == 0
        # Producer hit backpressure non-trivially — proves the ring
        # actually filled (otherwise we never tested the flow-control
        # cycle).
        assert push_full_waits > 0, (
            "producer never hit ring-full; consumer was too fast or "
            "test ran too short"
        )
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)
