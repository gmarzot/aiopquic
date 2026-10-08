"""aiopquic WebTransport client ↔ aioquic H3/WebTransport server.

aioquic enforces what a picoquic peer can mask: STOP_SENDING on a stream
that is receive-only at its end closes the connection (RFC 9000 §19.5),
and it does not negotiate reset_stream_at, so a reset sent only as
RESET_STREAM_AT never arrives. The peer (peers/aioquic_wt.py) runs as a
subprocess and reports its QUIC events as JSON lines.
"""
import asyncio
import glob
import json
import os
import sys

import pytest

from aiopquic.asyncio.webtransport import WT_SESSION_GONE, connect_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from ..wt_close_scenarios import _frames_sent
from .conftest import CA_FILE, CERT_FILE, KEY_FILE, _free_port

SNI = "test.example.com"  # the test certificate's name


PEER = os.path.join(os.path.dirname(__file__), "peers", "aioquic_wt.py")


def _aioquic_available() -> bool:
    try:
        import aioquic  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _aioquic_available(), reason="aioquic not installed"),
    pytest.mark.skipif(not os.path.exists(CERT_FILE),
                       reason="picoquic certs not found"),
]


class Peer:
    """The aioquic peer subprocess and the events it has reported."""

    def __init__(self, proc, port):
        self.proc = proc
        self.port = port
        self.events = []
        self.stderr = []
        self._changed = asyncio.Event()
        self._tasks = [asyncio.create_task(self._read_events()),
                       asyncio.create_task(self._read_stderr())]

    async def _read_events(self):
        async for line in self.proc.stdout:
            self.events.append(json.loads(line))
            self._changed.set()

    async def _read_stderr(self):
        async for line in self.proc.stderr:
            self.stderr.append(line.decode(errors="replace"))

    async def wait_for(self, pred, timeout):
        """The first reported event matching pred, or None at timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._changed.clear()
            for ev in self.events:
                if pred(ev):
                    return ev
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        if self.proc.returncode is None:
            self.proc.terminate()
        await self.proc.wait()
        for t in self._tasks:
            t.cancel()


async def _start_peer(*extra_args):
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, PEER, "--port", str(port),
        "--cert", CERT_FILE, "--key", KEY_FILE, *extra_args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    lines = []
    try:
        while True:
            line = await asyncio.wait_for(proc.stderr.readline(),
                                          max(deadline - loop.time(), 0.01))
            if not line:
                raise RuntimeError("aioquic peer exited before serving")
            if b"serving on" in line:
                return Peer(proc, port)
            lines.append(line.decode(errors="replace"))
    except BaseException as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if not isinstance(exc, Exception):
            raise
        raise RuntimeError(
            f"aioquic peer did not start: {exc!r}\n{''.join(lines)}") from exc


def _data_on(sid):
    return lambda e: e["event"] == "wt_data" and e["stream_id"] == sid


def _reset_on(sid):
    return lambda e: e["event"] == "stream_reset" and e["stream_id"] == sid


async def test_wt_close_keeps_strict_peer_connection():
    """Our session teardown must leave the peer's connection up.

    The teardown resets the streams we send on and stops the ones we
    receive on. STOP_SENDING on our own uni stream would be a
    STREAM_STATE_ERROR at the peer.
    """
    peer = await _start_peer()
    try:
        async with connect_webtransport(
                "127.0.0.1", peer.port, "/wt", timeout=5.0,
                sni=SNI, ca_file=CA_FILE) as wt:
            uni = await wt.create_stream(bidir=False)
            wt.send_stream_data(uni, b"c" * 1000)
            bidi = await wt.create_stream(bidir=True)
            wt.send_stream_data(bidi, b"d" * 100)
            for sid in (uni, bidi):
                assert await peer.wait_for(_data_on(sid), 5.0), (
                    f"peer saw no data on stream {sid}")

            wt.close()

            reset = await peer.wait_for(_reset_on(uni), 5.0)
            assert reset is not None, (
                f"peer saw no teardown reset on stream {uni}: {peer.events}")
            assert reset["error_code"] == WT_SESSION_GONE
            killed = await peer.wait_for(
                lambda e: e["event"] == "terminated", 1.0)
            assert killed is None, f"peer closed the connection: {killed}"
    finally:
        await peer.stop()


async def test_wt_reset_reaches_peer_without_reset_stream_at(tmp_path):
    """A stream reset reaches a peer that did not negotiate
    reset_stream_at, where RESET_STREAM_AT is not an option. The qlog must
    show the RESET_STREAM fallback frame, so the test fails rather than
    passing without the fallback if the peer ever negotiates it."""
    peer = await _start_peer()
    try:
        async with connect_webtransport(
                "127.0.0.1", peer.port, "/wt", timeout=5.0,
                sni=SNI, ca_file=CA_FILE,
                configuration=QuicConfiguration(
                    qlog_dir=str(tmp_path))) as wt:
            uni = await wt.create_stream(bidir=False)
            wt.send_stream_data(uni, b"c" * 1000)
            assert await peer.wait_for(_data_on(uni), 5.0), (
                f"peer saw no data on stream {uni}")

            wt.reset_stream(uni, 0x10)

            reset = await peer.wait_for(_reset_on(uni), 2.0)
            assert reset is not None, (
                f"reset on stream {uni} never reached the peer: {peer.events}")
            assert reset["error_code"] == 0x10
    finally:
        await peer.stop()
    [qlog] = glob.glob(str(tmp_path / "*.client.qlog"))
    assert ("reset_stream", uni, 0x10) in _frames_sent(qlog), (
        f"no RESET_STREAM 0x10 on {uni}: {_frames_sent(qlog)}")


async def test_wt_late_peer_stream_after_close_is_stopped():
    """A peer stream that arrives after our close gets STOP_SENDING with
    WT_SESSION_GONE (§6), and the peer keeps the connection. aioquic does
    not act on WT_CLOSE_SESSION, so it opens the stream anyway."""
    peer = await _start_peer("--late-uni", "500")
    try:
        async with connect_webtransport(
                "127.0.0.1", peer.port, "/wt", timeout=5.0,
                sni=SNI, ca_file=CA_FILE) as wt:
            wt.close()

            late = await peer.wait_for(lambda e: e["event"] == "late_uni", 5.0)
            assert late is not None, f"peer opened no late stream: {peer.events}"
            sid = late["stream_id"]
            stop = await peer.wait_for(
                lambda e: (e["event"] == "stop_sending"
                           and e["stream_id"] == sid), 2.0)
            assert stop is not None, (
                f"no STOP_SENDING on late stream {sid}: {peer.events}")
            assert stop["error_code"] == WT_SESSION_GONE
            killed = await peer.wait_for(
                lambda e: e["event"] == "terminated", 0.5)
            assert killed is None, f"peer closed the connection: {killed}"
    finally:
        await peer.stop()


async def test_wt_connect_protocol_token_is_webtransport():
    """The CONNECT's :protocol stays "webtransport" even when the server
    advertises SETTINGS_WT_ENABLED, after which draft-16 wants
    "webtransport-h3": proxygen advertises the setting and refuses the new
    token with 400.
    """
    peer = await _start_peer("--wt-enabled-setting", "--strict-protocol")
    try:
        async with connect_webtransport(
                "127.0.0.1", peer.port, "/wt", timeout=5.0,
                sni=SNI, ca_file=CA_FILE) as wt:
            assert wt.session_ready
        ev = await peer.wait_for(lambda e: e["event"] == "connect", 5.0)
        assert ev is not None and ev["protocol"] == "webtransport", peer.events
    finally:
        await peer.stop()
