"""WebTransport Interop Runner endpoint: GET/PUSH over streams and datagrams.

Contract: https://github.com/quic-interop/quic-interop-runner/blob/master/webtransport.md
The runner sets ROLE, TESTCASE, REQUESTS, PROTOCOLS and QLOGDIR; an
unsupported TESTCASE exits 127.
"""

import asyncio
import logging
import os
import ssl
import sys
from urllib.parse import urlparse

from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
)
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    WebTransportDatagramReceived, WebTransportNewStream,
    WebTransportSessionClosed, WebTransportStreamReset,
)

PORT = 443
CHUNK = 64 * 1024
# The bottleneck queue holds 25 packets; pace datagram requests below it,
# and responses below the 10 Mbps link rate.
DATAGRAM_INTERVAL = 0.02
DATAGRAM_RESPONSE_INTERVAL = 0.0015
TRANSFER_TIMEOUT = 60.0
EXIT_UNSUPPORTED = 127
GET = b"GET "
PUSH = b"PUSH "

# TESTCASE -> how this side requests files; None answers requests only.
CLIENT_CASES = {
    "handshake": None,
    "transfer": None,
    "transfer-unidirectional-receive": "uni",
    "transfer-bidirectional-receive": "bidi",
    "transfer-datagram-receive": "datagram",
}
SERVER_CASES = {
    "handshake": None,
    "transfer": None,
    "transfer-unidirectional-send": "uni",
    "transfer-bidirectional-send": "bidi",
    "transfer-datagram-send": "datagram",
}

log = logging.getLogger("qns.wt")


def configuration(is_client: bool) -> QuicConfiguration:
    qlog_dir = os.environ.get("QLOGDIR")
    if qlog_dir:
        os.makedirs(qlog_dir, exist_ok=True)
    cfg = QuicConfiguration(is_client=is_client, qlog_dir=qlog_dir)
    if is_client:
        cfg.verify_mode = ssl.CERT_NONE
    return cfg


def _is_unidirectional(stream_id: int) -> bool:
    return bool(stream_id & 0x2)


def write_negotiated_protocol(downloads: str, session) -> None:
    with open(os.path.join(downloads, "negotiated_protocol.txt"), "w") as f:
        f.write(session.negotiated_protocol or "")


