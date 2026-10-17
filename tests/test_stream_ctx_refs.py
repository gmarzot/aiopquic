"""Every event that carries a stream context holds a reference to it.

The connection drops its own reference on close and on stop(); a queued
event still naming the context keeps it alive until the drain or the
transport's teardown releases the event. Each scenario runs in a fresh
process under MALLOC_PERTURB_, so a touch of freed memory faults
(tests/stream_ctx_scenarios.py).
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
    script = os.path.join(os.path.dirname(__file__), "stream_ctx_scenarios.py")
    return subprocess.run(
        [sys.executable, script, mode, str(next_port()), CERT_FILE, KEY_FILE],
        env=dict(os.environ, MALLOC_PERTURB_="165"),
        capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("mode", ["close-then-data", "close-then-data-split"])
def test_stream_events_outlive_the_close(mode):
    """APP_CLOSE drops the connection's reference; the stream events behind
    it in the ring still read the context and deliver its bytes."""
    proc = _run(mode)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


@pytest.mark.parametrize("mode", ["undrained", "tx-ring-full"])
def test_undelivered_events_return_their_reference(mode):
    """Events the worker or the drain never consumes, or that never made it
    onto the ring, leave no context behind."""
    proc = _run(mode)
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")


def test_connection_churn_releases_every_context():
    """Connections closed by either side mid-transfer, with data, FIN, reset
    and stop-sending streams, release every context and chunk."""
    proc = _run("churn")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n{proc.stderr[-3000:]}")
