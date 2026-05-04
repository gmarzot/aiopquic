"""Bidirectional symmetric throughput — both sides send simultaneously."""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN, SPSC_EVT_TX_STREAM_FIN,
)


@pytest.mark.bench
@pytest.mark.parametrize("size_kb", [1024, 4096],
                         ids=["1MB", "4MB"])
def test_bench_bidirectional(benchmark, big_ring_pair, capsys, size_kb):
    """Client and server each send size_kb on a stream they originate."""
    server, client, client_cnx, server_cnx = big_ring_pair
    payload = b"x" * (size_kb * 1024)
    cli_sid_box = [0]    # client-initiated bidi
    srv_sid_box = [1]    # server-initiated bidi

    def round_trip():
        cli_sid = cli_sid_box[0]
        srv_sid = srv_sid_box[0]
        cli_sid_box[0] += 4
        srv_sid_box[0] += 4

        client.push_tx(SPSC_EVT_TX_STREAM_FIN, cli_sid,
                       data=payload, cnx_ptr=client_cnx)
        server.push_tx(SPSC_EVT_TX_STREAM_FIN, srv_sid,
                       data=payload, cnx_ptr=server_cnx)
        client.wake_up()
        server.wake_up()

        cli_received = 0  # bytes server got from client
        srv_received = 0  # bytes client got from server
        target = len(payload)
        deadline = time.monotonic() + 60.0
        t0 = time.monotonic()
        while time.monotonic() < deadline and \
                (cli_received < target or srv_received < target):
            for ev in server.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == cli_sid and ev[2] is not None:
                    cli_received += len(ev[2])
            for ev in client.drain_rx():
                if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                        and ev[1] == srv_sid and ev[2] is not None:
                    srv_received += len(ev[2])
        elapsed = time.monotonic() - t0

        total = cli_received + srv_received
        mbs = total / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
        gbps = total * 8 / elapsed / 1e9 if elapsed > 0 else 0.0
        with capsys.disabled():
            print(
                f"\n  bidirectional: 2x{size_kb}KB={total / 1e6:.1f}MB "
                f"in {elapsed * 1000:.0f}ms  =>  "
                f"{mbs:.0f} MB/s ({gbps:.2f} Gb/s combined)"
            )
        assert cli_received == target, f"cli->srv {cli_received}/{target}"
        assert srv_received == target, f"srv->cli {srv_received}/{target}"

    benchmark.pedantic(round_trip, rounds=2, iterations=1, warmup_rounds=1)
