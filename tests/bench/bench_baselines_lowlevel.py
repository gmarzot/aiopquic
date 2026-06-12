"""Sustained steady-state perf baselines — TRANSPORT-LAYER CEILING.

This suite pushes via the SPSC ring directly (`stream_buf_push` +
`SPSC_EVT_TX_MARK_ACTIVE`), bypassing the higher-level
`QuicConnection.send_stream_data` wrapper. The numbers here are the
ceiling — what picoquic + the SPSC ring + the network thread can
sustain when nothing else is in the way. They are NOT what a
client-library consumer sees through the public API; that's
bench_baselines_highlevel.py.

The two numbers together quantify the cost of the high-level
QuicConnection wrapper and let us track wrapper regressions
independently from transport regressions.

Steady-state (28s window after 2s warmup, 30s total per case):
  obj_size=1K   ~285K obj/s   ~2,330 Mbps
  obj_size=4K   ~ 78K obj/s   ~2,570 Mbps
  obj_size=16K  ~ 20K obj/s   ~2,560 Mbps

Each case asserts:
  * byte conservation (sent == received, magic + seq integrity)
  * achieved throughput >= a minimum floor (regression gate)
  * latency p50 / p99 reported (not asserted; per-host variance)

Run: pytest tests/bench/bench_baselines_lowlevel.py -s -v
"""
from __future__ import annotations

import struct
import sys
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
)
SPSC_EVT_TX_MARK_ACTIVE = 134

from aiopquic._binding._transport import (
    stream_buf_push, stream_buf_used, stream_buf_free,
    stream_ctx_create, stream_ctx_destroy,
    stream_ctx_ensure_tx, stream_ctx_get_tx,
)


HEADER_FMT = "<QQQ"           # u64 seq, u64 t_built_ns, u64 magic
HEADER_LEN = struct.calcsize(HEADER_FMT)
MAGIC = 0xDEADBEEFCAFEBABE
RING_CAPACITY = 1 << 20       # 1 MiB per-stream send buffer


def _build_object(seq, obj_size, fill_byte=0xBB):
    t_built = time.monotonic_ns()
    header = struct.pack(HEADER_FMT, seq, t_built, MAGIC)
    return header + bytes([fill_byte]) * (obj_size - HEADER_LEN)


def _q(sorted_values, q):
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * q)))
    return sorted_values[idx]


# Steady-state minima — Linux-only. Both macOS and Windows have UDP
# loopback ceilings well below these numbers (macOS argo: ~1.1 Gbps
# regardless of obj size — that's the kernel UDP loopback wall, not a
# regression). Tests still measure on those platforms but skip the
# assertion. Crossing below on Linux means a real regression.
#
# Floors set ~20% below clean Ryzen 7 PRO 7840U / WSL2 measurements
# (lowlevel 30s: 1024B=2322, 4096B=2330, 16384B=2332 Mbps). Headroom
# accommodates noise from CPU governor / scheduler.
BASELINE_MIN_MBPS = {
    1024:  1_800,
    4096:  1_900,
    16384: 1_900,
}


@pytest.mark.bench
@pytest.mark.parametrize("obj_size", list(BASELINE_MIN_MBPS.keys()),
                          ids=lambda s: f"{s}B")
