"""Worker-banded UDP port allocation for the test suite.

Each test module used to keep its own module-global port counter.
Under pytest-xdist every worker process imports the module afresh, so
all workers minted the same ports; two servers then co-bind one UDP
port and a long-running test receives other tests' traffic (seen as
"received N of M bytes" with N >> M, plus shuffling close/handshake
failures). One process-wide counter inside a per-worker band keeps
ports disjoint across workers and unique within one.
"""
import itertools
import os

_w = os.environ.get("PYTEST_XDIST_WORKER", "")
_band = int(_w[2:]) if _w.startswith("gw") and _w[2:].isdigit() else 0
# 500 ports per worker; band 0 serves serial runs. Bands 0-24 stay
# below the Linux ephemeral range (32768+), so client sockets can't
# squat on server ports at common worker counts.
_ports = itertools.count(20000 + 500 * _band)


def next_port() -> int:
    return next(_ports)
