# aiopquic - Async QUIC Transport (picoquic)

`aiopquic` is a Python/Cython binding to [picoquic](https://github.com/private-octopus/picoquic), providing high-performance QUIC transport for `asyncio` applications.

## Overview

`aiopquic` exposes picoquic's QUIC implementation through a lock-free SPSC ring buffer architecture that bridges the picoquic network thread with Python's asyncio event loop. It provides a qh3/aioquic-compatible **transport** API, making it a drop-in QUIC transport for existing applications. HTTP/3 is *not* bundled — apps that need H3 must bring their own H3 stack (e.g. qh3) layered on top of this transport.

### Architecture

- **SPSC Ring Buffers** -- Lock-free single producer/single consumer rings using C11 atomics for zero-copy event passing between threads
- **Cross-platform wake fd** -- Linux `eventfd` for efficient asyncio `add_reader()` notification; `pipe()` self-pipe fallback on macOS / BSD
- **Dedicated Network Thread** -- picoquic runs in its own thread via `picoquic_start_network_thread()`
- **Cython Bridge** -- Thin Cython layer over C callbacks, minimal overhead

### Features

- QUIC client and server support
- Stream data send/receive with FIN signaling
- Datagram support
- Stream reset
- Connection migration (inherited from picoquic)
- 0-RTT handshake (inherited from picoquic)
- Connection management (create, close, idle timeout)
- qh3-compatible asyncio API (`connect()`, `serve()`, `QuicConnectionProtocol`)

### Test Results

50 tests pass on Linux and macOS (excluding the network-dependent interop suite):

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_spsc_ring` | 13 | Lock-free ring buffer (Cython transport layer) |
| `test_transport` | 11 | Transport lifecycle, wake fd, wake-up, connection management |
| `test_loopback` | 17 | Client/server: handshake, streams, datagrams, reset, ALPN mismatch, idle timeout, app-close codes, stop_sending, many-streams stress |
| `test_asyncio` | 7 | Async API, stream/datagram exchange (loopback) |
| `test_baton_pattern` | 2 | Pure-QUIC baton-style stream multiplexing (UNI ↔ BIDI) |
| `test_interop` | 2 | Real public endpoints (network required, opt-in) |
| `tests/bench/` | 10 | Microbenches: ring push/pop, throughput, latency, handshake rate, datagrams (opt-in via `pytest tests/bench`) |

## Installation

Requires Python 3.14+ and a C build toolchain (picoquic is built from source as a git submodule).

```bash
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic
git submodule update --init --recursive
./bootstrap_python.sh
source .venv/bin/activate
pip install -e .
```

## Usage

### Low-level Transport API

```python
from aiopquic._binding._transport import TransportContext

# Server
server = TransportContext()
server.start(port=4433, cert_file="cert.pem", key_file="key.pem",
             alpn="moq-00", is_client=False)

# Client
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

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Known Limitations

- **Python 3.14+ required** -- relax planned (test on 3.12/3.13)
- **Free-threaded Python (3.14t) not yet supported** -- the TX-ring producer side and `TransportContext` lifecycle currently rely on the GIL for serialization; FT support deferred until a per-context locking audit lands
- **Source build only** -- requires building picoquic + picotls from submodules; binary wheel distribution planned
- **No bundled HTTP/3** -- aiopquic ships only the QUIC transport. Apps needing H3 must bring their own (e.g. qh3) on top.

## TODO

- Binary wheel distribution (manylinux + macOS, via cibuildwheel)
- Free-threaded Python (3.14t) support after TX-ring producer locking audit
- Native H3/WebTransport layer (using picoquic's built-in HTTP/3 / wt_baton)
- Relax Python version requirement (test on 3.12/3.13)
- Performance benchmarks vs qh3/aioquic

## Resources

- [picoquic](https://github.com/private-octopus/picoquic) -- QUIC implementation by Christian Huitema
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)

---

A [Marz Research](https://github.com/gmarzot) project.

## License

MIT License -- see [LICENSE](LICENSE)
