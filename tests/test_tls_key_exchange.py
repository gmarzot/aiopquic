"""The TLS key-exchange order a client offers.

picotls sends one key share, for the first group in the order. The
classic-first default keeps the ClientHello in one packet and lets a
server without hybrid support or HelloRetryRequest complete the
handshake; putting the hybrid first sends the post-quantum share.
"""
import asyncio
import glob
import json
import os
import re

import pytest

from aiopquic._binding._transport import (
    KEX_X25519, KEX_X25519MLKEM768, TransportContext,
)
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.versions import _openssl_info

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.path.exists(CA_FILE),
                       reason="picoquic certs not found"),
]


def _first_flight_crypto_bytes(qlog_dir) -> int:
    """CRYPTO bytes the client sent before its first received packet."""
    [path] = glob.glob(os.path.join(qlog_dir, "*.client.qlog"))
    with open(path) as f:
        text = f.read()
    # picoquic writes this transport parameter without its opening brace.
    text = re.sub(r'"version_negotiation": ("chosen": [^}]*})',
                  r'"version_negotiation": {\1', text)
    total = 0
    for _t, _cat, name, data in json.loads(text)["traces"][0]["events"]:
        if name == "packet_received":
            break
        if name == "packet_sent":
            total += sum(fr.get("length", 0) for fr in data.get("frames", [])
                         if fr.get("frame_type") == "crypto")
    return total


async def _handshake(qlog_dir, **client_kw) -> tuple[bool, int]:
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg)
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                 server_name=SNI, cafile=CA_FILE,
                                 qlog_dir=str(qlog_dir), **client_kw)
        async with asyncio.timeout(5):
            async with connect("127.0.0.1", port, configuration=ccfg) as c:
                await asyncio.sleep(0.2)
                connected = c._quic._connected
        await asyncio.sleep(0.2)
    finally:
        server.close()
    return connected, _first_flight_crypto_bytes(qlog_dir)


def _mlkem_available() -> bool:
    """ML-KEM groups need OpenSSL 3.5 in the libcrypto the binding links."""
    info = _openssl_info()
    if not info:
        return False
    digits = info[0].split()[1].split(".")[:2]
    return tuple(int(d) for d in digits) >= (3, 5)


def _restore_default_order() -> None:
    ctx = TransportContext()
    ctx.start(port=0, alpn=ALPN, is_client=True, key_exchange_groups=None)
    ctx.stop()


async def test_default_sends_a_classic_key_share(tmp_path):
    connected, crypto_bytes = await _handshake(tmp_path)
    assert connected
    assert crypto_bytes < 600, crypto_bytes


@pytest.mark.skipif(not _mlkem_available(),
                    reason="ML-KEM needs OpenSSL 3.5")
async def test_hybrid_first_sends_the_post_quantum_share(tmp_path):
    try:
        connected, crypto_bytes = await _handshake(
            tmp_path, key_exchange_groups=[KEX_X25519MLKEM768, KEX_X25519])
    finally:
        _restore_default_order()
    assert connected
    assert crypto_bytes > 1200, crypto_bytes
