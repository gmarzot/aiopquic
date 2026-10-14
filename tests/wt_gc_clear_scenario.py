"""A WebTransport session state the GC clears first, run in a fresh process.

test_wt_gc_clear.py runs this in a subprocess: a fault kills only the child.

    python tests/wt_gc_clear_scenario.py PORT CERT KEY
        A client holds its connection open. The server's session leaves the
        dispatcher and its state is collected, the GC clearing the state
        before deallocating it; then the server stops, deleting the live
        connection, whose close callback walks the sessions it still names.
        Exits 0 when the session struct outlives every reference picoquic
        holds to it.
"""
import asyncio
import gc
import os
import sys
import time

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.webtransport import (
    WebTransportClient, _get_dispatcher_registry, serve_webtransport,
)

SNI = "test.example.com"  # the test certificate's name


def _fail(msg):
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


async def main(port, cert, key):
    ca_file = os.path.join(os.path.dirname(cert), "test-ca.crt")
    accepted = []
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=accepted.append,
        cert_file=cert, key_file=key)
    transport = TransportContext()
    transport.start(is_client=True, alpn="h3", ca_file=ca_file,
                    max_datagram_frame_size=64 * 1024)
    client = WebTransportClient(transport, "127.0.0.1", port, "/wt", sni=SNI)
    # Everything older moves out of the youngest generation; from here the
    # GC visits objects in creation order.
    gc.collect()
    gc.disable()
    try:
        await client.open(timeout=5.0)
        deadline = time.monotonic() + 5.0
        while not accepted and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not accepted:
            _fail("the server accepted no session")
        session = accepted.pop()
        _get_dispatcher_registry().release(
            asyncio.get_running_loop(), server._transport, session)
        # Garbage only through a cycle created after the state, so the GC
        # reaches the state, and clears it, before the cycle releases it.
        cycle = [session._state]
        cycle.append(cycle)
        del session, cycle
        gc.collect()
    finally:
        gc.enable()
    # The client's connection is still up: picoquic_free deletes it.
    server.close()
    await client.aclose()
    await client.detach()
    transport.stop()


if __name__ == "__main__":
    port, cert, key = sys.argv[1:4]
    asyncio.run(main(int(port), cert, key))
