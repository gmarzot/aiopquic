# aiopquic - Async QUIC + WebTransport (picoquic)

`aiopquic` is a Python/Cython binding to [picoquic](https://github.com/private-octopus/picoquic), providing high-performance QUIC transport and WebTransport for `asyncio` applications.

## Overview

`aiopquic` exposes picoquic's QUIC implementation through a lock-free SPSC ring buffer architecture that bridges the picoquic network thread with Python's asyncio event loop. It provides an asyncio QUIC/HTTP3 transport API in the spirit of qh3/aioquic (similar shapes for `QuicConfiguration`, `QuicConnection`, `connect()` / `serve()`, and event types) plus a native WebTransport client/server layered on picoquic's H3 + h3zero. Note: not a drop-in replacement — semantics differ around backpressure (`send_stream_data` raises `BufferError` on full per-stream ring) and flow-control sizing.

### Architecture

- **SPSC Ring Buffers** -- Lock-free single producer/single consumer rings for zero-copy event passing between threads. RX uses per-event `malloc` with ownership transfer at pop (1-copy delivery to Python via `StreamChunk`).
- **Cross-platform wake fd** -- Linux `eventfd` for efficient asyncio `add_reader()` notification; `pipe()` self-pipe fallback on macOS / BSD.
- **Dedicated Network Thread** -- picoquic runs in its own thread via `picoquic_start_network_thread()`.
- **Cython Bridge** -- Thin Cython layer over C callbacks, minimal overhead.
- **WebTransport** -- `asyncio.webtransport.WebTransportSession` (client + server) over picoquic's `picowt_*` API and h3zero.

### Features

- QUIC client and server support (qh3-compatible asyncio API: `connect()`, `serve()`, `QuicConnectionProtocol`)
- Stream data send/receive with FIN signaling, stream reset, stop_sending
- Datagram support
- WebTransport client + server (`serve_webtransport`, `WebTransportSession`)
- Connection migration / 0-RTT (inherited from picoquic)
- Connection management (create, close, idle timeout, application close codes)
- Per-cnx multiplexing on the server side via `QuicEngine`
- Native picoquic_ct / picohttp_ct subprocess smoke (catches upstream regressions on every submodule bump)

### Test Results

Tests pass on Linux and macOS (excluding the network-dependent interop suite):

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_spsc_ring` | per-event malloc ring lifecycle |
| `test_buffer` | Cython `Buffer` (qh3-compatible) |
| `test_transport` | Transport lifecycle, wake fd, wake-up, connection management |
| `test_loopback` | 17 tests — handshake, streams, FIN, reset, datagrams, ALPN mismatch, idle timeout, app-close codes, stop_sending, many-streams stress, TX-ring overflow |
| `test_asyncio` | qh3-compat client/server stream and datagram exchange |
| `test_baton_pattern` | Pure-QUIC baton-style stream multiplexing (UNI ↔ BIDI) |
| `test_native_picoquic` | picoquic_ct / picohttp_ct subprocess driver |
| `test_interop` | Real public endpoints (network required, opt-in) |
| `tests/bench/` | 17 microbenches: ring push/pop, single-shot/sustained/parallel/bidirectional throughput, datagrams, RTT latency, handshake rate (opt-in via `pytest tests/bench`) |

## Installation

Requires Python 3.14+ and a C build toolchain. picoquic + picotls are built from source as git submodules.

```bash
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic
git submodule update --init --recursive
./bootstrap_python.sh
source .venv/bin/activate
./build_picoquic.sh        # builds picotls, picoquic, native test drivers
pip install -e '.[dev]'
```

On macOS, set `OPENSSL_ROOT_DIR` if Homebrew OpenSSL is not auto-detected (the build script tries `openssl@3` then `openssl@1.1`).

## Usage

### Low-level Transport API

```python
from aiopquic._binding._transport import TransportContext

server = TransportContext()
server.start(port=4433, cert_file="cert.pem", key_file="key.pem",
             alpn="moq-00", is_client=False)

client = TransportContext()
client.start(port=0, alpn="moq-00", is_client=True)
client.create_client_connection("127.0.0.1", 4433,
                                 sni="localhost", alpn="moq-00")
```

### qh3-compatible asyncio API

```python
from aiopquic.asyncio.client import connect
from aiopquic.quic.configuration import QuicConfiguration

configuration = QuicConfiguration(
    alpn_protocols=["h3"],
    is_client=True,
)

async with connect("quic.nginx.org", 443,
                   configuration=configuration) as protocol:
    quic = protocol._quic
    stream_id = quic.get_next_available_stream_id()
    quic.send_stream_data(stream_id, b"GET / HTTP/1.0\r\n\r\n", end_stream=True)
    protocol.transmit()
```

### WebTransport

```python
from aiopquic.asyncio.webtransport import (
    serve_webtransport, WebTransportSession,
)
# See src/aiopquic/asyncio/webtransport.py and tests/ for full examples.
```

## Development

```bash
pip install -e '.[dev]'
python -m pytest tests/ -v -m "not interop and not native"

# Microbenches (opt-in)
python -m pytest tests/bench
```

## Known Limitations

- **Python 3.14+ required** -- relax planned (test on 3.12/3.13).
- **Free-threaded Python (3.14t) not yet supported** -- the TX-ring producer side, `TransportContext` lifecycle, and the WebTransport engine state currently rely on the GIL for serialization. FT support deferred until a per-context locking audit lands.
- **STOP_SENDING error codes** surface as 0 today: picoquic's public stream-error getter only returns the RESET_STREAM code. STOP_SENDING's code lives in `stream->remote_stop_error` in `picoquic_internal.h` (no public getter). A small helper that pulls the field is straightforward future work — see TODO in `src/aiopquic/_binding/c/callback.h`.
- **Source build only** -- requires building picoquic + picotls from submodules; binary wheel distribution planned.

## TODO

- Free-threaded Python (3.14t) support after producer-side locking audit
- STOP_SENDING error-code surfacing helper (read `remote_stop_error` from picoquic_internal.h)
- Relax Python version requirement (test on 3.12/3.13)
- Per-stream wrapper cleanup on RESET/FIN before connection close (currently bounded leak per cnx)
- Performance comparison vs qh3/aioquic

## Resources

- [picoquic](https://github.com/private-octopus/picoquic) -- QUIC implementation by Christian Huitema
- [picotls](https://github.com/h2o/picotls) -- TLS 1.3 implementation
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)

---

A [Marz Research](https://github.com/gmarzot) project.

## License

MIT License -- see [LICENSE](LICENSE)
