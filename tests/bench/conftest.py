"""Shared bench fixtures."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from _helpers import (  # noqa: E402
    ALPN, CERT_FILE, KEY_FILE,
    next_port, wait_for_ready,
    start_server, connect_client, wait_for_server_cnx,
)
from aiopquic._binding._transport import TransportContext  # noqa: E402


@pytest.fixture
def loopback_pair():
    """A connected (server, client, client_cnx, server_cnx) tuple."""
    port = next_port()
    server = start_server(port)
    try:
        client, client_cnx = connect_client(port)
        try:
            _, server_cnx = wait_for_server_cnx(server)
            assert server_cnx != 0
            yield server, client, client_cnx, server_cnx
        finally:
            client.stop()
    finally:
        server.stop()


@pytest.fixture
def datagram_pair():
    """A connected pair with datagrams enabled on both sides."""
    port = next_port()
    server = TransportContext()
    server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                 alpn=ALPN, is_client=False, max_datagram_frame_size=1200)
    assert wait_for_ready(server)
    try:
        client, client_cnx = connect_client(port, max_datagram_frame_size=1200)
        try:
            _, server_cnx = wait_for_server_cnx(server)
            assert server_cnx != 0
            yield server, client, client_cnx, server_cnx
        finally:
            client.stop()
    finally:
        server.stop()


@pytest.fixture
def big_ring_pair():
    """Connected pair with a much larger ring for high-throughput benches.

    The default 4096-entry ring fills up fast at multi-Gbps loopback
    rates; the larger size gives the benchmark headroom so we measure
    picoquic + the wrapper, not ring-full backpressure.
    """
    port = next_port()
    # rx_ring_cap=1MB matches AIOPQUIC_RX_RING_CAP_DEFAULT in callback.h.
    # picoquic's default initial_max_stream_data is larger than our
    # per-stream ring, so without an explicit cap a slow consumer would
    # let the peer overrun the ring on raw-test paths. Setting it here
    # lines the worker-advertised window up with the ring capacity.
    server = TransportContext(ring_capacity=65536)
    server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                 alpn=ALPN, is_client=False, rx_ring_cap=1 << 20)
    assert wait_for_ready(server)
    try:
        client = TransportContext(ring_capacity=65536)
        client.start(port=0, alpn=ALPN, is_client=True,
                     rx_ring_cap=1 << 20)
        assert wait_for_ready(client)
        try:
            client.create_client_connection(
                "127.0.0.1", port, sni="localhost", alpn=ALPN,
            )
            from _helpers import drain_until, SPSC_EVT_ALMOST_READY, get_cnx_ptr
            evs = drain_until(client, SPSC_EVT_ALMOST_READY, timeout=5.0)
            client_cnx = get_cnx_ptr(evs)
            assert client_cnx != 0
            _, server_cnx = wait_for_server_cnx(server)
            assert server_cnx != 0
            yield server, client, client_cnx, server_cnx
        finally:
            client.stop()
    finally:
        server.stop()
