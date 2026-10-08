"""Concurrent-streams stress + byte verification (high-level API).

Open N unidirectional streams in parallel on a single QUIC
connection; round-robin objects across them; every stream gets the
same total but interleaved on the wire. Receiver verifies each
stream's bytes independently.

Stresses aiopquic's multiplexed-stream path (multiple per-stream
SPSC TX rings simultaneously fed, picoquic worker draining them
round-robin) — the surface aiomoqt's `-P` subgroup parameter
exercises in production.

Pass criterion: every stream byte-perfect (sent == recv, no bad
hash, no gaps, no dupes, FIN delivered).

Run: pytest tests/bench/bench_concurrent_streams_highlevel.py -s -v
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
import zlib
from typing import Optional

import pytest

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.server import serve


CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name
ALPN = "hq-interop"

HEADER_FMT = "<QII"   # u64 seq, u32 payload_len, u32 crc32
HEADER_LEN = struct.calcsize(HEADER_FMT)


def _hash(buf: bytes) -> int:
    return zlib.crc32(buf) & 0xFFFFFFFF


def _build(seq: int, pad: bytes) -> bytes:
    return struct.pack(HEADER_FMT, seq, len(pad), _hash(pad)) + pad


_port_counter = 39567


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_config() -> QuicConfiguration:
    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN],
        max_data=1 << 28, max_stream_data=1 << 23,
    )
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config() -> QuicConfiguration:
    return QuicConfiguration(
        is_client=True, alpn_protocols=[ALPN], server_name=SNI, cafile=CA_FILE,
        max_data=1 << 28, max_stream_data=1 << 23,
    )


class _StreamRx:
    __slots__ = ("buf", "objs", "bad_hash", "gaps", "dupes",
                 "last_seq", "fin")

    def __init__(self):
        self.buf = bytearray()
        self.objs = 0
        self.bad_hash = 0
        self.gaps = 0
        self.dupes = 0
        self.last_seq = -1
        self.fin = False


def _drain(rx: _StreamRx, data: bytes) -> None:
    rx.buf.extend(data)
    while len(rx.buf) >= HEADER_LEN:
        seq, plen, h = struct.unpack_from(HEADER_FMT, rx.buf, 0)
        need = HEADER_LEN + plen
        if len(rx.buf) < need:
            break
        payload = bytes(rx.buf[HEADER_LEN:need])
        del rx.buf[:need]
        if _hash(payload) != h:
            rx.bad_hash += 1
        expected = rx.last_seq + 1
        if seq < expected:
            rx.dupes += 1
        elif seq > expected:
            rx.gaps += seq - expected
        if seq > rx.last_seq:
            rx.last_seq = seq
        rx.objs += 1


class _RxProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.streams: dict[int, _StreamRx] = {}

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            rx = self.streams.get(event.stream_id)
            if rx is None:
                rx = _StreamRx()
                self.streams[event.stream_id] = rx
            _drain(rx, event.data)
            if event.end_stream:
                rx.fin = True


async def _run_concurrent(n_streams: int, objs_per_stream: int,
                          obj_size: int) -> dict:
    """Open n_streams uni streams up front; interleave object sends
    across all streams round-robin; FIN each at the end."""
    port = _next_port()
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_config(),
        create_protocol=lambda quic, **kw: _RxProtocol(quic, **kw),
    )

    pad = bytes(i & 0xFF for i in range(max(0, obj_size - HEADER_LEN)))
    captured: dict[int, _StreamRx] = {}
    sids: list[int] = []
    full_waits = 0
    t_start = 0.0
    t_send_done = 0.0

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_config(),
        ) as client:
            t_start = time.monotonic()
            # Open all streams up front. The first send to each
            # creates the per-stream wrapper + TX ring.
            for _ in range(n_streams):
                sids.append(
                    client._quic.get_next_available_stream_id(
                        is_unidirectional=True))
            # Interleave: round robin across streams. Per-stream
            # sequence increments independently (each stream's
            # receiver expects monotonic 0..N-1).
            per_stream_seq = [0] * n_streams
            for total in range(n_streams * objs_per_stream):
                idx = total % n_streams
                sid = sids[idx]
                seq = per_stream_seq[idx]
                obj = _build(seq, pad)
                while True:
                    try:
                        client._quic.send_stream_data(
                            sid, obj, end_stream=False)
                        break
                    except BufferError:
                        full_waits += 1
                        await asyncio.sleep(0.0001)
                per_stream_seq[idx] = seq + 1
                if (total & 0x3F) == 0:
                    await asyncio.sleep(0)
            # FIN each stream.
            for sid in sids:
                client._quic.send_stream_data(sid, b"", end_stream=True)
            t_send_done = time.monotonic()

            # Wait for all FINs at the receiver.
            engine = getattr(server, "_engine", None)
            drain_deadline = time.monotonic() + 15.0
            while time.monotonic() < drain_deadline:
                rx_proto = None
                if engine is not None:
                    for pr in engine._protocols.values():
                        if isinstance(pr, _RxProtocol):
                            rx_proto = pr
                            break
                if rx_proto is not None:
                    fin_count = sum(
                        1 for s in sids
                        if s in rx_proto.streams
                        and rx_proto.streams[s].fin
                        and rx_proto.streams[s].objs >= objs_per_stream
                    )
                    if fin_count == n_streams:
                        for s in sids:
                            captured[s] = rx_proto.streams[s]
                        break
                await asyncio.sleep(0.01)

            if not captured and engine is not None:
                for pr in engine._protocols.values():
                    if isinstance(pr, _RxProtocol):
                        for s in sids:
                            if s in pr.streams:
                                captured[s] = pr.streams[s]
                        break
    finally:
        server.close()
        await asyncio.sleep(0.05)

    total_sent = n_streams * objs_per_stream
    total_recv = sum(rx.objs for rx in captured.values())
    bad_hash = sum(rx.bad_hash for rx in captured.values())
    gaps = sum(rx.gaps for rx in captured.values())
    dupes = sum(rx.dupes for rx in captured.values())
    streams_complete = sum(
        1 for rx in captured.values()
        if rx.fin and rx.objs == objs_per_stream
        and rx.bad_hash == 0 and rx.gaps == 0 and rx.dupes == 0
    )

    elapsed = max(1e-6, t_send_done - t_start)
    obj_per_s = total_sent / elapsed
    mbps = (total_sent * obj_size * 8 / 1e6) / elapsed

    ok = (
        streams_complete == n_streams
        and total_recv == total_sent
        and bad_hash == 0 and gaps == 0 and dupes == 0
    )
    return {
        "n_streams": n_streams, "objs_per_stream": objs_per_stream,
        "obj_size": obj_size,
        "total_sent": total_sent, "total_recv": total_recv,
        "bad_hash": bad_hash, "gaps": gaps, "dupes": dupes,
        "streams_complete": streams_complete,
        "elapsed_s": round(elapsed, 3),
        "obj_per_s": round(obj_per_s, 1),
        "Mbps": round(mbps, 1),
        "full_waits": full_waits,
        "pass": ok,
        "reason": "" if ok else (
            f"complete={streams_complete}/{n_streams} "
            f"recv={total_recv}/{total_sent} "
            f"bad_hash={bad_hash} gaps={gaps} dupes={dupes}"
        ),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.bench
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size", [
    (   4,  1000,  1024),
    (  16,   500,  1024),
    (  64,   200,  1024),
    ( 256,   100,  1024),
    (   4,   500,  4096),
    (  16,   500,  4096),
    (  64,   200,  4096),
    ( 256,    50,  4096),
    (   4,   100, 16384),
    (  16,   100, 16384),
    (  64,    50, 16384),
], ids=[
    "4-1024B",   "16-1024B",  "64-1024B",  "256-1024B",
    "4-4K",      "16-4K",     "64-4K",     "256-4K",
    "4-16K",     "16-16K",    "64-16K",
])
def test_bench_concurrent_streams_highlevel(
        n_streams, objs_per_stream, obj_size, capsys):
    res = asyncio.run(_run_concurrent(
        n_streams, objs_per_stream, obj_size))
    print(
        f"\n  P={res['n_streams']:>3}  ×  {res['objs_per_stream']:>4}o  "
        f"×  {res['obj_size']:>5}B   "
        f"complete={res['streams_complete']}/{res['n_streams']}  "
        f"obj/s={res['obj_per_s']:>10,.0f}  "
        f"{res['Mbps']:>7,.1f} Mbps   "
        f"bad_hash={res['bad_hash']}  gaps={res['gaps']}  "
        f"dupes={res['dupes']}  full_waits={res['full_waits']}"
    )
    assert res["pass"], res["reason"]
