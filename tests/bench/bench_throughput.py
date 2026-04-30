"""Stream throughput benchmark — bulk client→server send over loopback."""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_STREAM_FIN,
)


@pytest.mark.bench
@pytest.mark.parametrize("size_kb", [64, 1024],
                         ids=["64KB", "1MB"])
def test_bench_stream_throughput(benchmark, loopback_pair, size_kb):
    """Client sends size_kb of data on stream 0; server fully receives.

    Reports seconds per round; divide by size to get MB/s.
    """
    server, client, client_cnx, _ = loopback_pair
    payload = b"x" * (size_kb * 1024)
    stream_id_box = [0]  # mutated each round so we don't reuse stream ids

    def round_trip():
        sid = stream_id_box[0]
        stream_id_box[0] += 4  # next client-initiated bidi
        client.push_tx(SPSC_EVT_TX_STREAM_FIN, sid,
                       data=payload, cnx_ptr=client_cnx)
        client.wake_up()
        # Drain until we've seen all bytes for this stream.
        received = 0
        deadline = time.monotonic() + 30.0
        while received < len(payload) and time.monotonic() < deadline:
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    received += len(ev[2])
        assert received == len(payload), \
            f"got {received}/{len(payload)} bytes on sid={sid}"

    # Bench just one round per measurement — large transfers dominate noise.
    benchmark.pedantic(round_trip, rounds=3, iterations=1, warmup_rounds=1)
