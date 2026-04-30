"""Sustained-throughput benchmark — keep the pipe full for N seconds.

Measures the steady-state rate after CC has opened up, not single-shot
round time. Reports MB/s as a printed result alongside the bench timing.
"""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_STREAM_DATA,
)

CHUNK = 64 * 1024  # bytes per push_tx


@pytest.mark.bench
@pytest.mark.parametrize("duration_s", [5.0],
                         ids=["5s"])
def test_bench_stream_sustained(benchmark, big_ring_pair, duration_s,
                                 capsys):
    """Open one stream, keep pushing until duration_s elapsed; measure MB/s.

    The benchmark fixture timing is just the wall time of the round; we
    derive bytes-per-second separately and print it so it shows up in
    the bench output even though pytest-benchmark reports the time only.
    """
    server, client, client_cnx, _ = big_ring_pair
    payload = b"x" * CHUNK
    stream_id_box = [0]

    def round_trip():
        sid = stream_id_box[0]
        stream_id_box[0] += 4

        # Drain in a tight loop on the server side and measure how
        # many bytes arrive over `duration_s`.
        end_send = time.monotonic() + duration_s
        sent = 0
        received = 0
        push_failures = 0

        while time.monotonic() < end_send:
            try:
                client.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, sid,
                    data=payload, cnx_ptr=client_cnx,
                )
                sent += CHUNK
                if sent % (CHUNK * 16) == 0:
                    client.wake_up()
            except BufferError:
                push_failures += 1
                client.wake_up()
                # Drain a little to free TX slots
                for _ in range(8):
                    server.drain_rx()
                    time.sleep(0.0005)
            # Drain RX as we go so the pipe doesn't stall
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    received += len(ev[2])

        client.wake_up()

        # Drain remaining RX after we stop sending (~2s grace).
        drain_deadline = time.monotonic() + 2.0
        while time.monotonic() < drain_deadline and received < sent:
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == sid and ev[2] is not None:
                    received += len(ev[2])
            if received >= sent:
                break
            time.sleep(0.001)

        mbs = received / duration_s / (1024 * 1024)
        gbps = received * 8 / duration_s / 1e9
        with capsys.disabled():
            print(
                f"\n  sustained: sent={sent / 1e6:.1f}MB "
                f"received={received / 1e6:.1f}MB "
                f"in {duration_s}s  =>  "
                f"{mbs:.0f} MB/s ({gbps:.2f} Gb/s)  "
                f"push_full={push_failures}"
            )
        assert received > 0, "nothing received"

    benchmark.pedantic(round_trip, rounds=2, iterations=1, warmup_rounds=1)
