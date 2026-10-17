"""QUIC Interop Runner endpoint: HTTP/0.9 (hq-interop) client and server.

Contract: https://github.com/quic-interop/quic-interop-runner/blob/master/quic.md
The runner sets ROLE, TESTCASE, REQUESTS, SSLKEYLOGFILE and QLOGDIR; an
unsupported TESTCASE exits 127.
"""

import asyncio
import logging
import os
import socket
import ssl
import sys
from urllib.parse import urlparse

from aiopquic.asyncio import connect, serve
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, StreamDataReceived, StreamReset,
)

ALPN = "hq-interop"
PORT = 443
CHUNK = 64 * 1024
MAX_IN_FLIGHT = 64
EXIT_UNSUPPORTED = 127

# picoquic follows a Retry and marks ECT(0) by itself; every other case
# needs a setting aiopquic does not expose.
SERVER_CASES = frozenset({"handshake", "transfer", "multiconnect", "ecn"})
CLIENT_CASES = SERVER_CASES | {"retry"}

log = logging.getLogger("qns")


class Unsupported(Exception):
    """The case needs something aiopquic cannot do."""


def _qlog_dir() -> str | None:
    path = os.environ.get("QLOGDIR")
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def server_configuration(cert_file: str, key_file: str) -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN],
                            qlog_dir=_qlog_dir())
    # Requests are one line; the per-stream RX ring is sized to this window.
    cfg.max_stream_data = 64 * 1024
    cfg.load_cert_chain(cert_file, key_file)
    return cfg


def client_configuration(testcase: str) -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN],
                            verify_mode=ssl.CERT_NONE, qlog_dir=_qlog_dir())
    if testcase == "transfer":
        # Smaller than the files, so the transfer depends on window updates.
        cfg.max_data = 1024 * 1024
        cfg.max_stream_data = 256 * 1024
    return cfg


