"""Failing-first tests for the RX flow-control accounting introduced
2026-05-25 (bytes_pending_release + per-stream effective_free grant).

These tests use the new ctx.dump_counters() / .counters API and the
existing loopback_pair / big_ring_pair fixtures. They are intended
to fail under the current implementation so we have a green-bar
target while fixing the WT-bloat / FC-storm issues.

Counter signatures asserted:

  - fc_credit_pushed   = SPSC_EVT_TX_OPEN_FLOW_CONTROL events asyncio queued
  - fc_credit_handled  = events the worker processed
  - fc_credit_dropped  = events the worker could not process (tx_event_ring full)
  - sc_alive_total     = process-wide sc_created - sc_destroyed
  - chunks_alive_total = process-wide StreamChunk wrap - dealloc

All counters live on ctx.counters dict.
"""
import time

import pytest

from _helpers import (
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
)


@pytest.mark.bench
def test_fc_credit_accounting_balanced(big_ring_pair):
    """Pushed == handled + dropped after the worker drains.

    Catches: leaks in either the asyncio or worker side of the
    OPEN_FLOW_CONTROL event lifecycle. If they diverge under steady
    load, a refcount or push/pop is missing."""
    server, client, client_cnx, _ = big_ring_pair
    # Burst 1 MB across one stream then drain to quiescence.
    sid = 0
    chunk = b"a" * (256 * 1024)
    for _ in range(4):
        try:
            client.tx_send_stream(client_cnx, sid, chunk, end_stream=False)
        except BufferError:
            time.sleep(0.05)
    client.tx_send_stream(client_cnx, sid, b"", end_stream=True)

    # Drain receiver until FIN.
    deadline = time.monotonic() + 10.0
    received = 0
    while received < 4 * len(chunk):
        if time.monotonic() > deadline:
            pytest.fail(
                f"timeout draining: received {received} / {4 * len(chunk)}; "
                f"counters={server.counters}")
        for evt in server.drain_rx():
            if evt[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN):
                data = evt[2]
                if data is not None:
                    received += len(data)
        time.sleep(0.001)

    # Quiescence: give worker a moment to process the final credit
    # event(s) before checking balance.
    time.sleep(0.2)

    c = server.counters
    pushed = c["fc_credit_pushed"]
    handled = c["fc_credit_handled"]
    dropped = c["fc_credit_dropped"]
    delta = pushed - handled - dropped
    assert pushed == handled + dropped, (
        f"fc_credit accounting unbalanced: pushed={pushed} "
        f"handled={handled} dropped={dropped} (delta={delta})\n"
        f"full counters: {c}")


