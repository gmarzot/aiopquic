"""Per-stream send priority (RFC 9000 §2.3) over a loopback connection.

Drives the Python -> SPSC TX ring -> picoquic_set_stream_priority path and
asserts on `set_priority_applied`, which counts picoquic accepting the
call. Ring drain is not enough: the stale-cnx guard pops an event exactly
as a successful apply does, so draining proves only that the worker saw
it.

These cover the binding, not the scheduler. picoquic reordering streams by
priority is not observable on loopback, where there is no congestion to
schedule against — see tests/bench/sim_link for that.
"""

import os
import time

import pytest

from aiopquic._binding._transport import TransportContext

try:
    from .test_loopback import (
        CERT_FILE, KEY_FILE, SPSC_EVT_STREAM_DATA,
        collect_stream_data, connect_client, drain_until, start_server,
    )
    from ._ports import next_port
except ImportError:      # loaded bare, outside the package
    from test_loopback import (
        CERT_FILE, KEY_FILE, SPSC_EVT_STREAM_DATA,
        collect_stream_data, connect_client, drain_until, start_server,
    )
    from _ports import next_port


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)

# Never dereferenced by aiopquic_cnx_is_alive(), which compares against the
# quic context's connection list.
BOGUS_CNX = 0xDEADBEEF


def wait_counter(ctx, key, target, timeout=2.0):
    """Wait for a counter to reach `target`. Returns its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = ctx.counters[key]
        if v >= target:
            return v
        time.sleep(0.01)
    return ctx.counters[key]


def applied(ctx):
    return ctx.counters['set_priority_applied']


def send_and_confirm(client, server, cnx_ptr, stream_id, payload):
    """Send on a stream and assert the server received the bytes."""
    client.tx_send_stream(cnx_ptr, stream_id, payload)
    events = drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
    assert payload in collect_stream_data(events, stream_id), (
        f"stream {stream_id} did not carry {payload!r}"
    )


class TestStreamPriority:

    def test_picoquic_applies_it(self):
        """picoquic accepts the priority for an open stream, and delivery
        is undisturbed."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"before")
                before = applied(client)

                assert client.set_stream_priority(cnx_ptr, 0, 2) == 0
                assert wait_counter(client, 'set_priority_applied',
                                    before + 1) == before + 1
                assert client.counters['set_priority_rejected'] == 0

                send_and_confirm(client, server, cnx_ptr, 0, b"after")
            finally:
                client.stop()
        finally:
            server.stop()

    def test_reprioritise_mid_stream(self):
        """A stream can be re-prioritised while open, which is what a
        subscription changing priority mid-track does."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"first")
                before = applied(client)

                for priority in (1, 200, 4):
                    assert client.set_stream_priority(
                        cnx_ptr, 0, priority) == 0
                assert wait_counter(client, 'set_priority_applied',
                                    before + 3) == before + 3
                assert client.counters['set_priority_rejected'] == 0

                send_and_confirm(client, server, cnx_ptr, 0, b"last")
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [0, 1, 254, 255])
    def test_byte_range_is_accepted(self, priority):
        """picoquic accepts the full uint8_t range."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"x")
                before = applied(client)
                assert client.set_stream_priority(
                    cnx_ptr, 0, priority) == 0
                assert wait_counter(client, 'set_priority_applied',
                                    before + 1) == before + 1
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [-1, 256])
    def test_out_of_range_is_refused(self, priority):
        """Values outside uint8_t raise rather than wrapping — a wrapped
        256 is 0, the *highest* priority, the opposite of the intent."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                with pytest.raises(OverflowError):
                    client.set_stream_priority(cnx_ptr, 0, priority)
            finally:
                client.stop()
        finally:
            server.stop()

    def test_unknown_cnx_is_dropped_not_applied(self):
        """A cnx freed between push and pop is dropped by the liveness
        guard: the drop counter advances, `applied` does not, and the
        connection stays usable. Ring drain cannot tell this from a
        successful apply — both pop the event."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"live")
                before_applied = applied(client)
                before_dropped = client.counters['tx_event_dropped_dead_cnx']

                assert client.set_stream_priority(BOGUS_CNX, 0, 3) == 0
                assert wait_counter(client, 'tx_event_dropped_dead_cnx',
                                    before_dropped + 1) == before_dropped + 1
                assert applied(client) == before_applied

                send_and_confirm(client, server, cnx_ptr, 4, b"still up")
            finally:
                client.stop()
        finally:
            server.stop()


class TestRingFull:
    """An unstarted context has no worker, so nothing drains the ring —
    which makes overflow deterministic rather than a race."""

    def test_full_ring_returns_one_and_arms_the_drain(self):
        ctx = TransportContext(tx_ring_cap=2)
        posted = 0
        while ctx.set_stream_priority(BOGUS_CNX, 0, 5) == 0:
            posted += 1
            assert posted <= 64, "ring never filled"
        assert posted == ctx.tx_event_ring_capacity

        arms = ctx.counters['tx_event_ring_arms']
        assert ctx.set_stream_priority(BOGUS_CNX, 0, 5) == 1
        assert ctx.counters['tx_event_ring_arms'] > arms, (
            "ring-full must arm the drain or a waiter never wakes")
        assert applied(ctx) == 0, "no worker ran, so nothing can be applied"


class TestDefaultStreamPriority:

    def test_before_start_raises(self):
        """No quic context means nothing to set it on."""
        ctx = TransportContext()
        with pytest.raises(ConnectionError):
            ctx.set_default_stream_priority(5)

    def test_after_start_applies(self):
        """Set on the quic context; affects streams created after."""
        port = next_port()
        server = start_server(port)
        try:
            server.set_default_stream_priority(4)

            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"default")
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [-1, 256])
    def test_out_of_range_is_refused(self, priority):
        port = next_port()
        server = start_server(port)
        try:
            with pytest.raises(OverflowError):
                server.set_default_stream_priority(priority)
        finally:
            server.stop()
