"""A WebTransport session struct outlives picoquic's references to it.

The GC may clear a WebTransportSessionState before deallocating it. Its
struct is still named by the live connection, whose close callback walks
it when stop() deletes the connection. The scenario runs in a fresh
process (tests/wt_gc_clear_scenario.py), so a fault kills only the child.
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
def test_gc_cleared_server_session_survives_stop():
    script = os.path.join(os.path.dirname(__file__), "wt_gc_clear_scenario.py")
    proc = subprocess.run(
        [sys.executable, script, str(next_port()), CERT_FILE, KEY_FILE],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")
