"""Stream throughput benchmark — bulk client→server send over loopback."""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
)


@pytest.mark.bench
@pytest.mark.parametrize("size_kb", [64, 1024, 32 * 1024],
                         ids=["64KB", "1MB", "32MB"])
def test_bench_stream_throughput(benchmark, big_ring_pair, size_kb):
    """Client sends size_kb of data on a fresh stream + FIN; server fully receives.

    Reports seconds per round; throughput = size_kb*1024 / mean_seconds.
    Payloads above 1 MB are pushed in 1 MB chunks because
    tx_send_stream is all-or-nothing against the per-stream TX ring
    cap (default 4 MB).
    """
    server, client, client_cnx, _ = big_ring_pair
    total = size_kb * 1024
    chunk_size = min(total, 1024 * 1024)
    chunk = b"x" * chunk_size
    stream_id_box = [0]

    def round_trip():
        sid = stream_id_box[0]
        stream_id_box[0] += 4
        # Interleave push + drain: the TX ring is per-stream and capped
        # at 4 MB by default, so pushing 32 MB before any drain stalls
        # forever. Each iteration: try to push the next chunk (skip on
        # BufferError; receiver drain in the same loop will eventually
        # free room), then drain available RX events.
        sent = 0
        received = 0
        deadline = time.monotonic() + 60.0
        while received < total:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"timeout at sid={sid}: sent {sent}/{total} "
                    f"received {received}/{total}")
            if sent < total:
                this = min(chunk_size, total - sent)
                data = chunk if this == chunk_size else chunk[:this]
                is_last = (sent + this == total)
                try:
                    client.tx_send_stream(
                        client_cnx, sid, data, end_stream=is_last)
                    sent += this
                except BufferError:
                    pass  # ring full; drain side will free room
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    received += len(ev[2])
        assert received == total, \
            f"got {received}/{total} bytes on sid={sid}"

    benchmark.pedantic(round_trip, rounds=3, iterations=1, warmup_rounds=1)
