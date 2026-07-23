"""Single-port dual-stack serving: raw QUIC and h3/WebTransport on one
UDP port, routed per connection by negotiated ALPN.

One TransportContext advertises the ALPN union ("h3" + the caller's raw
list); the C-side default callback (aiopquic_dispatch_cb) re-points each
connection to its stack at handshake time. On the asyncio side a single
eventfd reader drains the shared SPSC ring and routes each event to the
raw QuicEngine or the WebTransport dispatcher by the connection's
negotiated ALPN — queried once per connection through the transport
accessor (the same one QuicConnection uses at READY) and cached until
the connection closes.
"""

import asyncio

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicEngine, _EVT_CLOSE, _EVT_APP_CLOSE
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.webtransport import serve_webtransport


class _DualRouter:
    """Owns the shared transport's eventfd reader and routes each
    drained event to the raw engine or the WT dispatcher."""

    def __init__(self, loop, transport, engine, wt_dispatcher):
        self._transport = transport
        self._engine = engine
        self._wt = wt_dispatcher
        self._is_h3: dict[int, bool] = {}
        loop.add_reader(transport.eventfd, self._drain)

    def _drain(self) -> None:
        for ev in self._transport.drain_rx():
            cnx_ptr = ev[5]
            if cnx_ptr == 0:
                # Transport-level / session-keyed bookkeeping — only the
                # WT dispatcher consumes these (the engine ignores them).
                self._wt.route_event(ev)
                continue
            is_h3 = self._is_h3.get(cnx_ptr)
            if is_h3 is None:
                alpn = self._transport.get_negotiated_alpn(cnx_ptr)
                if not alpn:
                    continue   # cnx already gone — drop, mirroring close
                is_h3 = alpn == "h3"
                self._is_h3[cnx_ptr] = is_h3
            (self._wt if is_h3 else self._engine).route_event(ev)
            if ev[0] in (_EVT_CLOSE, _EVT_APP_CLOSE):
                self._is_h3.pop(cnx_ptr, None)


class DualStackServer:
    """Handle returned by serve_dispatch. close() tears down both
    stacks; the shared transport is stopped by the engine."""

    def __init__(self, engine, wt_server, loop, transport):
        self._engine = engine
        self._wt_server = wt_server
        self._loop = loop
        self._transport = transport

    def close(self) -> None:
        try:
            self._loop.remove_reader(self._transport.eventfd)
        except Exception:
            pass
        self._wt_server.close()   # detaches dispatcher; transport not owned
        self._engine.close()      # stops the shared transport


async def serve_dispatch(host: str, port: int, *,
                         configuration: QuicConfiguration,
                         create_protocol=None,
                         wt_path: str, wt_handler,
                         wt_session_factory=None,
                         wt_supported_protocols=None) -> DualStackServer:
    """Serve raw QUIC and h3/WebTransport on one UDP port.

    configuration.alpn_protocols is the RAW ALPN list (e.g.
    ["moqt-18", "moqt-16", "moq-00"]); "h3" is added to the advertised
    union here. Raw connections get create_protocol per connection
    (same contract as aiopquic.asyncio.serve); h3 connections run the
    WebTransport stack — wt_handler(session) per accepted CONNECT at
    wt_path, wt_session_factory as in serve_webtransport.
    """
    cfg = configuration
    if create_protocol is None:
        create_protocol = (
            lambda conn, stream_handler=None: QuicConnectionProtocol(
                conn, stream_handler=stream_handler))

    engine = QuicEngine(configuration=cfg, create_protocol=create_protocol)
    engine._start_transport(
        port=port,
        alpn=None,
        alpn_list=["h3"] + list(cfg.alpn_protocols or []),
        dual=True,
        wt_path=wt_path,
        wt_supported_protocols=(", ".join(wt_supported_protocols)
                                if wt_supported_protocols else None),
        max_datagram_frame_size=(cfg.max_datagram_frame_size or 64 * 1024),
    )
    transport = engine._transport

    wt_server = await serve_webtransport(
        host, port, wt_path,
        handler=wt_handler,
        cert_file=cfg.certificate_file,
        key_file=cfg.private_key_file,
        transport=transport,
        session_factory=wt_session_factory,
        configuration=cfg,
    )

    loop = asyncio.get_event_loop()
    _DualRouter(loop, transport, engine, wt_server._dispatcher)
    return DualStackServer(engine, wt_server, loop, transport)
