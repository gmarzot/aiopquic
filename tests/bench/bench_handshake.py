"""Handshake-rate benchmark — sustained new client connections."""
import time

import pytest

from _helpers import (
    SPSC_EVT_ALMOST_READY, ALPN,
    next_port, wait_for_ready, drain_until,
    get_cnx_ptr, has_connection_ready,
    start_server,
)
from aiopquic._binding._transport import TransportContext


@pytest.mark.bench
def test_bench_handshake_rate(benchmark):
    """One full handshake per round; server reused, fresh client each time."""
    port = next_port()
    server = start_server(port)

    def one_handshake():
        client = TransportContext()
        client.start(port=0, alpn=ALPN, is_client=True)
        assert wait_for_ready(client)
        try:
            client.create_client_connection(
                "127.0.0.1", port,
                sni="localhost", alpn=ALPN,
            )
            events = drain_until(
                client, SPSC_EVT_ALMOST_READY, timeout=5.0,
            )
            assert get_cnx_ptr(events) != 0, "no cnx pointer"
            if not has_connection_ready(events):
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    events.extend(client.drain_rx())
                    if has_connection_ready(events):
                        break
                    time.sleep(0.01)
            assert has_connection_ready(events), "handshake incomplete"
        finally:
            client.stop()

    try:
        benchmark.pedantic(one_handshake, rounds=10, iterations=1,
                           warmup_rounds=1)
    finally:
        server.stop()
