"""Both drained-send implementations own and reap their child wait tasks."""
import asyncio
from types import SimpleNamespace

import pytest

from aiopquic.asyncio.webtransport import WebTransportSession
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.connection import QuicConnection


class ObservedEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Queue()
        self.waiters = set()

    async def wait(self):
        task = asyncio.current_task()
        self.waiters.add(task)
        self.started.put_nowait(task)
        try:
            return await super().wait()
        finally:
            self.waiters.remove(task)


def sender(kind):
    transport = SimpleNamespace(tx_event_ring_drain_event=ObservedEvent())
    if kind == 'raw':
        s = QuicConnection(configuration=QuicConfiguration())
        s._transport = transport
        s._stream_ctxs[2] = 123  # Existing stream; no native allocation in this test.
    else:
        s = WebTransportSession(transport, 'client', SimpleNamespace())
    s._stream_tx_drain_events[2] = ObservedEvent()
    s.tx_event_ring_fill = lambda: 0
    return s


@pytest.mark.parametrize('kind', ['raw', 'wt'])
@pytest.mark.parametrize('outcome', ['cancel', 'stream-drain', 'ring-drain', 'close'])
async def test_send_reaps_both_waiters_on_every_exit(kind, outcome):
    s = sender(kind)
    attempts = []

    def send(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise BufferError('full')

    s.send_stream_data = send
    stream_event = s._stream_tx_drain_events[2]
    ring_event = s._transport.tx_event_ring_drain_event
    task = asyncio.create_task(s.send_stream_data_drained(2, b'payload'))
    waits = [await asyncio.wait_for(event.started.get(), 1)
             for event in (stream_event, ring_event)]
    if outcome == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(attempts) == 1
    else:
        if outcome == 'close':
            if kind == 'raw':
                s._closed = True
            else:
                s._session_closed.set()
        (stream_event if outcome == 'stream-drain' else ring_event).set()
        await asyncio.wait_for(task, 1)
        assert len(attempts) == (1 if outcome == 'close' else 2)
    assert all(wait.done() for wait in waits)
    assert not stream_event.waiters
    assert not ring_event.waiters
