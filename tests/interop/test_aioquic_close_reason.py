"""Reason phrases cross the wire to and from aioquic: a picoquic-to-picoquic
loopback could agree on a framing defect that a different stack exposes."""
import json
import os
import subprocess
import sys

import pytest

from .conftest import CERT_FILE, KEY_FILE, _free_port

PEER = os.path.join(os.path.dirname(__file__), "peers",
                    "aioquic_close_reason.py")


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
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stderr[-3000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_aioquic_close_reason_reaches_aiopquic():
    assert _run("peer")["client"] == [[0x10, None, "aioquic-bye"]]


def test_aiopquic_close_reason_reaches_aioquic():
    assert _run("ours")["server"] == [[9, None, "aiopquic-bye"]]
