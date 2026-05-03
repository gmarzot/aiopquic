"""Sustained throughput with byte-level verification — diagnostic.

Producer embeds (chunk_seq:u64, t_built_ns:u64, magic:u64) in the first
24 bytes of each 64KB chunk; consumer accumulates the byte stream from
drain_rx events and decodes the header at every CHUNK_SIZE-aligned
offset. Reports:

  - sequence contiguity (gaps, duplicates, out-of-order)
  - magic marker integrity (catches stream-offset drift)
  - drain_lag = t_drain - t_built distribution (p50/p90/p99/max)
  - rx_count (RX SPSC ring fill) sampled over time

All instrumentation lives in this bench file; no changes to the
core SPSC ring or transport hot path. Encoded in payload so the
overhead is a single time.monotonic_ns() per chunk on the producer
and a struct.unpack() per chunk-boundary on the consumer.

Usage:
    pytest tests/bench/bench_throughput_sustained_verify.py -s --tb=short
"""
import os
import struct
import time
from collections import Counter

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_STREAM_DATA,
)


CHUNK = 64 * 1024
HEADER_FMT = "<QQQ"        # little-endian u64 seq, u64 t_built_ns, u64 magic
HEADER_LEN = struct.calcsize(HEADER_FMT)
MAGIC = 0xDEADBEEFCAFEBABE


def _build_chunk(seq, fill_byte):
    """Build a CHUNK-sized chunk with a verifiable header at offset 0."""
    t_built = time.monotonic_ns()
    header = struct.pack(HEADER_FMT, seq, t_built, MAGIC)
    return header + bytes([fill_byte]) * (CHUNK - HEADER_LEN), t_built


def _quantile(sorted_values, q):
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * q)))
    return sorted_values[idx]


@pytest.mark.bench
@pytest.mark.parametrize("duration_s", [0.1, 0.5, 1.0, 2.0],
                         ids=["100ms", "500ms", "1s", "2s"])
def test_bench_sustained_verify(big_ring_pair, duration_s, capsys):
    """Sustained-throughput probe with byte-level integrity check."""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0  # client-initiated bidirectional

    sent = 0
    push_failures = 0
    chunk_seq = 0

    # Consumer-side byte stream tracking. rx_buf is a sliding window
    # of bytes received but not yet decoded into a CHUNK boundary.
    # As soon as rx_buf has >= CHUNK bytes we decode the first header
    # and drop the full chunk from the buffer.
    rx_buf = bytearray()
    drain_lags_ns = []           # t_drain - t_built per chunk
    seq_log = []                 # (seq, t_built, t_drain) per verified chunk
    bad_magic = 0
    rx_count_samples = []        # (t_rel, rx_count) samples
    last_seen_seq = -1
    out_of_order = 0
    duplicates = 0
    gaps = []                    # (expected, got)

    def _consume_events_and_verify(events):
        nonlocal last_seen_seq, out_of_order, duplicates, bad_magic
        for ev in events:
            if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                    and ev[1] == sid and ev[2] is not None:
                payload = bytes(ev[2])
                if payload:
                    rx_buf.extend(payload)
        # Consume complete chunks from the head of rx_buf.
        while len(rx_buf) >= CHUNK:
            seq, t_built, magic = struct.unpack_from(HEADER_FMT, rx_buf, 0)
            t_drain = time.monotonic_ns()
            if magic != MAGIC:
                bad_magic += 1
            drain_lags_ns.append(t_drain - t_built)
            seq_log.append((seq, t_built, t_drain))
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

    while time.monotonic() < end_send:
        chunk, _t = _build_chunk(chunk_seq, fill_byte=0xBB)
        try:
            client.push_tx(SPSC_EVT_TX_STREAM_DATA, sid,
                           data=chunk, cnx_ptr=client_cnx)
            sent += CHUNK
            chunk_seq += 1
            if chunk_seq % 16 == 0:
                client.wake_up()
        except BufferError:
            push_failures += 1
            client.wake_up()
            for _ in range(8):
                _consume_events_and_verify(server.drain_rx())
                time.sleep(0.0005)

        _consume_events_and_verify(server.drain_rx())

        # Sample ring fill ~10x/s
        now = time.monotonic()
        if now - last_sample >= 0.1:
            rx_count_samples.append((now - t_start, server.rx_count))
            last_sample = now

    client.wake_up()

    # Drain remaining ~3s
    drain_deadline = time.monotonic() + 3.0
    while time.monotonic() < drain_deadline:
        evs = server.drain_rx()
        if not evs:
            time.sleep(0.001)
            continue
        _consume_events_and_verify(evs)
        if last_seen_seq + 1 == chunk_seq:
            break

    # ------- Report -------
    received_chunks = len(seq_log)
    received_bytes = received_chunks * CHUNK
    mb_s = received_bytes / duration_s / (1024 * 1024)
    gb_s = received_bytes * 8 / duration_s / 1e9

    drain_lags_us = sorted(x / 1000 for x in drain_lags_ns)
    p50 = _quantile(drain_lags_us, 0.50)
    p90 = _quantile(drain_lags_us, 0.90)
    p99 = _quantile(drain_lags_us, 0.99)
    p_max = drain_lags_us[-1] if drain_lags_us else 0

    rx_count_max = max((c for _, c in rx_count_samples), default=0)
    rx_count_avg = (
        sum(c for _, c in rx_count_samples) / len(rx_count_samples)
        if rx_count_samples else 0
    )

    with capsys.disabled():
        print()
        print(f"  --- sustained verify {duration_s}s ---")
        print(f"  sent_chunks={chunk_seq} ({sent / 1e6:.1f}MB) "
              f"push_failures={push_failures}")
        print(f"  recv_chunks={received_chunks} "
              f"({received_bytes / 1e6:.1f}MB) "
              f"=> {mb_s:.0f} MB/s ({gb_s:.2f} Gb/s sustained over {duration_s}s)")
        print(f"  integrity: gaps={len(gaps)} dupes={duplicates} "
              f"out_of_order={out_of_order} bad_magic={bad_magic}")
        if gaps[:5]:
            print(f"  first 5 gaps (expected→got): {gaps[:5]}")
        print(f"  drain_lag_us: p50={p50:.0f} p90={p90:.0f} "
              f"p99={p99:.0f} max={p_max:.0f}  (n={len(drain_lags_us)})")
        print(f"  rx_count: avg={rx_count_avg:.0f} max={rx_count_max}")
        if rx_count_samples:
            # Show the spike profile — first 5, last 5, peak
            peak = max(rx_count_samples, key=lambda x: x[1])
            print(f"  rx_count peak: {peak[1]} at t={peak[0]:.1f}s")

    # The key correctness assertion:
    assert bad_magic == 0, (
        f"magic mismatch on {bad_magic} chunk(s) — stream offset drift")
    assert duplicates == 0, f"saw {duplicates} duplicate chunks"
    # Gaps are informational under load; we don't assert.
