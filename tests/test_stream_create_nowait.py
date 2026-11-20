"""create_stream_nowait: submitted without waiting, its reply paired in
order with awaited creates, declined with nothing submitted when it
would have to wait."""
import asyncio

import pytest

from tests.test_stream_create_cancellation import SC, reply, session, submitted


def nowait(s, ids, **kw):
    kw.setdefault("initial", b"hdr+obj")
    kw.setdefault("end_stream", True)
    return s.create_stream_nowait(on_created=ids.append, **kw)


async def test_the_reply_reports_the_stream_id():
    s = session()
    ids = []
    assert nowait(s, ids)
    assert s._state.submitted.get_nowait() is False
    assert s._tx_reserved == len(b"hdr+obj")
    reply(s, 6)
    assert ids == [6]
    assert s._stream_tx_ctxs == {6: SC}
    assert not s._pending_creates
    assert s._tx_reserved == 0


async def test_replies_pair_in_order_with_awaited_creates():
    s = session()
    ids = []
    first = await submitted(s, bidir=False)
    assert nowait(s, ids)
    assert s._state.submitted.get_nowait() is False
    last = await submitted(s, bidir=False)
    reply(s, 2)
    reply(s, 6)
    reply(s, 10)
    assert await first == 2
    assert ids == [6]
    assert await last == 10


async def test_a_refused_create_reports_zero_and_retires_the_stream():
    s = session()
    ids = []
    assert nowait(s, ids)
    reply(s, 6, error=1)
    assert ids == [0]
    assert not s._stream_tx_ctxs


async def test_session_close_reports_zero_for_a_pending_create():
    s = session()
    ids = []
    assert nowait(s, ids)
    s.close()
    assert ids == [0]
    assert not s._pending_creates


@pytest.mark.parametrize("why", ["closed", "ring full", "over budget",
                                 "retiring", "too large"])
async def test_declines_with_nothing_submitted(why):
    s = session()
    ids = []
    kw = {}
    if why == "closed":
        s.close()
    elif why == "ring full":
        s._state.create_error = BufferError("full")
    elif why == "over budget":
        s.tx_max_queued_bytes = 4
        s._tx_reserved = 8
    elif why == "retiring":
        s._orphan_cleanup_task = asyncio.get_running_loop().create_future()
    elif why == "too large":
        kw["initial"] = b"x" * (s.stream_ring_cap + 1)
    assert not nowait(s, ids, **kw)
    assert s._state.submitted.empty()
    assert not s._pending_creates
    assert ids == []
