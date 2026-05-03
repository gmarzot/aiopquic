"""QUIC connection — matches qh3.quic.connection API.

Wraps TransportContext (Cython/picoquic) and translates SPSC ring
events into QuicEvent objects that match qh3's event interface.
"""

import os
from enum import IntEnum

from .configuration import QuicConfiguration
from .events import (
    QuicEvent, HandshakeCompleted, ConnectionTerminated,
    ProtocolNegotiated, StreamDataReceived, StreamReset,
    StopSendingReceived, DatagramFrameReceived,
)
from aiopquic._binding._transport import (
    TransportContext,
    stream_buf_create, stream_buf_destroy,
    stream_buf_push, stream_buf_used, stream_buf_free, stream_buf_set_fin,
    stream_buf_stats,
)


# Per-stream send-ring capacity for the PULL-model send path.
# 1 MiB gives ~5-10ms of in-flight data at 1-2 Gbps which is enough
# pipelining headroom without unbounded queueing. Power of two required.
_STREAM_RING_CAP = 1 << 20

# SPSC event type constants (must match spsc_ring.h)
_EVT_STREAM_DATA = 0
_EVT_STREAM_FIN = 1
_EVT_STREAM_RESET = 2
_EVT_STOP_SENDING = 3
_EVT_CLOSE = 4
_EVT_APP_CLOSE = 5
_EVT_READY = 6
_EVT_ALMOST_READY = 7
_EVT_DATAGRAM = 8

# TX event types
_TX_STREAM_DATA = 128
_TX_STREAM_FIN = 129
_TX_DATAGRAM = 130
_TX_CLOSE = 131
_TX_STREAM_RESET = 132
_TX_STOP_SENDING = 133
_TX_MARK_ACTIVE = 134
_TX_CONNECT = 135


class QuicErrorCode(IntEnum):
    NO_ERROR = 0x0
    INTERNAL_ERROR = 0x1
    CONNECTION_REFUSED = 0x2
    FLOW_CONTROL_ERROR = 0x3
    STREAM_LIMIT_ERROR = 0x4
    STREAM_STATE_ERROR = 0x5
    FINAL_SIZE_ERROR = 0x6
    FRAME_ENCODING_ERROR = 0x7
    TRANSPORT_PARAMETER_ERROR = 0x8
    CONNECTION_ID_LIMIT_ERROR = 0x9
    PROTOCOL_VIOLATION = 0xA
    INVALID_TOKEN = 0xB
    APPLICATION_ERROR = 0xC
    CRYPTO_BUFFER_EXCEEDED = 0xD
    KEY_UPDATE_ERROR = 0xE
    AEAD_LIMIT_REACHED = 0xF
    VERSION_NEGOTIATION_ERROR = 0x11
    CRYPTO_ERROR = 0x100


def stream_is_unidirectional(stream_id: int) -> bool:
    """Returns True if the stream is unidirectional (bit 1 set)."""
    return bool(stream_id & 2)


