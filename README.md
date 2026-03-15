# aiopquic - Async QUIC Transport for Python

`aiopquic` is a Python/Cython binding to [picoquic](https://github.com/perlinguist/picoquic), providing high-performance QUIC transport for `asyncio` applications.

## Overview

Built as the transport layer for [aiomoqt](https://github.com/gmarzot/aiomoqt-python), `aiopquic` exposes picoquic's QUIC implementation through a lock-free SPSC ring buffer architecture that bridges the picoquic network thread with Python's asyncio event loop.

### Architecture

- **SPSC Ring Buffers** — Lock-free single producer/single consumer rings using C11 atomics for zero-copy event passing between threads
- **eventfd Integration** — Linux eventfd for efficient asyncio `add_reader()` notification
- **Dedicated Network Thread** — picoquic runs in its own thread via `picoquic_start_network_thread()`
- **Cython Bridge** — Thin Cython layer over C callbacks, minimal overhead

### Features

- QUIC client and server support
- Stream data send/receive with FIN signaling
- Datagram support
- Connection management (create, close, idle timeout)
- Designed for use with asyncio via eventfd-based dispatch

## Installation

```bash
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic
git submodule update --init --recursive
./bootstrap_python.sh
source .venv/bin/activate
pip install -e .
```

## Usage

```python
from aiopquic._binding._transport import TransportContext

# Server
server = TransportContext()
server.start(port=4433, cert_file="cert.pem", key_file="key.pem",
             alpn="moq-chat", is_client=False)

# Client
client = TransportContext()
client.start(port=0, alpn="moq-chat", is_client=True)
client.create_client_connection("127.0.0.1", 4433,
                                 sni="localhost", alpn="moq-chat")
```

## Development

```bash
pip install -e .
python -m pytest tests/ -v
```

## Status

Alpha — building the test suite and API surface to match aiomoqt's transport needs.

## Resources

- [picoquic](https://github.com/perlinguist/picoquic) — QUIC implementation by Christian Huitema
- [aiomoqt](https://github.com/gmarzot/aiomoqt-python) — MoQT protocol library using this transport
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)

## License

MIT License — see [LICENSE](LICENSE)
