"""Ping-pong latency benchmark — 1-byte BIDI round-trip on loopback."""
import time

import pytest

from _helpers import SPSC_EVT_STREAM_DATA, SPSC_EVT_TX_STREAM_DATA


@pytest.mark.bench
def test_bench_pingpong_latency(benchmark, loopback_pair):
    """Client sends 1 byte; server echoes 1 byte; measure RTT."""
    server, client, client_cnx, server_cnx = loopback_pair
    sid_box = [0]

    def round_trip():
        sid = sid_box[0]
        sid_box[0] += 4

        client.push_tx(SPSC_EVT_TX_STREAM_DATA, sid,
                       data=b"p", cnx_ptr=client_cnx)
        client.wake_up()

        deadline = time.monotonic() + 5.0
        srv_got = False
        while time.monotonic() < deadline:
            for ev in server.drain_rx():
                if ev[0] == SPSC_EVT_STREAM_DATA and ev[1] == sid:
                    srv_got = True
            if srv_got:
                break
        assert srv_got, "server didn't see ping"

        server.push_tx(SPSC_EVT_TX_STREAM_DATA, sid,
                       data=b"P", cnx_ptr=server_cnx)
        server.wake_up()

        deadline = time.monotonic() + 5.0
        cli_got = False
        while time.monotonic() < deadline:
            for ev in client.drain_rx():
                if ev[0] == SPSC_EVT_STREAM_DATA and ev[1] == sid:
                    cli_got = True
            if cli_got:
                break
        assert cli_got, "client didn't see pong"

    benchmark.pedantic(round_trip, rounds=20, iterations=1, warmup_rounds=2)
