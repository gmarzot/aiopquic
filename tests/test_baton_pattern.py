"""Pure-QUIC baton-pattern stress test.

Mirrors the WebTransport wt_baton interop scenario at the QUIC layer:
a 1-byte counter is passed back and forth, alternating between
unidirectional and bidirectional streams. Termination occurs when
the counter wraps to 0.

Exercises stream multiplexing, FIN handling, and parallel UNI/BIDI
patterns the same way wt_baton does — useful even though
aiopquic also ships a real WebTransport stack now (see
aiopquic.asyncio.webtransport): this test is independent of H3 so
regressions in the QUIC transport layer surface here without a WT
session in the loop.

For a faithful WebTransport wt_baton interop test, layer it on top of
the WT API (the picohttp reference implementation is at
third_party/picoquic/picohttp/wt_baton.c).
"""
import time

from tests.test_loopback import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
    SPSC_EVT_TX_STREAM_DATA, SPSC_EVT_TX_STREAM_FIN,
    next_port, start_server, connect_client, wait_for_server_cnx,
)


def _next_client_uni_sid(prev):
    return prev + 4


def _next_client_bidi_sid(prev):
    return prev + 4


def _drain_stream_payload(ctx, sid, expect_len, timeout=5.0):
    received = b""
    deadline = time.monotonic() + timeout
    while len(received) < expect_len and time.monotonic() < deadline:
        for ev in ctx.drain_rx():
            if ev[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN) \
                    and ev[1] == sid and ev[2] is not None:
                received += ev[2]
        if len(received) < expect_len:
            time.sleep(0.005)
    return received


class TestBatonPattern:
    """Alternating UNI / BIDI baton-passing over plain QUIC streams."""

    def test_baton_uni_then_bidi(self):
        """Counter walks UNI(client→server) → BIDI(client→server→client)."""
        port = next_port()
        server = start_server(port)
        try:
            client, client_cnx = connect_client(port)
            try:
                _, server_cnx = wait_for_server_cnx(server)
                assert server_cnx != 0

                rounds = 8
                start_value = 17
                client_uni_sid = 2
                client_bidi_sid = 0
                value = start_value

                for i in range(rounds):
                    sid_uni = client_uni_sid
                    client_uni_sid = _next_client_uni_sid(client_uni_sid)
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_FIN, sid_uni,
                        data=bytes([value]), cnx_ptr=client_cnx,
                    )
                    client.wake_up()

                    got = _drain_stream_payload(server, sid_uni, 1)
                    assert got == bytes([value]), (
                        f"round {i} UNI: server got {got!r}, "
                        f"expected {bytes([value])!r}"
                    )
                    value = (value + 1) & 0xFF

                    sid_bidi = client_bidi_sid
                    client_bidi_sid = _next_client_bidi_sid(client_bidi_sid)
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_FIN, sid_bidi,
                        data=bytes([value]), cnx_ptr=client_cnx,
                    )
                    client.wake_up()

                    got = _drain_stream_payload(server, sid_bidi, 1)
                    assert got == bytes([value]), (
                        f"round {i} BIDI->S: server got {got!r}, "
                        f"expected {bytes([value])!r}"
                    )
                    value = (value + 1) & 0xFF

                    server.push_tx(
                        SPSC_EVT_TX_STREAM_FIN, sid_bidi,
                        data=bytes([value]), cnx_ptr=server_cnx,
                    )
                    server.wake_up()

                    got = _drain_stream_payload(client, sid_bidi, 1)
                    assert got == bytes([value]), (
                        f"round {i} BIDI->C: client got {got!r}, "
                        f"expected {bytes([value])!r}"
                    )
                    value = (value + 1) & 0xFF

                assert value == (start_value + 3 * rounds) & 0xFF
            finally:
                client.stop()
        finally:
            server.stop()

    def test_baton_parallel_uni_streams(self):
        """Many UNI streams in flight at once — stress multiplexing."""
        port = next_port()
        server = start_server(port)
        try:
            client, client_cnx = connect_client(port)
            try:
                _, server_cnx = wait_for_server_cnx(server)
                assert server_cnx != 0

                n = 32
                streams = {}
                sid = 2
                for i in range(n):
                    streams[sid] = i & 0xFF
                    client.push_tx(
                        SPSC_EVT_TX_STREAM_FIN, sid,
                        data=bytes([i & 0xFF]), cnx_ptr=client_cnx,
                    )
                    sid = _next_client_uni_sid(sid)
                client.wake_up()

                received = {}
                deadline = time.monotonic() + 10.0
                while len(received) < n and time.monotonic() < deadline:
                    for ev in server.drain_rx():
                        if ev[0] in (SPSC_EVT_STREAM_DATA,
                                     SPSC_EVT_STREAM_FIN):
                            if ev[1] in streams and ev[2]:
                                received[ev[1]] = ev[2]
                    if len(received) < n:
                        time.sleep(0.005)

                missing = set(streams) - set(received)
                assert not missing, f"missing {len(missing)} streams: " \
                    f"first={sorted(missing)[0]}"
                for s, expected_byte in streams.items():
                    assert received[s] == bytes([expected_byte]), \
                        f"sid={s} got {received[s]!r}, expected " \
                        f"{bytes([expected_byte])!r}"
            finally:
                client.stop()
        finally:
            server.stop()
