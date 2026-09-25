"""Per-stream send priority (RFC 9000 §2.3) over a loopback connection.

Drives the Python -> SPSC TX ring -> picoquic_set_stream_priority path and
asserts the event is CONSUMED, not merely posted: tx_event_ring_count
returning to 0 is what proves the C handler popped it.

These cover the binding, not the scheduler. picoquic reordering streams by
priority is not observable on loopback, where there is no congestion to
schedule against.
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

DEFAULT_PRIORITY = 9


def wait_tx_ring_empty(ctx, timeout=2.0):
    """Wait for the TX event ring to drain, i.e. the C side popped."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.tx_event_ring_count == 0:
            return True
        time.sleep(0.01)
    return False


def send_and_confirm(client, server, cnx_ptr, stream_id, payload):
    """Send on a stream and assert the server received the bytes."""
    client.tx_send_stream(cnx_ptr, stream_id, payload)
    events = drain_until(server, SPSC_EVT_STREAM_DATA, timeout=5.0)
    assert payload in collect_stream_data(events, stream_id), (
        f"stream {stream_id} did not carry {payload!r}"
    )


class TestStreamPriority:

    def test_posts_and_is_consumed(self):
        """Priority on an open stream posts, drains, and does not
        disturb delivery."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"before")

                assert client.set_stream_priority(cnx_ptr, 0, 2) == 0
                assert wait_tx_ring_empty(client), (
                    "priority event was never popped by the C handler"
                )

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

                for priority in (1, 200, 4):
                    assert client.set_stream_priority(
                        cnx_ptr, 0, priority) == 0
                    assert wait_tx_ring_empty(client)

                send_and_confirm(client, server, cnx_ptr, 0, b"last")
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [0, 1, 8, 9, 254, 255])
    def test_byte_range_is_accepted(self, priority):
        """The full uint8_t range posts. 8/9 are the even/odd pair that
        selects picoquic's scheduling discipline among equals — the
        binding applies no policy and must treat them alike."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"x")
                assert client.set_stream_priority(
                    cnx_ptr, 0, priority) == 0
                assert wait_tx_ring_empty(client)
            finally:
                client.stop()
        finally:
            server.stop()

    @pytest.mark.parametrize("priority", [-1, 256])
    def test_out_of_range_is_refused(self, priority):
        """Values outside uint8_t raise rather than wrapping — a wrapped
        255 is the lowest priority, the opposite of an intended 256."""
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

    def test_unknown_cnx_is_dropped_not_fatal(self):
        """A cnx freed between push and pop is dropped by the liveness
        guard. The connection stays usable."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                send_and_confirm(client, server, cnx_ptr, 0, b"live")

                assert client.set_stream_priority(BOGUS_CNX, 0, 3) == 0
                assert wait_tx_ring_empty(client), (
                    "stale-cnx event was not drained"
                )

                send_and_confirm(client, server, cnx_ptr, 4, b"still up")
            finally:
                client.stop()
        finally:
            server.stop()


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
