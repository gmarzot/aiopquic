"""QUIC perf protocol (draft-banks-quic-performance) client and server.

Wire format, ALPN "perf": the client opens a stream and sends the number of
bytes it wants back as an 8-byte big-endian unsigned integer, then its upload
bytes, then FIN. The server discards the upload until FIN, then sends the
requested bytes: on the same stream for a bidirectional request, on a new
unidirectional stream for a unidirectional one. A request of 2**64-1 bytes is
unbounded and ends when the client sends STOP_SENDING. A stream that ends
before 8 bytes arrive is ignored.

Upload sizes here count the bytes after the header (as quinn's perf does);
picoquic's post_size and secnetperf's -up include the 8 header bytes.

    python tests/bench/perf.py server --port 4433 --cert C --key K
    python tests/bench/perf.py client HOST 4433 --download 10M --streams 4 --duration 10
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import struct
import sys
import time
from dataclasses import dataclass

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.server import serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, QuicEvent, StopSendingReceived, StreamDataReceived, StreamReset,
)

ALPN = "perf"
HEADER = struct.Struct("!Q")
UNBOUNDED = 2**64 - 1
CHUNK = 64 * 1024
_PAYLOAD = b"\x30" * CHUNK


def _client_initiated(sid: int) -> bool:
    return sid & 1 == 0


def _is_uni(sid: int) -> bool:
    return sid & 2 != 0


async def _send_bytes(quic, sid: int, n: int, prefix: bytes = b"") -> None:
    """Send `prefix` plus `n` filler bytes, then FIN; n=UNBOUNDED sends until stopped."""
    if prefix:
        await quic.send_stream_data_drained(sid, prefix, end_stream=(n == 0))
    elif n == 0:
        await quic.send_stream_data_drained(sid, b"", end_stream=True)
        return
    left = n
    while left > 0 and not quic.closed:
        k = CHUNK if n == UNBOUNDED else min(CHUNK, left)
        if n != UNBOUNDED:
            left -= k
        data = _PAYLOAD if k == CHUNK else _PAYLOAD[:k]
        await quic.send_stream_data_drained(sid, data, end_stream=(left == 0))


class _ServerStream:
    __slots__ = ("header", "uploaded")

    def __init__(self) -> None:
        self.header = bytearray()
        self.uploaded = 0


class PerfServerProtocol(QuicConnectionProtocol):
    """Answers perf requests on one connection."""

    def __init__(self, quic, **kw) -> None:
        super().__init__(quic, **kw)
        self._streams: dict[int, _ServerStream] = {}
        self._senders: dict[int, asyncio.Task] = {}
        self.bytes_received = 0
        self.bytes_sent = 0
        self.requests = 0

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            if _client_initiated(event.stream_id):
                self._on_request_data(event.stream_id, event.data, event.end_stream)
        elif isinstance(event, (StopSendingReceived, StreamReset)):
            self._streams.pop(event.stream_id, None)
            task = self._senders.pop(event.stream_id, None)
            if task is not None:
                task.cancel()
                if isinstance(event, StopSendingReceived):
                    self._quic.reset_stream(event.stream_id, 0)
        elif isinstance(event, ConnectionTerminated):
            for task in self._senders.values():
                task.cancel()
            self._senders.clear()

    def _on_request_data(self, sid: int, data, fin: bool) -> None:
        st = self._streams.get(sid)
        if st is None:
            st = self._streams[sid] = _ServerStream()
        n = len(data)
        self.bytes_received += n
        need = 8 - len(st.header)
        if need > 0:
            st.header += data[:need]
            st.uploaded += max(0, n - need)
        else:
            st.uploaded += n
        if not fin:
            return
        del self._streams[sid]
        if len(st.header) < 8:
            if not _is_uni(sid):
                self._quic.send_stream_data(sid, b"", end_stream=True)
            return
        size = HEADER.unpack(st.header)[0]
        self.requests += 1
        if _is_uni(sid):
            if size == 0:
                return
            out = self._quic.get_next_available_stream_id(is_unidirectional=True)
        else:
            out = sid
        task = asyncio.get_running_loop().create_task(self._respond(out, size))
        self._senders[out] = task

    async def _respond(self, sid: int, size: int) -> None:
        try:
            await _send_bytes(self._quic, sid, size)
            if size != UNBOUNDED:
                self.bytes_sent += size
        finally:
            if self._senders.get(sid) is asyncio.current_task():
                del self._senders[sid]


@dataclass
class _Pending:
    expected: int
    future: asyncio.Future
    received: int = 0


class PerfClientProtocol(QuicConnectionProtocol):
    """Issues perf requests; each request() returns once its response FIN arrives."""

    def __init__(self, quic, **kw) -> None:
        super().__init__(quic, **kw)
        self._pending: dict[int, _Pending] = {}
        self._uni_waiting: list[_Pending] = []
        self.bytes_sent = 0
        self.bytes_received = 0

    async def request(self, upload: int, download: int, *, uni: bool = False) -> int:
        """Send one request; return the response size received."""
        loop = asyncio.get_running_loop()
        sid = self._quic.get_next_available_stream_id(is_unidirectional=uni)
        pending = _Pending(download, loop.create_future())
        if uni:
            if download:
                self._uni_waiting.append(pending)
            else:
                pending.future.set_result(0)
        else:
            self._pending[sid] = pending
        try:
            await _send_bytes(self._quic, sid, upload, HEADER.pack(download))
            if self._quic.closed:
                raise ConnectionError("connection closed during upload")
            self.bytes_sent += 8 + upload
            return await pending.future
        finally:
            self._pending.pop(sid, None)
            if pending in self._uni_waiting:
                self._uni_waiting.remove(pending)

    def _fail(self, p: _Pending, exc: Exception) -> None:
        if not p.future.done():
            p.future.set_exception(exc)

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            sid = event.stream_id
            p = self._pending.get(sid)
            if p is None and _is_uni(sid) and not _client_initiated(sid) and self._uni_waiting:
                p = self._pending[sid] = self._uni_waiting.pop(0)
            if p is None:
                return
            n = len(event.data)
            p.received += n
            self.bytes_received += n
            if p.received > p.expected:
                self._fail(p, ValueError(f"stream {sid}: {p.received} > {p.expected} bytes"))
            elif event.end_stream:
                if p.received != p.expected:
                    self._fail(p, ValueError(
                        f"stream {sid}: FIN after {p.received} of {p.expected} bytes"))
                elif not p.future.done():
                    p.future.set_result(p.received)
                self._pending.pop(sid, None)
        elif isinstance(event, StreamReset):
            p = self._pending.pop(event.stream_id, None)
            if p is not None:
                self._fail(p, ConnectionResetError(
                    f"stream {event.stream_id} reset ({event.error_code})"))
        elif isinstance(event, ConnectionTerminated):
            exc = ConnectionError(f"connection terminated ({event.error_code})")
            for p in [*self._pending.values(), *self._uni_waiting]:
                self._fail(p, exc)


def server_configuration(cert: str, key: str, cc_algo: str | None = "bbr1") -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                            congestion_control_algorithm=cc_algo)
    cfg.load_cert_chain(cert, key)
    return cfg


def client_configuration(*, server_name: str | None = None, cafile: str | None = None,
                         insecure: bool = False,
                         cc_algo: str | None = "bbr1") -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN], server_name=server_name,
                            cafile=cafile, congestion_control_algorithm=cc_algo)
    if insecure:
        cfg.verify_mode = ssl.CERT_NONE
    return cfg


async def start_server(port: int, configuration: QuicConfiguration,
                       connections: list | None = None, host: str = "0.0.0.0"):
    """Serve perf on `port`; each accepted connection's protocol is appended to `connections`."""
    def factory(quic, **kw):
        proto = PerfServerProtocol(quic, **kw)
        if connections is not None:
            connections.append(proto)
        return proto
    return await serve(host, port, configuration=configuration, create_protocol=factory)


