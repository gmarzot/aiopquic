# aiopquic - Async QUIC + WebTransport (picoquic)

`aiopquic` is a Python/Cython binding to [picoquic](https://github.com/private-octopus/picoquic), providing high-performance QUIC transport and WebTransport for `asyncio` applications.

## Overview

`aiopquic` exposes picoquic's QUIC implementation through a lock-free SPSC ring buffer architecture that bridges the picoquic network thread with Python's asyncio event loop. It provides an asyncio QUIC/HTTP3 transport API in the spirit of aioquic (and its fork qh3) — similar shapes for `QuicConfiguration`, `QuicConnection`, `connect` / `serve`, and event types — plus a native WebTransport client/server layered on picoquic's H3 + h3zero. Not a drop-in replacement: semantics differ around backpressure (`send_stream_data` raises `BufferError` on full per-stream ring) and flow-control sizing.

### Architecture

- **SPSC Ring Buffers** -- Lock-free single producer/single consumer rings for event passing between threads, separate TX and RX rings per `TransportContext`.
- **TX path** -- Asyncio pushes into per-stream byte ring; picoquic pulls at wire rate via `prepare_to_send`.
- **RX path** -- picoquic pushes per-event `StreamChunk`s; ownership transfers at pop for 1-copy delivery.
- **Cross-platform wake fd** -- Linux `eventfd` for efficient asyncio `add_reader()` notification; `pipe()` self-pipe fallback on macOS / BSD.
- **Dedicated Network Thread** -- picoquic runs in its own thread via `picoquic_start_network_thread()`.
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

### Test Results

Tests pass on Linux and macOS. The interop suite is opt-in (network-dependent).

| Suite | Coverage |
|-------|----------|
| `test_spsc_ring` | per-event malloc ring lifecycle |
| `test_buffer` | Cython `Buffer` |
| `test_transport` | Transport lifecycle, wake fd, wake-up, connection management |
| `test_loopback` | 17 tests — handshake, streams, FIN, reset, datagrams, ALPN mismatch, idle timeout, app-close codes, stop_sending, many-streams stress, TX-ring overflow |
| `test_asyncio` | client/server stream + datagram exchange via `connect` / `serve` |
| `test_baton_pattern` | Pure-QUIC baton-style stream multiplexing (UNI ↔ BIDI) |
| `test_native_picoquic` | picoquic_ct / picohttp_ct subprocess driver |
| `test_interop` | Real public endpoints (opt-in) |
| `tests/bench/` | microbenches: ring push/pop, single-shot/sustained/parallel/bidirectional throughput, datagrams, RTT latency, handshake rate, byte-verifying object stress + stream churn + concurrent streams (opt-in via `pytest tests/bench`) |

### Performance

Sustained single-stream throughput, 30s steady-state, byte-verifying, high-level asyncio API (`QuicConnection.send_stream_data`):

| platform | 1 KiB | 4 KiB | 16 KiB |
|---|---|---|---|
| AMD Ryzen 7 PRO 7840U / WSL2 / Linux 6.6 | 1,570 Mbps | 2,118 Mbps | 2,031 Mbps |
| Apple M-series / macOS Sonoma | 953 Mbps | 1,130 Mbps | 1,104 Mbps |

These are over local UDP loopback at the QUIC default MTU (~1,400 B). **The realistic ceiling at that MTU is the kernel's per-syscall sendmsg rate, not bandwidth.** On Ryzen WSL2, raw `iperf3 -u -l 1400` over loopback maxes at **3.15 Gbps** (≈ 280 K syscalls/s); raise the datagram size and it climbs cleanly — 4 KiB → 7.9, 8 KiB → 12.8, 32 KiB → 33.7 Gbps. So QUIC pinned at MTU is in a regime where the syscall rate is the wall.

In that regime, here's where the layers land on Ryzen WSL2:

| layer | ss_mbps | of UDP@1400 ceiling |
|---|---|---|
| `iperf3 -u -l 1400` (raw UDP loopback) | 3,150 | 100 % |
| `picoquicdemo -a perf` (picoquic over UDP) | 2,184 | 69 % |
| `aiopquic` lowlevel (SPSC ring + UDP) | 2,322 | 74 % |
| `aiopquic` highlevel (asyncio + SPSC + UDP) | 2,031 | 64 % |
| **`sim_link_bench`** (picoquic only, no kernel UDP) | **11,216** | — *(off-axis)* |

The asyncio wrapper costs ~10 % below the lowlevel SPSC path; picoquic's own QUIC framing/encryption/ACK overhead accounts for ~25 % vs raw UDP. Both are normal for QUIC-over-loopback at MTU.

`sim_link_bench` (`tests/bench/sim_link/`) drives picoquic over its `picoquictest_sim_link` simulated link — packets are routed in-process between two `picoquic_quic_t` instances, no kernel UDP, no sockets, no syscall-rate ceiling. It isolates picoquic protocol CPU cost from the loopback wall and is platform-independent. The 11.2 Gbps number above is what picoquic can do without any kernel involvement on this hardware. Build with `./tests/bench/sim_link/build.sh` after `./build_picoquic.sh`.

Calibrate on your own hardware:

```bash
# UDP-over-loopback path (what aiopquic users actually see)
pytest tests/bench/bench_baselines_highlevel.py -s -v          # 30s default
pytest tests/bench/bench_baselines_highlevel.py -s -v --duration=60

# Protocol-only reference (no kernel UDP)
PICOQUIC_SOLUTION_DIR=third_party/picoquic/ \
    tests/bench/sim_link/sim_link_bench --duration-s 30 --rate-gbps 100
```

Microbenches (ring lifecycle, stream churn, concurrent-streams short bursts) live under `tests/bench/` for development reference. Their reported numbers are *not* representative of sustained throughput — short windows inflate numbers from warmup transients (a 100-stream churn case at 256 B per stream measures ~1 ms of work, dominated by setup cost).

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
aiopquic 0.3.5.dev4+g2ffe8947d.d20260522
         /path/to/aiopquic
picoquic 2b1e14d5a46532eadf691edef5bd747da6de6557
picotls  f350eab60742138ac62b42ee444adf04c7898b0d
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

## Performance build (opt-in)

Default builds use `CMAKE_BUILD_TYPE=Release` (`-O3 -DNDEBUG`), portable across hosts. Two opt-in env vars layer on host-tuned optimizations for local benching — neither is enabled in PyPI wheels:

```bash
# Host-tuned: Fusion AES-GCM (x86_64), DISABLE_DEBUG_PRINTF,
# -O3 -march=native -flto. Binary becomes machine-specific.
AIOPQUIC_PERF=1 ./build_picoquic.sh
```

Per-platform behavior:

| Knob                          | Linux x86_64 | Linux ARM64 | macOS arm64 | macOS x86_64 |
|-------------------------------|:------------:|:-----------:|:-----------:|:------------:|
| `-O3 -DNDEBUG` (always on)    |      ✓       |      ✓      |      ✓      |      ✓       |
| `DISABLE_DEBUG_PRINTF`        |      ✓       |      ✓      |      ✓      |      ✓       |
| Fusion AES-GCM (CPUID-dispatched) | ✓        |      –      |      –      |      ✓       |
| `-march=native` / `-mcpu=native` + `-flto` | ✓ | ✓     |      ✓      |      ✓       |

### Experimental: `AIOPQUIC_IO_URING=1` (DORMANT)

io_uring scaffolding is in the tree (`third_party/liburing` submodule, picoquic patch, setup.py linkage). Enabling it builds `picoquic_packet_loop_uring` into `libpicoquic-core.a` and statically links `liburing.a` into the Cython extension:

```bash
AIOPQUIC_IO_URING=1 ./build_picoquic.sh   # auto-fetches + builds liburing-2.7
uv pip install -e '.[dev]'                 # re-cythonize with PICOQUIC_WITH_IO_URING define
```

**This currently has no runtime effect.** aiopquic's worker thread uses its own callback/SPSC-ring path and does not invoke `picoquic_packet_loop_uring`. The scaffolding is preserved so the worker can be migrated to io_uring later without re-discovering the build recipe (liburing submodule pin, picoquic header patch for kernel-uapi conflicts, ABI-critical define propagation through setup.py).

Linux-only. Compatible CPU architectures: x86_64, ARM64. Build will hard-error if `AIOPQUIC_IO_URING=1` is set on macOS / BSD / Windows.

> **ABI note:** `picoquic_network_thread_ctx_t` and `picoquic_socket_ctx_t` have conditional fields gated on `PICOQUIC_WITH_IO_URING`. The build-script + setup.py propagate the define to both picoquic-core *and* the Cython extension. A mismatch silently shifts `thread_is_ready` and other field offsets — the network thread appears to never become ready. Don't enable WITH_IO_URING in picoquic without also defining PICOQUIC_WITH_IO_URING in the Cython build.

## Runtime deployment guidance

These are runtime tunings, separate from build-time flags above. PyPI wheels ship with portable perf flags baked in (see [Performance build](#performance-build-opt-in)); these knobs apply on top of any binary.

### jemalloc for tail-latency reduction

The default `glibc` allocator's per-thread arenas + occasional coalescing show up as max-latency outliers under sustained high-throughput workloads. Preloading `jemalloc` measurably tightens the tail:

```bash
# Debian/Ubuntu:
sudo apt install libjemalloc2
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 python -m your_app

# Fedora/RHEL:
sudo dnf install jemalloc
LD_PRELOAD=/usr/lib64/libjemalloc.so.2 python -m your_app
```

Validated improvement on a representative aiopquic sustained workload (Ryzen 7 PRO, Linux loopback): `sd 7.1 ms → 4.3 ms`, `max 437 ms → 310 ms`, throughput unchanged. Effect is most visible at multi-Gbps over 60+ second runs; small workloads see no difference.

### GSO and send-length-max

GSO (UDP segmentation offload) is **already enabled by default on Linux** with `send_length_max=65535` (max kernel-coalesced stride). No user action needed. macOS / FreeBSD default to GSO off — picoquic's per-datagram `sendmsg` path is used instead. Env overrides:

```bash
AIOPQUIC_GSO=0                  # force off (diagnostic only)
AIOPQUIC_SEND_LENGTH_MAX=8192   # cap kernel-coalesced buffer (Linux GSO on)
```

### TX wake threshold

The TX SPSC event ring's drain-wake threshold defaults to 50% — producer is signalled to resume only after ≥ half the queued events have drained. Overridable to tune for latency vs. context-switch overhead:

```bash
AIOPQUIC_TX_RING_WAKE_PCT=25    # wake earlier (lower per-send latency, more context switches)
AIOPQUIC_TX_RING_WAKE_PCT=75    # wake later (more batching, slightly higher latency)
```

## Known Limitations

- **Free-threaded Python (3.14t) not yet supported** -- the TX-ring producer side, `TransportContext` lifecycle, and the WebTransport engine state currently rely on the GIL for serialization. FT support deferred until a per-context locking audit lands.
- **STOP_SENDING error codes** surface as 0 today: picoquic's public stream-error getter only returns the RESET_STREAM code. STOP_SENDING's code lives in `stream->remote_stop_error` in `picoquic_internal.h` (no public getter). A small helper that pulls the field is straightforward future work — see TODO in `src/aiopquic/_binding/c/callback.h`.
- **Per-stream wrapper cleanup before connection close** -- per-stream `aiopquic_stream_ctx_t*` wrappers are freed at connection close rather than at stream RESET/FIN. Bounded leak per cnx; flagged for follow-up.

## TODO

- Windows support (eventfd alternative — IOCP / WSAEventSelect on the wake-fd path)
- Free-threaded Python (3.14t) support after producer-side locking audit
- STOP_SENDING error-code surfacing helper (read `remote_stop_error` from picoquic_internal.h)
- Per-stream wrapper cleanup on RESET/FIN before connection close
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
