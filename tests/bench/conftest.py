"""Shared bench fixtures.

Constants and helpers live in `_helpers.py` so bench modules can import
them directly (you can't `from conftest import ...` in plain modules).
"""
import os
import sys

import pytest

# Make the sibling _helpers module importable from conftest itself.
sys.path.insert(0, os.path.dirname(__file__))

from _helpers import (  # noqa: E402
    ALPN, CERT_FILE, KEY_FILE,
    next_port, wait_for_ready,
    start_server, connect_client, wait_for_server_cnx,
)
from aiopquic._binding._transport import TransportContext  # noqa: E402


@pytest.fixture
def loopback_pair():
    """A connected (server, client, client_cnx, server_cnx) tuple."""
    port = next_port()
    server = start_server(port)
    try:
        client, client_cnx = connect_client(port)
        try:
            _, server_cnx = wait_for_server_cnx(server)
            assert server_cnx != 0
            yield server, client, client_cnx, server_cnx
        finally:
            client.stop()
    finally:
        server.stop()


@pytest.fixture
def datagram_pair():
    """A connected pair with datagrams enabled on both sides."""
    port = next_port()
    server = TransportContext()
    server.start(port=port, cert_file=CERT_FILE, key_file=KEY_FILE,
                 alpn=ALPN, is_client=False, max_datagram_frame_size=1200)
    assert wait_for_ready(server)
    try:
        client, client_cnx = connect_client(port, max_datagram_frame_size=1200)
        try:
            _, server_cnx = wait_for_server_cnx(server)
            assert server_cnx != 0
            yield server, client, client_cnx, server_cnx
        finally:
            client.stop()
    finally:
        server.stop()
