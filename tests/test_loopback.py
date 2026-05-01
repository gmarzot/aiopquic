"""Loopback integration test: server + client connect
and exchange stream data."""

import os
import time
from aiopquic._binding._transport import TransportContext

# Event type constants (must match spsc_ring.h)
SPSC_EVT_STREAM_DATA = 0
SPSC_EVT_STREAM_FIN = 1
SPSC_EVT_STREAM_RESET = 2
SPSC_EVT_STOP_SENDING = 3
SPSC_EVT_CLOSE = 4
SPSC_EVT_APP_CLOSE = 5
SPSC_EVT_READY = 6
SPSC_EVT_ALMOST_READY = 7
SPSC_EVT_DATAGRAM = 8
# TX events (128+)
SPSC_EVT_TX_STREAM_DATA = 128
SPSC_EVT_TX_STREAM_FIN = 129
SPSC_EVT_TX_DATAGRAM = 130
SPSC_EVT_TX_CLOSE = 131
SPSC_EVT_TX_STREAM_RESET = 132
SPSC_EVT_TX_STOP_SENDING = 133

# Picoquic test certs
CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

ALPN = "hq-interop"

# Use unique high ports per test to avoid TIME_WAIT conflicts
_port_counter = 24567


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


def wait_for_ready(ctx, timeout=2.0):
    """Wait for the network thread to be ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.thread_ready:
            return True
        time.sleep(0.01)
    return False


def drain_until(ctx, event_type, timeout=5.0):
    """Drain RX ring until we see an event of the given type."""
    deadline = time.monotonic() + timeout
    all_events = []
    while time.monotonic() < deadline:
        events = ctx.drain_rx()
        all_events.extend(events)
        for ev in all_events:
            if ev[0] == event_type:
                return all_events
        time.sleep(0.02)
    return all_events


def get_cnx_ptr(events, event_type=SPSC_EVT_ALMOST_READY):
    """Extract cnx pointer from an ALMOST_READY event."""
    for ev in events:
        if ev[0] == event_type and ev[5] != 0:
            return ev[5]
    return 0


def has_event(events, event_type):
    """Check if event list contains an event of the given type."""
    return any(ev[0] == event_type for ev in events)


def collect_stream_data(events, stream_id=None):
    """Collect data bytes from STREAM_DATA and STREAM_FIN events."""
    result = b""
    for ev in events:
        if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) and ev[2] is not None:
            if stream_id is None or ev[1] == stream_id:
                result += ev[2]
    return result


def wait_for_server_cnx(server, timeout=5.0):
    """Wait for server to get a connection READY with cnx pointer.

    The loop-ready READY event has cnx=0; we need the
    connection-level READY which has the actual cnx pointer.
    """
    deadline = time.monotonic() + timeout
    all_events = []
    while time.monotonic() < deadline:
        events = server.drain_rx()
        all_events.extend(events)
        # Look for READY or ALMOST_READY with non-zero cnx
        for ev in all_events:
            if ev[0] in (SPSC_EVT_READY, SPSC_EVT_ALMOST_READY):
                if ev[5] != 0:
                    return all_events, ev[5]
        time.sleep(0.02)
    return all_events, 0


def start_server(port):
    """Start a server TransportContext on the given port."""
    server = TransportContext()
    server.start(
        port=port,
        cert_file=CERT_FILE,
        key_file=KEY_FILE,
        alpn=ALPN,
        is_client=False,
    )
    assert wait_for_ready(server), "Server not ready"
    return server


def has_connection_ready(events):
    """Check for a connection-level READY (cnx != 0).

    The loop-ready READY has cnx=0; connection READY has cnx set.
    """
    for ev in events:
        if ev[0] == SPSC_EVT_READY and ev[5] != 0:
            return True
    return False


def connect_client(port, max_datagram_frame_size=0):
    """Start a client, connect to server, return (client, cnx_ptr).

    Waits for ALMOST_READY (connection created) and connection
    READY (handshake complete, cnx != 0).
    """
    client = TransportContext()
    client.start(port=0, alpn=ALPN, is_client=True,
                 max_datagram_frame_size=max_datagram_frame_size)
    assert wait_for_ready(client), "Client not ready"

    client.create_client_connection(
        "127.0.0.1", port,
        sni="localhost", alpn=ALPN,
    )

    # Wait for ALMOST_READY (connection created by network thread)
    events = drain_until(
        client, SPSC_EVT_ALMOST_READY, timeout=5.0,
    )
    cnx_ptr = get_cnx_ptr(events, SPSC_EVT_ALMOST_READY)
    assert cnx_ptr != 0, (
        f"No ALMOST_READY with cnx, got: {events}"
    )

    # Wait for connection READY (cnx != 0, not loop-ready)
    if not has_connection_ready(events):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            more = client.drain_rx()
            events.extend(more)
            if has_connection_ready(events):
                break
            time.sleep(0.02)
        assert has_connection_ready(events), (
            f"Client handshake incomplete: {events}"
        )

    return client, cnx_ptr


class TestLoopback:
    """End-to-end loopback tests with real picoquic."""

    def test_client_server_handshake(self):
        """Client connects to server, both complete handshake."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                assert cnx_ptr != 0

                # Server should get connection READY (cnx != 0)
                srv_events, srv_cnx = wait_for_server_cnx(
                    server,
                )
                assert srv_cnx != 0, (
                    f"Server no connection, got: {srv_events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_stream_data_exchange(self):
        """Client sends stream data, server receives it."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                # Send data on stream 0
                test_data = b"Hello from aiopquic!"
                client.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, 0,
                    data=test_data, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Server should receive stream data
                server_events = drain_until(
                    server, SPSC_EVT_STREAM_DATA, timeout=5.0,
                )
                data_events = [
                    e for e in server_events
                    if e[0] == SPSC_EVT_STREAM_DATA
                ]
                assert len(data_events) >= 1, (
                    f"Server no stream data: {server_events}"
                )

                received = b"".join(
                    e[2] for e in data_events if e[2] is not None
                )
                assert received == test_data, (
                    f"Data mismatch: {received!r} != {test_data!r}"
                )

            finally:
                client.stop()
        finally:
            server.stop()

    def test_stream_fin(self):
        """Client sends data + FIN, server sees completion."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                # Send data + FIN
                test_data = b"final message"
                client.push_tx(
                    SPSC_EVT_TX_STREAM_FIN, 0,
                    data=test_data, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Server should see FIN
                all_events = []
                deadline = time.monotonic() + 5.0
                got_fin = False
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    all_events.extend(events)
                    for ev in all_events:
                        if ev[0] == SPSC_EVT_STREAM_FIN:
                            got_fin = True
                        elif (ev[0] == SPSC_EVT_STREAM_DATA
                              and ev[3]):
                            got_fin = True
                    if got_fin:
                        break
                    time.sleep(0.02)

                assert got_fin, (
                    f"Server no FIN, got: {all_events}"
                )

            finally:
                client.stop()
        finally:
            server.stop()

    def test_bidirectional_stream(self):
        """Client sends on stream 0, server echoes back."""
        port = next_port()
        server = start_server(port)
        try:
            client, client_cnx = connect_client(port)
            try:
                # Client sends data on stream 0 (client-initiated bidi)
                request = b"ping from client"
                client.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, 0,
                    data=request, cnx_ptr=client_cnx,
                )
                client.wake_up()

                # Server receives it — extract server cnx from event
                srv_events = drain_until(
                    server, SPSC_EVT_STREAM_DATA, timeout=5.0,
                )
                received = collect_stream_data(
                    srv_events, stream_id=0,
                )
                assert received == request, (
                    f"Server got {received!r}"
                )

                # Get server cnx from the STREAM_DATA event
                srv_cnx = 0
                for ev in srv_events:
                    if ev[0] == SPSC_EVT_STREAM_DATA and ev[5]:
                        srv_cnx = ev[5]
                        break
                assert srv_cnx != 0, "No server cnx"

                # Server echoes back on the same stream 0
                reply = b"pong from server"
                server.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, 0,
                    data=reply, cnx_ptr=srv_cnx,
                )
                server.wake_up()

                # Client receives the reply
                cli_events = drain_until(
                    client, SPSC_EVT_STREAM_DATA, timeout=5.0,
                )
                cli_received = collect_stream_data(
                    cli_events, stream_id=0,
                )
                assert cli_received == reply, (
                    f"Client got {cli_received!r}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_multiple_streams(self):
        """Client sends on multiple stream IDs, server receives all."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                # Client-initiated bidi streams: 0, 4, 8
                streams = {
                    0: b"stream zero",
                    4: b"stream four",
                    8: b"stream eight",
                }
                for sid, data in streams.items():
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_DATA, sid,
                        data=data, cnx_ptr=cnx_ptr,
                    )
                client.wake_up()

                # Collect all stream data on server
                all_events = []
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    all_events.extend(events)
                    # Check if we have data for all 3 streams
                    seen = set()
                    for ev in all_events:
                        if ev[0] == SPSC_EVT_STREAM_DATA:
                            seen.add(ev[1])
                    if seen >= {0, 4, 8}:
                        break
                    time.sleep(0.02)

                for sid, expected in streams.items():
                    received = collect_stream_data(all_events, stream_id=sid)
                    assert received == expected, (
                        f"Stream {sid}: got {received!r}, expected {expected!r}"
                    )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_connection_close(self):
        """Client closes connection, server sees CLOSE event."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                # Client closes with error code 0 (clean close)
                client.push_tx(
                    SPSC_EVT_TX_CLOSE, 0,
                    error_code=0, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Server should see CLOSE or APP_CLOSE
                all_events = []
                deadline = time.monotonic() + 5.0
                got_close = False
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    all_events.extend(events)
                    if (has_event(all_events, SPSC_EVT_CLOSE)
                            or has_event(
                                all_events, SPSC_EVT_APP_CLOSE,
                            )):
                        got_close = True
                        break
                    time.sleep(0.02)

                assert got_close, (
                    f"Server no CLOSE, got: {all_events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_large_data(self):
        """Client sends a large payload, server receives it all."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                # 32 KB payload
                test_data = bytes(range(256)) * 128
                client.push_tx(
                    SPSC_EVT_TX_STREAM_FIN, 0,
                    data=test_data, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Accumulate until we have all data or timeout
                all_events = []
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    all_events.extend(events)
                    received = collect_stream_data(
                        all_events, stream_id=0,
                    )
                    if len(received) >= len(test_data):
                        break
                    time.sleep(0.02)

                received = collect_stream_data(
                    all_events, stream_id=0,
                )
                assert len(received) == len(test_data), (
                    f"Got {len(received)}/{len(test_data)}"
                )
                assert received == test_data
            finally:
                client.stop()
        finally:
            server.stop()

    def test_multiple_connections(self):
        """Multiple clients connect to the same server."""
        port = next_port()
        server = start_server(port)
        try:
            client1, cnx1 = connect_client(port)
            try:
                client2, cnx2 = connect_client(port)
                try:
                    # Both should have distinct cnx pointers
                    assert cnx1 != cnx2, "Connections should be distinct"

                    # Each sends on stream 0
                    client1.push_tx(
                        SPSC_EVT_TX_STREAM_DATA, 0,
                        data=b"from client 1", cnx_ptr=cnx1,
                    )
                    client1.wake_up()
                    client2.push_tx(
                        SPSC_EVT_TX_STREAM_DATA, 0,
                        data=b"from client 2", cnx_ptr=cnx2,
                    )
                    client2.wake_up()

                    # Server should receive data from both
                    all_events = []
                    deadline = time.monotonic() + 5.0
                    seen_cnx = set()
                    while time.monotonic() < deadline:
                        events = server.drain_rx()
                        all_events.extend(events)
                        for ev in all_events:
                            if ev[0] == SPSC_EVT_STREAM_DATA and ev[5] != 0:
                                seen_cnx.add(ev[5])
                        if len(seen_cnx) >= 2:
                            break
                        time.sleep(0.02)

                    assert len(seen_cnx) >= 2, (
                        f"Server saw {len(seen_cnx)} connections, expected 2"
                    )
                finally:
                    client2.stop()
            finally:
                client1.stop()
        finally:
            server.stop()

    def test_datagram(self):
        """Send and receive datagrams."""
        port = next_port()
        # Both sides need max_datagram_frame_size > 0 to enable datagrams
        server = TransportContext()
        server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                     alpn=ALPN, is_client=False, max_datagram_frame_size=1200)
        assert wait_for_ready(server), "Server not ready"
        try:
            client, cnx_ptr = connect_client(port, max_datagram_frame_size=1200)
            try:
                # Wait for server to see connection
                srv_events, srv_cnx = wait_for_server_cnx(server)
                assert srv_cnx != 0

                # Client sends datagram
                payload = b"hello datagram"
                client.push_tx(
                    SPSC_EVT_TX_DATAGRAM, 0,
                    data=payload, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Server should receive it
                deadline = time.monotonic() + 5.0
                received = b""
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    for ev in events:
                        if ev[0] == SPSC_EVT_DATAGRAM and ev[2] is not None:
                            received = ev[2]
                    if received:
                        break
                    time.sleep(0.02)

                assert received == payload, (
                    f"Expected {payload!r}, got {received!r}"
                )

                # Server sends datagram back
                reply = b"datagram reply"
                server.push_tx(
                    SPSC_EVT_TX_DATAGRAM, 0,
                    data=reply, cnx_ptr=srv_cnx,
                )
                server.wake_up()

                # Client should receive it
                deadline = time.monotonic() + 5.0
                received = b""
                while time.monotonic() < deadline:
                    events = client.drain_rx()
                    for ev in events:
                        if ev[0] == SPSC_EVT_DATAGRAM and ev[2] is not None:
                            received = ev[2]
                    if received:
                        break
                    time.sleep(0.02)

                assert received == reply, (
                    f"Expected {reply!r}, got {received!r}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_stream_reset(self):
        """Client resets a stream, server receives reset event."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                srv_events, srv_cnx = wait_for_server_cnx(server)
                assert srv_cnx != 0

                # Client sends some data then resets the stream
                stream_id = 0  # client-initiated bidi
                client.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, stream_id,
                    data=b"before reset", cnx_ptr=cnx_ptr,
                )
                client.wake_up()
                time.sleep(0.1)

                # Reset with error code 42
                client.push_tx(
                    SPSC_EVT_TX_STREAM_RESET, stream_id,
                    error_code=42, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                # Server should receive STREAM_RESET
                deadline = time.monotonic() + 5.0
                reset_seen = False
                reset_code = None
                while time.monotonic() < deadline:
                    events = server.drain_rx()
                    for ev in events:
                        if ev[0] == SPSC_EVT_STREAM_RESET:
                            reset_seen = True
                            reset_code = ev[4]
                    if reset_seen:
                        break
                    time.sleep(0.02)

                assert reset_seen, "Server did not receive stream reset"
                assert reset_code == 42, (
                    f"Expected error_code=42, got {reset_code}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_alpn_mismatch(self):
        """Server with one ALPN, client with a different one — no handshake."""
        port = next_port()
        server = TransportContext()
        server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                     alpn="hq-interop", is_client=False)
        assert wait_for_ready(server), "Server not ready"
        try:
            client = TransportContext()
            client.start(port=0, alpn="other-alpn", is_client=True)
            assert wait_for_ready(client), "Client not ready"
            try:
                client.create_client_connection(
                    "127.0.0.1", port,
                    sni="localhost", alpn="other-alpn",
                )

                deadline = time.monotonic() + 5.0
                events = []
                while time.monotonic() < deadline:
                    events.extend(client.drain_rx())
                    if has_connection_ready(events):
                        break
                    if (has_event(events, SPSC_EVT_CLOSE)
                            or has_event(events, SPSC_EVT_APP_CLOSE)):
                        break
                    time.sleep(0.05)

                assert not has_connection_ready(events), (
                    f"Handshake unexpectedly succeeded with ALPN mismatch: "
                    f"{events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_idle_timeout_fires(self):
        """Idle timeout is configured and the connection actually closes."""
        port = next_port()
        server = TransportContext()
        server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                     alpn=ALPN, is_client=False, idle_timeout_ms=500)
        assert wait_for_ready(server), "Server not ready"
        try:
            client = TransportContext()
            client.start(port=0, alpn=ALPN, is_client=True,
                         idle_timeout_ms=500)
            assert wait_for_ready(client), "Client not ready"
            try:
                client.create_client_connection(
                    "127.0.0.1", port,
                    sni="localhost", alpn=ALPN,
                )
                events = drain_until(
                    client, SPSC_EVT_ALMOST_READY, timeout=5.0,
                )
                assert get_cnx_ptr(events) != 0, "No client cnx"

                # After ~500ms idle, both sides surface a CLOSE.
                deadline = time.monotonic() + 5.0
                got_close = False
                while time.monotonic() < deadline:
                    events.extend(client.drain_rx())
                    if (has_event(events, SPSC_EVT_CLOSE)
                            or has_event(events, SPSC_EVT_APP_CLOSE)):
                        got_close = True
                        break
                    time.sleep(0.05)

                assert got_close, (
                    f"Client did not see idle-timeout CLOSE within 5s: "
                    f"{events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_application_close_with_reason_code(self):
        """Client closes with a specific app error code; server sees it."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                _, srv_cnx = wait_for_server_cnx(server)
                assert srv_cnx != 0

                client.push_tx(
                    SPSC_EVT_TX_CLOSE, 0,
                    error_code=0xCAFE, cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                deadline = time.monotonic() + 5.0
                code = None
                events = []
                while time.monotonic() < deadline:
                    events.extend(server.drain_rx())
                    for ev in events:
                        if ev[0] == SPSC_EVT_APP_CLOSE:
                            code = ev[4]
                            break
                    if code is not None:
                        break
                    time.sleep(0.02)

                assert code == 0xCAFE, (
                    f"Expected app close code=0xCAFE, got {code!r}; "
                    f"events={events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_stop_sending(self):
        """Peer issues STOP_SENDING; receiver gets the event.

        NOTE: error_code on STOP_SENDING events is currently 0 because
        picoquic exposes only the RESET_STREAM error code via its
        public stream-error getter; STOP_SENDING's code lives in
        stream->remote_stop_error in picoquic_internal.h. See the TODO
        in c/callback.h. This test verifies event delivery; tighten
        the code-equality assertion once the helper lands.
        """
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                client.push_tx(
                    SPSC_EVT_TX_STREAM_DATA, 0,
                    data=b"x", cnx_ptr=cnx_ptr,
                )
                client.wake_up()

                srv_events = drain_until(
                    server, SPSC_EVT_STREAM_DATA, timeout=5.0,
                )
                srv_cnx = 0
                for ev in srv_events:
                    if ev[0] == SPSC_EVT_STREAM_DATA and ev[5]:
                        srv_cnx = ev[5]
                        break
                assert srv_cnx != 0, "No server cnx"

                server.push_tx(
                    SPSC_EVT_TX_STOP_SENDING, 0,
                    error_code=99, cnx_ptr=srv_cnx,
                )
                server.wake_up()

                deadline = time.monotonic() + 5.0
                seen = False
                events = []
                while time.monotonic() < deadline:
                    events.extend(client.drain_rx())
                    for ev in events:
                        if ev[0] == SPSC_EVT_STOP_SENDING:
                            seen = True
                            break
                    if seen:
                        break
                    time.sleep(0.02)

                assert seen, (
                    f"Client did not receive STOP_SENDING; events={events}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_many_concurrent_streams(self):
        """Open 64 client-initiated bidi streams; all data arrives intact."""
        port = next_port()
        server = start_server(port)
        try:
            client, cnx_ptr = connect_client(port)
            try:
                n_streams = 64
                streams = {
                    sid: f"stream-{sid:03d}-payload".encode()
                    for sid in range(0, n_streams * 4, 4)
                }
                for sid, data in streams.items():
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_FIN, sid,
                        data=data, cnx_ptr=cnx_ptr,
                    )
                client.wake_up()

                received = {sid: b"" for sid in streams}
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    for ev in server.drain_rx():
                        if ev[0] in (SPSC_EVT_STREAM_DATA,
                                     SPSC_EVT_STREAM_FIN):
                            sid = ev[1]
                            if sid in received and ev[2] is not None:
                                received[sid] += ev[2]
                    if all(received[sid] == streams[sid]
                           for sid in streams):
                        break
                    time.sleep(0.02)

                missing = [sid for sid in streams
                           if received[sid] != streams[sid]]
                assert not missing, (
                    f"{len(missing)}/{n_streams} streams incomplete: "
                    f"first missing sid={missing[0]} "
                    f"got={received[missing[0]]!r} "
                    f"want={streams[missing[0]]!r}"
                )
            finally:
                client.stop()
        finally:
            server.stop()

    def test_tx_ring_overflow_raises(self):
        """Filling the TX ring without a wake-up surfaces a clean error."""
        # Small ring so we can actually fill it. The branch's
        # TransportContext takes only ring_capacity (no arena_size).
        client = TransportContext(ring_capacity=8)
        client.start(port=0, alpn=ALPN, is_client=True)
        assert wait_for_ready(client), "Client not ready"
        try:
            import builtins
            raised = False
            for _ in range(64):
                try:
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_DATA, 0,
                        data=b"x" * 256, cnx_ptr=0,
                    )
                except builtins.BufferError:
                    raised = True
                    break
            assert raised, "TX ring overflow did not surface BufferError"
        finally:
            client.stop()

    def test_bad_sni_does_not_crash(self):
        """Non-matching / weird SNI values do not crash the wrapper."""
        port = next_port()
        server = start_server(port)
        try:
            for sni in ("", "  ", "this.does.not.match"):
                client = TransportContext()
                client.start(port=0, alpn=ALPN, is_client=True)
                assert wait_for_ready(client), "Client not ready"
                try:
                    client.create_client_connection(
                        "127.0.0.1", port,
                        sni=sni, alpn=ALPN,
                    )
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        client.drain_rx()
                        time.sleep(0.05)
                    assert client.thread_ready, (
                        f"Network thread died after sni={sni!r}"
                    )
                finally:
                    client.stop()
        finally:
            server.stop()
