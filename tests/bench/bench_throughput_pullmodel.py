"""Sustained throughput via the PULL-model send path.

Producer writes into a per-stream byte ring; picoquic pulls at wire
rate via prepare_to_send. Backpressure is real: stream_buf_push
returns # bytes accepted, and 0 means full — producer must wait.

Embeds (chunk_seq:u64, t_built_ns:u64, magic:u64) in the first 24 bytes
of each 64KB chunk; consumer accumulates the byte stream from drain_rx
events and decodes the header at every CHUNK_SIZE-aligned offset.

Compares to bench_throughput_sustained_verify.py (the PUSH-model probe
that demonstrated buffer bloat with no upstream backpressure).
"""
import struct
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
)
# SPSC_EVT_TX_MARK_ACTIVE = 134; not in _helpers re-exports, hardcode here.
SPSC_EVT_TX_MARK_ACTIVE = 134

from aiopquic._binding._transport import (
    stream_buf_create, stream_buf_destroy,
    stream_buf_push, stream_buf_used, stream_buf_free, stream_buf_set_fin,
)


CHUNK = 64 * 1024
HEADER_FMT = "<QQQ"        # u64 seq, u64 t_built_ns, u64 magic
HEADER_LEN = struct.calcsize(HEADER_FMT)
MAGIC = 0xDEADBEEFCAFEBABE
RING_CAPACITY = 1 << 20    # 1 MiB per-stream send buffer


def _build_chunk(seq, fill_byte=0xBB):
    t_built = time.monotonic_ns()
    header = struct.pack(HEADER_FMT, seq, t_built, MAGIC)
    return header + bytes([fill_byte]) * (CHUNK - HEADER_LEN)


def _quantile(sorted_values, q):
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * q)))
    return sorted_values[idx]


