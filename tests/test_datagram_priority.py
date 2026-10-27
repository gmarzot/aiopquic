"""Datagram send priority against streams (picoquic_set_datagram_priority).

Drives the Python -> SPSC TX ring -> picoquic path for raw QUIC and
WebTransport and reads the value back from picoquic's connection. These
cover the binding, not the scheduler: picoquic ordering datagrams against
streams is not observable on loopback.
"""

import asyncio
import os
import time

import pytest

from aiopquic._binding._transport import (
    TransportContext, _datagram_priority, _default_datagram_priority,
    _wt_datagram_priority,
)
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport

try:
    from .test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_ALMOST_READY,
        connect_client, drain_until, get_cnx_ptr, start_server, wait_for_ready,
    )
    from ._ports import next_port
except ImportError:      # loaded bare, outside the package
    from test_loopback import (
        ALPN, CA_FILE, CERT_FILE, KEY_FILE, SNI, SPSC_EVT_ALMOST_READY,
        connect_client, drain_until, get_cnx_ptr, start_server, wait_for_ready,
    )
    from _ports import next_port


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)

PICOQUIC_DEFAULT = 9
# Never dereferenced: the stale-cnx guard compares against the context's list.
BOGUS_CNX = 0xDEADBEEF


def wait_value(read, target, timeout=2.0):
    deadline = time.monotonic() + timeout
    value = read()
    while value != target and time.monotonic() < deadline:
        time.sleep(0.01)
        value = read()
    return value


async def wait_value_async(read, target, timeout=2.0):
    deadline = time.monotonic() + timeout
    value = read()
    while value != target and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        value = read()
    return value


class TestRawDatagramPriority:

    def test_starts_at_picoquics_default(self):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                assert _default_datagram_priority(client) == PICOQUIC_DEFAULT
                assert _datagram_priority(cnx_ptr) == PICOQUIC_DEFAULT
            finally:
                client.stop()
        finally:
            server.stop()

    def test_set_on_a_connection(self):
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                before = client.counters['set_dgram_priority_applied']
                assert client.set_datagram_priority(cnx_ptr, 129) == 0
                assert wait_value(lambda: _datagram_priority(cnx_ptr), 129) == 129
                assert client.counters['set_dgram_priority_applied'] == before + 1
                assert client.set_datagram_priority(cnx_ptr, 2) == 0
                assert wait_value(lambda: _datagram_priority(cnx_ptr), 2) == 2
            finally:
                client.stop()
        finally:
            server.stop()

    def test_default_applies_to_connections_created_after(self):
        port = next_port()
        server = start_server(port)
        try:
            client = TransportContext()
            client.start(port=0, alpn=ALPN, is_client=True, ca_file=CA_FILE)
            try:
                assert wait_for_ready(client), "Client not ready"
                client.set_default_datagram_priority(129)
                assert _default_datagram_priority(client) == 129
                client.create_client_connection("127.0.0.1", port, sni=SNI, alpn=ALPN)
                events = drain_until(client, SPSC_EVT_ALMOST_READY, timeout=5.0)
                cnx_ptr = get_cnx_ptr(events, SPSC_EVT_ALMOST_READY)
                assert cnx_ptr != 0
                assert _datagram_priority(cnx_ptr) == 129
            finally:
                client.stop()
        finally:
            server.stop()

    def test_freed_cnx_is_dropped(self):
        port = next_port()
        server = start_server(port)
        try:
            client, _cnx_ptr = connect_client(port)
            try:
                before = client.counters['set_dgram_priority_applied']
                dropped = client.counters['tx_event_dropped_dead_cnx']
                assert client.set_datagram_priority(BOGUS_CNX, 3) == 0
                assert wait_value(lambda: client.counters['tx_event_dropped_dead_cnx'],
                                  dropped + 1) == dropped + 1
                assert client.counters['set_dgram_priority_applied'] == before
            finally:
                client.stop()
        finally:
            server.stop()

    def test_default_before_start_raises(self):
        with pytest.raises(ConnectionError):
            TransportContext().set_default_datagram_priority(129)


async def test_webtransport_sessions_set_their_connection():
    port = next_port()
    accepted = asyncio.get_running_loop().create_future()

    def handler(session):
        if not accepted.done():
            accepted.set_result(session)

    server = await serve_webtransport("127.0.0.1", port, "/wt", handler=handler,
                                      cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        ca_file=CA_FILE) as session:
            peer = await asyncio.wait_for(accepted, 3.0)
            assert _wt_datagram_priority(session._state) == PICOQUIC_DEFAULT
            assert session.set_datagram_priority(129) == 0
            assert await wait_value_async(
                lambda: _wt_datagram_priority(session._state), 129) == 129
            assert peer.set_datagram_priority(131) == 0
            assert await wait_value_async(
                lambda: _wt_datagram_priority(peer._state), 131) == 131
    finally:
        server.close()
