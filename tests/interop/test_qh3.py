"""aiopquic ↔ qh3 cross-stack interop.

Spawns the qh3 peer as a subprocess (separate asyncio loop, isolated
config). Each test verifies handshake completion + byte-conservation
end-to-end via CRC32. Pass criterion: bytes received == bytes sent and
CRCs equal on both sides.

Run with:
  pytest tests/interop/test_qh3.py -v -s
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import zlib

import pytest

from .conftest import counted_pad, crc32, _free_port


PEER = os.path.join(os.path.dirname(__file__), "peers", "qh3_echo.py")
ALPN = "hq-interop"


pytestmark = pytest.mark.asyncio


def _qh3_available() -> bool:
    try:
        import qh3  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _qh3_available(), reason="qh3 not installed"),
]


def _spawn_peer(args, stderr_to=None):
    return subprocess.Popen(
        [sys.executable, PEER] + args,
        stdout=subprocess.PIPE,
        stderr=stderr_to or subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _wait_for_listen(proc, timeout=5.0):
    """qh3_echo emits 'serving on ...' on stderr when ready."""
    deadline = time.monotonic() + timeout
    line = ""
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        if "serving on" in line:
            return
    raise TimeoutError(f"qh3 peer did not start within {timeout}s; "
                       f"last stderr: {line!r}")


def _read_summary(proc, timeout=15.0) -> dict:
    """Read one JSON line of summary on stdout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            return json.loads(line.strip())
        except json.JSONDecodeError:
            continue
    raise TimeoutError(f"no summary line from qh3 peer within {timeout}s")


# ---------------------------------------------------------------------------
# aiopquic-as-client → qh3-as-server (sink mode):
#   verifies aiopquic's TX is byte-faithful through a different stack.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload_bytes", [
    1 * 1024 * 1024,    # 1 MB — quick smoke
    10 * 1024 * 1024,   # 10 MB — meaningful sustained transfer
])
async def test_aiopquic_client_qh3_server_sink(cert_paths, payload_bytes):
    port = _free_port()
    server = _spawn_peer([
        "serve", "--port", str(port),
        "--cert", cert_paths["cert"], "--key", cert_paths["key"],
        "--alpn", ALPN, "--mode", "sink",
    ])
    try:
        _wait_for_listen(server)

        # aiopquic client transfers counted-pad and FINs.
        from aiopquic.asyncio import connect
        from aiopquic.quic.configuration import QuicConfiguration

        cfg = QuicConfiguration(
            is_client=True,
            alpn_protocols=[ALPN],
            verify_mode=__import__("ssl").CERT_NONE,
        )

        crc_sent = 0
        bytes_sent = 0
        chunk_size = 4096
        pad = counted_pad(chunk_size)

        async with connect("127.0.0.1", port, configuration=cfg) as client:
            stream_id = client._quic.get_next_available_stream_id()
            remaining = payload_bytes
            while remaining > 0:
                n = min(chunk_size, remaining)
                chunk = pad if n == chunk_size else pad[:n]
                # Pull-model raises BufferError when per-stream send ring
                # is full. Retry with a short sleep until picoquic drains.
                while True:
                    try:
                        client._quic.send_stream_data(
                            stream_id, chunk,
                            end_stream=(remaining == n),
                        )
                        client.transmit()
                        break
                    except BufferError:
                        await asyncio.sleep(0.001)
                crc_sent = zlib.crc32(chunk, crc_sent) & 0xFFFFFFFF
                bytes_sent += n
                remaining -= n
                await asyncio.sleep(0)
            # Wait for the sink's FIN echo so we know it consumed everything.
            await asyncio.sleep(0.5)

        # Server reports total bytes + crc; gracefully shut it down.
        server.terminate()
        try:
            summary = _read_summary(server, timeout=5.0)
        except TimeoutError:
            pytest.fail("qh3 sink did not emit summary line")

        assert summary["ok"], summary
        assert summary["stream_bytes"] == bytes_sent, (
            f"qh3 received {summary['stream_bytes']} bytes; "
            f"aiopquic sent {bytes_sent}"
        )
        assert summary["crc32"] == crc_sent, (
            f"crc mismatch: qh3=0x{summary['crc32']:08x} "
            f"aiopquic=0x{crc_sent:08x}"
        )
    finally:
        if server.poll() is None:
            server.kill()
        server.wait(timeout=2)


# ---------------------------------------------------------------------------
# qh3-as-client → aiopquic-as-server (sink mode):
#   verifies aiopquic's RX is byte-faithful from a different stack.
#   Currently silent-drop hazard on RX SPSC ring full at high rates;
#   this test exercises the same path we're refactoring next.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload_bytes", [
    1 * 1024 * 1024,
    10 * 1024 * 1024,
])
async def test_qh3_client_aiopquic_server_sink(cert_paths, payload_bytes):
    port = _free_port()
    # Spawn aiopquic server in-process; collect bytes received per stream.
    from aiopquic.asyncio import serve
    from aiopquic.quic.configuration import QuicConfiguration
    from aiopquic.quic.events import StreamDataReceived

    cfg = QuicConfiguration(
        is_client=False,
        alpn_protocols=[ALPN],
    )
    cfg.load_cert_chain(cert_paths["cert"], cert_paths["key"])

    received = {"bytes": 0, "crc": 0, "fin": False}
    fin_event = asyncio.Event()

    async def handle_protocol(protocol):
        # Drain stream events from the protocol's event queue.
        while True:
            evt = await protocol._event_queue.get()
            if isinstance(evt, StreamDataReceived):
                if evt.data:
                    received["bytes"] += len(evt.data)
                    received["crc"] = zlib.crc32(
                        bytes(evt.data), received["crc"]
                    ) & 0xFFFFFFFF
                if evt.end_stream:
                    received["fin"] = True
                    fin_event.set()
                    return

    server = await serve(
        host="127.0.0.1", port=port,
        configuration=cfg,
        stream_handler=handle_protocol,
    )

    try:
        client = _spawn_peer([
            "connect", "--host", "127.0.0.1", "--port", str(port),
            "--alpn", ALPN, "--insecure",
            "--send-bytes", str(payload_bytes),
            "--object-size", "4096",
        ])
        try:
            summary = _read_summary(client, timeout=30.0)
            assert summary["ok"], summary
        finally:
            client.wait(timeout=5)

        # Wait for FIN delivery on the aiopquic side too.
        await asyncio.wait_for(fin_event.wait(), timeout=5)

        assert received["bytes"] == summary["stream_bytes_sent"], (
            f"aiopquic received {received['bytes']} bytes; "
            f"qh3 sent {summary['stream_bytes_sent']}"
        )
        assert received["crc"] == summary["crc32_sent"], (
            f"crc mismatch: aiopquic=0x{received['crc']:08x} "
            f"qh3=0x{summary['crc32_sent']:08x}"
        )
    finally:
        server.close()
