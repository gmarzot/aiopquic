"""WebTransport split-write stream-churn stress: header + object pattern.

WT analogue of `bench_split_writes_stress.py`. Replicates the exact
write shape aiomoqt's `PublishedTrack._generate_subgroup` uses on a
WT data stream:

  per uni WT stream, the publisher writes a SMALL header (5 B) via
  one send_stream_data call, then K LARGER object bodies via SEPARATE
  send_stream_data calls, then FINs.

aiomoqt's `mp-loopback` over WebTransport at 250 Mbps shows
parse-rejects with the classic "missing 5-byte SubgroupHeader"
signature: head_hex starts with `01 09 20 c0 ...` decodable cleanly
as ObjectHeader at offset 0 + valid `"<group>.<obj>|"` payload
prefix. The aiopquic raw-QUIC stress is CLEAN at the same shape
(committed v0.3.0). This test isolates whether the WT TX path
(pull-model `tx_send_stream` / per-stream sc->tx ring) loses bytes
under the same churn that raw QUIC handles cleanly. (The 0.3.5
push-API removal means there is no longer a legacy `picoquic_add_to_stream`
fallback for stream data; this comment retains the historical context.)

Pass criterion: every byte sent on every stream is received
byte-perfect in offset order. byte[0] of every received stream must
match the first byte the publisher wrote — the small-header byte.

Run: pytest tests/bench/bench_wt_split_writes_stress.py -s -v
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from aiopquic.asyncio.webtransport import (
    connect_webtransport, serve_webtransport,
    WebTransportNewStream, WebTransportStreamDataReceived,
)


CERTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "third_party", "picoquic", "certs",
)
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")


pytestmark = pytest.mark.skipif(
    not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)),
    reason="picoquic certs not found",
)


HEADER_SIZE = 5
# Mirror aiomoqt's SubgroupHeader byte 0 (0x10 = SUBGROUP_HEADER_BASE for
# d14). Use a 5-byte header byte 1 onwards from aiomoqt-distinct values
# so we can still spot a missing-header signature unambiguously.
HEADER_SENTINEL = b"\x10\xA2\xC3\xD4\xE5"


_port_counter = 39400


def _next_port() -> int:
    global _port_counter
    _port_counter += 1
    return _port_counter


async def _run_split_writes_wt(n_streams: int, objs_per_stream: int,
                                 obj_size: int,
                                 yield_per_stream: bool = False) -> dict:
    """Open n_streams uni WT streams. For each: write HEADER (5 B), then
    K objects of obj_size each via SEPARATE send_stream_data calls,
    FIN on the last."""

    port = _next_port()
    pad = bytes(i & 0xFF for i in range(obj_size))
    expected_per_stream = HEADER_SIZE + objs_per_stream * obj_size

    captured: dict[int, bytearray] = {}
    captured_fin: dict[int, bool] = {}
    new_stream_count = 0
    server_collect_done = asyncio.Event()
    server_session_ref: list = []

    async def _collect_stream(session, sid):
        buf = bytearray()
        try:
            async for sev in session.receive_stream_data(sid):
                if isinstance(sev, WebTransportStreamDataReceived):
                    buf.extend(sev.data)
                    if sev.end_stream:
                        captured[sid] = buf
                        captured_fin[sid] = True
                        if len(captured) == n_streams:
                            server_collect_done.set()
                        return
        except Exception:
            captured[sid] = buf
            captured_fin[sid] = False

    async def server_handler(session):
        server_session_ref.append(session)

        async def _accept_streams():
            nonlocal new_stream_count
            async for ev in session.events():
                if isinstance(ev, WebTransportNewStream):
                    new_stream_count += 1
                    asyncio.create_task(
                        _collect_stream(session, ev.stream_id))
        asyncio.create_task(_accept_streams())

    server = await serve_webtransport(
        "127.0.0.1", port, "/wt",
        handler=server_handler,
        cert_file=CERT_FILE, key_file=KEY_FILE,
    )

    sids: list[int] = []
    full_waits = 0
    t_start = 0.0
    t_send_done = 0.0

    try:
        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            t_start = time.monotonic()
            for stream_idx in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                sids.append(sid)

                # WRITE 1: small header (5 B), no FIN
                while True:
                    try:
                        wt.send_stream_data(
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
                            wt.send_stream_data(
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

            # Drain — wait for all streams to FIN on the server.
            try:
                await asyncio.wait_for(
                    server_collect_done.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
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
        if not captured_fin.get(sid, False):
            streams_no_fin += 1
            if len(examples_no_fin) < 5:
                examples_no_fin.append((sid, len(rx)))
            continue
        n = len(rx)
        if n != bytes_per_stream:
            streams_short += 1
            if len(examples_short) < 5:
                examples_short.append((sid, n))
            continue
        if bytes(rx[:HEADER_SIZE]) != HEADER_SENTINEL:
            streams_no_header += 1
            if len(examples_no_header) < 5:
                examples_no_header.append(
                    (sid, bytes(rx[:16]).hex())
                )
            continue
        ok = True
        off = HEADER_SIZE
        for _ in range(objs_per_stream):
            if bytes(rx[off:off + obj_size]) != pad:
                ok = False
                break
            off += obj_size
        if not ok:
            streams_bad_payload += 1
            continue
        streams_complete += 1

    bps = (n_streams * bytes_per_stream * 8 / 1e6) / elapsed

    runs = []
    if missing_sids:
        # WT uni stream IDs are 4n+2 (client) or 4n+3 (server) depending
        # on side. Use a relaxed group-by-stride detection: any gap > 4.
        sorted_sids = sorted(missing_sids)
        start = sorted_sids[0]
        prev = start
        for sid in sorted_sids[1:]:
            if sid - prev <= 4:
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
        "new_stream_events": new_stream_count,
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
        f"missing={res['streams_missing']}  "
        f"no_fin={res['streams_no_fin']}  "
        f"short={res['streams_short']}  "
        f"no_header={res['streams_no_header']}  "
        f"bad_payload={res['streams_bad_payload']}  "
        f"full_waits={res['full_waits']}"
    )
    print(
        f"  WT events: new_stream={res['new_stream_events']}"
    )
    if res['no_header_examples']:
        print(
            f"  no_header examples (sid, head_hex): "
            f"{res['no_header_examples']}"
        )
    if res['short_examples']:
        print(
            f"  short examples (sid, got_bytes): "
            f"{res['short_examples']}"
        )
    if res['missing_sids_first8']:
        print(
            f"  missing range: first={res['missing_sids_first8']} "
            f"last={res['missing_sids_last8']}"
        )
        print(
            f"  missing-cluster runs={len(res['missing_runs'])} "
            f"largest_consecutive={res['missing_runs_largest']}"
        )


def _server_subproc_entry(port: int, n_streams: int,
                            objs_per_stream: int, obj_size: int,
                            ready_event, summary_path: str):
    """Subprocess entrypoint for the receiver/server side.

    Runs its own asyncio loop and TransportContext — separate eventfd,
    separate SPSC rings — exactly like aiomoqt's mp-loopback shape.
    Writes per-stream byte counts to summary_path (pickle) when all
    streams have FIN'd or after a 30s timeout. Parent reads the file
    after joining."""
    import asyncio as _asyncio
    from aiopquic.asyncio.webtransport import (
        serve_webtransport,
        WebTransportNewStream as _NS,
        WebTransportStreamDataReceived as _SDR,
    )

    captured: dict[int, bytearray] = {}
    captured_fin: dict[int, bool] = {}
    new_stream_count = [0]
    server_done = _asyncio.Event() if False else None

    async def _main():
        nonlocal server_done
        server_done = _asyncio.Event()

        async def _collect_stream(session, sid):
            buf = bytearray()
            try:
                async for sev in session.receive_stream_data(sid):
                    if isinstance(sev, _SDR):
                        buf.extend(sev.data)
                        if sev.end_stream:
                            captured[sid] = buf
                            captured_fin[sid] = True
                            if len(captured) == n_streams:
                                server_done.set()
                            return
            except Exception:
                captured[sid] = buf
                captured_fin[sid] = False

        async def server_handler(session):
            async def _accept_streams():
                async for ev in session.events():
                    if isinstance(ev, _NS):
                        new_stream_count[0] += 1
                        _asyncio.create_task(
                            _collect_stream(session, ev.stream_id))
            _asyncio.create_task(_accept_streams())

        server = await serve_webtransport(
            "127.0.0.1", port, "/wt",
            handler=server_handler,
            cert_file=CERT_FILE, key_file=KEY_FILE,
        )
        try:
            ready_event.set()
            try:
                await _asyncio.wait_for(server_done.wait(), timeout=30.0)
            except _asyncio.TimeoutError:
                pass
        finally:
            server.close()
            await _asyncio.sleep(0.05)
            import pickle as _pickle
            summary = {
                "captured": {sid: bytes(buf)
                               for sid, buf in captured.items()},
                "captured_fin": dict(captured_fin),
                "new_stream_count": new_stream_count[0],
            }
            with open(summary_path, "wb") as _f:
                _pickle.dump(summary, _f)

    try:
        _asyncio.run(_main())
    except BaseException:
        pass
    # Force-exit: orphaned tasks (server.events() async-for) would
    # otherwise prevent asyncio.run from returning. The summary is
    # already on disk via the file write — parent reads it after join.
    os._exit(0)


async def _run_split_writes_wt_mp(n_streams: int, objs_per_stream: int,
                                    obj_size: int) -> dict:
    """Multi-process variant of _run_split_writes_wt.

    Server runs in a subprocess (separate asyncio loop, separate
    TransportContext, separate eventfd, separate SPSC rings). Client
    runs in the test process. UDP loopback between two real sockets
    on different file descriptors — same shape as aiomoqt mp-loopback."""
    import multiprocessing as _mp
    # 'fork' avoids the re-import + re-run-of-module-level-code pytest
    # spawns under default 'spawn'/'forkserver' methods. Linux-only;
    # adjust if running on macOS where 'fork' is unsafe with asyncio.
    try:
        ctx = _mp.get_context("fork")
    except ValueError:
        ctx = _mp.get_context()
    port = _next_port()
    pad = bytes(i & 0xFF for i in range(obj_size))
    expected_per_stream = HEADER_SIZE + objs_per_stream * obj_size

    ready_event = ctx.Event()
    import tempfile as _tempfile
    _f = _tempfile.NamedTemporaryFile(prefix="wt_mp_summary_",
                                          suffix=".pkl", delete=False)
    summary_path = _f.name
    _f.close()
    proc = ctx.Process(
        target=_server_subproc_entry,
        args=(port, n_streams, objs_per_stream, obj_size, ready_event,
              summary_path),
        daemon=True,
    )
    proc.start()

    try:
        # Wait for server to bind (subprocess does asyncio.run + TLS
        # listener setup, which takes >2s on cold start).
        for _ in range(500):
            if ready_event.is_set():
                break
            await asyncio.sleep(0.02)
        if not ready_event.is_set():
            raise RuntimeError("server subproc never signaled ready")

        sids: list[int] = []
        full_waits = 0
        t_start = 0.0
        t_send_done = 0.0

        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:
            t_start = time.monotonic()
            for stream_idx in range(n_streams):
                sid = await wt.create_stream(bidir=False)
                sids.append(sid)

                while True:
                    try:
                        wt.send_stream_data(
                            sid, HEADER_SENTINEL, end_stream=False)
                        break
                    except BufferError:
                        full_waits += 1
                        await asyncio.sleep(0.0001)

                for k in range(objs_per_stream):
                    is_last = (k == objs_per_stream - 1)
                    while True:
                        try:
                            wt.send_stream_data(
                                sid, pad, end_stream=is_last)
                            break
                        except BufferError:
                            full_waits += 1
                            await asyncio.sleep(0.0001)

                if (stream_idx & 0x1F) == 0:
                    await asyncio.sleep(0)

            t_send_done = time.monotonic()

            # Wait for the subproc to finish by polling for summary
            # file existence + non-zero size. Subprocess writes the
            # file then os._exit's, so we don't need cross-process
            # synchronization primitives that share state with the
            # (now-dead) child.
            import os.path as _osp
            proc_deadline = time.monotonic() + 30.0
            while time.monotonic() < proc_deadline:
                if (_osp.exists(summary_path)
                        and os.path.getsize(summary_path) > 0):
                    break
                await asyncio.sleep(0.1)
    finally:
        # Subproc os._exit's after writing summary file, so join is fast.
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)

    summary = {}
    try:
        import pickle as _pickle
        with open(summary_path, "rb") as _f:
            summary = _pickle.load(_f)
        os.unlink(summary_path)
    except Exception:
        pass
    captured: dict[int, bytes] = summary.get("captured", {})
    captured_fin: dict[int, bool] = summary.get("captured_fin", {})
    new_stream_count = summary.get("new_stream_count", -1)

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
        if not captured_fin.get(sid, False):
            streams_no_fin += 1
            if len(examples_no_fin) < 5:
                examples_no_fin.append((sid, len(rx)))
            continue
        n = len(rx)
        if n != bytes_per_stream:
            streams_short += 1
            if len(examples_short) < 5:
                examples_short.append((sid, n))
            continue
        if rx[:HEADER_SIZE] != HEADER_SENTINEL:
            streams_no_header += 1
            if len(examples_no_header) < 5:
                examples_no_header.append(
                    (sid, rx[:16].hex())
                )
            continue
        ok = True
        off = HEADER_SIZE
        for _ in range(objs_per_stream):
            if rx[off:off + obj_size] != pad:
                ok = False
                break
            off += obj_size
        if not ok:
            streams_bad_payload += 1
            continue
        streams_complete += 1

    bps = (n_streams * bytes_per_stream * 8 / 1e6) / elapsed

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
        "missing_runs": [],
        "missing_runs_largest": 0,
        "no_header_examples": examples_no_header,
        "short_examples": examples_short,
        "no_fin_examples": examples_no_fin,
        "full_waits": full_waits,
        "new_stream_events": new_stream_count,
        "yield_per_stream": False,
        "pass": streams_complete == n_streams,
    }


@pytest.mark.bench
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size,yield_per", [
    # Mirror aiomoqt -g 120 / -P 2 / -s 1024 cadence on WT.
    (   50,  60, 1024, False),
    (  100,  60, 1024, False),
    (  500,  60, 1024, False),
    ( 1000,  60, 1024, False),
    ( 1000,   1, 1024, False),
    (  500,  60, 1024, True),
], ids=[
    "50s-60o-1K", "100s-60o-1K", "500s-60o-1K", "1000s-60o-1K",
    "1000s-1o-1K",
    "500s-60o-1K-yield",
])
def test_bench_wt_split_writes_stress(n_streams, objs_per_stream, obj_size,
                                        yield_per):
    """Reproducer for the desync seen at the aiomoqt mp-loopback WT
    layer. If aiopquic-alone WT shows the same missing-header signature,
    fix scope is aiopquic. If aiopquic-alone WT is byte-perfect, the
    bug is at the aiomoqt protocol layer (StreamChain reassembly,
    parser, or session demux)."""
    res = asyncio.run(_run_split_writes_wt(n_streams, objs_per_stream,
                                             obj_size,
                                             yield_per_stream=yield_per))
    _print(res)
    assert res['pass'], (
        f"streams_complete={res['streams_complete']}/{res['n_streams']} "
        f"missing={res['streams_missing']} "
        f"no_fin={res['streams_no_fin']} "
        f"no_header={res['streams_no_header']} "
        f"short={res['streams_short']} "
        f"bad_payload={res['streams_bad_payload']} "
        f"largest_consecutive_missing={res['missing_runs_largest']}"
    )


@pytest.mark.bench
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size", [
    (   50,  60, 1024),
    (  100,  60, 1024),
    (  500,  60, 1024),
    ( 1000,  60, 1024),
], ids=[
    "mp-50s-60o-1K", "mp-100s-60o-1K",
    "mp-500s-60o-1K", "mp-1000s-60o-1K",
])
def test_bench_wt_split_writes_stress_mp(n_streams, objs_per_stream,
                                            obj_size):
    """Multi-process variant: server runs in a subprocess (separate
    asyncio loop, transport, eventfd, SPSC rings; UDP loopback through
    real kernel sockets between distinct processes). Mirrors aiomoqt
    --mp-loopback shape. Use to confirm whether the aiomoqt parse-reject
    is reproducible at the aiopquic-WT layer alone in mp shape."""
    res = asyncio.run(_run_split_writes_wt_mp(n_streams, objs_per_stream,
                                                 obj_size))
    _print(res)
    assert res['pass'], (
        f"streams_complete={res['streams_complete']}/{res['n_streams']} "
        f"missing={res['streams_missing']} "
        f"no_fin={res['streams_no_fin']} "
        f"no_header={res['streams_no_header']} "
        f"short={res['streams_short']} "
        f"bad_payload={res['streams_bad_payload']}"
    )


# =======================================================================
# Concurrent-writers MP variant — closer to aiomoqt's actual TX shape.
# aiomoqt's PublishedTrack._generate_subgroup launches num_subgroups
# concurrent asyncio tasks, each opening + writing + FINing streams in
# a continuous loop. The streams INTERLEAVE on the same WT session.
# This is the TX-shape diff between the sequential MP test above
# (which is byte-perfect at scale) and aiomoqt mp-loopback (which
# parse-rejects with the missing-5-byte-SubgroupHeader signature).
# =======================================================================


async def _run_split_writes_wt_mp_concurrent(
        n_streams_total: int, objs_per_stream: int, obj_size: int,
        num_writers: int) -> dict:
    """Concurrent-writers MP variant.

    num_writers asyncio tasks share a single WT connection. Each
    writer opens streams sequentially within its own task body; the
    TX side of the session sees calls from N tasks interleaved
    arbitrarily by the asyncio scheduler. n_streams_total streams are
    distributed across the writers (n_streams_total / num_writers
    per writer, remainder distributed to first writers)."""
    import multiprocessing as _mp
    try:
        ctx = _mp.get_context("fork")
    except ValueError:
        ctx = _mp.get_context()
    port = _next_port()
    pad = bytes(i & 0xFF for i in range(obj_size))
    expected_per_stream = HEADER_SIZE + objs_per_stream * obj_size

    ready_event = ctx.Event()
    import tempfile as _tempfile
    _f = _tempfile.NamedTemporaryFile(prefix="wt_mp_concurrent_summary_",
                                          suffix=".pkl", delete=False)
    summary_path = _f.name
    _f.close()
    proc = ctx.Process(
        target=_server_subproc_entry,
        args=(port, n_streams_total, objs_per_stream, obj_size,
              ready_event, summary_path),
        daemon=True,
    )
    proc.start()

    try:
        for _ in range(500):
            if ready_event.is_set():
                break
            await asyncio.sleep(0.02)
        if not ready_event.is_set():
            raise RuntimeError("server subproc never signaled ready")

        sids: list[int] = []
        full_waits_box = [0]
        t_start = 0.0
        t_send_done = 0.0

        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:

            async def _writer(writer_idx: int, n_for_writer: int):
                local_full_waits = 0
                for _ in range(n_for_writer):
                    sid = await wt.create_stream(bidir=False)
                    sids.append(sid)
                    while True:
                        try:
                            wt.send_stream_data(
                                sid, HEADER_SENTINEL, end_stream=False)
                            break
                        except BufferError:
                            local_full_waits += 1
                            await asyncio.sleep(0.0001)
                    for k in range(objs_per_stream):
                        is_last = (k == objs_per_stream - 1)
                        while True:
                            try:
                                wt.send_stream_data(
                                    sid, pad, end_stream=is_last)
                                break
                            except BufferError:
                                local_full_waits += 1
                                await asyncio.sleep(0.0001)
                    # Yield between streams to give other writers a
                    # chance — mirrors aiomoqt's per-stream cadence
                    # under concurrent subgroup tasks.
                    await asyncio.sleep(0)
                full_waits_box[0] += local_full_waits

            t_start = time.monotonic()
            base = n_streams_total // num_writers
            extra = n_streams_total - base * num_writers
            writer_tasks = []
            for w in range(num_writers):
                n_for_writer = base + (1 if w < extra else 0)
                writer_tasks.append(asyncio.create_task(
                    _writer(w, n_for_writer)))
            await asyncio.gather(*writer_tasks)
            t_send_done = time.monotonic()

            import os.path as _osp
            proc_deadline = time.monotonic() + 30.0
            while time.monotonic() < proc_deadline:
                if (_osp.exists(summary_path)
                        and os.path.getsize(summary_path) > 0):
                    break
                await asyncio.sleep(0.1)
    finally:
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)

    summary = {}
    try:
        import pickle as _pickle
        with open(summary_path, "rb") as _f:
            summary = _pickle.load(_f)
        os.unlink(summary_path)
    except Exception:
        pass
    captured: dict[int, bytes] = summary.get("captured", {})
    captured_fin: dict[int, bool] = summary.get("captured_fin", {})
    new_stream_count = summary.get("new_stream_count", -1)

    elapsed = max(1e-6, t_send_done - t_start)
    streams_per_s = n_streams_total / elapsed
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
        if not captured_fin.get(sid, False):
            streams_no_fin += 1
            if len(examples_no_fin) < 5:
                examples_no_fin.append((sid, len(rx)))
            continue
        n = len(rx)
        if n != bytes_per_stream:
            streams_short += 1
            if len(examples_short) < 5:
                examples_short.append((sid, n))
            continue
        if rx[:HEADER_SIZE] != HEADER_SENTINEL:
            streams_no_header += 1
            if len(examples_no_header) < 5:
                examples_no_header.append(
                    (sid, rx[:16].hex())
                )
            continue
        ok = True
        off = HEADER_SIZE
        for _ in range(objs_per_stream):
            if rx[off:off + obj_size] != pad:
                ok = False
                break
            off += obj_size
        if not ok:
            streams_bad_payload += 1
            continue
        streams_complete += 1

    bps = (n_streams_total * bytes_per_stream * 8 / 1e6) / elapsed

    return {
        "n_streams": n_streams_total,
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
        "missing_runs": [],
        "missing_runs_largest": 0,
        "no_header_examples": examples_no_header,
        "short_examples": examples_short,
        "no_fin_examples": examples_no_fin,
        "full_waits": full_waits_box[0],
        "new_stream_events": new_stream_count,
        "yield_per_stream": False,
        "num_writers": num_writers,
        "pass": streams_complete == n_streams_total,
    }


@pytest.mark.bench
@pytest.mark.parametrize("n_streams,objs_per_stream,obj_size,num_writers", [
    # Mirror aiomoqt -P 2 -g 120 -s 1024 mp-loopback shape:
    # n_streams = group count; objs_per_stream = objs per stream
    # (group_size 120 / num_subgroups 2 = 60); num_writers = -P value.
    (   50,  60, 1024, 2),
    (  100,  60, 1024, 2),
    (  500,  60, 1024, 2),
    ( 1000,  60, 1024, 2),
    (  500,  60, 1024, 4),
    (  500,  60, 1024, 8),
    ( 1000,  60, 1024, 8),
], ids=[
    "mpc-50s-60o-1K-P2", "mpc-100s-60o-1K-P2",
    "mpc-500s-60o-1K-P2", "mpc-1000s-60o-1K-P2",
    "mpc-500s-60o-1K-P4", "mpc-500s-60o-1K-P8",
    "mpc-1000s-60o-1K-P8",
])
def test_bench_wt_split_writes_stress_mp_concurrent(
        n_streams, objs_per_stream, obj_size, num_writers):
    """Concurrent-writers MP variant. num_writers asyncio tasks share
    a single WT session, each opening and writing streams in
    parallel — exactly mirroring aiomoqt PublishedTrack's per-subgroup
    task layout. The interleaved TX call pattern is the most likely
    trigger differential vs the sequential mp test."""
    res = asyncio.run(_run_split_writes_wt_mp_concurrent(
        n_streams, objs_per_stream, obj_size, num_writers))
    _print(res)
    print(f"  num_writers: {res['num_writers']}")
    assert res['pass'], (
        f"streams_complete={res['streams_complete']}/{res['n_streams']} "
        f"missing={res['streams_missing']} "
        f"no_fin={res['streams_no_fin']} "
        f"no_header={res['streams_no_header']} "
        f"short={res['streams_short']} "
        f"bad_payload={res['streams_bad_payload']}"
    )


# =======================================================================
# Sustained-duration MP variant — matches aiomoqt's bench shape: open
# streams continuously at a target rate for a fixed wall-clock window,
# rather than a one-shot batch. The aiomoqt-observed parse-reject rate
# (~0.02% at 250 Mbps) is too rare to catch in a small batch; a 20s+
# run at the same target rate accumulates enough streams to surface it.
# =======================================================================


def _server_subproc_entry_sustained(port: int, target_obj_size: int,
                                       ready_event, summary_path: str,
                                       runtime_sec: float):
    """Subprocess server for sustained mode. Collects every uni stream
    as it arrives until runtime_sec elapses (plus 5s drain grace)."""
    import asyncio as _asyncio
    from aiopquic.asyncio.webtransport import (
        serve_webtransport,
        WebTransportNewStream as _NS,
        WebTransportStreamDataReceived as _SDR,
    )

    captured: dict[int, bytearray] = {}
    captured_fin: dict[int, bool] = {}
    new_stream_count = [0]

    async def _main():
        async def _collect_stream(session, sid):
            buf = bytearray()
            try:
                async for sev in session.receive_stream_data(sid):
                    if isinstance(sev, _SDR):
                        buf.extend(sev.data)
                        if sev.end_stream:
                            captured[sid] = buf
                            captured_fin[sid] = True
                            return
            except Exception:
                captured[sid] = buf
                captured_fin[sid] = False

        async def server_handler(session):
            async def _accept_streams():
                async for ev in session.events():
                    if isinstance(ev, _NS):
                        new_stream_count[0] += 1
                        _asyncio.create_task(
                            _collect_stream(session, ev.stream_id))
            _asyncio.create_task(_accept_streams())

        server = await serve_webtransport(
            "127.0.0.1", port, "/wt",
            handler=server_handler,
            cert_file=CERT_FILE, key_file=KEY_FILE,
        )
        try:
            ready_event.set()
            # Run for runtime_sec + drain grace.
            await _asyncio.sleep(runtime_sec + 5.0)
        finally:
            server.close()
            await _asyncio.sleep(0.05)
            import pickle as _pickle
            summary = {
                "captured": {sid: bytes(buf)
                               for sid, buf in captured.items()},
                "captured_fin": dict(captured_fin),
                "new_stream_count": new_stream_count[0],
            }
            with open(summary_path, "wb") as _f:
                _pickle.dump(summary, _f)

    try:
        _asyncio.run(_main())
    except BaseException:
        pass
    os._exit(0)


async def _run_split_writes_wt_mp_sustained(
        runtime_sec: float, objs_per_stream: int, obj_size: int,
        num_writers: int, target_obj_per_sec: int) -> dict:
    """Sustained-duration concurrent MP variant.

    Each writer opens streams in a continuous loop for runtime_sec
    seconds, paced to target_obj_per_sec / num_writers per writer.
    Mirrors aiomoqt's bench shape: continuous churn at a target
    object rate rather than a one-shot batch."""
    import multiprocessing as _mp
    try:
        ctx = _mp.get_context("fork")
    except ValueError:
        ctx = _mp.get_context()
    port = _next_port()
    pad = bytes(i & 0xFF for i in range(obj_size))
    bytes_per_stream = HEADER_SIZE + objs_per_stream * obj_size

    ready_event = ctx.Event()
    import tempfile as _tempfile
    _f = _tempfile.NamedTemporaryFile(prefix="wt_mp_sustained_summary_",
                                          suffix=".pkl", delete=False)
    summary_path = _f.name
    _f.close()
    proc = ctx.Process(
        target=_server_subproc_entry_sustained,
        args=(port, obj_size, ready_event, summary_path, runtime_sec),
        daemon=True,
    )
    proc.start()

    sids: list[int] = []
    full_waits_box = [0]
    t_start = 0.0
    t_send_done = 0.0

    try:
        for _ in range(500):
            if ready_event.is_set():
                break
            await asyncio.sleep(0.02)
        if not ready_event.is_set():
            raise RuntimeError("server subproc never signaled ready")

        async with connect_webtransport("127.0.0.1", port, "/wt") as wt:

            obj_period = num_writers / target_obj_per_sec
            stream_period = obj_period * objs_per_stream

            async def _writer(writer_idx: int):
                local_full_waits = 0
                writer_t_start = asyncio.get_event_loop().time()
                stream_count_local = 0
                next_stream_t = writer_t_start
                deadline = writer_t_start + runtime_sec
                while True:
                    now = asyncio.get_event_loop().time()
                    if now >= deadline:
                        break
                    # Pace
                    sleep_for = next_stream_t - now
                    if sleep_for > 0:
                        await asyncio.sleep(sleep_for)
                    sid = await wt.create_stream(bidir=False)
                    sids.append(sid)
                    while True:
                        try:
                            wt.send_stream_data(
                                sid, HEADER_SENTINEL, end_stream=False)
                            break
                        except BufferError:
                            local_full_waits += 1
                            await asyncio.sleep(0.0001)
                    for k in range(objs_per_stream):
                        is_last = (k == objs_per_stream - 1)
                        while True:
                            try:
                                wt.send_stream_data(
                                    sid, pad, end_stream=is_last)
                                break
                            except BufferError:
                                local_full_waits += 1
                                await asyncio.sleep(0.0001)
                    stream_count_local += 1
                    next_stream_t = (writer_t_start
                                       + stream_count_local * stream_period)
                full_waits_box[0] += local_full_waits

            t_start = time.monotonic()
            tasks = [asyncio.create_task(_writer(w))
                       for w in range(num_writers)]
            await asyncio.gather(*tasks)
            t_send_done = time.monotonic()

            import os.path as _osp
            proc_deadline = time.monotonic() + (runtime_sec + 10.0)
            while time.monotonic() < proc_deadline:
                if (_osp.exists(summary_path)
                        and os.path.getsize(summary_path) > 0):
                    break
                await asyncio.sleep(0.1)
    finally:
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)

    summary = {}
    try:
        import pickle as _pickle
        with open(summary_path, "rb") as _f:
            summary = _pickle.load(_f)
        os.unlink(summary_path)
    except Exception:
        pass
    captured: dict[int, bytes] = summary.get("captured", {})
    captured_fin: dict[int, bool] = summary.get("captured_fin", {})
    new_stream_count = summary.get("new_stream_count", -1)

    n_streams = len(sids)
    elapsed = max(1e-6, t_send_done - t_start)
    streams_per_s = n_streams / elapsed

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
        if not captured_fin.get(sid, False):
            streams_no_fin += 1
            if len(examples_no_fin) < 5:
                examples_no_fin.append((sid, len(rx)))
            continue
        n = len(rx)
        if n != bytes_per_stream:
            streams_short += 1
            if len(examples_short) < 5:
                examples_short.append((sid, n))
            continue
        if rx[:HEADER_SIZE] != HEADER_SENTINEL:
            streams_no_header += 1
            if len(examples_no_header) < 5:
                examples_no_header.append(
                    (sid, rx[:16].hex())
                )
            continue
        ok = True
        off = HEADER_SIZE
        for _ in range(objs_per_stream):
            if rx[off:off + obj_size] != pad:
                ok = False
                break
            off += obj_size
        if not ok:
            streams_bad_payload += 1
            continue
        streams_complete += 1

    bps = (n_streams * bytes_per_stream * 8 / 1e6) / elapsed

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
        "missing_runs": [],
        "missing_runs_largest": 0,
        "no_header_examples": examples_no_header,
        "short_examples": examples_short,
        "no_fin_examples": examples_no_fin,
        "full_waits": full_waits_box[0],
        "new_stream_events": new_stream_count,
        "yield_per_stream": False,
        "num_writers": num_writers,
        "runtime_sec": runtime_sec,
        "target_obj_per_sec": target_obj_per_sec,
        "pass": (streams_complete == n_streams) and n_streams > 0,
    }


@pytest.mark.bench
@pytest.mark.parametrize("runtime,objs_per_stream,obj_size,num_writers,target_obj_s", [
    # Mirror aiomoqt mp-loopback at common rate points. Tx-side:
    # 250 Mbps with 1024 B obj = ~30,500 obj/s. 500 Mbps = ~61,000.
    # P=2 spreads across 2 concurrent writers.
    (15.0,  60, 1024, 2,  30500),  # ~250 Mbps for 15s
    (15.0,  60, 1024, 2,  61000),  # ~500 Mbps for 15s
    (20.0,  60, 1024, 2, 122000),  # ~1 Gbps for 20s
], ids=[
    "sus-250M-15s-P2", "sus-500M-15s-P2", "sus-1G-20s-P2",
])
def test_bench_wt_split_writes_stress_mp_sustained(
        runtime, objs_per_stream, obj_size, num_writers, target_obj_s):
    """Sustained-duration MP variant. Matches aiomoqt mp-loopback
    bench shape: continuous churn at target rate for runtime_sec
    seconds. The 0.02% parse-reject rate seen in aiomoqt at 250 Mbps
    requires ~5000+ streams to catch reproducibly — this test runs
    that volume in 15-20 seconds."""
    res = asyncio.run(_run_split_writes_wt_mp_sustained(
        runtime, objs_per_stream, obj_size, num_writers, target_obj_s))
    _print(res)
    print(f"  num_writers: {res['num_writers']}  "
          f"runtime: {res['runtime_sec']}s  "
          f"target: {res['target_obj_per_sec']} obj/s")
    assert res['pass'], (
        f"streams_complete={res['streams_complete']}/{res['n_streams']} "
        f"missing={res['streams_missing']} "
        f"no_fin={res['streams_no_fin']} "
        f"no_header={res['streams_no_header']} "
        f"short={res['streams_short']} "
        f"bad_payload={res['streams_bad_payload']}"
    )
