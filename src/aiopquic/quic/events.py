"""QUIC event classes — matches qh3.quic.events API."""

from dataclasses import dataclass


@dataclass
class QuicEvent:
    """Base class for QUIC events."""
    pass


@dataclass
class HandshakeCompleted(QuicEvent):
    alpn_protocol: str | None = None
    early_data_accepted: bool = False
    session_resumed: bool = False


@dataclass
class ConnectionTerminated(QuicEvent):
    error_code: int = 0
    frame_type: int | None = None
    reason_phrase: str = ""


@dataclass
class ProtocolNegotiated(QuicEvent):
    alpn_protocol: str | None = None


@dataclass
class StreamDataReceived(QuicEvent):
    stream_id: int = 0
    data: bytes = b""
    end_stream: bool = False


@dataclass
class StreamReset(QuicEvent):
    stream_id: int = 0
    error_code: int = 0


@dataclass
class StopSendingReceived(QuicEvent):
    stream_id: int = 0
    error_code: int = 0


@dataclass
class DatagramFrameReceived(QuicEvent):
    data: bytes = b""


@dataclass
class ConnectionIdIssued(QuicEvent):
    connection_id: bytes = b""


@dataclass
class ConnectionIdRetired(QuicEvent):
    connection_id: bytes = b""


@dataclass
class PingAcknowledged(QuicEvent):
    uid: int = 0
