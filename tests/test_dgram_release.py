"""A datagram ring outlives every MARK queued for it.

A MARK names its ring without holding a reference. A release that freed the
ring while one was queued let the worker register freed memory, which the
connection's last close callback then freed again. Each scenario runs in a
fresh process under MALLOC_PERTURB_, so a touch of freed memory faults
(tests/dgram_release_scenarios.py).
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

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


def _run(mode):
    script = os.path.join(os.path.dirname(__file__),
                          "dgram_release_scenarios.py")
    return subprocess.run(
        [sys.executable, script, mode, str(next_port()), CERT_FILE, KEY_FILE],
        env=dict(os.environ, MALLOC_PERTURB_="165"),
        capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("mode", ["raw-queued", "wt-queued"])
def test_release_is_ordered_behind_a_queued_mark(mode):
    """The worker sees the MARK while the ring is alive and drops the
    released reference after it: no table entry or session reference to
    freed memory, and the ring freed exactly once."""
    proc = _run(mode)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


@pytest.mark.parametrize("mode", ["raw-peer-close", "wt-peer-close"])
def test_datagram_sender_survives_peer_close(mode):
    """Datagrams queued while the session or connection closes leave no
    freed ring behind and no ring alive."""
    proc = _run(mode)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")
