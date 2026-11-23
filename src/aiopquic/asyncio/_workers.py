"""Several picoquic contexts (workers) serving one port."""
import logging
import os
import sys

_log = logging.getLogger("aiopquic")


def worker_count(workers: int, port: int) -> int:
    """Validated worker count: more than one needs Linux and a fixed port."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if workers > 1 and not sys.platform.startswith("linux"):
        _log.warning("workers=%d needs Linux SO_REUSEPORT load balancing; "
                     "using 1", workers)
        return 1
    if workers > 1 and port == 0:
        raise ValueError("workers > 1 needs a fixed port")
    return workers


def shared_port_args(workers: int) -> dict:
    """start() arguments that let several workers share one port."""
    if workers == 1:
        return {}
    return dict(is_port_shared=True, reset_seed=os.urandom(16),
                ticket_key=os.urandom(32))
