"""Datagram throughput benchmark — many unordered datagrams over loopback."""
import time

import pytest

from _helpers import SPSC_EVT_DATAGRAM


@pytest.mark.bench
@pytest.mark.parametrize("count", [100, 1000],
                         ids=["100dg", "1000dg"])
def test_bench_datagram_throughput(benchmark, datagram_pair, count):
    """Client fires <count> datagrams; server counts arrivals.

    Datagrams are unreliable so we don't assert delivery == count;
    bench reports actual fire+drain time. Stops as soon as all
    expected arrivals or 50ms quiescence after at least one arrival.
    """
    server, client, client_cnx, _ = datagram_pair
    payload = b"x" * 256
    ring = client.dgram_ring_create(1 << 20, 1200)

    def fire_and_count():
        for _ in range(count):
            # Ring-full (rc==0) is legal under sustained fire — spin
            # briefly; the worker drains as fast as it can pack frames.
            while client.dgram_send(client_cnx, ring, payload) == 0:
                time.sleep(0.0005)
        deadline = time.monotonic() + 5.0
        received = 0
        last_arrival = time.monotonic()
        while time.monotonic() < deadline and received < count:
            evs = server.drain_rx()
            if evs:
                last_arrival = time.monotonic()
                for ev in evs:
                    if ev[0] == SPSC_EVT_DATAGRAM:
                        received += 1
            elif time.monotonic() - last_arrival > 0.05 and received > 0:
                break
        assert received > 0, "no datagrams received"

    benchmark.pedantic(fire_and_count, rounds=3, iterations=1, warmup_rounds=1)
