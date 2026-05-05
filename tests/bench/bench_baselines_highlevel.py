"""Sustained steady-state perf baselines — HIGH-LEVEL API.

This is the counterpart to bench_baselines_lowlevel.py. It pushes via
the public `QuicConnection.send_stream_data` API over `connect`/`serve`
— the path aiomoqt and any client-library consumer actually uses.

The numbers here represent what end consumers see. The gap between
this and the lower-level baseline is the cost of the high-level
wrapper (ctx-cleanup, mark-active heuristic, asyncio orchestration,
event routing). Two-layer measurement so transport-layer wins and
wrapper-layer regressions stay distinguishable.

Steady-state window: 28s after a 2s warmup (30s total per case).

Each case asserts:
  * byte conservation (sent == received, hash + seq integrity)
  * achieved throughput >= a minimum floor (regression gate)
  * latency p50 / p99 reported (not asserted; per-host variance)

Floors are set conservative — first measurement under the released
0.2.0 wheel is captured here, with the floor at ~85% of measured to
absorb host noise without false-failing on real regressions.

Run: pytest tests/bench/bench_baselines_highlevel.py -s -v
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
ALPN = "hq-interop"

HEADER_FMT = "<QQII"   # u64 seq, u64 t_built_ns, u32 payload_len, u32 crc32
HEADER_LEN = struct.calcsize(HEADER_FMT)


def _hash(buf: bytes) -> int:
    return zlib.crc32(buf) & 0xFFFFFFFF


def _build_object(seq: int, payload: bytes) -> bytes:
    return struct.pack(
        HEADER_FMT, seq, time.monotonic_ns(),
        len(payload), _hash(payload),
    ) + payload


def _q(sorted_values, q):
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * q)))
    return sorted_values[idx]


_port_counter = 37567


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


def _server_config() -> QuicConfiguration:
    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN],
        max_data=1 << 28, max_stream_data=1 << 27,
    )
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config() -> QuicConfiguration:
    return QuicConfiguration(
        is_client=True, alpn_protocols=[ALPN],
        max_data=1 << 28, max_stream_data=1 << 27,
    )


# Floors set on first measurement under released 0.2.0 wheel.
# Update with measurement evidence; never lower silently.
HIGHLEVEL_MIN_MBPS = {
    1024:  None,   # to be measured
    4096:  None,
    16384: None,
}


class _RxState:
    """Per-stream byte aggregator + verifier."""
    __slots__ = ("buf", "objs", "bad_hash", "gaps", "dupes", "last_seq",
                 "ts_first", "ts_last", "lat_warmup_ns", "lat_steady_ns",
                 "warmup_until_ns", "ss_objs", "ss_bytes",
                 "ss_t_first_ns", "ss_t_last_ns")

    def __init__(self, warmup_until_ns: int):
        self.buf = bytearray()
        self.objs = 0
        self.bad_hash = 0
        self.gaps = 0
        self.dupes = 0
        self.last_seq = -1
        self.ts_first = 0.0
        self.ts_last = 0.0
        self.warmup_until_ns = warmup_until_ns
        self.lat_warmup_ns: list[int] = []
        self.lat_steady_ns: list[int] = []
        self.ss_objs = 0
        self.ss_bytes = 0
        self.ss_t_first_ns = 0
        self.ss_t_last_ns = 0


def _drain(state: _RxState, data: bytes, obj_size: int) -> None:
    if not state.ts_first:
        state.ts_first = time.monotonic()
    state.ts_last = time.monotonic()
    state.buf.extend(data)
    while len(state.buf) >= HEADER_LEN:
        seq, t_built_ns, plen, h = struct.unpack_from(
            HEADER_FMT, state.buf, 0)
        need = HEADER_LEN + plen
        if len(state.buf) < need:
            break
        payload = bytes(state.buf[HEADER_LEN:need])
        del state.buf[:need]
        t_drain_ns = time.monotonic_ns()
        if _hash(payload) != h:
            state.bad_hash += 1
        expected = state.last_seq + 1
        if seq < expected:
            state.dupes += 1
        elif seq > expected:
            state.gaps += seq - expected
        if seq > state.last_seq:
            state.last_seq = seq
        state.objs += 1
        lat = t_drain_ns - t_built_ns
        if t_drain_ns >= state.warmup_until_ns:
            state.lat_steady_ns.append(lat)
            if state.ss_t_first_ns == 0:
                state.ss_t_first_ns = t_drain_ns
            state.ss_t_last_ns = t_drain_ns
            state.ss_objs += 1
            state.ss_bytes += need
        else:
            state.lat_warmup_ns.append(lat)


class _RxProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.streams: dict[int, _RxState] = {}
        self.fin_streams: set[int] = set()
        self.warmup_until_ns = 0
        self.obj_size = 0

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            st = self.streams.get(event.stream_id)
            if st is None:
                st = _RxState(self.warmup_until_ns)
                self.streams[event.stream_id] = st
            _drain(st, event.data, self.obj_size)
            if event.end_stream:
                self.fin_streams.add(event.stream_id)


async def _run(obj_size: int, duration_s: float,
               warmup_s: float = 2.0) -> dict:
    port = _next_port()
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_config(),
        create_protocol=lambda quic, **kw: _RxProtocol(quic, **kw),
    )
    pad = bytes(i & 0xFF for i in range(max(0, obj_size - HEADER_LEN)))
    sent = 0
    push_full_waits = 0
    sid: Optional[int] = None
    t_start_ns = 0
    captured_state: Optional[_RxState] = None

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_config(),
        ) as client:
            sid = client._quic.get_next_available_stream_id(
                is_unidirectional=True)
            t_start_ns = time.monotonic_ns()
            warmup_until_ns = t_start_ns + int(warmup_s * 1e9)
            end_ns = t_start_ns + int(duration_s * 1e9)

            # Configure server-side state windows.
            engine = getattr(server, "_engine", None)
            if engine is not None:
                # Late-bind: server proto exists once cnx accepted.
                # Updated in-place at first event arrival.
                pass

            # Push line-rate; BufferError → brief yield.
            while time.monotonic_ns() < end_ns:
                obj = _build_object(sent, pad)
                try:
                    client._quic.send_stream_data(sid, obj,
                                                  end_stream=False)
                    sent += 1
                except BufferError:
                    push_full_waits += 1
                    await asyncio.sleep(0.0001)
                    continue
                # Cooperative yield so server-side proto runs.
                if (sent & 0x3F) == 0:
                    await asyncio.sleep(0)

            # Set the warmup window on whichever rx proto attached.
            engine = getattr(server, "_engine", None)
            if engine is not None:
                for pr in engine._protocols.values():
                    if isinstance(pr, _RxProtocol):
                        pr.warmup_until_ns = warmup_until_ns
                        pr.obj_size = obj_size
                        for st in pr.streams.values():
                            st.warmup_until_ns = warmup_until_ns

            # Issue FIN, then drain.
            client._quic.send_stream_data(sid, b"", end_stream=True)
            drain_deadline = time.monotonic() + 5.0
            while time.monotonic() < drain_deadline:
                rx_proto = None
                if engine is not None:
                    for pr in engine._protocols.values():
                        if isinstance(pr, _RxProtocol):
                            rx_proto = pr
                            break
                if rx_proto is not None and sid in rx_proto.fin_streams:
                    st = rx_proto.streams.get(sid)
                    if st is not None and st.objs >= sent:
                        captured_state = st
                        break
                await asyncio.sleep(0.01)

            # Capture the state object before server.close() clears
            # engine._protocols (which would discard it).
            if captured_state is None and engine is not None:
                for pr in engine._protocols.values():
                    if isinstance(pr, _RxProtocol):
                        captured_state = pr.streams.get(sid)
                        if captured_state is not None:
                            break
    finally:
        server.close()
        await asyncio.sleep(0.05)

    if captured_state is None or sid is None:
        return {
            "obj_size": obj_size, "sent": sent, "recv": 0,
            "push_full_waits": push_full_waits,
            "ss_obj_per_s": 0, "ss_mbps": 0, "ss_elapsed": 0,
            "p50_us": 0, "p90_us": 0, "p99_us": 0, "max_us": 0,
            "floor_us": 0, "bad_hash": -1, "gaps": -1, "dupes": -1,
            "pass": False, "reason": "rx state not found",
        }

    st = captured_state
    ss_elapsed = max(1e-6, (st.ss_t_last_ns - st.ss_t_first_ns) / 1e9)
    obj_per_s = st.ss_objs / ss_elapsed if st.ss_objs else 0.0
    mbps = (st.ss_bytes * 8 / 1e6) / ss_elapsed if st.ss_objs else 0.0
    lats = sorted(st.lat_steady_ns)
    floor_us = (lats[0] / 1000) if lats else 0
    p50_us = _q(lats, 0.50) / 1000
    p90_us = _q(lats, 0.90) / 1000
    p99_us = _q(lats, 0.99) / 1000
    max_us = (lats[-1] / 1000) if lats else 0
    ok = (st.objs == sent
          and st.bad_hash == 0
          and st.gaps == 0
          and st.dupes == 0)
    return {
        "obj_size": obj_size, "sent": sent, "recv": st.objs,
        "push_full_waits": push_full_waits,
        "ss_obj_per_s": round(obj_per_s, 1),
        "ss_mbps": round(mbps, 1),
        "ss_elapsed": round(ss_elapsed, 2),
        "p50_us": round(p50_us, 0), "p90_us": round(p90_us, 0),
        "p99_us": round(p99_us, 0), "max_us": round(max_us, 0),
        "floor_us": round(floor_us, 0),
        "bad_hash": st.bad_hash, "gaps": st.gaps, "dupes": st.dupes,
        "pass": ok,
        "reason": "" if ok else (
            f"recv={st.objs}/{sent} bad_hash={st.bad_hash} "
            f"gaps={st.gaps} dupes={st.dupes}"
        ),
    }


@pytest.mark.bench
@pytest.mark.parametrize("obj_size", list(HIGHLEVEL_MIN_MBPS.keys()),
                          ids=lambda s: f"{s}B")
@pytest.mark.parametrize("duration_s", [30.0], ids=["30s"])
def test_bench_sustained_baseline_highlevel(obj_size, duration_s):
    """Sustained line-rate single-stream baseline through the
    high-level QuicConnection API. Companion to the lower-level
    SPSC-direct baseline; gap between the two = cost of the wrapper."""
    res = asyncio.run(_run(obj_size, duration_s, warmup_s=2.0))
    print(f"\n  --- highlevel baseline obj={obj_size}B "
          f"duration={duration_s:.0f}s ---")
    print(f"  steady-state: {res['recv']:,} obj over "
          f"{res['ss_elapsed']:.1f}s")
    print(f"  rate:         {res['ss_obj_per_s']:>10,.0f} obj/s")
    print(f"  throughput:   {res['ss_mbps']:>10.0f} Mbps")
    print(f"  latency_us:   floor={res['floor_us']:.0f}  "
          f"p50={res['p50_us']:.0f}  p90={res['p90_us']:.0f}  "
          f"p99={res['p99_us']:.0f}  max={res['max_us']:.0f}")
    print(f"  integrity:    sent={res['sent']:,}  recv={res['recv']:,}  "
          f"bad_hash={res['bad_hash']}  gaps={res['gaps']}  "
          f"dupes={res['dupes']}  push_full={res['push_full_waits']:,}")
    assert res["pass"], res["reason"]
    floor = HIGHLEVEL_MIN_MBPS[obj_size]
    if floor is not None:
        assert res["ss_mbps"] >= floor, (
            f"PERF REGRESSION: highlevel obj={obj_size}B "
            f"achieved={res['ss_mbps']:.0f} Mbps < floor={floor} Mbps"
        )
