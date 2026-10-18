"""QuicConfiguration.max_connections caps the connections a server holds;
past it picoquic refuses new ones."""
import asyncio
import contextlib
import os

import pytest

from aiopquic.asyncio import connect
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from ._ports import next_port

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
WT_PATH = "/cap"
CAP = 2


def _server_cfg():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                            max_connections=CAP)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg():
    return QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                             server_name=SNI, cafile=CA_FILE, idle_timeout=3)


async def _refused(open_one) -> bool:
    try:
        async with asyncio.timeout(8):
            async with open_one():
                return False
    except Exception:
        return True


async def test_raw_quic_server_refuses_past_max_connections():
    port = next_port()
    server = await serve("127.0.0.1", port, configuration=_server_cfg())
    try:
        async with contextlib.AsyncExitStack() as held:
            for _ in range(CAP):
                await held.enter_async_context(
                    connect("127.0.0.1", port, configuration=_client_cfg()))
            assert await _refused(lambda: connect(
                "127.0.0.1", port, configuration=_client_cfg()))
        # Room again once the held connections close.
        async with asyncio.timeout(10):
            while await _refused(lambda: connect(
                    "127.0.0.1", port, configuration=_client_cfg())):
                await asyncio.sleep(0.2)
    finally:
        server.close()


async def test_webtransport_server_refuses_past_max_connections():
    port = next_port()

    async def handler(session):
        await session.wait_closed()

    server = await serve_webtransport(
        "127.0.0.1", port, WT_PATH, handler=handler,
        cert_file=CERT_FILE, key_file=KEY_FILE,
        configuration=QuicConfiguration(is_client=False,
                                        max_connections=CAP))

    def _open():
        return connect_webtransport("127.0.0.1", port, WT_PATH, sni=SNI,
                                    ca_file=CA_FILE)
    try:
        async with contextlib.AsyncExitStack() as held:
            for _ in range(CAP):
                await held.enter_async_context(_open())
            assert await _refused(_open)
    finally:
        await server.aclose()


def test_default_is_256():
    assert QuicConfiguration().max_connections == 256


@pytest.mark.parametrize("cap", [1, 4096])
def test_configurable(cap):
    assert QuicConfiguration(max_connections=cap).max_connections == cap
