"""Version-negotiation loopback tests — raw-QUIC ALPN and WebTransport
WT-Protocol, positive and negative cases.

Raw QUIC: a multi-ALPN client offers an ordered list (ClientHello),
a multi-ALPN server selects the highest mutual; no overlap fails the
handshake promptly (not a 30s idle hang).

WebTransport: a client offers WT-Available-Protocols, the server picks
one into the WT-Protocol response; both ends read it back. No overlap
still establishes the session, with negotiated_protocol == None.
"""
import asyncio
import os
import pytest

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection
from aiopquic.quic.events import HandshakeCompleted
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve
from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)

_port_counter = 38600


def next_port():
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_cfg(alpn_protocols):
    cfg = QuicConfiguration(is_client=False, alpn_protocols=alpn_protocols)
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_cfg(alpn_protocols):
    return QuicConfiguration(is_client=True, alpn_protocols=alpn_protocols)


# ===================================================================
# Raw-QUIC ALPN negotiation
# ===================================================================

async def _client_negotiated_alpn(port, client_alpns, *, timeout=5.0):
    """Connect a raw-QUIC client offering client_alpns; return the ALPN
    reported by its HandshakeCompleted event. Raises if the handshake
    fails (e.g. no mutual ALPN)."""
    events = []

    class _Proto(QuicConnectionProtocol):
        def quic_event_received(self, event):
            events.append(event)

    async def _go():
        async with connect(
            "127.0.0.1", port,
            configuration=_client_cfg(client_alpns),
            create_protocol=lambda quic, **kw: _Proto(quic, **kw),
        ):
            for e in events:
                if isinstance(e, HandshakeCompleted):
                    return e.alpn_protocol
            return None

    return await asyncio.wait_for(_go(), timeout=timeout)


@pytest.mark.asyncio
async def test_rawquic_multi_x_multi_picks_highest_mutual():
    """Client offers [18,16,14], server allows [16,14] -> 16 (server
    does not support 18, so the newest mutual is moqt-16)."""
    port = next_port()
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_cfg(["moqt-16", "moqt-14"]))
    try:
        alpn = await _client_negotiated_alpn(
            port, ["moqt-18", "moqt-16", "moqt-14"])
        assert alpn == "moqt-16"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_rawquic_multi_client_x_single_server():
    """Multi-ALPN client against a single-ALPN server (default_alpn
    path, no select_fn) still negotiates the server's one protocol."""
    port = next_port()
    server = await serve(
        "127.0.0.1", port, configuration=_server_cfg(["moqt-16"]))
    try:
        alpn = await _client_negotiated_alpn(port, ["moqt-18", "moqt-16"])
        assert alpn == "moqt-16"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_rawquic_single_x_single_baseline():
    """Single-ALPN both ends — the unchanged default path still works."""
    port = next_port()
    server = await serve(
        "127.0.0.1", port, configuration=_server_cfg(["moqt-16"]))
    try:
        alpn = await _client_negotiated_alpn(port, ["moqt-16"])
        assert alpn == "moqt-16"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_rawquic_no_mutual_alpn_fails_fast():
    """No overlap: multi-ALPN server [16,14] rejects a client offering
    only [18]. wait_connected() must raise promptly (the select_fn
    returns 'reject' -> WRONG_ALPN), not hang until the idle timeout —
    wait_for(5s) would surface a regression as TimeoutError, which is
    NOT a ConnectionError, so the test would fail."""
    port = next_port()
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_cfg(["moqt-16", "moqt-14"]))
    try:
        with pytest.raises(ConnectionError):
            await _client_negotiated_alpn(port, ["moqt-18"], timeout=5.0)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_rawquic_single_x_single_no_match_fails_fast():
    """No overlap on the single-ALPN path (default_alpn mismatch) also
    fails promptly rather than hanging."""
    port = next_port()
    server = await serve(
        "127.0.0.1", port, configuration=_server_cfg(["moqt-16"]))
    try:
        with pytest.raises(ConnectionError):
            await _client_negotiated_alpn(port, ["moqt-18"], timeout=5.0)
    finally:
        server.close()


# ===================================================================
# WebTransport WT-Protocol negotiation
# ===================================================================

@pytest.mark.asyncio
async def test_wt_protocol_match_both_ends():
    """Server allows [16,14], client offers [18,16] -> both ends read
    back the selected 'moqt-16'."""
    port = next_port()
    seen = {}

    async def handler(session):
        seen["server"] = session.negotiated_protocol

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE,
        wt_supported_protocols=["moqt-16", "moqt-14"])
    try:
        async with connect_webtransport(
                "127.0.0.1", port, "/wt",
                wt_available_protocols=["moqt-18", "moqt-16"]) as wt:
            assert wt.session_ready
            assert wt.negotiated_protocol == "moqt-16"
            await asyncio.sleep(0.15)  # let the server handler run
        assert seen.get("server") == "moqt-16"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_protocol_no_match_session_still_opens():
    """No overlap (server [16], client offers [18]): the CONNECT still
    succeeds (WT-Protocol is optional), with negotiated_protocol None on
    both ends."""
    port = next_port()
    seen = {}

    async def handler(session):
        seen["server"] = session.negotiated_protocol

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE,
        wt_supported_protocols=["moqt-16"])
    try:
        async with connect_webtransport(
                "127.0.0.1", port, "/wt",
                wt_available_protocols=["moqt-18"]) as wt:
            assert wt.session_ready
            assert wt.negotiated_protocol is None
            await asyncio.sleep(0.15)
        assert seen.get("server") is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_no_allowlist_no_negotiation():
    """Server with no allowlist never selects a protocol even when the
    client offers some — negotiated_protocol stays None."""
    port = next_port()

    async def handler(session):
        pass

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=handler, cert_file=CERT_FILE, key_file=KEY_FILE)
    try:
        async with connect_webtransport(
                "127.0.0.1", port, "/wt",
                wt_available_protocols=["moqt-18", "moqt-16"]) as wt:
            assert wt.session_ready
            assert wt.negotiated_protocol is None
    finally:
        server.close()
