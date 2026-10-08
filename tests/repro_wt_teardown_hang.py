"""Reproducer for the WebTransport teardown hang. Not collected by pytest.

    python tests/repro_wt_teardown_hang.py            # default 1.0s phases
    REPRO_DUR=5.0 python tests/repro_wt_teardown_hang.py

Drives the three real test coroutines, each on its own event loop as
pytest-asyncio does, and exits non-zero when the third one hangs. Runs in
~40s at REPRO_DUR=1.0, which makes it usable under a sanitizer or
valgrind where the pytest suite is not.

Only reproduces with the A14 change applied — `close()` recording
termination per draft-ietf-webtrans-http3 §6 ("either sent or received"),
which removes the 2s wait_closed() timeout that otherwise masks this:

    # aiopquic/asyncio/webtransport.py, WebTransportSession.close()
    self._session_closed.set()      # after push_close

What is established:

  - All three phases are required, in order. Any two pass.
  - It needs loop TURNS, not elapsed time: 2000 unconditional
    `await asyncio.sleep(0)` before transport.stop() passes with zero
    wall-clock wait, while a 10ms sleep does not and 25ms does.
  - Not memory pressure: reproduces at 176MB as readily as at 882MB.
  - Not fds, threads, ports, the dispatcher registry, the session table,
    a stray close(), or an unjoined worker thread — each measured.
  - At teardown of phase 2 the server's accept-handler task is still
    pending. The dispatcher now owns and cancels those, which is
    necessary but not sufficient: WebTransportServer.close() is
    synchronous and cannot await the cancellation.

Full history in notes/release-plan-0.5.0-0.12.0-2026-09-24.md under A14.
"""
import asyncio
import faulthandler
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_flow_control_quic as Q          # noqa: E402
import test_flow_control_wt as W            # noqa: E402

DUR = float(os.environ.get("REPRO_DUR", "1.0"))
Q.DURATION = W.DURATION = DUR

PHASES = [
    ("quic::buffer_error", Q.test_buffer_error_under_sustained_push),
    ("wt::buffer_error", W.test_buffer_error_under_sustained_push),
    ("wt::drained", W.test_drained_helper_absorbs_backpressure),
]


def rss_kb() -> int:
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def main() -> int:
    for name, fn in PHASES:
        # A teardown that blocks the loop never reaches wait_for's timeout.
        faulthandler.dump_traceback_later(60.0, exit=True)
        try:
            asyncio.run(asyncio.wait_for(fn(), timeout=45.0))
        except asyncio.TimeoutError:
            print(f"{name}: HUNG  rss={rss_kb()}kB")
            return 1
        finally:
            faulthandler.cancel_dump_traceback_later()
        print(f"{name}: ok  rss={rss_kb()}kB")
    print("all phases passed — not reproduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