class Peer:
    """One session: answers GETs, stores PUSHes, and can request files."""

    def __init__(self, session, endpoint: str | None, www: str,
                 downloads: str):
        self.session = session
        self.endpoint = endpoint
        self.www = www
        self.downloads = downloads
        self._tasks: set[asyncio.Task] = set()
        self._wanted: dict[str, asyncio.Future] = {}
        self._response_pacer = asyncio.Lock()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("handler failed: %r", task.exception())

    async def run(self) -> None:
        """Handle peer streams and datagrams until the session closes."""
        try:
            async for ev in self.session.events():
                if isinstance(ev, WebTransportNewStream):
                    self._spawn(self._on_stream(ev.stream_id))
                elif isinstance(ev, WebTransportDatagramReceived):
                    self._spawn(self._on_datagram(bytes(ev.data)))
                elif isinstance(ev, WebTransportSessionClosed):
                    break
        finally:
            for task in list(self._tasks):
                task.cancel()
            for fut in self._wanted.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("session closed"))

    # --- files ---------------------------------------------------------

    def _source(self, name: str) -> str | None:
        name = os.path.basename(name)
        roots = ([self.endpoint] if self.endpoint else
                 sorted(d for d in os.listdir(self.www)
                        if os.path.isdir(os.path.join(self.www, d))))
        for root in roots:
            path = os.path.join(self.www, root, name)
            if os.path.isfile(path):
                return path
        return None

    def _load(self, request: bytes) -> tuple[str, bytes] | None:
        if not request.startswith(GET):
            log.warning("not a GET: %r", request[:80])
            return None
        name = request[len(GET):].strip().decode("utf-8", "replace")
        path = self._source(name)
        if path is None:
            log.warning("no file for GET %r", name)
            return None
        with open(path, "rb") as f:
            return os.path.basename(name), f.read()

    def _store(self, name: str, payload: bytes) -> None:
        name = os.path.basename(name)
        directory = os.path.join(self.downloads, self.endpoint or "")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "wb") as f:
            f.write(payload)
        fut = self._wanted.get(name)
        if fut is not None and not fut.done():
            fut.set_result(None)

    def _store_push(self, data: bytes) -> None:
        if not data.startswith(PUSH) or b"\n" not in data:
            log.warning("not a PUSH: %r", data[:80])
            return
        header, payload = data[len(PUSH):].split(b"\n", 1)
        self._store(header.strip().decode("utf-8", "replace"), payload)

    # --- I/O -----------------------------------------------------------

    async def _read(self, stream_id: int) -> bytes:
        buf = bytearray()
        async for ev in self.session.receive_stream_data(stream_id):
            if isinstance(ev, WebTransportStreamReset):
                raise ConnectionError(
                    f"stream {stream_id} reset ({ev.error_code:#x})")
            buf += ev.data
        return bytes(buf)

    async def _write(self, stream_id: int, data: bytes) -> None:
        offset = 0
        while True:
            chunk = data[offset:offset + CHUNK]
            fin = offset + len(chunk) >= len(data)
            await self.session.send_stream_data_drained(
                stream_id, chunk, end_stream=fin)
            if fin:
                return
            offset += len(chunk)

    async def _send_datagram(self, data: bytes) -> None:
        while self.session.send_datagram_frame(data) == 0:
            try:
                await asyncio.wait_for(
                    self.session.get_datagram_tx_drain_event().wait(), 0.05)
            except asyncio.TimeoutError:
                pass

    # --- responder -----------------------------------------------------

    async def _on_stream(self, stream_id: int) -> None:
        data = await self._read(stream_id)
        if not _is_unidirectional(stream_id):
            found = self._load(data)
            if found is None:
                self.session.reset_stream(stream_id, 0x10)
                return
            await self._write(stream_id, found[1])
        elif data.startswith(PUSH):
            self._store_push(data)
        else:
            found = self._load(data)
            if found is not None:
                name, payload = found
                sid = await self.session.create_stream(bidir=False)
                await self._write(sid, PUSH + name.encode() + b"\n" + payload)

    async def _on_datagram(self, data: bytes) -> None:
        if data.startswith(PUSH):
            self._store_push(data)
            return
        found = self._load(data)
        if found is not None:
            name, payload = found
            async with self._response_pacer:
                await self._send_datagram(PUSH + name.encode() + b"\n" + payload)
                await asyncio.sleep(DATAGRAM_RESPONSE_INTERVAL)

    # --- requester -----------------------------------------------------

    async def _request_stream(self, name: str, bidir: bool) -> None:
        sid = await self.session.create_stream(bidir=bidir)
        await self._write(sid, GET + name.encode())
        if bidir:
            self._store(name, await self._read(sid))

    async def request(self, kind: str, names: list[str]) -> None:
        """Fetch names over kind ('uni', 'bidi' or 'datagram') in parallel."""
        loop = asyncio.get_running_loop()
        for name in names:
            self._wanted[os.path.basename(name)] = loop.create_future()
        if kind == "datagram":
            for name in names:
                await self._send_datagram(GET + name.encode())
                await asyncio.sleep(DATAGRAM_INTERVAL)
        else:
            await asyncio.gather(*(self._request_stream(n, kind == "bidi")
                                   for n in names))
        await asyncio.wait_for(asyncio.gather(*self._wanted.values()),
                               TRANSFER_TIMEOUT)


async def _drive(session, kind: str | None, names: list[str],
                 endpoint: str | None, www: str, downloads: str) -> None:
    peer = Peer(session, endpoint, www, downloads)
    runner = asyncio.get_running_loop().create_task(peer.run())
    try:
        if kind is None:
            await session.wait_closed()
        else:
            await peer.request(kind, names)
    finally:
        runner.cancel()