@pytest.mark.bench
def test_fc_credit_count_bounded(big_ring_pair):
    """Receiver should not emit O(N) credit events per N bytes.

    Catches the May-25 FC-storm regression: 2.3M MAX_STREAM_DATA
    frames in 60s at ~2 Gbps. Sensible upper bound is O(bytes /
    advertise_cap) — one push per ring-cap window's worth of drain,
    plus modest hysteresis."""
    server, client, client_cnx, _ = big_ring_pair
    sid = 0
    total = 8 * 1024 * 1024     # 8 MB
    chunk = b"b" * (256 * 1024)
    advertise_cap = 4 * 1024 * 1024
    # Generous upper bound: 32 grants per MB received.
    max_acceptable = (total // (1024 * 1024)) * 32

    # Push + drain interleaved.
    sent = 0
    received = 0
    deadline = time.monotonic() + 30.0
    while received < total:
        if time.monotonic() > deadline:
            pytest.fail(
                f"timeout: sent={sent} received={received}/{total} "
                f"counters={server.counters}")
        if sent < total:
            try:
                client.tx_send_stream(
                    client_cnx, sid, chunk,
                    end_stream=(sent + len(chunk) >= total))
                sent += len(chunk)
            except BufferError:
                pass
        for evt in server.drain_rx():
            if evt[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN):
                data = evt[2]
                if data is not None:
                    received += len(data)
        time.sleep(0.0005)

    c = server.counters
    pushed = c["fc_credit_pushed"]
    assert pushed <= max_acceptable, (
        f"fc_credit_pushed={pushed} exceeds bound {max_acceptable} "
        f"(received={received} bytes, advertise_cap={advertise_cap}). "
        f"Likely cause: per-chunk credit push instead of per-cycle.\n"
        f"full counters: {c}")


@pytest.mark.bench
def test_chunks_alive_drains_after_stream_close(big_ring_pair):
    """After a stream completes and all bytes are released by the
    consumer, chunks_alive_total should return to baseline.

    Catches: StreamChunk leaks (missing __dealloc__, stuck refs)."""
    server, client, client_cnx, _ = big_ring_pair
    baseline = server.counters["chunks_alive_total"]

    sid = 0
    total = 2 * 1024 * 1024     # 2 MB
    chunk = b"c" * (256 * 1024)
    sent = 0
    received = 0
    received_chunks = []
    deadline = time.monotonic() + 10.0
    while received < total:
        if time.monotonic() > deadline:
            pytest.fail(
                f"timeout: sent={sent} received={received}/{total}")
        if sent < total:
            try:
                client.tx_send_stream(
                    client_cnx, sid, chunk,
                    end_stream=(sent + len(chunk) >= total))
                sent += len(chunk)
            except BufferError:
                pass
        for evt in server.drain_rx():
            if evt[0] in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN):
                data = evt[2]
                if data is not None:
                    received += len(data)
                    received_chunks.append(data)
        time.sleep(0.0005)

    # Drop all references to received chunks.
    received_chunks.clear()
    # Give Python a beat to run __dealloc__ + worker to pop any
    # final fc_credit events.
    time.sleep(0.3)

    final = server.counters["chunks_alive_total"]
    assert final == baseline, (
        f"chunks_alive_total leaked: baseline={baseline} final={final} "
        f"(delta={final - baseline})\nfull counters: {server.counters}")


@pytest.mark.bench
def test_sc_alive_returns_to_baseline_across_streams(big_ring_pair):
    """Open and close 50 streams sequentially. sc_alive_total should
    return to its starting value once all streams are FIN'd and
    consumed.

    Catches: sc refcount leaks on FIN path."""
    server, client, client_cnx, _ = big_ring_pair
    baseline = server.counters["sc_alive_total"]

    n_streams = 50
    payload = b"d" * 1024

    # Use client-initiated uni streams (sid % 4 == 2). picoquic
    # considers uni streams closed when our FIN is ACKed; bidi
    # requires BOTH directions to FIN (and the test server never
    # sends FIN back, so bidi streams stay half-open forever and
    # never trigger picoquic_callback_stream_released). UNI matches
    # how real MoQT subgroups flow anyway.
    for i in range(n_streams):
        sid = i * 4 + 2
        client.tx_send_stream(client_cnx, sid, payload, end_stream=True)

    # Drain all bytes + FINs. Both sides — server gets STREAM_DESTROY
    # for its inbound sc; client gets STREAM_DESTROY for its outbound
    # sc (pure-sender path, emitted by prepare_to_send when is_fin
    # fires). drain_rx is what releases the stream-lifetime ref.
    fins_received = 0
    deadline = time.monotonic() + 10.0
    while fins_received < n_streams:
        if time.monotonic() > deadline:
            pytest.fail(
                f"timeout: only saw {fins_received}/{n_streams} FINs "
                f"counters={server.counters}")
        for evt in server.drain_rx():
            if evt[0] == SPSC_EVT_STREAM_FIN:
                fins_received += 1
        # drain client-side events so STREAM_DESTROY can be processed
        client.drain_rx()
        time.sleep(0.001)

    # Final drains: both sides + a beat for in-flight destroys.
    for _ in range(3):
        server.drain_rx()
        client.drain_rx()
        time.sleep(0.1)

    final = server.counters["sc_alive_total"]
    assert final == baseline, (
        f"sc_alive_total leaked across {n_streams} streams: "
        f"baseline={baseline} final={final} (delta={final - baseline})\n"
        f"server counters: {server.counters}\n"
        f"client counters: {client.counters}")
