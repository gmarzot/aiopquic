"""QUIC event classes.

`StreamDataReceived.data` and `DatagramFrameReceived.data` are memoryview
objects backed by aiopquic's internal StreamChunk — the buffer is the same
memory written by the picoquic callback's mandatory copy-out, with no
further copy on the way to Python. Consumers parse via memoryview slicing.
If a real `bytes` is needed, call `bytes(event.data)` at the call site.
"""

from dataclasses import dataclass, field


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
    data: memoryview = field(default_factory=lambda: memoryview(b""))
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
    data: memoryview = field(default_factory=lambda: memoryview(b""))


@dataclass
class ConnectionIdIssued(QuicEvent):
    connection_id: bytes = b""


@dataclass
class ConnectionIdRetired(QuicEvent):
    connection_id: bytes = b""


@dataclass
class PingAcknowledged(QuicEvent):
    uid: int = 0