async def send_all(quic, stream_id: int, data: bytes) -> None:
    """Write data and FIN, waiting out per-stream TX backpressure."""
    drained = quic.get_tx_data_drain_event(stream_id)
    offset = 0
    while True:
        chunk = data[offset:offset + CHUNK]
        fin = offset + len(chunk) >= len(data)
        drained.clear()
        try:
            quic.send_stream_data(stream_id, chunk, end_stream=fin)
        except BufferError:
            # A full TX event ring arms no per-stream event; poll it out.
            try:
                await asyncio.wait_for(drained.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
            continue
        if fin:
            return
        offset += len(chunk)
        await asyncio.sleep(0)


def parse_request(request: bytes) -> str | None:
    """Path of an HTTP/0.9 'GET /path', relative to the web root."""
    line = request.split(b"\n", 1)[0].strip()
    if not line.startswith(b"GET "):
        return None
    path = line[4:].strip().decode("utf-8", "replace")
    return urlparse(path).path.lstrip("/")


class HqServerProtocol(QuicConnectionProtocol):
    """Serves one file per bidirectional stream from the web root."""

    www = "/www"

    def __init__(self, quic, stream_handler=None):
        super().__init__(quic, stream_handler=stream_handler)
        self._requests: dict[int, bytearray] = {}
        self._answered: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    def quic_event_received(self, event) -> None:
        if not isinstance(event, StreamDataReceived):
            return
        sid = event.stream_id
        if sid in self._answered:
            return
        buf = self._requests.setdefault(sid, bytearray())
        buf += event.data
        if event.end_stream or b"\n" in buf:
            self._answered.add(sid)
            request = bytes(self._requests.pop(sid))
            task = asyncio.get_running_loop().create_task(
                self._respond(sid, request))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _respond(self, stream_id: int, request: bytes) -> None:
        path = parse_request(request)
        root = os.path.realpath(self.www)
        target = os.path.realpath(os.path.join(root, path or ""))
        if path is None or not target.startswith(root + os.sep) \
                or not os.path.isfile(target):
            log.warning("stream %d: no file for %r", stream_id, request[:80])
            self._quic.reset_stream(stream_id, 0x10)
            return
        with open(target, "rb") as f:
            data = f.read()
        await send_all(self._quic, stream_id, data)


class HqClientProtocol(QuicConnectionProtocol):
    """Fetches files, one bidirectional stream per request."""

    def __init__(self, quic, stream_handler=None):
        super().__init__(quic, stream_handler=stream_handler)
        self._bodies: dict[int, bytearray] = {}
        self._waiters: dict[int, asyncio.Future] = {}

    def quic_event_received(self, event) -> None:
        if isinstance(event, StreamDataReceived):
            body = self._bodies.get(event.stream_id)
            if body is None:
                return
            body += event.data
            if event.end_stream:
                self._finish(event.stream_id, result=bytes(body))
        elif isinstance(event, StreamReset):
            self._finish(event.stream_id, error=ConnectionError(
                f"stream {event.stream_id} reset ({event.error_code:#x})"))
        elif isinstance(event, ConnectionTerminated):
            for sid in list(self._waiters):
                self._finish(sid, error=ConnectionError(
                    f"connection closed ({event.error_code:#x})"))

    def _finish(self, stream_id: int, *, result=None, error=None) -> None:
        self._bodies.pop(stream_id, None)
        fut = self._waiters.pop(stream_id, None)
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    async def fetch(self, path: str) -> bytes:
        sid = self._quic.get_next_available_stream_id()
        fut = asyncio.get_running_loop().create_future()
        self._bodies[sid] = bytearray()
        self._waiters[sid] = fut
        await send_all(self._quic, sid, f"GET {path}\r\n".encode())
        return await fut


async def start_server(port: int, www: str, cert_file: str, key_file: str):
    """Listen on port, serving www. Returns the server; close() stops it."""
    protocol = type("HqServer", (HqServerProtocol,), {"www": www})
    server = await serve("0.0.0.0", port,
                         configuration=server_configuration(cert_file, key_file),
                         create_protocol=protocol)
    log.info("serving %s on UDP %d", www, port)
    return server


async def _download(urls: list[str], downloads: str, testcase: str) -> None:
    first = urlparse(urls[0])
    try:
        socket.getaddrinfo(first.hostname, None, socket.AF_INET)
    except socket.gaierror:
        # The ipv6 case arrives as "transfer" to an IPv6-only host.
        raise Unsupported(f"{first.hostname}: no IPv4 address") from None
    async with connect(first.hostname, first.port or PORT,
                       configuration=client_configuration(testcase),
                       create_protocol=HqClientProtocol) as proto:
        in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)

        async def one(url: str) -> None:
            path = urlparse(url).path
            async with in_flight:
                body = await proto.fetch(path)
            with open(os.path.join(downloads, os.path.basename(path)), "wb") as f:
                f.write(body)

        await asyncio.gather(*(one(u) for u in urls))


async def run_client(testcase: str, urls: list[str], downloads: str) -> None:
    """Fetch urls into downloads: one connection, or one per file for
    multiconnect."""
    if not urls:
        raise ValueError("REQUESTS is empty")
    if testcase == "multiconnect":
        for url in urls:
            await _download([url], downloads, testcase)
    else:
        await _download(urls, downloads, testcase)


async def _serve_forever() -> None:
    await start_server(PORT, "/www", "/certs/cert.pem", "/certs/priv.key")
    await asyncio.Event().wait()


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    role = os.environ.get("ROLE", "")
    testcase = os.environ.get("TESTCASE", "")
    cases = CLIENT_CASES if role == "client" else SERVER_CASES
    if role not in ("client", "server") or testcase not in cases:
        log.info("unsupported: role=%r testcase=%r", role, testcase)
        return EXIT_UNSUPPORTED
    try:
        if role == "client":
            urls = os.environ.get("REQUESTS", "").split()
            asyncio.run(run_client(testcase, urls, "/downloads"))
        else:
            asyncio.run(_serve_forever())
    except Unsupported as exc:
        log.info("unsupported: %s", exc)
        return EXIT_UNSUPPORTED
    except Exception:
        log.exception("%s %s failed", role, testcase)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