class QuicConnection:
    """QUIC connection wrapping picoquic via TransportContext.

    Presents the same public API as qh3.quic.connection.QuicConnection
    so that higher layers (H3Connection, aiomoqt) can use it unchanged.

    Two construction modes:
    - Client / standalone: QuicConnection(configuration=cfg). Owns its
      own TransportContext; calls _start_transport() then connect().
    - Engine-spawned (server side): QuicConnection(configuration=cfg,
      engine=engine, cnx_ptr=cnx). Shares engine's transport; cnx_ptr
      is the picoquic cnx pointer for this peer. Engine routes events
      to this connection's _events queue; no own drain.
    """

    def __init__(self, *, configuration: QuicConfiguration,
                 engine: "QuicEngine | None" = None,
                 cnx_ptr: int = 0):
        self._configuration = configuration
        self._engine = engine
        if engine is not None:
            # Engine-spawned: share engine's transport, no own drain
            self._transport = engine._transport
            self._cnx_ptr = cnx_ptr
            self._connected = True
            self._closed = False
        else:
            self._transport: TransportContext | None = None
            self._cnx_ptr = 0
            self._connected = False
            self._closed = False
        self._events: list[QuicEvent] = []
        self._next_bidi_id: int = 0 if configuration.is_client else 1
        self._next_uni_id: int = 2 if configuration.is_client else 3
        # H3Connection compatibility
        self._quic_logger = None
        self._remote_max_datagram_frame_size = (
            configuration.max_datagram_frame_size
        )
        # PULL-model send path: per-stream byte rings. Lazy-created on
        # first send_stream_data; destroyed in close(). picoquic pulls
        # from these via prepare_to_send + stream_ctx.
        self._stream_bufs: dict[int, int] = {}

    @property
    def configuration(self) -> QuicConfiguration:
        return self._configuration

    def _start_transport(self, port: int = 0) -> None:
        """Create and start the TransportContext."""
        if self._transport is not None:
            return
        cfg = self._configuration
        self._transport = TransportContext()
        # SSLKEYLOGFILE env var as a fallback when configuration didn't
        # set secrets_log_file explicitly — matches the convention used
        # by curl, openssl s_client, and Chromium-based tooling.
        keylog = cfg.secrets_log_file or os.environ.get('SSLKEYLOGFILE')
        self._transport.start(
            port=port,
            cert_file=cfg.certificate_file,
            key_file=cfg.private_key_file,
            alpn=(cfg.alpn_protocols[0] if cfg.alpn_protocols else None),
            is_client=cfg.is_client,
            idle_timeout_ms=int(cfg.idle_timeout * 1000),
            max_datagram_frame_size=(cfg.max_datagram_frame_size or 0),
            keylog_filename=keylog,
        )

    def connect(self, addr: tuple[str, int], now: float = 0.0) -> None:
        """Initiate a client connection to the given address."""
        if self._transport is None:
            self._start_transport(port=0)
        cfg = self._configuration
        host, port = addr
        self._transport.create_client_connection(
            host, port,
            sni=cfg.server_name or host,
            alpn=(cfg.alpn_protocols[0] if cfg.alpn_protocols else None),
        )

    @property
    def eventfd(self) -> int:
        """File descriptor for asyncio add_reader() registration."""
        if self._transport is None:
            return -1
        return self._transport.eventfd

    @property
    def bytes_sent(self) -> int:
        """Cumulative bytes this cnx has placed on the wire (picoquic-
        accounting). Differs from bytes-queued: send_stream_data only
        appends to picoquic's per-stream send buffer; bytes_sent is
        the on-wire count after cwnd/pacing has done its work."""
        from aiopquic._binding._transport import cnx_data_sent
        return cnx_data_sent(self._cnx_ptr)

    @property
    def bytes_received(self) -> int:
        """Cumulative bytes this cnx has received from the wire."""
        from aiopquic._binding._transport import cnx_data_received
        return cnx_data_received(self._cnx_ptr)

    def _drain_and_convert(self) -> None:
        """Drain SPSC ring and convert to QuicEvent objects."""
        if self._transport is None:
            return
        raw_events = self._transport.drain_rx()
        for (evt_type, stream_id, data, is_fin, error_code,
             cnx_ptr, _stream_ctx_ptr) in raw_events:
            if evt_type == _EVT_STREAM_DATA:
                self._events.append(StreamDataReceived(
                    stream_id=stream_id,
                    data=data if data is not None else memoryview(b""),
                    end_stream=False,
                ))
            elif evt_type == _EVT_STREAM_FIN:
                self._events.append(StreamDataReceived(
                    stream_id=stream_id,
                    data=data if data is not None else memoryview(b""),
                    end_stream=True,
                ))
            elif evt_type == _EVT_STREAM_RESET:
                self._events.append(StreamReset(
                    stream_id=stream_id,
                    error_code=error_code,
                ))
            elif evt_type == _EVT_STOP_SENDING:
                self._events.append(StopSendingReceived(
                    stream_id=stream_id,
                    error_code=error_code,
                ))
            elif evt_type == _EVT_CLOSE:
                self._closed = True
                self._events.append(ConnectionTerminated(
                    error_code=error_code,
                ))
            elif evt_type == _EVT_APP_CLOSE:
                self._closed = True
                self._events.append(ConnectionTerminated(
                    error_code=error_code,
                ))
            elif evt_type == _EVT_READY:
                if cnx_ptr != 0:
                    # Connection handshake complete
                    self._cnx_ptr = cnx_ptr
                    self._connected = True
                    alpn = (self._configuration.alpn_protocols[0]
                            if self._configuration.alpn_protocols else None)
                    self._events.append(HandshakeCompleted(
                        alpn_protocol=alpn,
                    ))
                    self._events.append(ProtocolNegotiated(
                        alpn_protocol=alpn,
                    ))
            elif evt_type == _EVT_ALMOST_READY:
                if cnx_ptr != 0:
                    self._cnx_ptr = cnx_ptr
            elif evt_type == _EVT_DATAGRAM:
                self._events.append(DatagramFrameReceived(
                    data=data if data is not None else memoryview(b""),
                ))

    def next_event(self) -> QuicEvent | None:
        """Dequeue next event from the connection."""
        if self._engine is None:
            self._drain_and_convert()
        if self._events:
            return self._events.pop(0)
        return None

    def _enqueue_raw(self, evt_type: int, stream_id: int, data,
                     is_fin: bool, error_code: int,
                     stream_ctx_ptr: int) -> None:
        """Engine-side: append a raw event tuple as a QuicEvent.

        Routes one drained SPSC entry into this connection's queue.
        Mirrors the conversion logic in _drain_and_convert. Called by
        QuicEngine after demuxing by cnx_ptr.
        """
        if evt_type == _EVT_STREAM_DATA:
            self._events.append(StreamDataReceived(
                stream_id=stream_id,
                data=data if data is not None else memoryview(b""),
                end_stream=False,
            ))
        elif evt_type == _EVT_STREAM_FIN:
            self._events.append(StreamDataReceived(
                stream_id=stream_id,
                data=data if data is not None else memoryview(b""),
                end_stream=True,
            ))
        elif evt_type == _EVT_STREAM_RESET:
            self._events.append(StreamReset(
                stream_id=stream_id, error_code=error_code,
            ))
        elif evt_type == _EVT_STOP_SENDING:
            self._events.append(StopSendingReceived(
                stream_id=stream_id, error_code=error_code,
            ))
        elif evt_type in (_EVT_CLOSE, _EVT_APP_CLOSE):
            self._closed = True
            self._events.append(ConnectionTerminated(error_code=error_code))
        elif evt_type == _EVT_READY:
            alpn = (self._configuration.alpn_protocols[0]
                    if self._configuration.alpn_protocols else None)
            self._events.append(HandshakeCompleted(alpn_protocol=alpn))
            self._events.append(ProtocolNegotiated(alpn_protocol=alpn))
        elif evt_type == _EVT_DATAGRAM:
            self._events.append(DatagramFrameReceived(
                data=data if data is not None else memoryview(b""),
            ))

    def send_stream_data(self, stream_id: int, data: bytes,
                         end_stream: bool = False) -> None:
        """Send data on a stream — PULL-model with real backpressure.

        Lazily creates a per-stream byte ring on first call; subsequent
        calls push bytes into the ring. picoquic pulls at wire rate via
        the prepare_to_send callback. Raises BufferError when the ring
        cannot fit `data` (caller must wait + retry, e.g. via
        stream_write_drain's existing BufferError loop).

        end_stream=True marks FIN to follow once the ring is fully drained.
        """
        sb = self._stream_bufs.get(stream_id)
        if sb is None:
            sb = stream_buf_create(_STREAM_RING_CAP)
            self._stream_bufs[stream_id] = sb

        if data:
            if stream_buf_free(sb) < len(data):
                raise BufferError(
                    f"per-stream send ring full "
                    f"(stream={stream_id}, "
                    f"need={len(data)}, free={stream_buf_free(sb)})"
                )
            accepted = stream_buf_push(sb, data)
            if accepted != len(data):
                # Defensive: free check passed but push didn't take all.
                # Shouldn't happen with single-producer; surface as error.
                raise BufferError(
                    f"partial push on stream={stream_id}: "
                    f"{accepted}/{len(data)} accepted"
                )

        if end_stream:
            stream_buf_set_fin(sb)

        # Mark the stream active so picoquic schedules prepare_to_send.
        # Idempotent — picoquic's mark_active_stream is fine to repeat.
        self._transport.push_tx(
            _TX_MARK_ACTIVE, stream_id,
            cnx_ptr=self._cnx_ptr, stream_ctx=sb,
        )
        self._transport.wake_up()

    def send_datagram_frame(self, data: bytes) -> None:
        """Send a datagram frame."""
        self._transport.push_tx(
            _TX_DATAGRAM, 0,
            data=data, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def get_stream_buf_stats(self, stream_id: int):
        """Return (pushed, popped, push_hash, pop_hash) for the per-stream
        byte ring. pushed and popped are cumulative byte totals; in a clean
        run they are equal at stream close. push_hash and pop_hash are FNV-1a
        accumulators (only meaningful when AIOPQUIC_TX_HASH=1 was set when
        the ring was created); equal hashes prove byte-for-byte conservation
        through the ring. Returns None if the stream has no per-stream ring.
        """
        sb = self._stream_bufs.get(stream_id)
        if sb is None:
            return None
        return stream_buf_stats(sb)

    def get_next_available_stream_id(self, is_unidirectional: bool = False) -> int:
        """Allocate the next stream ID."""
        if is_unidirectional:
            stream_id = self._next_uni_id
            self._next_uni_id += 4
        else:
            stream_id = self._next_bidi_id
            self._next_bidi_id += 4
        return stream_id

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        """Reset a stream with the given error code."""
        self._transport.push_tx(
            _TX_STREAM_RESET, stream_id,
            error_code=error_code, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def stop_stream(self, stream_id: int, error_code: int) -> None:
        """Send STOP_SENDING on a stream."""
        self._transport.push_tx(
            _TX_STOP_SENDING, stream_id,
            error_code=error_code, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def close(self, error_code: int = 0, frame_type: int | None = None,
              reason_phrase: str = "") -> None:
        """Close the connection."""
        if self._transport is not None and not self._closed:
            self._transport.push_tx(
                _TX_CLOSE, 0,
                error_code=error_code, cnx_ptr=self._cnx_ptr,
            )
            self._transport.wake_up()
            self._closed = True
        # NOTE: per-stream send rings are intentionally NOT destroyed
        # here. picoquic-pthread may still hold stream_ctx pointers to
        # them; freeing now risks use-after-free. Proper lifecycle
        # requires an explicit deactivate→free dance via picoquic
        # mark_active_stream(active=0) before destroy. Until that lands,
        # buffers are reclaimed at process exit. Acceptable for tests
        # and short-lived sessions; leaks per long-lived connection
        # otherwise — TODO before production ship.

    def stop(self) -> None:
        """Stop the transport entirely.

        For engine-spawned connections this is a no-op; the engine
        owns the transport and shuts it down on engine.close().
        """
        if self._engine is not None:
            return
        if self._transport is not None:
            self._transport.stop()
            self._transport = None


class QuicEngine:
    """Server-side multiplexer over a single picoquic engine.

    Owns the TransportContext, drains SPSC events, and routes them by
    cnx pointer to a per-connection QuicConnection. On the first READY
    event for a new cnx, calls create_protocol(connection) to spawn a
    fresh user protocol bound to that connection.

    The engine is what serve() returns to the user. Lifetime of all
    connections is bound to the engine (engine.close() tears them
    down). Used only for the server case; client connect() wraps a
    standalone QuicConnection.
    """

    def __init__(self, *, configuration: QuicConfiguration,
                 create_protocol, stream_handler=None):
        self._configuration = configuration
        self._create_protocol = create_protocol
        self._stream_handler = stream_handler
        self._transport: TransportContext | None = None
        self._connections: dict[int, QuicConnection] = {}
        self._protocols: dict[int, "object"] = {}

    def _start_transport(self, port: int = 0) -> None:
        if self._transport is not None:
            return
        cfg = self._configuration
        self._transport = TransportContext()
        keylog = cfg.secrets_log_file or os.environ.get('SSLKEYLOGFILE')
        self._transport.start(
            port=port,
            cert_file=cfg.certificate_file,
            key_file=cfg.private_key_file,
            alpn=(cfg.alpn_protocols[0] if cfg.alpn_protocols else None),
            is_client=cfg.is_client,
            idle_timeout_ms=int(cfg.idle_timeout * 1000),
            max_datagram_frame_size=(cfg.max_datagram_frame_size or 0),
            keylog_filename=keylog,
        )

    @property
    def eventfd(self) -> int:
        if self._transport is None:
            return -1
        return self._transport.eventfd

    def drain_and_route(self) -> None:
        """Drain SPSC events and route to per-cnx protocols.

        For each event:
          - cnx_ptr == 0: engine-level (transport ready); ignored here.
          - cnx_ptr unknown: first READY for a new cnx — spawn
            QuicConnection + protocol via create_protocol().
          - cnx_ptr known: enqueue raw event on that connection, then
            drive the bound protocol to drain it.
        """
        if self._transport is None:
            return
        raw_events = self._transport.drain_rx()
        for (evt_type, stream_id, data, is_fin, error_code,
             cnx_ptr, stream_ctx_ptr) in raw_events:
            if cnx_ptr == 0:
                continue  # engine-level event, not per-cnx
            conn = self._connections.get(cnx_ptr)
            if conn is None:
                conn = QuicConnection(
                    configuration=self._configuration,
                    engine=self, cnx_ptr=cnx_ptr,
                )
                self._connections[cnx_ptr] = conn
                proto = self._create_protocol(
                    conn, stream_handler=self._stream_handler,
                )
                # Engine owns the eventfd reader; bind protocol to the
                # loop without adding a second reader (which would
                # replace ours and break demux for all other cnx).
                import asyncio as _asyncio
                proto._loop = _asyncio.get_event_loop()
                self._protocols[cnx_ptr] = proto
            conn._enqueue_raw(evt_type, stream_id, data, is_fin,
                              error_code, stream_ctx_ptr)
            proto = self._protocols[cnx_ptr]
            proto._process_events()
            if evt_type in (_EVT_CLOSE, _EVT_APP_CLOSE):
                self._connections.pop(cnx_ptr, None)
                self._protocols.pop(cnx_ptr, None)

    def close(self) -> None:
        """Tear down all connections and stop the transport."""
        for proto in list(self._protocols.values()):
            try:
                proto.close()
            except Exception:
                pass
        self._connections.clear()
        self._protocols.clear()
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
