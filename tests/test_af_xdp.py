"""use_af_xdp asks picoquic's network thread for AF_XDP copy-mode TX.
Where AF_XDP can't open (no CAP_NET_ADMIN or BPF, no usable interface)
the thread sends by sendmsg and says why; data still flows either way."""
import asyncio
import os
import time

import pytest

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
ALPN = "hq-interop"
AF_XDP = "AF_XDP copy (GSO as TX batch)"

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.path.exists(CERT_FILE),
                       reason="picoquic certs not found"),
]


def _started(**kw):
    t = TransportContext()
    t.start(port=next_port(), cert_file=CERT_FILE, key_file=KEY_FILE,
            is_client=False, alpn=ALPN, **kw)
    deadline = time.monotonic() + 2.0
    while not t.thread_ready and time.monotonic() < deadline:
        time.sleep(0.005)
    assert t.thread_ready
    return t


@pytest.mark.parametrize("env, arg, requested", [
    (None, None, False), (None, False, False), ("1", None, True),
    ("1", False, False), ("0", True, True)])
async def test_the_argument_wins_over_the_environment(monkeypatch, env, arg,
                                                      requested):
    if env is None:
        monkeypatch.delenv("AIOPQUIC_AF_XDP", raising=False)
    else:
        monkeypatch.setenv("AIOPQUIC_AF_XDP", env)
    t = _started(use_af_xdp=arg)
    try:
        method, reason = t.tx_method
        if requested:
            assert method == AF_XDP or reason not in ("", "not requested")
        else:
            assert (method, reason) == ("sendmsg", "not requested")
            assert t.af_xdp_kick_failures == 0
    finally:
        t.stop()


async def test_a_connection_asking_for_af_xdp_delivers_every_byte():
    n = 256 * 1024
    port = next_port()
    scfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                             use_af_xdp=True)
    scfg.load_cert_chain(CERT_FILE, KEY_FILE)
    server = await serve("127.0.0.1", port, configuration=scfg)
    try:
        ccfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                                 server_name=SNI, cafile=CA_FILE,
                                 use_af_xdp=True)
        async with connect("127.0.0.1", port, configuration=ccfg) as client:
            quic = client._quic
            method, reason = quic._transport.tx_method
            assert method == AF_XDP or reason not in ("", "not requested")
            sid = quic.get_next_available_stream_id()
            quic.send_stream_data(sid, b"x" * n)
            deadline = time.monotonic() + 5.0
            while quic.tx_sent_bytes(sid) < n and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert quic.tx_sent_bytes(sid) == n
    finally:
        server.close()