@pytest.mark.bench
@pytest.mark.parametrize("duration_s", [1.0, 5.0], ids=["1s", "5s"])
def test_bench_pull_sustained(big_ring_pair, duration_s, capsys):
    server, client, client_cnx, _ = big_ring_pair
    sid = 0  # client-initiated bidirectional

    sb = stream_buf_create(RING_CAPACITY)
    try:
        # Activate the stream with our buffer pointer as stream_ctx.
        # picoquic will fire prepare_to_send when it has wire credit;
        # the C callback finds sb via stream_ctx and pulls bytes.
        client.push_tx(SPSC_EVT_TX_MARK_ACTIVE, sid,
                       cnx_ptr=client_cnx, stream_ctx=sb)
        client.wake_up()

        sent = 0
        chunk_seq = 0
        push_partial = 0
        push_full_waits = 0

        # Consumer-side accumulator.
        rx_buf = bytearray()
        seq_log = []
        drain_lags_ns = []
        bad_magic = 0
        out_of_order = 0
        duplicates = 0
        gaps = []
        last_seen_seq = -1

        ring_used_samples = []   # (t_rel, used_bytes) — producer-side
        rx_count_samples = []    # (t_rel, rx_count)   — consumer-side

        def _consume_and_verify(events):
            nonlocal last_seen_seq, out_of_order, duplicates, bad_magic
            for ev in events:
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    payload = bytes(ev[2])
                    if payload:
                        rx_buf.extend(payload)
            while len(rx_buf) >= CHUNK:
                seq, t_built, magic = struct.unpack_from(
                    HEADER_FMT, rx_buf, 0)
                t_drain = time.monotonic_ns()
                if magic != MAGIC:
                    bad_magic += 1
                drain_lags_ns.append(t_drain - t_built)
                seq_log.append(seq)
                expected = last_seen_seq + 1
                if seq < expected:
                    duplicates += 1
                elif seq > expected:
                    gaps.append((expected, seq))
                    out_of_order += 1
                if seq > last_seen_seq:
                    last_seen_seq = seq
                del rx_buf[:CHUNK]

        t_start = time.monotonic()
        end_send = t_start + duration_s
        last_sample = t_start
        pending_chunk = None       # bytes left over from a partial push

        while time.monotonic() < end_send:
            if pending_chunk is None:
                pending_chunk = _build_chunk(chunk_seq)
                chunk_seq += 1
            accepted = stream_buf_push(sb, pending_chunk)
            sent += accepted
            if accepted == len(pending_chunk):
                pending_chunk = None
                if (chunk_seq & 0xF) == 0:
                    # nudge picoquic periodically; mark_active is sticky
                    # but wake_up tells the loop to schedule sooner.
                    client.wake_up()
            else:
                # partial or zero accept = ring is full = real backpressure.
                if accepted > 0:
                    pending_chunk = pending_chunk[accepted:]
                    push_partial += 1
                else:
                    push_full_waits += 1
                client.wake_up()
                # tiny yield so picoquic-pthread can drain
                time.sleep(0.00005)

            _consume_and_verify(server.drain_rx())

            now = time.monotonic()
            if now - last_sample >= 0.1:
                ring_used_samples.append((now - t_start, stream_buf_used(sb)))
                rx_count_samples.append((now - t_start, server.rx_count))
                last_sample = now

        # Stop producing; drain anything remaining.
        client.wake_up()
        drain_deadline = time.monotonic() + 3.0
        while time.monotonic() < drain_deadline:
            _consume_and_verify(server.drain_rx())
            if last_seen_seq + 1 == chunk_seq and stream_buf_used(sb) == 0:
                break
            time.sleep(0.001)

        received_chunks = len(seq_log)
        received_bytes = received_chunks * CHUNK
        gb_s = received_bytes * 8 / duration_s / 1e9
        mb_s = received_bytes / duration_s / (1024 * 1024)

        drain_lags_us = sorted(x / 1000 for x in drain_lags_ns)
        p50 = _quantile(drain_lags_us, 0.50)
        p90 = _quantile(drain_lags_us, 0.90)
        p99 = _quantile(drain_lags_us, 0.99)
        p_max = drain_lags_us[-1] if drain_lags_us else 0

        ring_used_max = max((u for _, u in ring_used_samples), default=0)
        ring_used_avg = (
            sum(u for _, u in ring_used_samples) / len(ring_used_samples)
            if ring_used_samples else 0
        )

        with capsys.disabled():
            print()
            print(f"  --- PULL-model sustained {duration_s}s ---")
            print(f"  sent_chunks={chunk_seq} ({sent / 1e6:.1f}MB)")
            print(f"  push_partial={push_partial} push_full_waits={push_full_waits}")
            print(f"  recv_chunks={received_chunks} "
                  f"({received_bytes / 1e6:.1f}MB) "
                  f"=> {mb_s:.0f} MB/s ({gb_s:.2f} Gb/s sustained over {duration_s}s)")
            print(f"  integrity: gaps={len(gaps)} dupes={duplicates} "
                  f"out_of_order={out_of_order} bad_magic={bad_magic}")
            if gaps[:5]:
                print(f"  first 5 gaps (expected→got): {gaps[:5]}")
            print(f"  drain_lag_us: p50={p50:.0f} p90={p90:.0f} "
                  f"p99={p99:.0f} max={p_max:.0f}  (n={len(drain_lags_us)})")
            print(f"  ring_used: avg={ring_used_avg / 1024:.0f}KB "
                  f"max={ring_used_max / 1024:.0f}KB / cap={RING_CAPACITY / 1024:.0f}KB")

        assert bad_magic == 0, (
            f"magic mismatch on {bad_magic} chunk(s) — stream offset drift")
        assert duplicates == 0, f"saw {duplicates} duplicate chunks"
    finally:
        # Best-effort cleanup. picoquic should have stopped pulling once
        # the connection closes; the fixture tears that down on yield exit.
        # We destroy the buffer here; if picoquic still references it, it
        # would be a use-after-free, so we wait briefly first.
        time.sleep(0.05)
        stream_buf_destroy(sb)
