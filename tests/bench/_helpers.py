"""Bench helpers — re-exports constants/utilities from the loopback tests.

`conftest.py` is a pytest-special module that you can't `from conftest
import ...` from a regular file, so we put the shared imports here.
"""
import os
import sys

# Make tests/test_loopback.py importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_loopback import (  # noqa: E402,F401
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
    SPSC_EVT_DATAGRAM, SPSC_EVT_ALMOST_READY, SPSC_EVT_READY,
    SPSC_EVT_CLOSE, SPSC_EVT_APP_CLOSE,
    SPSC_EVT_TX_STREAM_DATA, SPSC_EVT_TX_STREAM_FIN,
    SPSC_EVT_TX_DATAGRAM,
    ALPN, CERT_FILE, KEY_FILE,
    next_port, wait_for_ready, drain_until,
    get_cnx_ptr, has_connection_ready,
    start_server, connect_client, wait_for_server_cnx,
)
