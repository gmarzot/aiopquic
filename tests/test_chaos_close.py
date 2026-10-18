"""Chaos-close stress: seeded random interleavings of stream data, FIN,
RESET_STREAM, STOP_SENDING and connection close from both ends, over raw
QUIC and WebTransport (tests/chaos_close_scenarios.py). Each mode runs in a
fresh process under MALLOC_PERTURB_, so a touch of freed memory faults and
process-wide counters start at zero.

AIOPQUIC_CHAOS_SECONDS sets the run time per mode (default 3) for soaks;
AIOPQUIC_CHAOS_SEED replays a failing run.
"""
import os
import random
import subprocess
import sys

import pytest

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
SCRIPT = os.path.join(os.path.dirname(__file__), "chaos_close_scenarios.py")

SECONDS = float(os.environ.get("AIOPQUIC_CHAOS_SECONDS", "3"))
SEED = int(os.environ.get("AIOPQUIC_CHAOS_SEED")
           or random.SystemRandom().randrange(1 << 32))

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


@pytest.mark.parametrize("mode", ["quic", "wt"])
def test_chaos_close(mode):
    """No crash, no hang, and every stream context and chunk released."""
    replay = (f"seed={SEED}: AIOPQUIC_CHAOS_SEED={SEED} "
              f"AIOPQUIC_CHAOS_SECONDS={SECONDS:g} python -m pytest "
              f"'tests/test_chaos_close.py::test_chaos_close[{mode}]'")
    cmd = [sys.executable, SCRIPT, mode, str(next_port()), CERT_FILE,
           KEY_FILE, str(SEED), str(SECONDS)]
    try:
        proc = subprocess.run(
            cmd, env=dict(os.environ, MALLOC_PERTURB_="165"),
            capture_output=True, text=True, timeout=SECONDS * 3 + 120)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"hung; {replay}\n{(exc.stderr or b'')[-3000:]!r}")
    assert proc.returncode == 0, (
        f"exit {proc.returncode}; {replay}\n{proc.stderr[-3000:]}")
    print(f"{mode} seed={SEED} {proc.stdout.strip()}")
