"""qh3-based echo peer — runs as either server (echoes back received bytes,
used when aiopquic is the client) or sink (consumes bytes and reports
cumulative-bytes + crc32 on FIN, used when aiopquic is the server pushing).

Spawned as a subprocess from the test harness so it runs in its own asyncio
loop (qh3's loop policies don't always coexist cleanly with aiopquic's in the
same process). Communication: stdout JSON one-line summary on shutdown.

Usage (server modes):
  python qh3_echo.py serve --port 4433 --cert PATH --key PATH
                              [--alpn hq-interop] [--mode echo|sink]

Usage (client modes):
  python qh3_echo.py connect --host 127.0.0.1 --port 4433
                              [--alpn hq-interop] [--insecure]
                              [--send-bytes N] [--object-size N]
                              [--ca PATH]

Output on exit: one JSON line: {"role": "...", "stream_bytes": N,
"crc32": 0x..., "duration_s": ..., "ok": true|false}
"""
import argparse
import asyncio
import json
import sys
import time
import zlib
from typing import Optional

from qh3.asyncio import serve, connect
from qh3.asyncio.protocol import QuicConnectionProtocol
from qh3.quic.configuration import QuicConfiguration
from qh3.quic.events import StreamDataReceived, ConnectionTerminated


def counted_pad(size: int) -> bytes:
    return bytes(i & 0xFF for i in range(size))


class EchoSinkProtocol(QuicConnectionProtocol):
    """Echo back received bytes (mode=echo) or just consume (mode=sink).
    Tracks cumulative bytes + rolling CRC32 per stream; reports aggregate
    on connection close."""

    def __init__(self, *args, mode: str = "echo", **kwargs):
        super().__init__(*args, **kwargs)
        self._mode = mode
        self._stream_bytes = 0
        self._crc = 0
        self._t0 = time.monotonic()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            data = bytes(event.data) if event.data else b""
            if data:
                self._stream_bytes += len(data)
                self._crc = zlib.crc32(data, self._crc) & 0xFFFFFFFF
                if self._mode == "echo":
                    self._quic.send_stream_data(
                        event.stream_id, data, end_stream=event.end_stream
                    )
                    self.transmit()
            if event.end_stream:
                # Sink completion signal: dump summary as soon as we see
                # FIN. Echo mode: also FIN our send-side. Either way, the
                # transfer is logically complete from our perspective.
                if self._mode == "sink":
                    self._quic.send_stream_data(
                        event.stream_id, b"", end_stream=True
                    )
                    self.transmit()
                self._dump()
        elif isinstance(event, ConnectionTerminated):
            self._dump()

    def connection_lost(self, exc):
        self._dump()
        super().connection_lost(exc)

    def _dump(self):
        if getattr(self, "_dumped", False):
            return
        self._dumped = True
        sys.stdout.write(json.dumps({
            "role": "server",
            "mode": self._mode,
            "stream_bytes": self._stream_bytes,
            "crc32": self._crc,
            "duration_s": time.monotonic() - self._t0,
            "ok": True,
        }) + "\n")
        sys.stdout.flush()


async def run_server(args):
    cfg = QuicConfiguration(
        is_client=False,
        alpn_protocols=[args.alpn],
        max_stream_data=16 * 1024 * 1024,
        max_data=64 * 1024 * 1024,
    )
    cfg.load_cert_chain(args.cert, args.key)

    def proto_factory(*p_args, **p_kwargs):
        return EchoSinkProtocol(*p_args, mode=args.mode, **p_kwargs)

    server = await serve(
        host=args.host, port=args.port,
        configuration=cfg,
        create_protocol=proto_factory,
    )
    sys.stderr.write(f"qh3_echo serving on {args.host}:{args.port}\n")
    sys.stderr.flush()
    try:
        await asyncio.Future()  # run forever
    except asyncio.CancelledError:
        pass
    finally:
        server.close()


async def run_client(args):
    cfg = QuicConfiguration(
        is_client=True,
        alpn_protocols=[args.alpn],
        verify_mode=__import__("ssl").CERT_NONE if args.insecure else None,
        max_stream_data=16 * 1024 * 1024,
        max_data=64 * 1024 * 1024,
    )
    if args.ca and not args.insecure:
        cfg.load_verify_locations(args.ca)

    crc_sent = 0
    bytes_sent = 0
    crc_recv = 0
    bytes_recv = 0

    t0 = time.monotonic()
    async with connect(
        args.host, args.port,
        configuration=cfg,
    ) as protocol:
        reader, writer = await protocol.create_stream()
        # Push counted-pad in chunks so we don't allocate the full payload.
        remaining = args.send_bytes
        chunk_size = args.object_size
        full_pad = counted_pad(min(remaining, max(chunk_size, 4096)))
        offset = 0
        while remaining > 0:
            n = min(chunk_size, remaining)
            chunk = full_pad[offset:offset + n] if offset + n <= len(full_pad) \
                    else counted_pad(n)
            writer.write(chunk)
            crc_sent = zlib.crc32(chunk, crc_sent) & 0xFFFFFFFF
            bytes_sent += n
            offset = (offset + n) % len(full_pad)
            remaining -= n
            await writer.drain()
        writer.write_eof()
        await writer.drain()

        # Drain echoed/sink response until EOF.
        while True:
            data = await reader.read(65536)
            if not data:
                break
            crc_recv = zlib.crc32(data, crc_recv) & 0xFFFFFFFF
            bytes_recv += len(data)

    sys.stdout.write(json.dumps({
        "role": "client",
        "stream_bytes_sent": bytes_sent,
        "crc32_sent": crc_sent,
        "stream_bytes_recv": bytes_recv,
        "crc32_recv": crc_recv,
        "duration_s": time.monotonic() - t0,
        "ok": True,
    }) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--cert", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--alpn", default="hq-interop")
    s.add_argument("--mode", choices=["echo", "sink"], default="echo")

    c = sub.add_parser("connect")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, required=True)
    c.add_argument("--alpn", default="hq-interop")
    c.add_argument("--insecure", action="store_true")
    c.add_argument("--ca", default=None)
    c.add_argument("--send-bytes", type=int, default=10 * 1024 * 1024)
    c.add_argument("--object-size", type=int, default=4096)

    args = ap.parse_args()
    if args.cmd == "serve":
        asyncio.run(run_server(args))
    else:
        asyncio.run(run_client(args))


if __name__ == "__main__":
    main()
