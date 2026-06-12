"""Stream-level flow control tests — WebTransport.

Companion to tests/test_flow_control_quic.py — the same observable
contract exercised through WebTransportSession instead of
QuicConnection:

  - send_stream_data raises BufferError when the WT stream can't
    accept more bytes right now (per-stream backpressure).
  - get_tx_data_drain_event(stream_id) returns an asyncio.Event that
    fires when the writer can retry.
  - send_stream_data_drained() composes the two so the caller never
    sees a BufferError and never strands bytes.

Both tests run for several seconds so the contract is observed in
steady state, not a transient burst.

  - test_buffer_error_under_sustained_push:
      Producer pushes 256 KB chunks on a uni WT stream while the
      server holds the session open without consuming bytes.
      Asserts BufferError surfaces and pushes succeed as the
      transport drains.

  - test_drained_helper_absorbs_backpressure:
      Producer drives every push through send_stream_data_drained.
      Server drains asynchronously; on FIN every pushed byte is
      received byte-perfect.
"""
import asyncio
import os
import time

import pytest

from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.events import (
    WebTransportNewStream, WebTransportStreamDataReceived,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

_port_counter = 36600


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


CHUNK = b"\xaa" * (256 * 1024)         # 256 KB per push
DURATION = 5.0                          # seconds of steady-state push


@pytest.mark.asyncio
async def test_buffer_error_under_sustained_push():
    """Producer pushes synchronously while the server holds the WT
    session open without consuming. Backpressure surfaces as
    BufferError when the stream can't accept more bytes; the drain
    event fires as the transport makes room, allowing further pushes
    to succeed in steady state."""
    port = next_port()

    async def handler(session):
        # Hold the session open; do not start any stream-drain task.
        await session._session_closed.wait()

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=False)
            event = wt.get_tx_data_drain_event(sid)

            push_ok = 0
            push_err = 0
            drain_observed = 0
            deadline = time.monotonic() + DURATION

            while time.monotonic() < deadline:
                try:
                    wt.send_stream_data(sid, CHUNK, end_stream=False)
                    push_ok += 1
                except BufferError:
                    push_err += 1
                    if event.is_set():
                        drain_observed += 1
                        event.clear()
                # Yield so the picoquic worker + dispatcher get cycles.
                await asyncio.sleep(0)

            # Saturation evidence: at least one BufferError observed
            # over a multi-second window.
            assert push_err >= 1, (
                f"no BufferError observed in {DURATION}s "
                f"(pushes={push_ok})"
            )
            # Pushed something — proves the WT session came up.
            assert push_ok >= 4, f"only {push_ok} successful pushes"
            print(f"[diag] push_ok={push_ok} push_err={push_err} "
                  f"drain_observed={drain_observed}")
    finally:
        server.close()


@pytest.mark.asyncio
async def test_drained_helper_absorbs_backpressure():
    """Producer drives every push through send_stream_data_drained.
    Server drains asynchronously at network rate. On FIN every pushed
    byte is received byte-perfect — the helper absorbed every
    BufferError, awaited the drain event, and retried correctly with
    no dropped or duplicated bytes."""
    port = next_port()

    received = bytearray()
    received_done = asyncio.Event()

    async def handler(session):
        async def _recv():
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    sid = ev.stream_id
                    async for sev in session.receive_stream_data(sid):
                        if isinstance(sev, WebTransportStreamDataReceived):
                            received.extend(sev.data)
                            if sev.end_stream:
                                received_done.set()
                                return
        asyncio.create_task(_recv())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            sid = await wt.create_stream(bidir=False)

            pushed = 0
            deadline = time.monotonic() + DURATION

            while time.monotonic() < deadline:
                await wt.send_stream_data_drained(
                    sid, CHUNK, end_stream=False)
                pushed += len(CHUNK)

            # Final FIN. send_stream_data_drained handles the same
            # backpressure shape on a possible empty-payload FIN.
            await wt.send_stream_data_drained(
                sid, b"", end_stream=True)

            await asyncio.wait_for(received_done.wait(), timeout=30.0)

            assert len(received) == pushed, (
                f"received {len(received)} of {pushed} bytes")
            assert bytes(received) == CHUNK * (pushed // len(CHUNK)), (
                "received bytes do not match pushed pattern")
    finally:
        server.close()
