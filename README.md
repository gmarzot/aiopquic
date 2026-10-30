# aiopquic - Async QUIC + WebTransport (picoquic)

`aiopquic` is a Python/Cython binding to [picoquic](https://github.com/private-octopus/picoquic), providing high-performance QUIC transport and WebTransport for `asyncio` applications.

## Overview

`aiopquic` exposes picoquic's QUIC implementation through a lock-free SPSC ring buffer architecture that bridges the picoquic network thread with Python's asyncio event loop. It provides an asyncio QUIC/HTTP3 transport API in the spirit of aioquic (and its fork qh3) — similar shapes for `QuicConfiguration`, `QuicConnection`, `connect` / `serve`, and event types — plus a native WebTransport client/server layered on picoquic's H3 + h3zero. Not a drop-in replacement: semantics differ around backpressure (`send_stream_data` raises `BufferError` on full per-stream ring) and flow-control sizing.

### Architecture

- **SPSC Ring Buffers** -- Lock-free single producer/single consumer rings for event passing between threads, separate TX and RX rings per `TransportContext`.
- **TX path** -- Asyncio pushes into per-stream byte ring; picoquic pulls at wire rate via `prepare_to_send`.
- **RX path** -- picoquic pushes per-event `StreamChunk`s; ownership transfers at pop for 1-copy delivery.
- **Cross-platform wake fd** -- Linux `eventfd` for efficient asyncio `add_reader()` notification; `pipe()` self-pipe fallback on macOS / BSD.
- **Dedicated Network Thread** -- picoquic runs in its own thread via `picoquic_start_network_thread()`. One worker thread per `TransportContext`; multiple contexts share the asyncio event loop within a single Python process.
- **Cython Bridge** -- Thin Cython layer over C callbacks, minimal overhead.
- **WebTransport** -- `asyncio.webtransport.WebTransportSession` (client + server) over picoquic's `picowt_*` API and h3zero.

### Features

- QUIC client and server: `connect`, `serve`, `QuicConnectionProtocol`
- WebTransport client and server: `connect_webtransport`, `serve_webtransport`, `WebTransportSession`;
  subprotocol negotiation (`wt_available_protocols` / `wt_supported_protocols`); static files over
  HTTP/3 (`serve_webtransport(web_folder=...)`)
- Raw QUIC and WebTransport on one UDP port, dispatched by ALPN: `serve_dispatch`
- Streams: FIN, reset, stop_sending, and RESET_STREAM_AT (`reset_stream(..., reliable_size=N)`);
  all-or-nothing writes with drain events for backpressure
- QUIC and WebTransport datagrams, TX + RX
- Stream and datagram priority on one scale: `set_stream_priority`, `set_datagram_priority`
- Congestion control by name: `bbr` (v3, default), `bbr1`, `cubic`, `dcubic`, `newreno`, `fast`,
  `prague`, `c4`
- Address validation with Retry (`serve(..., retry=True)`), QUIC v2 by compatible version
  negotiation (`supported_versions`), session resumption and 0-RTT (`session_ticket_file`)
- TLS: certificate verification against certifi's roots or a private CA, post-quantum key exchange
  (`KEX_X25519MLKEM768`, OpenSSL 3.5), cipher suite selection, key update interval
- Dual-stack IPv4/IPv6 servers, a bind host, `ip_family` to pin one family
- Connection limits: `max_connections`, and `set_max_connections` at runtime (new connections are
  refused with CONNECTION_REFUSED)
- Path MTU discovery policy: `pmtud="basic" | "delayed" | "blocked"`
- Connection management: idle timeout, keep-alive, application close codes
- Connection migration (inherited from picoquic)
- Per-connection multiplexing on the server side via `QuicEngine`
- qlog (`qlog_dir`), TLS keylog (NSS Key Log Format) for pcap decryption, and a version report
  (`python -m aiopquic.versions`)

### Performance

Multi-Gbps sustained throughput over loopback on commodity hardware: 2+ Gbps through the full asyncio API at the QUIC default MTU (where the kernel's sendmsg syscall rate is the wall), 11+ Gbps protocol-only. Numbers, methodology, reproduction commands, build flags, and runtime tuning (jemalloc, GSO, wake thresholds) live in [PERFORMANCE.md](PERFORMANCE.md). The TX/RX dataflow and flow-control model — including the TX backpressure configuration parameters — are documented in [DATAFLOW.md](DATAFLOW.md).

## Installation

Wheels for cp312 / cp313 / cp314 on Linux (manylinux_2_34, glibc 2.34+) and macOS arm64 are published to PyPI:

```bash
uv pip install aiopquic     # or: pip install aiopquic
```

For older Linux (glibc 2.28–2.33) install via sdist; build toolchain required.

### From source

```bash
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic
./bootstrap_python.sh    # creates .venv with uv-managed Python 3.14 (GIL build) and pins cython 3.2+
source .venv/bin/activate
./build.sh               # reconcile submodules, build picotls/picoquic + drivers, relink + verify
uv pip install -e '.[dev]'    # add dev/test extras — or: pip install -e '.[dev]'
```

Re-run `./build.sh` after any `git checkout` or submodule bump — it is idempotent (a matching fingerprint skips the compile) and guarantees the *imported* aiopquic reflects the current source, host-tuned. `./build.sh --check` is a read-only doctor that reports submodule drift, a stale native build, or a portable wheel shadowing the editable install, and exits nonzero — handy as a pre-benchmark or CI gate.

On macOS, set `OPENSSL_ROOT_DIR` if Homebrew OpenSSL is not auto-detected (the build script tries `openssl@3` then `openssl@1.1`).

### Reporting issues

Include the full version report in any issue — it captures aiopquic plus the picoquic + picotls submodule SHAs the binding was built from:

```bash
python -m aiopquic.versions   # or the console script: aiopquic-versions
```

Sample output:

```
aiopquic:  0.7.0rc2 (.venv/lib/python3.13/site-packages/aiopquic) [2026-10-29 09:21]
  - picoquic:  1.1.53.0 (2026.10.22.70b5fd5-0-70b5fd5a) [2026-10-22]
  - picotls:   master (f06553b8) [2026-09-30]
  - openssl:   OpenSSL 3.5.8 (.venv/lib/python3.13/site-packages/aiopquic.libs/libcrypto-c37e28d8.so.3.5.8)
```

If you're running aiomoqt on top, prefer `python -m aiomoqt.versions` — it chains through to this report and includes the aiomoqt version too.

## Usage

### QUIC client and server

Subclass `QuicConnectionProtocol` and override `quic_event_received`; `self._quic` is the
connection. An echo server:

```python
import asyncio

from aiopquic.asyncio import QuicConnectionProtocol, serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived


class Echo(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self._quic.send_stream_data(event.stream_id, bytes(event.data),
                                        end_stream=event.end_stream)


async def main():
    cfg = QuicConfiguration(is_client=False, alpn_protocols=["echo"])
    cfg.load_cert_chain("cert.pem", "key.pem")
    server = await serve("::", 4433, configuration=cfg, create_protocol=Echo)
    try:
        await asyncio.Future()          # serve until cancelled
    finally:
        server.close()

asyncio.run(main())
```

And a client for it:

```python
import asyncio

from aiopquic.asyncio import QuicConnectionProtocol, connect
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import StreamDataReceived


class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reply = bytearray()
        self.done = asyncio.Event()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.reply += event.data
            if event.end_stream:
                self.done.set()


async def main():
    cfg = QuicConfiguration(is_client=True, alpn_protocols=["echo"],
                            server_name="localhost", cafile="ca.pem")
    async with connect("localhost", 4433, configuration=cfg,
                       create_protocol=Client) as client:
        quic = client._quic
        sid = quic.get_next_available_stream_id()
        quic.send_stream_data(sid, b"hello", end_stream=True)
        await client.done.wait()
        print(bytes(client.reply))

asyncio.run(main())
```

Payloads are opaque bytes; the library imposes no framing. `send_stream_data` is all-or-nothing:
`BufferError` means the stream's TX ring is full and nothing was taken, so resend the same buffer,
or `await send_stream_data_drained(...)` to wait. [DATAFLOW.md](DATAFLOW.md) covers the
backpressure model.

Clients verify the server's certificate chain and name against certifi's
roots. A private CA goes in `QuicConfiguration.cafile` (or
`load_verify_locations(cafile=...)`), and `connect_webtransport(ca_file=...)`
takes the same file. To accept any certificate, such as a self-signed test
server, set `verify_mode=ssl.CERT_NONE` or pass
`connect_webtransport(verify_peer=False)`.
Clients offer a classic key share first; `QuicConfiguration.key_exchange_groups`
(or `start(key_exchange_groups=...)`) puts `KEX_X25519MLKEM768` first for a
post-quantum key exchange with servers that support it.

### WebTransport

The server calls `handler(session)` once per accepted CONNECT. `session.events()` yields new
streams, datagrams and the session's close; `receive_stream_data(sid)` yields one stream's data
up to its FIN or reset.

```python
import asyncio

from aiopquic.asyncio.webtransport import serve_webtransport
from aiopquic.quic.events import (WebTransportNewStream,
                                  WebTransportStreamDataReceived)


async def echo(session, sid):
    async for ev in session.receive_stream_data(sid):
        if isinstance(ev, WebTransportStreamDataReceived):
            session.send_stream_data(sid, bytes(ev.data), end_stream=ev.end_stream)


async def handler(session):             # once per accepted CONNECT
    tasks = set()
    async for ev in session.events():
        if isinstance(ev, WebTransportNewStream):
            task = asyncio.create_task(echo(session, ev.stream_id))
            tasks.add(task)
            task.add_done_callback(tasks.discard)


async def main():
    server = await serve_webtransport("::", 4433, "/echo", handler=handler,
                                      cert_file="cert.pem", key_file="key.pem")
    try:
        await asyncio.Future()
    finally:
        server.close()

asyncio.run(main())
```

```python
import asyncio

from aiopquic.asyncio.webtransport import connect_webtransport
from aiopquic.quic.events import WebTransportStreamDataReceived


async def main():
    async with connect_webtransport("localhost", 4433, "/echo",
                                    ca_file="ca.pem") as wt:
        sid = await wt.create_stream()
        wt.send_stream_data(sid, b"hello", end_stream=True)
        reply = bytearray()
        async for ev in wt.receive_stream_data(sid):
            if isinstance(ev, WebTransportStreamDataReceived):
                reply += ev.data
        print(bytes(reply))

asyncio.run(main())
```

`connect_webtransport()` and `serve_webtransport()` without `configuration=` run picoquic's
defaults, not `QuicConfiguration`'s; pass one to set congestion control, flow-control windows and
the rest.

### Raw QUIC and WebTransport on one port

`serve_dispatch` routes each connection by ALPN: raw QUIC to `create_protocol`, `h3` to the
WebTransport handler. With the `Echo` protocol and `handler` above:

```python
server = await serve_dispatch("::", 4433, configuration=cfg, create_protocol=Echo,
                              wt_path="/echo", wt_handler=handler)
```

### Low-level binding: TransportContext

The asyncio API runs on `TransportContext`, which owns one picoquic instance and its network
thread. Use it directly only to share one transport between WebTransport sessions
(`connect_webtransport(..., transport=t)`, `serve_webtransport(..., transport=t)`) or in tests;
the rest of `aiopquic._binding._transport` is internal.

```python
from aiopquic._binding._transport import TransportContext

server = TransportContext()
server.start(port=4433, cert_file="cert.pem", key_file="key.pem", alpn="echo", is_client=False)

client = TransportContext()
client.start(port=0, alpn="echo", is_client=True)
client.create_client_connection("127.0.0.1", 4433, sni="localhost", alpn="echo")
```

## Development

```bash
uv pip install -e '.[dev]'    # or: pip install -e '.[dev]'
python -m pytest tests/ -v -m "not interop and not native"

# picoquic's own picoquic_ct / picohttp_ct suites, run as subprocesses (catches upstream
# regressions on a submodule update)
python -m pytest tests -m native

# Microbenches (opt-in)
python -m pytest tests/bench
```

Test-suite coverage, benchmark methodology, performance builds (`AIOPQUIC_PERF=1`, io_uring scaffolding), and runtime tuning are documented in [PERFORMANCE.md](PERFORMANCE.md).

## Known Limitations

- **Free-threaded Python (3.14t) not yet supported** -- the TX-ring producer side, `TransportContext` lifecycle, and the WebTransport engine state currently rely on the GIL for serialization. FT support deferred until a per-context locking audit lands.

## TODO

- Windows support (eventfd alternative — IOCP / WSAEventSelect on the wake-fd path)
- Free-threaded Python (3.14t) support after producer-side locking audit
- Datagram benches: latency percentiles, payload-size sweep, loss / jitter under load (today's `bench_datagram` is fire-and-count throughput only)
- Pure stream open/close microbench (lifecycle rate without payload, separate from `bench_stream_churn_highlevel` which bundles writes + FIN)
- [QUIC interop runner](https://interop.seemann.io/): client and server endpoints proposed upstream in [quic-interop-runner#518](https://github.com/quic-interop/quic-interop-runner/pull/518), awaiting review

## Acknowledgments

aiopquic is built on [picoquic](https://github.com/private-octopus/picoquic): QUIC, HTTP/3 and WebTransport all come from it. Many thanks to Christian Huitema, picoquic's author, and to its contributors for that work.

## Resources

- [picoquic](https://github.com/private-octopus/picoquic) -- QUIC implementation by Christian Huitema
- [picotls](https://github.com/h2o/picotls) -- TLS 1.3 implementation
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)

---

<br>A [Marz Research](https://github.com/gmarzot) project.<br>
Author: G. S. Marzot &lt;gmarzot@marzresearch.net&gt;

## License

MIT License -- see [LICENSE](LICENSE)
