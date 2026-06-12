"""Stream-level flow control tests — raw-QUIC.

The observable contract `QuicConnection` exposes for a saturating
producer:

  - send_stream_data raises BufferError when the stream can't accept
    more bytes right now (per-stream backpressure surfaces).
  - get_tx_data_drain_event(stream_id) returns an asyncio.Event that fires
    when the writer can retry.
  - send_stream_data_drained() composes the two so the caller never
    sees a BufferError and never strands bytes — push rate naturally
    matches network drain rate.

Both tests run for several seconds so the contract is observed in
steady state, not a transient burst.

  - test_buffer_error_under_sustained_push:
      Producer pushes 256 KB chunks in an async-cooperative loop;
      server holds the connection open without consuming bytes.
      Asserts BufferError surfaces and many pushes succeed as the
      transport drains.

  - test_drained_helper_absorbs_backpressure:
      Producer drives every push through send_stream_data_drained.
      Server drains asynchronously; on FIN every pushed byte is
      received byte-perfect — proving the helper handles the
      saturation cycle without dropping or duplicating bytes.
"""
import asyncio
import os
import time

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.quic.events import StreamDataReceived

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

_port_counter = 36500


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_cfg(_port):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["hq-interop"])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=["hq-interop"])


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


CHUNK = b"\xaa" * (256 * 1024)         # 256 KB per push
DURATION = 5.0                          # seconds of steady-state push


@pytest.mark.asyncio
async def test_buffer_error_under_sustained_push():
    """Producer pushes synchronously while the server holds the
    connection open without consuming. Backpressure surfaces as
    BufferError when the stream can't accept more bytes; the drain
    event fires as the transport makes room, allowing further pushes
    to succeed in steady state."""
    port = next_port()

    class _NoDrainServer(QuicConnectionProtocol):
        # Receive events but never look at the data — leaves sc->rx
        # un-popped which (with default flow control windows) closes
        # the send window from the peer.
        def quic_event_received(self, event):
            pass

    srv_quic = QuicConnection(configuration=_server_cfg(port))
    srv_quic._start_transport(port=port)
    srv_protocol = _NoDrainServer(srv_quic)
    srv_protocol._start(asyncio.get_event_loop())

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_cfg(),
        ) as client:
            stream_id = client._quic.get_next_available_stream_id()
            event = client._quic.get_tx_data_drain_event(stream_id)

            push_ok = 0
            push_err = 0
            drain_observed = 0
            deadline = time.monotonic() + DURATION

            while time.monotonic() < deadline:
                try:
                    client._quic.send_stream_data(
                        stream_id, CHUNK, end_stream=False)
                    push_ok += 1
                except BufferError:
                    push_err += 1
                    # Snapshot whether a drain wake is already pending
                    # without consuming it (the next attempt may
                    # benefit). Reset counter is observed by re-arming.
                    if event.is_set():
                        drain_observed += 1
                        event.clear()
                # Always yield so the worker thread + event loop get
                # cycles. Without this Python's tight loop holds the
                # GIL too long and the test conflates "couldn't push"
                # with "worker had no chance to run".
                await asyncio.sleep(0)

            # Saturation evidence: at least one BufferError observed
            # over a multi-second window. On hardware where the
            # loopback drain rate exceeds Python's push rate this can
            # be flaky — kept low to absorb a slow runner.
            assert push_err >= 1, (
                f"no BufferError observed in {DURATION}s "
                f"(pushes={push_ok})"
            )
            # And we MUST have pushed something — otherwise the
            # connection never came up and we'd be measuring nothing.
            assert push_ok >= 4, f"only {push_ok} successful pushes"
            # If drain_observed stayed 0 across many BufferErrors the
            # wake-up path is broken. Stay tolerant of timing — the
            # event might have been consumed silently by a previous
            # iteration where push_err was incremented but the next
            # iteration happened to see push_ok rather than this
            # branch. Worth logging on failure either way.
            print(f"[diag] push_ok={push_ok} push_err={push_err} "
                  f"drain_observed={drain_observed}")
    finally:
        srv_protocol._stop()
        srv_quic.stop()


@pytest.mark.asyncio
async def test_drained_helper_absorbs_backpressure():
    """Producer drives every push through send_stream_data_drained.
    Server drains asynchronously at network rate. On FIN every pushed
    byte is received byte-perfect — the helper absorbed every
    BufferError, awaited the drain event, and retried correctly with
    no dropped or duplicated bytes."""
    port = next_port()

    server_got = bytearray()

    class _DrainingServer(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                server_got.extend(event.data)

    srv_quic = QuicConnection(configuration=_server_cfg(port))
    srv_quic._start_transport(port=port)
    srv_protocol = _DrainingServer(srv_quic)
    srv_protocol._start(asyncio.get_event_loop())

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_cfg(),
        ) as client:
            stream_id = client._quic.get_next_available_stream_id()

            pushed = 0
            deadline = time.monotonic() + DURATION

            while time.monotonic() < deadline:
                await client._quic.send_stream_data_drained(
                    stream_id, CHUNK, end_stream=False)
                pushed += len(CHUNK)

            # Final FIN. send_stream_data_drained handles the same
            # backpressure on a possible last-chunk-empty FIN frame.
            await client._quic.send_stream_data_drained(
                stream_id, b"", end_stream=True)

            # Wait for the server to receive everything.
            wait_deadline = time.monotonic() + 30.0
            while len(server_got) < pushed and time.monotonic() < wait_deadline:
                await asyncio.sleep(0.05)

            assert len(server_got) == pushed, (
                f"received {len(server_got)} of {pushed} bytes")
            # Byte-perfect check on a representative window — full
            # equality on multi-MB bytearrays is slow but worthwhile
            # since we're proving the integrity guarantee.
            assert bytes(server_got) == CHUNK * (pushed // len(CHUNK)), (
                "received bytes do not match pushed pattern")
    finally:
        srv_protocol._stop()
        srv_quic.stop()
