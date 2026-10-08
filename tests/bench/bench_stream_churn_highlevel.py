"""Stream churn stress + byte verification (high-level API).

Open many short-lived unidirectional streams on a single QUIC
connection; each stream carries N small objects with FNV1a-equivalent
(zlib.crc32) per-object verification, then FINs. Receiver verifies
every stream end-to-end.

Stresses the per-stream wrapper lifecycle that aiomoqt exercises
heavily: every subgroup is a new uni stream that opens, sends, and
FINs. The intermittent segfault we saw in 0.2.0 + the [wt] flake
pattern were both consistent with a UAF in the create/destroy path
of `aiopquic_stream_ctx_t`. This bench drives that path hard.

Pass criterion: every stream that opens delivers all its objects
byte-perfect, no parser exceptions, no segfault.

Run: pytest tests/bench/bench_stream_churn_highlevel.py -s -v
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


_port_counter = 38567


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_config() -> QuicConfiguration:
    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN],
        max_data=1 << 28, max_stream_data=1 << 22,
    )
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config() -> QuicConfiguration:
    return QuicConfiguration(
        is_client=True, alpn_protocols=[ALPN], server_name=SNI, cafile=CA_FILE,
        max_data=1 << 28, max_stream_data=1 << 22,
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


async def _run_churn(n_streams: int, objs_per_stream: int,
                     obj_size: int, pace_objs_per_s: float = 0.0) -> dict:
    """Open n_streams uni streams sequentially, each carrying
    objs_per_stream objects, then FIN. pace=0 means line rate."""
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

    interval = (1.0 / pace_objs_per_s) if pace_objs_per_s > 0 else 0.0

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_config(),
        ) as client:
            t_start = time.monotonic()
            obj_counter = 0
            for stream_idx in range(n_streams):
                sid = client._quic.get_next_available_stream_id(
                    is_unidirectional=True)
                sids.append(sid)
                next_t = time.monotonic()
                # Per-stream sequence — each stream's _StreamRx
                # validates monotonic seq starting from 0.
                for seq in range(objs_per_stream):
                    obj = _build(seq, pad)
                    while True:
                        try:
                            client._quic.send_stream_data(
                                sid, obj, end_stream=False)
                            break
                        except BufferError:
                            full_waits += 1
                            await asyncio.sleep(0.0001)
                    obj_counter += 1
                    if interval > 0:
                        next_t += interval
                        delay = next_t - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    elif (obj_counter & 0x3F) == 0:
                        await asyncio.sleep(0)
                # FIN this stream and yield so the receiver runs.
                client._quic.send_stream_data(sid, b"", end_stream=True)
                if (stream_idx & 0x1F) == 0:
                    await asyncio.sleep(0)
            t_send_done = time.monotonic()

            # Wait for every stream to FIN at the receiver.
            engine = getattr(server, "_engine", None)
            drain_deadline = time.monotonic() + 10.0
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

            # Capture whatever we have before server.close clears
            # engine._protocols.
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

    # Aggregate.
    total_sent = n_streams * objs_per_stream
    total_recv = sum(rx.objs for rx in captured.values())
    bad_hash = sum(rx.bad_hash for rx in captured.values())
    gaps = sum(rx.gaps for rx in captured.values())
    dupes = sum(rx.dupes for rx in captured.values())
    streams_fin = sum(1 for rx in captured.values() if rx.fin)
    streams_complete = sum(
        1 for rx in captured.values()
        if rx.fin and rx.objs == objs_per_stream
        and rx.bad_hash == 0 and rx.gaps == 0 and rx.dupes == 0
    )

    elapsed = max(1e-6, t_send_done - t_start)
    streams_per_s = n_streams / elapsed
    obj_per_s = total_sent / elapsed
    mbps = (total_sent * obj_size * 8 / 1e6) / elapsed

    ok = (
        streams_complete == n_streams
        and total_recv == total_sent
        and bad_hash == 0 and gaps == 0 and dupes == 0
    )
    return {
        "n_streams": n_streams, "objs_per_stream": objs_per_stream,
        "obj_size": obj_size, "pace": pace_objs_per_s,
        "total_sent": total_sent, "total_recv": total_recv,
        "bad_hash": bad_hash, "gaps": gaps, "dupes": dupes,
        "streams_fin": streams_fin,
        "streams_complete": streams_complete,
        "elapsed_s": round(elapsed, 3),
        "streams_per_s": round(streams_per_s, 1),
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
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size,pace", [
    # Pure churn: many short streams, few small objects each.
    # This is the aiomoqt "every-subgroup-is-its-own-stream" pattern.
    ( 100,    4,    256,       0),
    ( 500,    4,    256,       0),
    (1000,    4,    256,       0),
    ( 100,   16,   1024,       0),
    ( 500,   16,   1024,       0),
    (1000,   16,   1024,       0),
    # Mid-size streams.
    ( 200,   64,   4096,       0),
    ( 100,  256,   4096,       0),
    # Paced churn — gives the worker time between bursts.
    ( 500,   16,   1024,  10_000),
    (1000,    4,    256,  20_000),
], ids=[
    "100s-4o-256B-line",   "500s-4o-256B-line",   "1000s-4o-256B-line",
    "100s-16o-1K-line",    "500s-16o-1K-line",    "1000s-16o-1K-line",
    "200s-64o-4K-line",    "100s-256o-4K-line",
    "500s-16o-1K-paced10k", "1000s-4o-256B-paced20k",
])
def test_bench_stream_churn_highlevel(
        n_streams, objs_per_stream, obj_size, pace, capsys):
    res = asyncio.run(_run_churn(
        n_streams, objs_per_stream, obj_size, float(pace)))
    print(
        f"\n  {res['n_streams']:>4}s × {res['objs_per_stream']:>3}o × "
        f"{res['obj_size']:>5}B  pace={res['pace']:>6,.0f}/s   "
        f"complete={res['streams_complete']}/{res['n_streams']}  "
        f"streams/s={res['streams_per_s']:>7,.0f}  "
        f"{res['Mbps']:>6,.1f} Mbps  "
        f"bad_hash={res['bad_hash']}  gaps={res['gaps']}  "
        f"dupes={res['dupes']}  full_waits={res['full_waits']}"
    )
    assert res["pass"], res["reason"]