def test_bench_sustained_baseline(big_ring_pair, obj_size, bench_duration):
    """Sustained line-rate single-stream baseline. Default 30s = ~25× the
    ring-fill window so buffer-fill transients are < 4% of the run."""
    duration_s = bench_duration
    server, client, client_cnx, _ = big_ring_pair
    sid = 0

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)
    try:
        client.push_tx_event(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        sent_objs = 0
        recv_objs = 0
        push_full_waits = 0
        latencies_ns: list[int] = []
        rx_buf = bytearray()
        last_seq = -1
        bad_magic = 0
        gaps = 0
        duplicates = 0

        # Steady-state window: skip the first 2s of the run when
        # building the latency histogram so initial spin-up doesn't
        # taint percentiles.
        warmup_ns = 2 * 1_000_000_000
        ss_recv_objs = 0
        ss_recv_bytes = 0
        ss_t_first_ns = 0
        ss_t_last_ns = 0

        def _consume(events):
            nonlocal last_seq, bad_magic, gaps, duplicates
            nonlocal recv_objs, ss_recv_objs, ss_recv_bytes
            nonlocal ss_t_first_ns, ss_t_last_ns
            for ev in events:
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    payload = bytes(ev[2])
                    if payload:
                        rx_buf.extend(payload)
            while len(rx_buf) >= obj_size:
                seq, t_built, magic = struct.unpack_from(
                    HEADER_FMT, rx_buf, 0)
                t_drain_ns = time.monotonic_ns()
                if magic != MAGIC:
                    bad_magic += 1
                expected = last_seq + 1
                if seq < expected:
                    duplicates += 1
                elif seq > expected:
                    gaps += seq - expected
                if seq > last_seq:
                    last_seq = seq
                recv_objs += 1
                if t_drain_ns - t_start_ns >= warmup_ns:
                    latencies_ns.append(t_drain_ns - t_built)
                    if ss_t_first_ns == 0:
                        ss_t_first_ns = t_drain_ns
                    ss_t_last_ns = t_drain_ns
                    ss_recv_objs += 1
                    ss_recv_bytes += obj_size
                del rx_buf[:obj_size]

        t_start_ns = time.monotonic_ns()
        end_send = time.monotonic() + duration_s
        pending = None

        while time.monotonic() < end_send:
            if pending is None:
                pending = _build_object(sent_objs, obj_size)
            accepted = stream_buf_push(sb, pending)
            if accepted == len(pending):
                pending = None
                sent_objs += 1
                client.push_tx_event(SPSC_EVT_TX_MARK_ACTIVE, sid,
                               cnx_ptr=client_cnx, stream_ctx=sc)
                client.wake_up()
            else:
                push_full_waits += 1
                if accepted > 0:
                    pending = pending[accepted:]
                time.sleep(0.00002)
            _consume(server.drain_rx())

        # Drain residual.
        client.wake_up()
        drain_deadline = time.monotonic() + 2.0
        while time.monotonic() < drain_deadline and recv_objs < sent_objs:
            _consume(server.drain_rx())
            time.sleep(0.0005)

        # Steady-state metrics: skip warmup + drain tail; the window
        # is the time between first post-warmup arrival and the last.
        ss_elapsed = max(1e-6, (ss_t_last_ns - ss_t_first_ns) / 1e9)
        objs_per_sec = ss_recv_objs / ss_elapsed
        mbps = (ss_recv_bytes * 8 / 1e6) / ss_elapsed

        latencies_ns.sort()
        p50_us = _q(latencies_ns, 0.50) / 1000
        p90_us = _q(latencies_ns, 0.90) / 1000
        p99_us = _q(latencies_ns, 0.99) / 1000
        max_us = (latencies_ns[-1] / 1000) if latencies_ns else 0
        floor_us = (latencies_ns[0] / 1000) if latencies_ns else 0

        print(f"\n  --- baseline obj={obj_size}B duration={duration_s:.0f}s ---")
        print(f"  steady-state: {ss_recv_objs:,} obj over {ss_elapsed:.1f}s")
        print(f"  rate:         {objs_per_sec:>10,.0f} obj/s")
        print(f"  throughput:   {mbps:>10.0f} Mbps")
        print(f"  latency_us:   floor={floor_us:.0f}  p50={p50_us:.0f}  "
              f"p90={p90_us:.0f}  p99={p99_us:.0f}  max={max_us:.0f}")
        print(f"  integrity:    sent={sent_objs:,}  recv={recv_objs:,}  "
              f"bad_magic={bad_magic}  gaps={gaps}  dupes={duplicates}  "
              f"push_full={push_full_waits:,}")

        assert bad_magic == 0, f"{bad_magic} corrupted objects"
        assert gaps == 0, f"{gaps} sequence gaps"
        assert duplicates == 0, f"{duplicates} duplicate seqs"
        assert recv_objs == sent_objs, (
            f"byte conservation: sent {sent_objs} != recv {recv_objs}"
        )
        floor = BASELINE_MIN_MBPS[obj_size]
        if (sys.platform.startswith("linux") and mbps < floor):
            print(f"  [PERF WARN] obj={obj_size}B  achieved={mbps:.0f} "
                  f"Mbps < floor={floor} Mbps "
                  f"— investigate (load, governor, regression?)")
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)
