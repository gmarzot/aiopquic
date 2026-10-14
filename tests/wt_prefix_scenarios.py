"""A WebTransport session Python releases while its connection lives.

test_wt_prefix.py runs each mode in a fresh process: a fault kills only the child.

    python tests/wt_prefix_scenarios.py MODE PORT CERT KEY

    server-close  the server session is released, the client opens a stream
                  on it, then the client closes the connection
    server-stop   as server-close, but the server stops with the connection up
    client        the client session is released, the server opens a stream
                  on it, then the server closes and the client stops
    stale-close   the server session is released without ever closing; no
                  SESSION_CLOSED for it may reach the dispatcher

Exits 0 when no callback reaches the freed session.
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
EVT_WT_SESSION_CLOSED = 66


def _fail(msg):
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


async def _until(cond, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            _fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def _release(session, transport):
    _get_dispatcher_registry().release(
        asyncio.get_running_loop(), transport, session)


async def _tx_drained(transport):
    """The worker has handled every TX event pushed so far."""
    c = transport.counters
    target = c["tx_event_ring_pushes"]
    await _until(lambda: transport.counters["tx_event_ring_pops"] >= target,
                 "the worker to drain the TX ring")


async def _open_client(transport, port, accepted):
    n = len(accepted)
    client = WebTransportClient(transport, "127.0.0.1", port, "/wt", sni=SNI)
    await client.open(timeout=5.0)
    await _until(lambda: len(accepted) > n, "the server session")
    return client, accepted.pop()


async def main(mode, port, cert, key):
    ca_file = os.path.join(os.path.dirname(cert), "test-ca.crt")
    accepted = []
    server = await serve_webtransport(
        "127.0.0.1", port, "/wt", handler=accepted.append,
        cert_file=cert, key_file=key)
    transport = TransportContext()
    transport.start(is_client=True, alpn="h3", ca_file=ca_file,
                    max_datagram_frame_size=64 * 1024)
    client, session = await _open_client(transport, port, accepted)

    if mode in ("server-close", "server-stop", "stale-close"):
        seen = []
        if mode == "stale-close":
            d = _get_dispatcher_registry()._lookup(
                asyncio.get_running_loop(), server._transport)
            route = d.route_event
            ptr = session._state.session_ptr

            def record(ev):
                if ev[6] == ptr:
                    seen.append(ev[0])
                route(ev)
            d.route_event = record
        _release(session, server._transport)
        del session
        gc.collect()
        # The release FINs CONNECT: the client sees the session end.
        await client.wait_closed(timeout=5.0)
        if mode == "stale-close":
            # Events drain in order: once a later session is accepted, any
            # event pushed for the released one has been routed.
            client2, session2 = await _open_client(transport, port, accepted)
            if EVT_WT_SESSION_CLOSED in seen:
                _fail("SESSION_CLOSED pushed for a released session")
            await client2.aclose()
            await client2.detach()
            server.close()
        else:
            client._state.push_create_stream(True)
            if mode == "server-close":
                await client.aclose()
                server.close()
            else:
                server.close()
                await client.aclose()
        await client.detach()
        transport.stop()
    else:
        _release(client, transport)
        client._dispatcher_attached = False
        del client
        gc.collect()
        await _tx_drained(transport)
        session._state.push_create_stream(True)
        server.close()
        transport.stop()
    print("ok", flush=True)


if __name__ == "__main__":
    mode, port, cert, key = sys.argv[1:5]
    asyncio.run(main(mode, int(port), cert, key))
