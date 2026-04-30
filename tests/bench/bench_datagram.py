"""Datagram throughput benchmark — many unordered datagrams over loopback."""
import time

import pytest

from _helpers import SPSC_EVT_DATAGRAM, SPSC_EVT_TX_DATAGRAM


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

    def fire_and_count():
        for _ in range(count):
            client.push_tx(SPSC_EVT_TX_DATAGRAM, 0,
                           data=payload, cnx_ptr=client_cnx)
        client.wake_up()
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
