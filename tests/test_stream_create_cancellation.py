"""WT create replies keep their submission order across cancellation and failure."""
import asyncio
from collections import deque
from contextlib import suppress

import pytest

from aiopquic.asyncio import webtransport as wt


class Transport:
    def __init__(self):
        self._tx_event_ring_drain_event = asyncio.Event()
        self.armed = asyncio.Queue()
        self.arm_result = True
        self.on_arm = None

    @property
    def tx_event_ring_drain_event(self):
        return self._tx_event_ring_drain_event

    def arm_tx_event_ring_drain_pending(self):
        self.armed.put_nowait(None)
        if self.on_arm:
            self.on_arm()
        return self.arm_result


class State:
    def __init__(self):
        self.submitted = asyncio.Queue()
        self.create_error = None
        self.commands = []
        self.blocked = False
        self.retire_errors = deque()

    def push_create_stream(self, bidir, initial=b"", end_stream=False,
                           stream_ring_cap=0, priority=-1):
        if self.create_error:
            raise self.create_error
        self.submitted.put_nowait(bidir)

    def _retire(self, command, sid, code):
        if self.retire_errors:
            raise self.retire_errors.popleft()
        if self.blocked:
            raise BufferError('TX ring full')
        self.commands.append((command, sid, code))

    def push_reset_stream(self, sid, code):
        self._retire('reset', sid, code)

    def push_stop_sending(self, sid, code):
        self._retire('stop', sid, code)

    def push_session_cleanup(self):
        self.commands.append(('cleanup',))

    def push_close(self, *args):
        self.commands.append(('close',))

    def push_fin(self):
        self.commands.append(('fin',))


def session():
    s = wt.WebTransportSession(Transport(), 'client', State())
    s._loop = asyncio.get_running_loop()
    s._session_ready = s._loop.create_future()
    s._session_ready.set_result(None)
    s.tx_max_queued_bytes = 0
    return s


async def submitted(s, *, bidir=True, timeout=5):
    task = asyncio.create_task(s.create_stream(bidir=bidir, timeout=timeout))
    assert await asyncio.wait_for(s._state.submitted.get(), 1) == bidir
    return task


def reply(s, sid, error=0):
    s._on_event((wt._EVT_WT_STREAM_CREATED, sid, None, False, error,
                 0, 0, 123 if not error else 0))


async def retired(s):
    task = s._orphan_cleanup_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 1)
    assert not s._orphan_stream_commands
    assert s._orphan_cleanup_task is None


@pytest.mark.parametrize('cancel', [False, True])
async def test_abandoned_create_keeps_exactly_one_reply_slot(cancel):
    s = session()
    first = await submitted(s, bidir=True, timeout=0.01 if not cancel else 5)
    second = await submitted(s, bidir=False)
    if cancel:
        first.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await first
    assert len(s._pending_creates) == 2
    reply(s, 4)
    assert not second.done()
    reply(s, 6)
    assert await second == 6
    await retired(s)
    assert s._state.commands == [('reset', 4, 0), ('stop', 4, 0)]
    assert s._stream_tx_ctxs == {6: 123}
    assert not s._pending_creates


async def test_failed_submission_does_not_consume_the_next_reply():
    s = session()
    s._state.create_error = ConnectionError('closed')
    with pytest.raises(ConnectionError):
        await s.create_stream()
    assert not s._pending_creates
    s._state.create_error = None
    next_create = await submitted(s, bidir=False)
    reply(s, 6)
    assert await next_create == 6
    assert not s._pending_creates


async def test_full_ring_waits_for_drain_and_resubmits():
    s = session()
    s._state.create_error = BufferError('full')
    task = asyncio.create_task(s.create_stream(bidir=False))
    await asyncio.wait_for(s._transport.armed.get(), 1)
    assert not task.done() and not s._pending_creates
    s._state.create_error = None
    s._transport.tx_event_ring_drain_event.set()
    assert await asyncio.wait_for(s._state.submitted.get(), 1) is False
    reply(s, 6)
    assert await task == 6
    assert not s._pending_creates


async def test_full_ring_wait_counts_toward_timeout():
    s = session()
    s._state.create_error = BufferError('full')
    with pytest.raises(TimeoutError):
        await s.create_stream(timeout=0.05)
    assert not s._pending_creates and s._ring_parked_creates == 0


