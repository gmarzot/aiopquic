"""create_stream(initial=...) counts its initial bytes against the session's
queued-byte budget from submission until the reply hands them to the ring."""
import asyncio
import os
from contextlib import suppress

import pytest

from aiopquic.asyncio import webtransport as wt
from aiopquic.asyncio.webtransport import connect_webtransport, serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from ._ports import next_port
from .test_stream_create_cancellation import Transport, reply, retired, session

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"


@pytest.mark.parametrize("budget,initial,streams", [
    (64 * 1024, 32 * 1024, 16),
    (4 * 1024 * 1024, 128 * 1024, 64),
])
async def test_concurrent_creates_stay_within_budget(budget, initial, streams):
    """With the peer gone nothing drains; resetting created streams frees
    the budget for the parked creates."""
    async def handler(session):
        async for _ in session.events():
            pass

    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=handler, cert_file=CERT_FILE,
        key_file=KEY_FILE, configuration=QuicConfiguration(is_client=False))
    cfg = QuicConfiguration(is_client=True, tx_max_queued_bytes=budget,
                            cafile=CA_FILE, idle_timeout=30)
    try:
        async with connect_webtransport("127.0.0.1", port, "/wt", sni=SNI,
                                        configuration=cfg) as s:
            server.close()
            payload = b"x" * initial
            creates = [asyncio.create_task(s.create_stream(
                bidir=False, initial=payload, timeout=10))
                for _ in range(streams)]
            peak = 0
            for _ in range(100):
                peak = max(peak, s._tx_backlog())
                await asyncio.sleep(0.002)
            assert peak <= budget + initial, peak
            assert not all(c.done() for c in creates), "no create parked"
            later = asyncio.create_task(s.create_stream(
                bidir=False, initial=b"tail", timeout=10))
            done, _ = await asyncio.wait({later}, timeout=0.05)
            assert not done, "a create over the budget did not park"

            pending = set(creates) | {later}
            reset = set()
            async with asyncio.timeout(20):
                while pending:
                    done, pending = await asyncio.wait(
                        pending, timeout=0.002)
                    for task in done:
                        sid = task.result()
                        s.reset_stream(sid, 0)
                        reset.add(sid)
                    peak = max(peak, s._tx_backlog())
            assert len(reset) == streams + 1
            assert peak <= budget + initial, peak
            assert s._tx_reserved == 0
    finally:
        server.close()


class RingTransport(Transport):
    """Mock transport whose stream rings each hold `used` bytes."""

    def __init__(self):
        super().__init__()
        self.used = 0

    def tx_data_ring_used(self, sc_ptr):
        return self.used


def gated_session(cap):
    s = session()
    s._transport = RingTransport()
    s.tx_max_queued_bytes = cap
    return s


async def created(s, initial, **kw):
    task = asyncio.create_task(s.create_stream(initial=initial, **kw))
    await asyncio.wait_for(s._state.submitted.get(), 1)
    return task


async def test_reply_hands_the_reservation_to_the_ring():
    s = gated_session(1 << 20)
    task = await created(s, b"x" * 1000)
    assert s._tx_reserved == 1000 and s._tx_backlog() == 1000
    s._transport.used = 1000
    reply(s, 6)
    assert s._tx_reserved == 0 and s._tx_backlog() == 1000
    assert await task == 6


async def test_rejected_create_releases_its_reservation():
    s = gated_session(1 << 20)
    task = await created(s, b"x" * 1000)
    reply(s, 0, error=2)
    with pytest.raises(wt.WebTransportError, match="create rejected"):
        await task
    assert s._tx_reserved == 0


@pytest.mark.parametrize("late_reply", [False, True])
async def test_abandoned_create_releases_at_its_reply(late_reply):
    s = gated_session(1 << 20)
    task = await created(s, b"x" * 1000)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    # Its bytes are still on their way to the worker until the reply.
    assert s._tx_reserved == 1000
    if late_reply:
        reply(s, 6)
        await retired(s)
    else:
        reply(s, 0, error=2)
    assert s._tx_reserved == 0


@pytest.mark.parametrize("remote", [False, True])
async def test_session_close_releases_every_reservation(remote):
    s = gated_session(1 << 20)
    tasks = [await created(s, b"x" * 1000) for _ in range(3)]
    tasks[0].cancel()
    if remote:
        s._on_session_closed(0, None)
    else:
        s.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert s._tx_reserved == 0


async def test_churned_session_returns_to_zero_and_admits_at_once():
    cap = 64 * 1024
    s = gated_session(cap)
    for round_ in range(20):
        tasks = [await created(s, b"x" * 30_000) for _ in range(3)]
        tasks[round_ % 3].cancel()
        await asyncio.sleep(0)
        reply(s, 4 * round_ + 2, error=2 if round_ % 2 else 0)
        reply(s, 0, error=2)
        reply(s, 4 * round_ + 3)
        await asyncio.gather(*tasks, return_exceptions=True)
        await retired(s)
        assert s._tx_reserved == 0
    s._stream_tx_ctxs.clear()
    task = await created(s, b"x" * cap)
    reply(s, 1000)
    assert await asyncio.wait_for(task, 1) == 1000
    assert s._tx_reserved == 0


async def test_one_large_create_passes_under_the_budget():
    cap = 64 * 1024
    s = gated_session(cap)
    task = await created(s, b"x" * (4 * cap))
    reply(s, 6)
    assert await task == 6
