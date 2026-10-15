"""A WebTransport session released while its connection lives.

Its stream prefix must leave h3zero with it: the connection's close walks
the prefixes, and a late stream on the session dispatches through one.
Each mode runs in a fresh process (tests/wt_prefix_scenarios.py), so a
fault kills only the child.
"""
import os
import subprocess
import sys

import pytest

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port


@pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found")
@pytest.mark.parametrize(
    "mode", ["server-close", "server-stop", "client", "stale-close"])
def test_released_session_leaves_no_callback(mode):
    script = os.path.join(os.path.dirname(__file__), "wt_prefix_scenarios.py")
    proc = subprocess.run(
        [sys.executable, script, mode, str(next_port()), CERT_FILE, KEY_FILE],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")
    assert "ok" in proc.stdout
