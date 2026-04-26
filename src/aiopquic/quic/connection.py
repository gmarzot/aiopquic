"""QUIC connection — matches qh3.quic.connection API.

Wraps TransportContext (Cython/picoquic) and translates SPSC ring
events into QuicEvent objects that match qh3's event interface.
"""

from enum import IntEnum

from .configuration import QuicConfiguration
from .events import (
    QuicEvent, HandshakeCompleted, ConnectionTerminated,
    ProtocolNegotiated, StreamDataReceived, StreamReset,
    StopSendingReceived, DatagramFrameReceived,
)
from aiopquic._binding._transport import TransportContext


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
    """

    def __init__(self, *, configuration: QuicConfiguration):
        self._configuration = configuration
        self._transport: TransportContext | None = None
        self._cnx_ptr: int = 0
        self._events: list[QuicEvent] = []
        self._connected = False
        self._closed = False
        self._next_bidi_id: int = 0 if configuration.is_client else 1
        self._next_uni_id: int = 2 if configuration.is_client else 3
        # H3Connection compatibility
        self._quic_logger = None
        self._remote_max_datagram_frame_size = (
            configuration.max_datagram_frame_size
        )

    @property
    def configuration(self) -> QuicConfiguration:
        return self._configuration

    def _start_transport(self, port: int = 0) -> None:
        """Create and start the TransportContext."""
        if self._transport is not None:
            return
        cfg = self._configuration
        self._transport = TransportContext()
        self._transport.start(
            port=port,
            cert_file=cfg.certificate_file,
            key_file=cfg.private_key_file,
            alpn=(cfg.alpn_protocols[0] if cfg.alpn_protocols else None),
            is_client=cfg.is_client,
            idle_timeout_ms=int(cfg.idle_timeout * 1000),
            max_datagram_frame_size=(cfg.max_datagram_frame_size or 0),
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
        self._drain_and_convert()
        if self._events:
            return self._events.pop(0)
        return None

    def send_stream_data(self, stream_id: int, data: bytes,
                         end_stream: bool = False) -> None:
        """Send data on a stream."""
        if not end_stream:
            self._transport.push_tx(
                _TX_STREAM_DATA, stream_id,
                data=data, cnx_ptr=self._cnx_ptr,
            )
        else:
            self._transport.push_tx(
                _TX_STREAM_FIN, stream_id,
                data=data, cnx_ptr=self._cnx_ptr,
            )
        self._transport.wake_up()

    def send_datagram_frame(self, data: bytes) -> None:
        """Send a datagram frame."""
        self._transport.push_tx(
            _TX_DATAGRAM, 0,
            data=data, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

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

    def stop(self) -> None:
        """Stop the transport entirely."""
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
