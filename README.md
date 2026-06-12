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
- Stream data send/receive with FIN signaling, stream reset, stop_sending
- WebTransport client + server: `serve_webtransport`, `WebTransportSession`
- QUIC datagram TX + RX (note: WebTransport datagram TX not yet wired)
- Connection migration / 0-RTT (inherited from picoquic)
- Connection management: create, close, idle timeout, application close codes
- Per-cnx multiplexing on the server side via `QuicEngine`
- TLS keylog (NSS Key Log Format) for pcap decryption
- Native picoquic_ct / picohttp_ct subprocess smoke (catches upstream regressions on every submodule update)

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
git submodule update --init --recursive
./bootstrap_python.sh    # creates .venv with uv-managed Python 3.14 (GIL build) and pins cython 3.2+
source .venv/bin/activate
./build_picoquic.sh      # builds picotls, picoquic, native test drivers
uv pip install -e '.[dev]'    # or: pip install -e '.[dev]'
```

On macOS, set `OPENSSL_ROOT_DIR` if Homebrew OpenSSL is not auto-detected (the build script tries `openssl@3` then `openssl@1.1`).

### Reporting issues

Include the full version report in any issue — it captures aiopquic plus the picoquic + picotls submodule SHAs the binding was built from:

```bash
python -m aiopquic.versions   # or the console script: aiopquic-versions
```

Sample output:

```
aiopquic:  0.3.7.dev12+g6eef9caf6 (~/Projects/moq/aiopquic/src/aiopquic) [2026-06-12 08:55]
  - picoquic:  1.1.49.2 (d6c5653d) [2026-06-05]
  - picotls:   master (bfa67875) [2026-04-20]
```

If you're running aiomoqt on top, prefer `python -m aiomoqt.versions` — it chains through to this report and includes the aiomoqt version too.

## Usage

### Low-level Transport API

```python
from aiopquic._binding._transport import TransportContext

server = TransportContext()
server.start(port=4433, cert_file="cert.pem", key_file="key.pem", alpn="moq-00", is_client=False)

client = TransportContext()
client.start(port=0, alpn="moq-00", is_client=True)
client.create_client_connection("127.0.0.1", 4433, sni="localhost", alpn="moq-00")
```

### Asyncio API

```python
from aiopquic.asyncio.client import connect
from aiopquic.quic.configuration import QuicConfiguration

configuration = QuicConfiguration(alpn_protocols=["myproto"], is_client=True)

async with connect("server", 4433, configuration=configuration) as protocol:
    quic = protocol._quic
    stream_id = quic.get_next_available_stream_id()
    quic.send_stream_data(stream_id, payload, end_stream=True)
    protocol.transmit()
```

`payload` is opaque bytes; the library doesn't impose framing. Consumers
that want HTTP/3 layer on top of `aiopquic`'s `picowt`-backed h3zero
plumbing; consumers that want WebTransport use `serve_webtransport` /
`connect_webtransport`. Most direct users of the asyncio API ship their
own protocol bytes (MoQT, custom binary frames, etc.).

### WebTransport

```python
from aiopquic.asyncio.webtransport import (
    serve_webtransport, WebTransportSession,
)
# See src/aiopquic/asyncio/webtransport.py and tests/ for full examples.
```

## Development

```bash
uv pip install -e '.[dev]'    # or: pip install -e '.[dev]'
python -m pytest tests/ -v -m "not interop and not native"

# Microbenches (opt-in)
python -m pytest tests/bench
```

Test-suite coverage, benchmark methodology, performance builds (`AIOPQUIC_PERF=1`, io_uring scaffolding), and runtime tuning are documented in [PERFORMANCE.md](PERFORMANCE.md).

## Known Limitations

- **Free-threaded Python (3.14t) not yet supported** -- the TX-ring producer side, `TransportContext` lifecycle, and the WebTransport engine state currently rely on the GIL for serialization. FT support deferred until a per-context locking audit lands.
- **STOP_SENDING error codes** surface as 0 today: picoquic's public stream-error getter only returns the RESET_STREAM code. STOP_SENDING's code lives in `stream->remote_stop_error` in `picoquic_internal.h` (no public getter). A small helper that pulls the field is straightforward future work — see TODO in `src/aiopquic/_binding/c/callback.h`.

## TODO

- Windows support (eventfd alternative — IOCP / WSAEventSelect on the wake-fd path)
- Free-threaded Python (3.14t) support after producer-side locking audit
- STOP_SENDING error-code surfacing helper (read `remote_stop_error` from picoquic_internal.h)
- WebTransport datagram TX path through the C bridge
- Datagram benches: latency percentiles, payload-size sweep, loss / jitter under load (today's `bench_datagram` is fire-and-count throughput only)
- Pure stream open/close microbench (lifecycle rate without payload, separate from `bench_stream_churn_highlevel` which bundles writes + FIN)
- Submit aiopquic to the [QUIC interop runner](https://interop.seemann.io/) for cross-implementation coverage

## Resources

- [picoquic](https://github.com/private-octopus/picoquic) -- QUIC implementation by Christian Huitema
- [picotls](https://github.com/h2o/picotls) -- TLS 1.3 implementation
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)

---

<br>A [Marz Research](https://github.com/gmarzot) project.<br>
Author: G. S. Marzot &lt;gmarzot@marzresearch.net&gt;

## License

MIT License -- see [LICENSE](LICENSE)
