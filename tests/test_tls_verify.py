"""Clients verify the server's certificate (RFC 9114 §3.1).

The test server presents third_party/picoquic/certs/cert.pem, issued to
test.example.com by the picotls test CA, which no system or certifi root
trusts.
"""
import asyncio
import os
import ssl

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    WebTransportError, connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name
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


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg(**kw):
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN], **kw)


async def _raw_connects(port, cfg) -> bool:
    try:
        async with asyncio.timeout(5):
            async with connect("127.0.0.1", port, configuration=cfg) as c:
                return c._quic._connected
    except (ConnectionError, TimeoutError):
        return False


async def _wt_connects(port, **kw) -> bool:
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt",
                                        timeout=3.0, **kw) as wt:
            return wt.session_ready
    except WebTransportError:
        return False


@pytest.fixture
async def raw_server():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    try:
        yield port
    finally:
        server.close()


@pytest.fixture
async def wt_server():
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        yield port
    finally:
        server.close()


async def test_raw_default_refuses_the_test_certificate(raw_server):
    assert not await _raw_connects(raw_server, _client_cfg(server_name=SNI))


async def test_raw_opt_out_connects(raw_server):
    assert await _raw_connects(
        raw_server, _client_cfg(server_name=SNI, verify_mode=ssl.CERT_NONE))


async def test_raw_trusted_root_connects(raw_server):
    cfg = _client_cfg(server_name=SNI)
    cfg.load_verify_locations(cafile=CA_FILE)
    assert await _raw_connects(raw_server, cfg)


async def test_raw_wrong_name_is_refused(raw_server):
    assert not await _raw_connects(
        raw_server, _client_cfg(server_name="other.example.com",
                                cafile=CA_FILE))


async def test_wt_default_refuses_the_test_certificate(wt_server):
    assert not await _wt_connects(wt_server, sni=SNI)


async def test_wt_opt_out_connects(wt_server):
    assert await _wt_connects(wt_server, sni=SNI, verify_peer=False)


async def test_wt_trusted_root_connects(wt_server):
    assert await _wt_connects(wt_server, sni=SNI, ca_file=CA_FILE)


async def test_wt_wrong_name_is_refused(wt_server):
    assert not await _wt_connects(wt_server, sni="other.example.com",
                                  ca_file=CA_FILE)


async def test_empty_root_file_raises(wt_server, tmp_path):
    """picoquic treats an empty root store as "verify nothing"; the
    binding refuses it instead."""
    empty = tmp_path / "roots.pem"
    empty.write_text("")
    with pytest.raises(ssl.SSLError):
        async with connect_webtransport("127.0.0.1", wt_server, "/wt",
                                        sni=SNI, ca_file=str(empty)):
            pass