def parse_client_requests(urls: list[str]) -> tuple[str, int, str, list[str]]:
    """(host, port, endpoint, files) for the one session REQUESTS names."""
    sessions: dict[tuple[str, int, str], list[str]] = {}
    for url in urls:
        u = urlparse(url)
        endpoint, _, name = u.path.lstrip("/").partition("/")
        files = sessions.setdefault((u.hostname, u.port or PORT, endpoint), [])
        if name:
            files.append(name)
    if len(sessions) != 1:
        raise ValueError(
            f"{len(sessions)} sessions requested; one QUIC connection "
            "carries one WebTransport session here")
    (host, port, endpoint), files = next(iter(sessions.items()))
    return host, port, endpoint, files


async def run_client(testcase: str, urls: list[str], protocols: list[str],
                     www: str, downloads: str) -> None:
    host, port, endpoint, files = parse_client_requests(urls)
    async with connect_webtransport(
            host, port, "/" + endpoint, sni=host,
            wt_available_protocols=protocols or None,
            configuration=configuration(is_client=True),
            verify_peer=False) as session:
        if testcase == "handshake":
            write_negotiated_protocol(downloads, session)
            return
        await _drive(session, CLIENT_CASES[testcase], files, endpoint,
                     www, downloads)


async def start_server(testcase: str, port: int, www: str, downloads: str,
                       cert_file: str, key_file: str, protocols: list[str],
                       requests: list[str]):
    """Serve the files under www on any session path. Returns (server,
    done); done resolves when the first session's test case completes."""
    endpoints = sorted(d for d in os.listdir(www)
                       if os.path.isdir(os.path.join(www, d)))
    wanted: dict[str, list[str]] = {}
    for req in requests:
        endpoint, _, name = req.partition("/")
        wanted.setdefault(endpoint, []).append(name)
    if len(wanted) == 1:
        endpoint = next(iter(wanted))
    elif len(endpoints) == 1:
        endpoint = endpoints[0]
    else:
        endpoint = None
    done = asyncio.get_running_loop().create_future()

    async def handler(session) -> None:
        try:
            if testcase == "handshake":
                write_negotiated_protocol(downloads, session)
                await session.wait_closed()
            else:
                kind = SERVER_CASES[testcase]
                await _drive(session, kind, wanted.get(endpoint, []),
                             endpoint, www, downloads)
                if kind is not None:
                    await session.aclose()
        except Exception as exc:
            if not done.done():
                done.set_exception(exc)
            return
        if not done.done():
            done.set_result(None)

    server = await serve_webtransport(
        "0.0.0.0", port, "*", handler=handler,
        cert_file=cert_file, key_file=key_file,
        configuration=configuration(is_client=False),
        wt_supported_protocols=protocols or None)
    log.info("serving %s on UDP %d", www, port)
    return server, done


async def _serve_once(testcase: str, protocols: list[str],
                      requests: list[str]) -> None:
    server, done = await start_server(
        testcase, PORT, "/www", "/downloads", "/certs/cert.pem",
        "/certs/priv.key", protocols, requests)
    try:
        await done
        if SERVER_CASES[testcase] is None:
            # Answer later sessions too; the runner stops the container.
            await asyncio.Event().wait()
    finally:
        await server.aclose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    role = os.environ.get("ROLE", "")
    testcase = os.environ.get("TESTCASE", "")
    cases = CLIENT_CASES if role == "client" else SERVER_CASES
    if role not in ("client", "server") or testcase not in cases:
        log.info("unsupported: role=%r testcase=%r", role, testcase)
        return EXIT_UNSUPPORTED
    protocols = os.environ.get("PROTOCOLS", "").split()
    requests = os.environ.get("REQUESTS", "").split()
    try:
        if role == "client":
            asyncio.run(run_client(testcase, requests, protocols, "/www",
                                   "/downloads"))
        else:
            asyncio.run(_serve_once(testcase, protocols, requests))
    except Exception:
        log.exception("%s %s failed", role, testcase)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
