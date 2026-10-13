"""WebTransportSession.detach() releases what the session holds in its
transport's event dispatcher, and only that.

The dispatcher registry holds every attached session and its transport, so
a session the caller builds itself keeps its TransportContext, rings and
wake fd alive until it detaches.
"""
import asyncio
import gc
import os

import pytest

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.webtransport import (
    WebTransportClient, WebTransportSession, _get_dispatcher_registry,
    serve_webtransport,
)

CERTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"  # the test certificate's name

try:
    from ._ports import next_port
except ImportError:
    from _ports import next_port

pytestmark = pytest.mark.asyncio


def _fd_count():
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd")
                          else "/dev/fd"))


@pytest.mark.skipif(not os.path.exists(CERT_FILE),
                    reason="picoquic certs not found")
async def test_caller_built_sessions_release_their_transport():
    """Sessions built on their own transports, as aiomoqt's client does,
    open and close 20 times; each detach leaves no dispatcher and no task,
    and the process gets every fd back."""
    port = next_port()
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=lambda session: None,
        cert_file=CERT_FILE, key_file=KEY_FILE)
    reg = _get_dispatcher_registry()
    loop = asyncio.get_running_loop()
    tasks = asyncio.all_tasks()
    try:
        gc.collect()
        fds = _fd_count()
        for _ in range(20):
            transport = TransportContext()
            transport.start(is_client=True, alpn="h3", ca_file=CA_FILE,
                            max_datagram_frame_size=64 * 1024)
            session = WebTransportClient(transport, "127.0.0.1", port, "/wt",
                                         sni=SNI)
            await session.open(timeout=5.0)
            await session.aclose()
            await session.detach()
            await session.detach()
            assert (id(loop), id(transport)) not in reg._dispatchers
            assert asyncio.all_tasks() == tasks
            transport.stop()
            del session, transport
            gc.collect()
        assert _fd_count() == fds
    finally:
        server.close()


class _Transport:
    """The surface the dispatcher reads; a pipe stands in for the wake fd."""

    def __init__(self):
        self.eventfd, self._wake = os.pipe()

    def drain_rx(self):
        return []

    def close(self):
        os.close(self.eventfd)
        os.close(self._wake)


class _State:
    def __init__(self, ptr):
        self.session_ptr = ptr


def _session(transport, ptr, attach=True):
    session = WebTransportSession(transport, "client", state=_State(ptr))
    if attach:
        session._attach_dispatcher()
    return session


async def test_detach_keeps_a_dispatcher_others_still_use():
    """A detach leaves the dispatcher, its reader and its acceptor tasks to
    the sessions and acceptor still on the transport; the last session out
    of a dispatcher without an acceptor retires it and reaps its tasks."""
    loop = asyncio.get_running_loop()
    reg = _get_dispatcher_registry()
    tr = _Transport()
    key = (id(loop), id(tr))
    handler = loop.create_task(asyncio.sleep(3600))
    try:
        never_opened = _session(tr, 9, attach=False)
        await never_opened.detach()
        assert key not in reg._dispatchers

        a, b = _session(tr, 1), _session(tr, 2)
        d = reg._dispatchers[key]
        d._tasks.add(handler)  # an accepted session's handler coroutine

        await a.detach()
        assert reg._dispatchers.get(key) is d and not handler.done()

        reg.attach_acceptor(loop, tr, lambda session: None)
        await b.detach()
        assert reg._dispatchers.get(key) is d and not handler.done()

        c = _session(tr, 3)
        reg.detach_acceptor(loop, tr)
        assert reg._dispatchers.get(key) is d and not handler.done()
        await c.detach()
        assert key not in reg._dispatchers
        assert handler.cancelled()
    finally:
        handler.cancel()
        loop.remove_reader(tr.eventfd)
        reg._dispatchers.pop(key, None)
        tr.close()
