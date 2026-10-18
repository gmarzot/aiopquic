"""Minimal HTTP/3 (RFC 9114) GET client over aiopquic, for the runner's http3 case.

QPACK uses the static table only: SETTINGS advertise a zero-capacity dynamic table.
"""

import asyncio
import os
from urllib.parse import urlparse

import pylsqpack

from aiopquic.asyncio import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, StreamDataReceived, StreamReset,
)

ALPN = "h3"
PORT = 443
CHUNK = 64 * 1024
MAX_IN_FLIGHT = 64

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_SETTINGS = 0x4
STREAM_CONTROL = 0x00
STREAM_QPACK_ENCODER = 0x02
STREAM_QPACK_DECODER = 0x03
SETTING_QPACK_MAX_TABLE_CAPACITY = 0x01
SETTING_QPACK_BLOCKED_STREAMS = 0x07
H3_NO_ERROR = 0x100


def encode_varint(value: int) -> bytes:
    """RFC 9000 §16 variable-length integer."""
    if value < 0x40:
        return bytes([value])
    if value < 0x4000:
        return (value | 0x4000).to_bytes(2, "big")
    if value < 0x40000000:
        return (value | 0x80000000).to_bytes(4, "big")
    if value < 0x4000000000000000:
        return (value | 0xC000000000000000).to_bytes(8, "big")
    raise ValueError(f"varint out of range: {value}")


def decode_varint(buf, pos: int) -> tuple[int, int] | None:
    """(value, next position), or None if buf ends inside the varint."""
    if pos >= len(buf):
        return None
    size = 1 << (buf[pos] >> 6)
    if pos + size > len(buf):
        return None
    value = buf[pos] & 0x3F
    for i in range(1, size):
        value = (value << 8) | buf[pos + i]
    return value, pos + size


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    return encode_varint(frame_type) + encode_varint(len(payload)) + payload


def settings_frame() -> bytes:
    payload = b"".join(encode_varint(k) + encode_varint(v) for k, v in (
        (SETTING_QPACK_MAX_TABLE_CAPACITY, 0),
        (SETTING_QPACK_BLOCKED_STREAMS, 0),
    ))
    return encode_frame(FRAME_SETTINGS, payload)


