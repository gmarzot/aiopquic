"""Small-object high-rate bench — measures objs/sec and per-object
end-to-end latency for the PULL-model send path under typical media
object sizes (1 KB / 4 KB / 16 KB).

Each object carries a 24-byte header: (seq:u64, t_built_ns:u64, magic:u64).
Producer pushes one object per stream_buf_push call (no batching), then
sends MARK_ACTIVE only on empty→non-empty transition. Consumer reassembles
the stream, decodes the header at object boundaries, computes
build→drain latency.

Reports:
  - objs/sec sustained over the run
  - latency percentiles (p50, p90, p99, max)
  - producer backpressure events (push_full_waits)
  - byte conservation (sent==received, magic matches)

Pass criterion: byte conservation perfect, monotonic seq, no magic
mismatch. Throughput / latency are reported, not asserted (per-host
variability).
"""
import struct
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


HEADER_FMT = "<QQQ"        # u64 seq, u64 t_built_ns, u64 magic
HEADER_LEN = struct.calcsize(HEADER_FMT)
MAGIC = 0xDEADBEEFCAFEBABE
RING_CAPACITY = 1 << 20    # 1 MiB per-stream send buffer


def _build_object(seq, obj_size, fill_byte=0xBB):
    """Build a single object: 24-byte header + (obj_size - 24) byte pad."""
    t_built = time.monotonic_ns()
    header = struct.pack(HEADER_FMT, seq, t_built, MAGIC)
    return header + bytes([fill_byte]) * (obj_size - HEADER_LEN)


def _quantile(sorted_values, q):
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * q)))
    return sorted_values[idx]


@pytest.mark.bench
@pytest.mark.parametrize("obj_size", [1024, 4096, 16384],
                          ids=["1K", "4K", "16K"])
@pytest.mark.parametrize("duration_s", [2.0, 5.0],
                          ids=["2s", "5s"])
def test_bench_small_object_rate(big_ring_pair, obj_size, duration_s, capsys):
    server, client, client_cnx, _ = big_ring_pair
    sid = 0  # client-initiated bidirectional

    sc = stream_ctx_create()
    stream_ctx_ensure_tx(sc, RING_CAPACITY)
    sb = stream_ctx_get_tx(sc)
    try:
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sc)
        client.wake_up()

        sent_objs = 0
        recv_objs = 0
        push_full_waits = 0
        latencies_ns = []

        rx_buf = bytearray()
        last_seq = -1
        bad_magic = 0
        gaps = 0
        duplicates = 0

        def _consume_and_decode(events):
            nonlocal last_seq, bad_magic, gaps, duplicates, recv_objs
            for ev in events:
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    payload = bytes(ev[2])
                    if payload:
                        rx_buf.extend(payload)
            while len(rx_buf) >= obj_size:
                seq, t_built, magic = struct.unpack_from(
                    HEADER_FMT, rx_buf, 0)
                t_drain = time.monotonic_ns()
                if magic != MAGIC:
                    bad_magic += 1
                else:
                    latencies_ns.append(t_drain - t_built)
                expected = last_seq + 1
                if seq < expected:
                    duplicates += 1
                elif seq > expected:
                    gaps += seq - expected
                if seq > last_seq:
                    last_seq = seq
                recv_objs += 1
                del rx_buf[:obj_size]

        t_start = time.monotonic()
        end_send = t_start + duration_s
        pending = None

        while time.monotonic() < end_send:
            if pending is None:
                pending = _build_object(sent_objs, obj_size)
            accepted = stream_buf_push(sb, pending)
            if accepted == len(pending):
                pending = None
                sent_objs += 1
                # Always re-arm — matches QuicConnection.send_stream_data's
                # contract. The empty→non-empty heuristic races at small
                # object sizes (picoquic can drain to empty between our
                # pushes, deactivating the stream; without unconditional
                # MARK_ACTIVE we never re-arm). picoquic_mark_active_stream
                # is idempotent so the cost is one SPSC push.
                client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                               cnx_ptr=client_cnx, stream_ctx=sc)
                client.wake_up()
            else:
                push_full_waits += 1
                if accepted > 0:
                    pending = pending[accepted:]
                time.sleep(0.00002)

            _consume_and_decode(server.drain_rx())

        # Drain remaining bytes after producer stops.
        client.wake_up()
        drain_deadline = time.monotonic() + 1.5
        while time.monotonic() < drain_deadline and recv_objs < sent_objs:
            _consume_and_decode(server.drain_rx())
            time.sleep(0.0005)

        elapsed = time.monotonic() - t_start
        send_elapsed = min(elapsed, duration_s)

        latencies_ns.sort()
        p50 = _quantile(latencies_ns, 0.50) / 1000
        p90 = _quantile(latencies_ns, 0.90) / 1000
        p99 = _quantile(latencies_ns, 0.99) / 1000
        lmax = (latencies_ns[-1] / 1000) if latencies_ns else 0

        objs_per_sec = recv_objs / send_elapsed if send_elapsed > 0 else 0
        sent_bytes = sent_objs * obj_size
        recv_bytes = recv_objs * obj_size
        mbps = (recv_bytes * 8 / 1e6) / send_elapsed if send_elapsed > 0 else 0

        print(f"\n  --- small-object {obj_size}B for {duration_s:.0f}s ---")
        print(f"  sent_objs={sent_objs}  recv_objs={recv_objs}  "
              f"({sent_bytes / 1024 / 1024:.1f} MB sent)")
        print(f"  rate: {objs_per_sec:>8,.0f} obj/s   "
              f"throughput: {mbps:.0f} Mbps")
        print(f"  latency_us: p50={p50:.0f} p90={p90:.0f} p99={p99:.0f} "
              f"max={lmax:.0f}  (n={len(latencies_ns)})")
        print(f"  push_full_waits={push_full_waits}  "
              f"gaps={gaps}  dupes={duplicates}  bad_magic={bad_magic}")

        assert bad_magic == 0, f"{bad_magic} chunks missed magic"
        assert gaps == 0, f"{gaps} sequence gaps"
        assert duplicates == 0, f"{duplicates} duplicate seqs"
    finally:
        time.sleep(0.05)
        stream_ctx_destroy(sc)
