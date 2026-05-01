"""Parallel-stream throughput — N streams in flight simultaneously."""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_STREAM_FIN,
)


@pytest.mark.bench
@pytest.mark.parametrize("n_streams,size_kb",
                         [(4, 1024), (8, 1024), (16, 256)],
                         ids=["4x1MB", "8x1MB", "16x256KB"])
def test_bench_parallel_streams(benchmark, big_ring_pair, capsys,
                                  n_streams, size_kb):
    """Open n_streams BIDI streams, each carrying size_kb; total = N*size_kb."""
    server, client, client_cnx, _ = big_ring_pair
    payload = b"x" * (size_kb * 1024)
    base_sid_box = [0]

    def round_trip():
        base_sid = base_sid_box[0]
        sids = [base_sid + 4 * i for i in range(n_streams)]
        base_sid_box[0] += 4 * n_streams

        # Push every stream's payload then wake once.
        for sid in sids:
            client.push_tx(SPSC_EVT_TX_STREAM_FIN, sid,
                           data=payload, cnx_ptr=client_cnx)
        client.wake_up()

        per_sid_received = {sid: 0 for sid in sids}
        target_each = len(payload)
        deadline = time.monotonic() + 60.0
        t0 = time.monotonic()
        while time.monotonic() < deadline and \
                any(per_sid_received[s] < target_each for s in sids):
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] in per_sid_received and ev[2] is not None:
                    per_sid_received[ev[1]] += len(ev[2])
        elapsed = time.monotonic() - t0

        total = sum(per_sid_received.values())
        expected = n_streams * target_each
        mbs = total / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
        gbps = total * 8 / elapsed / 1e9 if elapsed > 0 else 0.0
        with capsys.disabled():
            print(
                f"\n  parallel: {n_streams} streams x {size_kb}KB "
                f"= {total / 1e6:.1f}MB in {elapsed * 1000:.0f}ms  =>  "
                f"{mbs:.0f} MB/s ({gbps:.2f} Gb/s)"
            )
        assert total == expected, (
            f"got {total}/{expected} bytes "
            f"(per-stream: {dict(per_sid_received)})"
        )

    benchmark.pedantic(round_trip, rounds=2, iterations=1, warmup_rounds=1)
