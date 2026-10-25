"""RX reader ownership, a safety net for lost wakes, and their forensics.

Each TransportContext has one RX reader on its eventfd, held by one owner.
A timer drains the RX ring when it has held events through two ticks with
no drain, and logs what it found: the wake counters, whether the eventfd
was readable, which callback the loop had registered for it, the reader
registrations and the order of recent wake events on both threads. That
separates a wake that was never written, one that was consumed, a reader
that was removed or replaced, and a registration the poller no longer
delivers (an fd number closed while registered, then reused).
"""
import collections
import logging
import select
import time
import weakref

_log = logging.getLogger("aiopquic")

PERIOD = 0.15            # seconds between checks; a stall is drained within 3 periods
_FULL_REPORTS = 3        # rescues logged with the full report, per transport
_BRIEF_EVERY = 60.0      # seconds between one-line reports after that
_COUNTERS = (
    "rx_notify_pending_now", "rx_event_ring_count_now", "rx_overflow_depth_now",
    "rx_notify_flips", "rx_notify_skips", "rx_notify_write_errs",
    "rx_notify_last_errno", "rx_clears", "rx_clear_read_empty", "rx_rearms",
    "loop_callbacks",
)

_watches = weakref.WeakKeyDictionary()
_by_fd: dict = {}        # fd number -> the watch whose reader holds it


def _name(owner) -> str:
    return f"{type(owner).__name__}@{id(owner):#x}"


def _readable(fd: int) -> bool | None:
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(0))
    except (OSError, ValueError, AttributeError):
        return None


def _registered(loop, fd: int) -> str | None:
    """The callback the loop holds for fd's readability, or None."""
    try:
        key = loop._selector.get_key(fd)
    except (KeyError, AttributeError, ValueError):
        return None
    reader = key.data[0] if key.data else None
    if reader is None or reader.cancelled():
        return None
    callback = reader._callback
    owner = getattr(callback, "__self__", None)
    return getattr(callback, "__qualname__", repr(callback)) + (
        f"@{id(owner):#x}" if owner is not None else "")


class _Watch:
    def __init__(self, loop, transport):
        self.loop = loop
        self.transport = weakref.ref(transport)
        self.fd = transport.eventfd
        self.owner_id = None
        self.owner_name = None
        self.callback = None
        self.history = collections.deque(maxlen=32)
        self.handle = None
        self.clears = None
        self.waiting = 0
        self.rescues = 0
        self.last_brief = 0.0

    def note(self, op: str, owner) -> None:
        self.history.append((round(time.monotonic(), 3), op, _name(owner)))

    def schedule(self) -> None:
        if self.handle is None:
            self.handle = self.loop.call_later(PERIOD, self.tick)

    def cancel(self) -> None:
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None

    def tick(self) -> None:
        self.handle = None
        transport = self.transport()
        if transport is None or self.callback is None or transport.eventfd < 0:
            return
        count = transport.rx_event_ring_count
        clears = transport.rx_clears
        self.waiting = self.waiting + 1 if count and clears == self.clears else 0
        self.clears = clears
        if self.waiting >= 2:
            self.waiting = 0
            self.rescue(transport, count)
        self.schedule()

    def rescue(self, transport, count: int) -> None:
        self.rescues += 1
        reader = _registered(self.loop, self.fd)
        counters = transport.counters
        now = time.monotonic()
        if self.rescues <= _FULL_REPORTS:
            now_ns = time.monotonic_ns()
            # Ages in ms before this rescue (the trace clock is
            # CLOCK_MONOTONIC, Python's monotonic clock on Linux).
            report = dict(
                owner=self.owner_name, ring=count,
                fd_readable=_readable(self.fd), reader=reader,
                counters={k: counters.get(k) for k in _COUNTERS},
                readers=[(round((now - t) * 1000), op, who)
                         for t, op, who in self.history],
                trace=[(round((now_ns - ts) / 1e6, 3), side, kind, a, b)
                       for ts, side, kind, a, b in transport.wake_trace(48)])
            _log.warning("rx wake lost on fd %d, draining now (rescue %d): %s",
                         self.fd, self.rescues, report)
        elif now - self.last_brief >= _BRIEF_EVERY:
            self.last_brief = now
            _log.warning("rx wake lost on fd %d again (%d rescues): ring=%d "
                         "pending=%s fd_readable=%s reader=%s", self.fd,
                         self.rescues, count,
                         counters.get("rx_notify_pending_now"),
                         _readable(self.fd), reader)
        if reader is None:
            self.loop.add_reader(self.fd, self.callback)
            self.history.append((round(now, 3), "re-added after rescue",
                                 self.owner_name))
        elif _readable(self.fd):
            # Registered and readable, yet never called: the poller has
            # lost the fd. Register it afresh.
            self.loop.remove_reader(self.fd)
            self.loop.add_reader(self.fd, self.callback)
            self.history.append((round(now, 3), "re-registered after rescue",
                                 self.owner_name))
        self.callback()


def set_reader(loop, transport, callback, owner) -> None:
    """Make owner's callback the transport's RX reader, replacing any."""
    watch = _watches.get(transport)
    if watch is None or watch.loop is not loop:
        if watch is not None:
            watch.cancel()
        watch = _Watch(loop, transport)
        _watches[transport] = watch
    watch.note("add" if watch.owner_id is None
               else f"replace {watch.owner_name}", owner)
    previous = _by_fd.get(watch.fd)
    if previous is not None and previous is not watch:
        # The number was held by a reader whose transport has gone: the
        # loop may still carry its registration.
        watch.note(f"fd {watch.fd} reused from {previous.owner_name}", owner)
        _log.warning("rx reader fd %d reused from %s for %s", watch.fd,
                     previous.owner_name, _name(owner))
        previous.cancel()
        try:
            loop.remove_reader(watch.fd)
        except Exception:
            pass
    loop.add_reader(transport.eventfd, callback)
    _by_fd[watch.fd] = watch
    watch.owner_id, watch.owner_name = id(owner), _name(owner)
    watch.callback = callback
    watch.schedule()


def clear_reader(loop, transport, owner) -> None:
    """Remove the RX reader if owner holds it. A removal by anyone else is
    recorded and ignored: the fd's reader belongs to its current owner."""
    watch = _watches.get(transport)
    if watch is not None and watch.owner_id not in (None, id(owner)):
        watch.note(f"remove refused (held by {watch.owner_name})", owner)
        return
    if watch is None:
        if transport.eventfd >= 0:
            try:
                loop.remove_reader(transport.eventfd)
            except Exception:
                pass
        return
    if _by_fd.get(watch.fd) is watch:
        try:
            loop.remove_reader(watch.fd)
        except Exception:
            pass
        del _by_fd[watch.fd]
        watch.note("remove", owner)
    else:
        watch.note(f"remove skipped: fd {watch.fd} now held by another", owner)
    watch.owner_id = watch.owner_name = watch.callback = None
    watch.cancel()


def rescues(transport) -> int:
    """Lost wakes the safety net has drained for this transport."""
    watch = _watches.get(transport)
    return watch.rescues if watch is not None else 0
