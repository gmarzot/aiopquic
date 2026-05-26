"""Split-write stream-churn stress: header + object pattern.

Replicates aiomoqt's exact pattern that exposes a stream-loss bug:
  per uni stream, the publisher writes a SMALL header (5 B) via one
  send_stream_data call, then immediately writes K LARGER object bodies
  via SEPARATE send_stream_data calls, then FINs.

This differs from bench_stream_churn_highlevel which writes one full
object per send_stream_data call. aiomoqt's PublishedTrack always writes
the SubgroupHeader as a separate send_stream_data and then per-object
ObjectHeader+payload as another. Under high stream-churn rate (-g 120
in aiomoqt = ~4000 stream open/close per second at 500 Mbps), some
streams' data NEVER reaches the receiver.

Pass criterion: every byte sent on every stream is received byte-perfect
in offset order. Specifically, byte[0] of every received stream must
match the first byte the publisher wrote — the small-header byte.

Reproducer for: aiomoqt's "framer desync at high stream churn" issue.

Run: pytest tests/bench/bench_split_writes_stress.py -s -v
"""
from __future__ import annotations

import asyncio
import os
import time

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


def _server_config(event_ring_capacity: int | None = None) -> QuicConfiguration:
    cfg = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN],
        max_data=1 << 30, max_stream_data=1 << 22,
        event_ring_capacity=event_ring_capacity,
    )
    cfg.load_cert_chain(CERT_FILE, KEY_FILE)
    return cfg


def _client_config(event_ring_capacity: int | None = None) -> QuicConfiguration:
    return QuicConfiguration(
        is_client=True, alpn_protocols=[ALPN],
        max_data=1 << 30, max_stream_data=1 << 22,
        event_ring_capacity=event_ring_capacity,
    )


# Distinctive sentinel header. byte[0] = 0xA1 cannot match any of the
# pad bytes (0x00..0xFF cyclic) at offset 0 of the same stream, so a
# rx_buf[0] != 0xA1 is unambiguously a "header dropped" signature.
HEADER_SIZE = 5
HEADER_SENTINEL = b"\xA1\xB2\xC3\xD4\xE5"


class _StreamRx:
    __slots__ = ("buf", "fin")

    def __init__(self):
        self.buf = bytearray()
        self.fin = False


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
            rx.buf.extend(event.data)
            if event.end_stream:
                rx.fin = True


_port_counter = 38900


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


