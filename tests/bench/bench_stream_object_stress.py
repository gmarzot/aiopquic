"""Stream-level object stress + byte verification (high-level API).

Exercises QuicConnection.send_stream_data (the path aiomoqt uses) at
varying object sizes and target rates on a single unidirectional
stream. Each object carries a (seq:u64, payload_len:u32, fnv1a:u64)
prefix; receiver reassembles the contiguous stream, decodes per-object,
and verifies:

  - sent count == received count (byte conservation)
  - monotonic seq, no gaps, no duplicates
  - FNV1a of payload bytes matches the prefix on every object

Pass: all three checks are tight. Throughput / latency are reported
but not asserted (host variance).

Counted-byte payload (`bytes(i & 0xFF for i in range(N))`) makes
on-the-wire corruption identifiable from any 256-byte window.

Loopback in-process via asyncio connect/serve. No relay, no MoQT
framer — pure aiopquic transport.
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
import zlib
from typing import List, Tuple

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
ALPN = "hq-interop"

HEADER_FMT = "<QII"          # u64 seq, u32 payload_len, u32 crc32
HEADER_LEN = struct.calcsize(HEADER_FMT)


def _hash(buf: bytes) -> int:
    """Fast C-backed CRC32 — strong enough for transport corruption,
    cheap enough to run on every object at multi-Gbps rates."""
    return zlib.crc32(buf) & 0xFFFFFFFF


def _counted_pad(size: int) -> bytes:
    """`bytes(i & 0xFF)` — predictable + corruption-visible."""
    return bytes(i & 0xFF for i in range(size))


def _build_object(seq: int, payload: bytes) -> bytes:
    return struct.pack(HEADER_FMT, seq, len(payload), _hash(payload)) + payload


_port_counter = 36567


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_config(max_data: int = 1 << 28,
                   max_stream_data: int = 1 << 27) -> QuicConfiguration:
    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN],
        max_data=max_data, max_stream_data=max_stream_data,
    )
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config(max_data: int = 1 << 28,
                   max_stream_data: int = 1 << 27) -> QuicConfiguration:
    return QuicConfiguration(
        is_client=True, alpn_protocols=[ALPN],
        max_data=max_data, max_stream_data=max_stream_data,
    )


class _RxAggregator:
    """Reassembles per-stream bytes; decodes objects at boundaries."""
    __slots__ = ("buf", "objs", "bad_hash", "gaps", "dupes", "last_seq",
                 "ts_first", "ts_last")

    def __init__(self):
        self.buf = bytearray()
        self.objs = 0
        self.bad_hash = 0
        self.gaps = 0
        self.dupes = 0
        self.last_seq = -1
        self.ts_first = 0.0
        self.ts_last = 0.0

    def feed(self, data: bytes) -> None:
        if not self.ts_first:
            self.ts_first = time.monotonic()
        self.ts_last = time.monotonic()
        self.buf.extend(data)
        # Drain whole objects.
        while len(self.buf) >= HEADER_LEN:
            seq, plen, h = struct.unpack_from(HEADER_FMT, self.buf, 0)
            need = HEADER_LEN + plen
            if len(self.buf) < need:
                break
            payload = bytes(self.buf[HEADER_LEN:need])
            del self.buf[:need]
            if _hash(payload) != h:
                self.bad_hash += 1
            expected = self.last_seq + 1
            if seq < expected:
                self.dupes += 1
            elif seq > expected:
                self.gaps += seq - expected
            if seq > self.last_seq:
                self.last_seq = seq
            self.objs += 1


class _RxProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.rx_by_stream: dict[int, _RxAggregator] = {}
        self.fin_streams: set[int] = set()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            agg = self.rx_by_stream.get(event.stream_id)
            if agg is None:
                agg = _RxAggregator()
                self.rx_by_stream[event.stream_id] = agg
            agg.feed(event.data)
            if event.end_stream:
                self.fin_streams.add(event.stream_id)


async def _run_one(obj_size: int, target_obj_per_s: float,
                   duration_s: float) -> dict:
    """Returns a dict of measurements + pass/fail metrics."""
    port = _next_port()
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_config(),
        create_protocol=lambda quic, **kw: _RxProtocol(quic, **kw),
    )
    rx_proto = None
    pad = _counted_pad(max(0, obj_size - HEADER_LEN))

    sent_objs = 0
    sent_bytes = 0
    push_full_waits = 0

    t_start = 0.0
    elapsed = 0.0

    async def _publisher(client_proto):
        nonlocal sent_objs, sent_bytes, push_full_waits, t_start, elapsed
        sid = client_proto._quic.get_next_available_stream_id(
            is_unidirectional=True)
        next_t = time.monotonic()
        t_start = next_t
        end = next_t + duration_s
        interval = (1.0 / target_obj_per_s) if target_obj_per_s > 0 else 0.0
        while time.monotonic() < end:
            obj = _build_object(sent_objs, pad)
            try:
                client_proto._quic.send_stream_data(sid, obj,
                                                    end_stream=False)
                sent_objs += 1
                sent_bytes += len(obj)
            except BufferError:
                push_full_waits += 1
                await asyncio.sleep(0.0001)
                continue
            if interval > 0.0:
                next_t += interval
                delay = next_t - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                if (sent_objs & 0x3F) == 0:
                    await asyncio.sleep(0)
        client_proto._quic.send_stream_data(sid, b"", end_stream=True)
        elapsed = time.monotonic() - t_start
        return sid

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_config(),
        ) as client_proto:
            sid = await _publisher(client_proto)
            # Wait for delivery; bound by time, not just counts.
            drain_deadline = time.monotonic() + 5.0
            while time.monotonic() < drain_deadline:
                # Find the receiver protocol on the server side.
                if rx_proto is None:
                    engine = getattr(server, "_engine", None)
                    if engine is not None:
                        for pr in list(engine._protocols.values()):
                            if isinstance(pr, _RxProtocol):
                                rx_proto = pr
                                break
                if rx_proto is not None:
                    agg = rx_proto.rx_by_stream.get(sid)
                    if (agg is not None
                            and agg.objs >= sent_objs
                            and sid in rx_proto.fin_streams):
                        break
                await asyncio.sleep(0.01)
    finally:
        server.close()
        await asyncio.sleep(0.05)

    if rx_proto is None:
        engine = getattr(server, "_engine", None)
        if engine is not None:
            for pr in engine._protocols.values():
                if isinstance(pr, _RxProtocol):
                    rx_proto = pr
                    break

    if rx_proto is None:
        return {
            "obj_size": obj_size, "target_obj_per_s": target_obj_per_s,
            "sent_objs": sent_objs, "recv_objs": 0,
            "elapsed_s": round(elapsed, 3),
            "obj_per_s": 0, "Mbps": 0,
            "bad_hash": -1, "gaps": -1, "dupes": -1,
            "push_full_waits": push_full_waits,
            "pass": False, "reason": "rx protocol not found",
        }

    # Single-stream bench: pick the one stream with traffic.
    agg = next(iter(rx_proto.rx_by_stream.values()), _RxAggregator())
    recv_objs = agg.objs
    rx_elapsed = max(1e-6, agg.ts_last - agg.ts_first)
    obj_per_s = recv_objs / rx_elapsed if recv_objs else 0.0
    mbps = (recv_objs * obj_size * 8 / 1e6) / rx_elapsed if recv_objs else 0.0

    ok = (recv_objs == sent_objs
          and agg.bad_hash == 0
          and agg.gaps == 0
          and agg.dupes == 0)

    return {
        "obj_size": obj_size,
        "target_obj_per_s": target_obj_per_s,
        "sent_objs": sent_objs,
        "recv_objs": recv_objs,
        "elapsed_s": round(elapsed, 3),
        "obj_per_s": round(obj_per_s, 1),
        "Mbps": round(mbps, 1),
        "bad_hash": agg.bad_hash,
        "gaps": agg.gaps,
        "dupes": agg.dupes,
        "push_full_waits": push_full_waits,
        "pass": ok,
        "reason": "" if ok else (
            f"recv={recv_objs}/{sent_objs} bad_hash={agg.bad_hash} "
            f"gaps={agg.gaps} dupes={agg.dupes}"
        ),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.bench
@pytest.mark.parametrize("obj_size,target,duration", [
    # Small-object regime — Python+framer overhead dominates per object.
    (    64,    10_000,  2.0),
    (    64,   100_000,  2.0),
    (   256,    50_000,  2.0),
    (  1024,    10_000,  2.0),
    (  1024,   100_000,  2.0),
    (  1024,         0,  2.0),    # line rate
    # Mid-size — typical media-frame range.
    (  4096,    50_000,  2.0),
    (  4096,         0,  2.0),
    ( 16384,    10_000,  2.0),
    ( 16384,         0,  2.0),
    # Large-object regime — fewer but larger writes per second.
    ( 65536,     5_000,  2.0),
    ( 65536,         0,  2.0),
    (262144,         0,  2.0),
], ids=[
    "64B-10k",  "64B-100k",
    "256B-50k",
    "1K-10k",   "1K-100k",  "1K-line",
    "4K-50k",   "4K-line",
    "16K-10k",  "16K-line",
    "64K-5k",   "64K-line",
    "256K-line",
])
def test_bench_stream_object_stress(obj_size, target, duration, capsys):
    res = asyncio.run(_run_one(obj_size, float(target), duration))
    print(f"\n  obj={res['obj_size']:>6}B  target={res['target_obj_per_s']:>8,.0f}/s  "
          f"sent={res['sent_objs']:>9,}  recv={res['recv_objs']:>9,}  "
          f"rate={res['obj_per_s']:>10,.0f}/s  {res['Mbps']:>7,.1f} Mbps  "
          f"bad_hash={res['bad_hash']}  gaps={res['gaps']}  "
          f"dupes={res['dupes']}  full_waits={res['push_full_waits']}")
    assert res["pass"], res["reason"]
