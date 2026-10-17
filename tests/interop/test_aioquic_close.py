"""Stream frames after the peer's CONNECTION_CLOSE in one packet.

picoquic decodes the frames behind a peer's close in the same packet, after
the aiopquic client queued APP_CLOSE, on which Python drops the connection's
reference to every stream context. Those stream callbacks are ignored, so
they neither read a context nor create or release one. The scenario
(peers/aioquic_close_coalesced.py) runs in a subprocess, so a native crash
fails the test rather than the run.
"""
import json
import os
import subprocess
import sys

import pytest

from .conftest import CERT_FILE, KEY_FILE, _free_port

PEER = os.path.join(os.path.dirname(__file__), "peers",
                    "aioquic_close_coalesced.py")


def _aioquic_available() -> bool:
    try:
        import aioquic  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not _aioquic_available(), reason="aioquic not installed"),
    pytest.mark.skipif(not os.path.exists(CERT_FILE),
                       reason="picoquic certs not found"),
]


def _run(mode):
    proc = subprocess.run(
        [sys.executable, PEER, mode, str(_free_port()), CERT_FILE, KEY_FILE],
        env=dict(os.environ, MALLOC_PERTURB_="165"),
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _after_close(report):
    events = report["events"]
    first = next(i for i, ev in enumerate(events) if ev[0] == "terminated")
    return [ev for ev in events[first + 1:] if ev[0] != "terminated"]


@pytest.mark.parametrize("mode", ["none", "data", "reset", "newstream", "unifin"])
def test_stream_frames_after_close_release_every_context(mode):
    report = _run(mode)
    assert ["data", 0, "hello", False] in report["events"], report
    assert report["sc_alive"] == 0 and report["chunks_alive"] == 0, report


@pytest.mark.parametrize("mode", ["data", "reset", "newstream", "unifin"])
def test_stream_frames_after_close_are_ignored(mode):
    """The frames reach the client after the close and nothing past the
    close is delivered."""
    report = _run(mode)
    assert report["stream_cb_after_close"] > 0, report
    assert _after_close(report) == [], report