async def _run_split_writes(n_streams: int, objs_per_stream: int,
                             obj_size: int,
                             yield_per_stream: bool = False) -> dict:
    """Open n_streams uni streams. For each: write HEADER (5 B), then
    K objects of obj_size each via SEPARATE send_stream_data calls,
    FIN on the last.

    yield_per_stream: when True, await asyncio.sleep(0) after each
    stream's full data is queued. Empirically, this prevents the
    stream-loss bug — useful for confirming whether a candidate fix
    addresses the same race or papers over it.
    """
    port = _next_port()
    # Size the event ring to absorb high-stream-count bursts. 16K
    # default (0.3.5) is sized for normal workloads; 2000-stream
    # stress saturates it (120K events in flight) → stream loss
    # from rx_event_ring drops. 262144 mirrors the pre-0.3.5 shared
    # default and gives the test the headroom it was designed against.
    erc = 262144 if n_streams >= 1000 else None
    server = await serve(
        "127.0.0.1", port,
        configuration=_server_config(event_ring_capacity=erc),
        create_protocol=lambda quic, **kw: _RxProtocol(quic, **kw),
    )

    pad = bytes(i & 0xFF for i in range(obj_size))
    sids: list[int] = []
    full_waits = 0
    t_start = 0.0
    t_send_done = 0.0
    captured: dict[int, _StreamRx] = {}
    expected_per_stream = HEADER_SIZE + objs_per_stream * obj_size

    try:
        async with connect(
            "127.0.0.1", port,
            configuration=_client_config(event_ring_capacity=erc),
        ) as client:
            t_start = time.monotonic()
            for stream_idx in range(n_streams):
                sid = client._quic.get_next_available_stream_id(
                    is_unidirectional=True)
                sids.append(sid)

                # WRITE 1: small header
                while True:
                    try:
                        client._quic.send_stream_data(
                            sid, HEADER_SENTINEL, end_stream=False)
                        break
                    except BufferError:
                        full_waits += 1
                        await asyncio.sleep(0.0001)

                # WRITE 2..K+1: K full objects, FIN on last
                for k in range(objs_per_stream):
                    is_last = (k == objs_per_stream - 1)
                    while True:
                        try:
                            client._quic.send_stream_data(
                                sid, pad, end_stream=is_last)
                            break
                        except BufferError:
                            full_waits += 1
                            await asyncio.sleep(0.0001)

                if yield_per_stream:
                    await asyncio.sleep(0)
                elif (stream_idx & 0x1F) == 0:
                    await asyncio.sleep(0)
            t_send_done = time.monotonic()

            # Drain — wait for all streams to FIN.
            engine = getattr(server, "_engine", None)
            drain_deadline = time.monotonic() + 30.0
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
                        and len(rx_proto.streams[s].buf) >= expected_per_stream
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
            # Forensics from the atomic Cython send primitive.
            transport = client._quic._transport
            send_calls = getattr(transport, "send_calls", -1)
            busy_evt = getattr(transport, "send_busy_event_ring", -1)
            busy_stream = getattr(transport, "send_busy_stream_ring", -1)
            alloc_fail = getattr(transport, "send_alloc_fail", -1)
            worker_mark_active = getattr(
                transport, "worker_mark_active_processed", -1)
            worker_prepare = getattr(
                transport, "worker_prepare_to_send_calls", -1)
            worker_pulled = getattr(
                transport, "worker_prepare_to_send_pulled_bytes", -1)
            # ROOT-CAUSE COUNTERS: receiver-side rx_event_ring overflow drops.
            # The receiver is the SERVER side in this test; client and
            # server share the same _engine in single-process bench.
            # Pull from the server's TransportContext if accessible.
            srv_drops = -1
            srv_drops_data = -1
            if engine is not None:
                for pr in engine._protocols.values():
                    srv_t = getattr(pr, "_transport", None)
                    if srv_t is not None:
                        srv_drops = getattr(
                            srv_t, "worker_rx_event_drops", -1)
                        srv_drops_data = getattr(
                            srv_t, "worker_rx_event_drops_stream_data",
                            -1)
                        break
    finally:
        server.close()
        await asyncio.sleep(0.05)

    elapsed = max(1e-6, t_send_done - t_start)
    streams_per_s = n_streams / elapsed
    bytes_per_stream = expected_per_stream

    streams_complete = 0
    streams_short = 0
    streams_no_header = 0
    streams_bad_payload = 0
    streams_no_fin = 0
    streams_missing = 0
    missing_sids: list[int] = []
    examples_no_header: list[tuple[int, str]] = []
    examples_short: list[tuple[int, int]] = []
    examples_no_fin: list[tuple[int, int]] = []
    for sid in sids:
        rx = captured.get(sid)
        if rx is None:
            streams_missing += 1
            missing_sids.append(sid)
            continue
        if not rx.fin:
            streams_no_fin += 1
            if len(examples_no_fin) < 5:
                examples_no_fin.append((sid, len(rx.buf)))
            continue
        n = len(rx.buf)
        if n != bytes_per_stream:
            streams_short += 1
            if len(examples_short) < 5:
                examples_short.append((sid, n))
            continue
        if bytes(rx.buf[:HEADER_SIZE]) != HEADER_SENTINEL:
            streams_no_header += 1
            if len(examples_no_header) < 5:
                examples_no_header.append(
                    (sid, bytes(rx.buf[:8]).hex())
                )
            continue
        ok = True
        off = HEADER_SIZE
        for _ in range(objs_per_stream):
            if bytes(rx.buf[off:off + obj_size]) != pad:
                ok = False
                break
            off += obj_size
        if not ok:
            streams_bad_payload += 1
            continue
        streams_complete += 1

    bps = (n_streams * bytes_per_stream * 8 / 1e6) / elapsed

    # Summarize the missing-sid cluster pattern. Uni-stream stride is 4.
    runs = []
    if missing_sids:
        start = missing_sids[0]
        prev = missing_sids[0]
        for sid in missing_sids[1:]:
            if sid == prev + 4:
                prev = sid
            else:
                runs.append(((prev - start) // 4) + 1)
                start = sid
                prev = sid
        runs.append(((prev - start) // 4) + 1)

    return {
        "n_streams": n_streams,
        "objs_per_stream": objs_per_stream,
        "obj_size": obj_size,
        "elapsed_s": round(elapsed, 3),
        "streams_per_s": round(streams_per_s, 0),
        "Mbps": round(bps, 1),
        "streams_complete": streams_complete,
        "streams_short": streams_short,
        "streams_no_header": streams_no_header,
        "streams_bad_payload": streams_bad_payload,
        "streams_no_fin": streams_no_fin,
        "streams_missing": streams_missing,
        "missing_sids_first8": missing_sids[:8],
        "missing_sids_last8": missing_sids[-8:],
        "missing_runs": runs,
        "missing_runs_largest": max(runs) if runs else 0,
        "no_header_examples": examples_no_header,
        "short_examples": examples_short,
        "no_fin_examples": examples_no_fin,
        "full_waits": full_waits,
        "send_calls": send_calls,
        "send_busy_event_ring": busy_evt,
        "send_busy_stream_ring": busy_stream,
        "send_alloc_fail": alloc_fail,
        "worker_mark_active": worker_mark_active,
        "worker_prepare_calls": worker_prepare,
        "worker_pulled_bytes": worker_pulled,
        "rx_event_drops": srv_drops,
        "rx_event_drops_stream_data": srv_drops_data,
        "yield_per_stream": yield_per_stream,
        "pass": streams_complete == n_streams,
    }


def _print(res):
    print(
        f"\n  {res['n_streams']:>4}s × {res['objs_per_stream']:>3}o × "
        f"{res['obj_size']:>5}B yield={int(res['yield_per_stream'])}  "
        f"streams/s={res['streams_per_s']:>7,.0f}  "
        f"{res['Mbps']:>7,.1f} Mbps"
    )
    print(
        f"  complete={res['streams_complete']}/{res['n_streams']}  "
        f"missing={res['streams_missing']}  no_fin={res['streams_no_fin']}  "
        f"short={res['streams_short']}  no_header={res['streams_no_header']}  "
        f"full_waits={res['full_waits']}"
    )
    print(
        f"  cython send: calls={res['send_calls']}  "
        f"busy_event_ring={res['send_busy_event_ring']}  "
        f"busy_stream_ring={res['send_busy_stream_ring']}  "
        f"alloc_fail={res['send_alloc_fail']}"
    )
    print(
        f"  worker:      mark_active={res['worker_mark_active']}  "
        f"prepare_calls={res['worker_prepare_calls']}  "
        f"pulled_bytes={res['worker_pulled_bytes']:,}"
    )
    print(
        f"  RX-OVERFLOW: rx_event_drops={res['rx_event_drops']}  "
        f"of_which_stream_data={res['rx_event_drops_stream_data']}"
    )
    if res['missing_sids_first8']:
        print(
            f"  missing range: first={res['missing_sids_first8']} "
            f"last={res['missing_sids_last8']}"
        )
        print(
            f"  missing-cluster runs={len(res['missing_runs'])} "
            f"largest_consecutive={res['missing_runs_largest']} "
            f"first_runs={res['missing_runs'][:10]}"
        )
    if res['no_fin_examples']:
        print(
            f"  no_fin examples (sid, partial_len): "
            f"{res['no_fin_examples']}"
        )


@pytest.mark.bench
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size,yield_per", [
    # Mirror aiomoqt -g 120 / -P 2 / -s 1024 cadence.
    # objs_per_stream=60 = group_size 120 / num_subgroups 2.
    (   100,  60, 1024, False),
    (   500,  60, 1024, False),
    (  1000,  60, 1024, False),
    (  2000,  60, 1024, False),
    (  1000,   1, 1024, False),
    # With per-stream yield (workaround / control case)
    (   500,  60, 1024, True),
    (  1000,  60, 1024, True),
], ids=[
    "100s-60o-1K", "500s-60o-1K", "1000s-60o-1K", "2000s-60o-1K",
    "1000s-1o-1K",
    "500s-60o-1K-yield", "1000s-60o-1K-yield",
])
def test_bench_split_writes_stress(n_streams, objs_per_stream, obj_size,
                                     yield_per):
    """Reproducer for the aiomoqt-observed split-write stream-loss bug.
    With yield_per=True, the per-stream asyncio yield is the empirical
    workaround. Comparing the two cases narrows the race."""
    res = asyncio.run(_run_split_writes(n_streams, objs_per_stream,
                                          obj_size,
                                          yield_per_stream=yield_per))
    _print(res)
    assert res['pass'], (
        f"streams_complete={res['streams_complete']}/{res['n_streams']} "
        f"missing={res['streams_missing']} "
        f"no_fin={res['streams_no_fin']} "
        f"no_header={res['streams_no_header']} "
        f"short={res['streams_short']} "
        f"largest_consecutive_missing={res['missing_runs_largest']}"
    )