async def test_session_close_ends_a_full_ring_wait():
    s = session()
    s._state.create_error = BufferError('full')
    task = asyncio.create_task(s.create_stream())
    await asyncio.wait_for(s._transport.armed.get(), 1)
    s.close()
    with pytest.raises(wt.WebTransportError, match='session closed'):
        await asyncio.wait_for(task, 1)
    assert not s._pending_creates


@pytest.mark.parametrize('sid', [0, 4])
async def test_native_rejection_consumes_its_slot_and_retires_partial_stream(sid):
    s = session()
    failed = await submitted(s)
    successful = await submitted(s, bidir=False)
    reply(s, sid, error=2)
    reply(s, 6)
    with pytest.raises(wt.WebTransportError, match='create rejected'):
        await failed
    assert await successful == 6
    await retired(s)
    assert s._state.commands == ([('reset', 4, 0), ('stop', 4, 0)] if sid else [])
    assert not s._pending_creates


async def test_cancel_after_reply_retires_stream_before_caller_resumes():
    s = session()
    task = await submitted(s, bidir=False)
    reply(s, 6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await retired(s)
    assert s._state.commands == [('reset', 6, 0)]
    assert not s._stream_tx_ctxs
    assert not s._pending_creates


async def test_unsolicited_reply_retires_stream_without_stashing_context():
    s = session()
    reply(s, 6)
    await retired(s)
    assert s._state.commands == [('reset', 6, 0)]
    assert not s._stream_tx_ctxs


async def test_one_cleanup_task_retries_backpressure_and_gates_new_creates():
    s = session()
    first = await submitted(s)
    second = await submitted(s, bidir=False)
    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    s._state.blocked = True
    reply(s, 4)
    cleanup = s._orphan_cleanup_task
    reply(s, 6)
    assert s._orphan_cleanup_task is cleanup
    await asyncio.wait_for(s._transport.armed.get(), 1)
    # A cancellation while waiting for cleanup must not cancel the cleanup.
    blocked = asyncio.create_task(s.create_stream())
    await asyncio.sleep(0)
    assert s._state.submitted.empty()
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert not cleanup.done()
    next_create = asyncio.create_task(s.create_stream(bidir=False))
    await asyncio.sleep(0)
    assert s._state.submitted.empty()
    s._state.blocked = False
    s._transport.tx_event_ring_drain_event.set()
    assert await asyncio.wait_for(s._state.submitted.get(), 1) is False
    reply(s, 10)
    assert await next_create == 10
    await retired(s)
    assert s._state.commands == [('reset', 4, 0), ('stop', 4, 0), ('reset', 6, 0)]


async def test_retirement_drain_between_push_and_arm_does_not_lose_wake():
    s = session()
    s._state.retire_errors.append(BufferError('full'))
    s._transport.arm_result = False
    reply(s, 6)
    await retired(s)
    assert s._state.commands == [('reset', 6, 0)]


async def test_connection_teardown_ends_orphan_cleanup():
    s = session()
    s._state.retire_errors.append(ConnectionError('closed'))
    reply(s, 6)
    await retired(s)
    assert not s._stream_tx_ctxs


@pytest.mark.parametrize('remote', [False, True])
async def test_session_close_releases_tombstones_cleanup_and_pending_callers(remote):
    s = session()
    first = await submitted(s)
    live = await submitted(s, bidir=False)
    first.cancel()
    with suppress(asyncio.CancelledError):
        await first
    s._state.blocked = True
    reply(s, 4)
    await asyncio.wait_for(s._transport.armed.get(), 1)
    behind_cleanup = asyncio.create_task(s.create_stream())
    await asyncio.sleep(0)
    if remote:
        s._on_session_closed(0, None)
    else:
        s.close()
    with pytest.raises(wt.WebTransportError, match='session closed'):
        await live
    with pytest.raises(wt.WebTransportError, match='session closed'):
        await behind_cleanup
    await retired(s)
    assert not s._pending_creates
    assert not s._stream_tx_ctxs
    # A late native reply cannot recreate Python stream state after close.
    reply(s, 6)
    assert not s._stream_tx_ctxs
    assert not s._orphan_stream_commands