@dataclass
class Result:
    bytes_up: int
    bytes_down: int
    elapsed: float
    requests: int

    def line(self) -> str:
        e = self.elapsed or 1e-9
        return (f"perf: up={self.bytes_up} down={self.bytes_down} elapsed={e:.3f}s "
                f"up_mbps={self.bytes_up * 8 / e / 1e6:.1f} "
                f"down_mbps={self.bytes_down * 8 / e / 1e6:.1f} "
                f"requests={self.requests} rps={self.requests / e:.1f}")


async def run_client(host: str, port: int, configuration: QuicConfiguration, *,
                     upload: int = 0, download: int = 0, streams: int = 1,
                     requests: int | None = None, duration: float | None = None) -> Result:
    """Keep `streams` requests in flight until `requests` complete or `duration` elapses."""
    if requests is None and duration is None:
        requests = streams
    async with connect(host, port, configuration=configuration,
                       create_protocol=PerfClientProtocol) as proto:
        done = 0
        issued = 0
        start = time.monotonic()
        deadline = start + duration if duration is not None else None

        def more() -> bool:
            if requests is not None and issued >= requests:
                return False
            return deadline is None or time.monotonic() < deadline

        async def worker() -> None:
            nonlocal done, issued
            while more():
                issued += 1
                await proto.request(upload, download)
                done += 1

        await asyncio.gather(*(worker() for _ in range(streams)))
        elapsed = time.monotonic() - start
        return Result(proto.bytes_sent, proto.bytes_received, elapsed, done)


def parse_size(text: str) -> int:
    """Byte count with optional binary suffix: 512, 64K, 10M, 1G."""
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    t = text.strip().upper().removesuffix("B")
    if t and t[-1] in mult:
        return int(t[:-1]) * mult[t[-1]]
    return int(t)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QUIC perf protocol (ALPN perf) over aiopquic")
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("server")
    s.add_argument("--port", type=int, default=4433)
    s.add_argument("--cert", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--cc-algo", default="bbr1")

    c = sub.add_parser("client")
    c.add_argument("host")
    c.add_argument("port", type=int, nargs="?", default=4433)
    c.add_argument("--upload", type=parse_size, default=0,
                   help="bytes sent per request after the 8-byte header")
    c.add_argument("--download", type=parse_size, default=0,
                   help="bytes requested per request")
    c.add_argument("--streams", type=int, default=1, help="concurrent requests")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--requests", type=int, help="total requests (default: one per stream)")
    g.add_argument("--duration", type=float, help="seconds to keep issuing requests")
    c.add_argument("--cc-algo", default="bbr1")
    c.add_argument("--insecure", action="store_true", help="skip certificate verification")
    c.add_argument("--server-name", help="SNI and verified name (default: host)")
    c.add_argument("--ca-file", help="trust roots for the server certificate")

    args = ap.parse_args(argv)
    if args.mode == "server":
        try:
            return asyncio.run(_serve_forever(args))
        except KeyboardInterrupt:
            return 0
    cfg = client_configuration(server_name=args.server_name, cafile=args.ca_file,
                               insecure=args.insecure, cc_algo=args.cc_algo)
    result = asyncio.run(run_client(
        args.host, args.port, cfg, upload=args.upload, download=args.download,
        streams=args.streams, requests=args.requests, duration=args.duration))
    print(result.line())
    return 0


async def _serve_forever(args) -> int:
    server = await start_server(args.port, server_configuration(args.cert, args.key,
                                                                args.cc_algo))
    print(f"perf server on port {args.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
