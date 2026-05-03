"""Shared interop fixtures: cert paths, port allocator, payload generator."""
import os
import socket
import zlib
from contextlib import closing

import pytest


CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")


@pytest.fixture(scope="session")
def cert_paths():
    if not os.path.exists(CERT_FILE):
        pytest.skip(f"cert not found at {CERT_FILE}")
    return {"cert": CERT_FILE, "key": KEY_FILE, "ca": CA_FILE}


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port():
    return _free_port()


def counted_pad(size: int) -> bytes:
    """Deterministic payload: byte i = i & 0xFF. Lets receivers verify
    byte-position alignment from any 256-byte window."""
    return bytes(i & 0xFF for i in range(size))


def crc32(data: bytes, seed: int = 0) -> int:
    return zlib.crc32(data, seed) & 0xFFFFFFFF