async def send(quic, stream_id: int, data: bytes, fin: bool = True) -> None:
    """Write data, waiting out per-stream TX backpressure; FIN only if fin."""
    drained = quic.get_tx_data_drain_event(stream_id)
    offset = 0
    while True:
        chunk = data[offset:offset + CHUNK]
        last = offset + len(chunk) >= len(data)
        drained.clear()
        try:
            quic.send_stream_data(stream_id, chunk, end_stream=fin and last)
        except BufferError:
            # A full TX event ring arms no per-stream event; poll it out.
            try:
                await asyncio.wait_for(drained.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
            continue
        if last:
            return
        offset += len(chunk)
        await asyncio.sleep(0)


class _Response:
    """Incremental frame parser for one request stream."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.headers: bytes | None = None
        self.body = bytearray()

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            head = decode_varint(self.buf, 0)
            if head is None:
                return
            length = decode_varint(self.buf, head[1])
            if length is None:
                return
            start, end = length[1], length[1] + length[0]
            if end > len(self.buf):
                return
            if head[0] == FRAME_HEADERS and self.headers is None:
                self.headers = bytes(self.buf[start:end])
            elif head[0] == FRAME_DATA:
                self.body += self.buf[start:end]
            del self.buf[:end]


class H3ClientProtocol(QuicConnectionProtocol):
    """Collects HTTP/3 responses on request streams; ignores server uni streams."""

    def __init__(self, quic, stream_handler=None):
        super().__init__(quic, stream_handler=stream_handler)
        self._responses: dict[int, _Response] = {}
        self._waiters: dict[int, asyncio.Future] = {}
        self._encoder = pylsqpack.Encoder()
        self._decoder = pylsqpack.Decoder(0, 0)
        self._decoder_sid: int | None = None

    async def open_streams(self) -> None:
        """Open the control and QPACK encoder/decoder uni streams."""
        control = self._quic.get_next_available_stream_id(is_unidirectional=True)
        encoder = self._quic.get_next_available_stream_id(is_unidirectional=True)
        self._decoder_sid = self._quic.get_next_available_stream_id(is_unidirectional=True)
        enc_data = self._encoder.apply_settings(max_table_capacity=0, blocked_streams=0)
        await send(self._quic, control,
                   encode_varint(STREAM_CONTROL) + settings_frame(), fin=False)
        await send(self._quic, encoder,
                   encode_varint(STREAM_QPACK_ENCODER) + enc_data, fin=False)
        await send(self._quic, self._decoder_sid,
                   encode_varint(STREAM_QPACK_DECODER), fin=False)

    def quic_event_received(self, event) -> None:
        if isinstance(event, StreamDataReceived):
            resp = self._responses.get(event.stream_id)
            if resp is None:
                return
            try:
                resp.feed(event.data)
            except Exception as exc:
                self._finish(event.stream_id, error=exc)
                return
            if event.end_stream:
                if resp.buf:
                    self._finish(event.stream_id, error=ConnectionError(
                        f"stream {event.stream_id}: truncated frame"))
                else:
                    self._finish(event.stream_id, result=resp)
        elif isinstance(event, StreamReset):
            self._finish(event.stream_id, error=ConnectionError(
                f"stream {event.stream_id} reset ({event.error_code:#x})"))
        elif isinstance(event, ConnectionTerminated):
            for sid in list(self._waiters):
                self._finish(sid, error=ConnectionError(
                    f"connection closed ({event.error_code:#x})"))

    def _finish(self, stream_id: int, *, result=None, error=None) -> None:
        self._responses.pop(stream_id, None)
        fut = self._waiters.pop(stream_id, None)
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    async def get(self, authority: str, path: str) -> bytes:
        """GET path; returns the body of a 200 response."""
        sid = self._quic.get_next_available_stream_id()
        fut = asyncio.get_running_loop().create_future()
        self._responses[sid] = _Response()
        self._waiters[sid] = fut
        _, block = self._encoder.encode(sid, [
            (b":method", b"GET"),
            (b":scheme", b"https"),
            (b":authority", authority.encode()),
            (b":path", path.encode()),
        ])
        await send(self._quic, sid, encode_frame(FRAME_HEADERS, block))
        resp: _Response = await fut
        if resp.headers is None:
            raise ConnectionError(f"stream {sid}: no HEADERS")
        dec_data, headers = self._decoder.feed_header(sid, resp.headers)
        if dec_data and self._decoder_sid is not None:
            await send(self._quic, self._decoder_sid, dec_data, fin=False)
        status = dict(headers).get(b":status")
        if status != b"200":
            raise ConnectionError(f"GET {path}: status {status!r}")
        return bytes(resp.body)


async def fetch_h3(urls: list[str], downloads: str,
                   configuration: QuicConfiguration) -> None:
    """Fetch urls in parallel over one HTTP/3 connection into downloads/<basename>.

    configuration's ALPN is replaced with h3.
    """
    if not urls:
        raise ValueError("no urls")
    first = urlparse(urls[0])
    configuration.alpn_protocols = [ALPN]
    async with connect(first.hostname, first.port or PORT,
                       configuration=configuration,
                       create_protocol=H3ClientProtocol) as proto:
        await proto.open_streams()
        in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)

        async def one(url: str) -> None:
            parsed = urlparse(url)
            path = parsed.path or "/"
            async with in_flight:
                body = await proto.get(parsed.netloc, path)
            with open(os.path.join(downloads, os.path.basename(path)), "wb") as f:
                f.write(body)

        await asyncio.gather(*(one(u) for u in urls))
        await proto.aclose(H3_NO_ERROR)
