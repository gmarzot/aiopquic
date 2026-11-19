"""queue_new_streams: a peer-opened stream is queued for events() unless
the session turns it off; its send context is recorded either way."""
import pytest

from aiopquic.asyncio import webtransport as wt
from tests.test_stream_create_cancellation import session


def new_stream(s, sid, sc=0x55):
    s._on_event((wt._EVT_WT_NEW_STREAM, sid, None, False, 0, 0, 0, sc))


@pytest.mark.parametrize("queue", [True, False])
async def test_a_peer_opened_stream_is_queued_only_when_asked(queue):
    s = session()
    if not queue:
        s.queue_new_streams = False
    for sid in (3, 7, 11):
        new_stream(s, sid)
    assert s._event_queue.qsize() == (3 if queue else 0)
    assert s._stream_tx_ctxs == {3: 0x55, 7: 0x55, 11: 0x55}


async def test_a_subclass_turns_it_off_for_every_session():
    class Quiet(wt.WebTransportSession):
        queue_new_streams = False

    s = session()
    s.__class__ = Quiet
    new_stream(s, 3)
    assert s._event_queue.empty() and s._stream_tx_ctxs == {3: 0x55}
