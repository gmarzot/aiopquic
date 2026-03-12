# aiopquic — Implementation Plan

## Goal

Build a Python/Cython wrapper around **picoquic** (C) that acts as a **drop-in replacement for aioquic**, targeting Python 3.14 free-threaded (noGIL) and Cython 3.1+. Minimize data copies. Include QUIC transport + HTTP/3.

---

## Architecture Overview

```
Application (Python asyncio)
    |
    v
aiopquic Python layer          # aioquic-compatible API
  QuicConnection, QuicConfiguration, connect(), serve()
  H3Connection, H3 events
    |
    v
Cython bridge layer            # typed memoryviews, zero-copy
  _transport.pyx               # manages picoquic lifecycle + thread
    |                |
    v                v
  RX SPSC Ring     TX SPSC Ring   # lock-free, C atomics
  (eventfd notify)  (pipe wakeup)
    |                |
    v                v
picoquic thread (C)            # picoquic_start_network_thread()
  callback_fn ──> RX ring      # stream data, events -> ring
  wake_up cb  <── TX ring      # drains TX ring -> picoquic APIs
    |
    v
UDP sockets (managed by picoquic's sockloop)
```

### Key Design Decisions

1. **picoquic's built-in threaded packet loop** — Use `picoquic_start_network_thread()` which handles sockets, timers, and packet processing in a dedicated pthread. The `picoquic_packet_loop_wake_up` callback runs in the network thread context, making picoquic API calls safe.

2. **SPSC ring buffers + eventfd** — Lock-free data path. Two rings:
   - **RX ring**: picoquic callback (network thread) → asyncio thread. Notified via `eventfd`.
   - **TX ring**: asyncio thread → picoquic network thread. Drained via `picoquic_wake_up_network_thread()` which triggers the wake_up callback.

3. **Zero-copy send via active streams** — Use `picoquic_mark_active_stream()` + `picoquic_provide_stream_data_buffer()`. When picoquic is ready to send, its `prepare_to_send` callback reads from the TX ring and writes directly into the packet buffer. One copy total (ring → packet buffer).

4. **Zero-copy receive** — picoquic's callback provides `(uint8_t* bytes, size_t length)` valid only during the callback. We copy into the RX ring's inline data area (one unavoidable copy). The asyncio side reads via typed memoryview — no further copies.

5. **Vendored build** — picoquic as git submodule. Built separately via a build script (`build_picoquic.sh`), then `setup.py` + `Cython.Build` links against it. No scikit-build-core — simple and transparent.

6. **Free-threading** — `# cython: freethreading_compatible = True`. The SPSC rings use C11 atomics. Control-plane operations (connection create/destroy, stream open/close) use `cython.pymutex` where needed.

---

## Data Flow Detail

### Receive Path (1 copy)

```
UDP packet arrives
  → picoquic decrypts, parses frames
  → calls stream_data_cb_fn(cnx, stream_id, bytes, length, event, ...)
  → C callback: memcpy(rx_ring.data_arena + write_pos, bytes, length)
  → C callback: write ring entry {stream_id, event, offset, length}
  → C callback: eventfd_write(efd, 1)  [wakes asyncio]
  → asyncio add_reader fires
  → Python drains ring: creates StreamDataReceived event with memoryview into arena
  → Application processes event (zero additional copies)
```

### Send Path — Active Stream (1 copy)

```
Application: conn.send_stream_data(stream_id, data)
  → Python writes {stream_id, data_ptr, length, is_fin} to TX ring
  → Python calls picoquic_wake_up_network_thread()
  → Network thread: wake_up callback marks stream active
  → Later: picoquic calls prepare_to_send callback
  → C callback: picoquic_provide_stream_data_buffer() → gets packet buffer ptr
  → C callback: memcpy(packet_buf, tx_ring.data, length)  [1 copy]
  → picoquic encrypts and sends
```

### Datagram Path (1 copy each direction)

```
Send: app data → picoquic_queue_datagram_frame() via wake_up callback (1 copy into picoquic)
Recv: picoquic_callback_datagram → RX ring (1 copy) → DatagramFrameReceived event
```

---

## SPSC Ring Buffer Design

Implemented in C, wrapped by Cython `.pxd`:

```c
typedef struct {
    _Alignas(64) _Atomic(uint64_t) head;  // consumer (reader) position
    _Alignas(64) _Atomic(uint64_t) tail;  // producer (writer) position
    uint32_t capacity;                     // must be power of 2
    uint32_t entry_size;                   // sizeof(ring_entry_t)
    uint8_t* entries;                      // ring_entry_t[capacity]
    uint8_t* data_arena;                   // inline data storage
    uint32_t arena_size;
    _Alignas(64) _Atomic(uint64_t) arena_write_pos;
    _Alignas(64) _Atomic(uint64_t) arena_read_pos;
} spsc_ring_t;

typedef struct {
    uint64_t stream_id;
    uint32_t event_type;      // picoquic_call_back_event_t
    uint32_t data_offset;     // offset into data_arena
    uint32_t data_length;
    uint8_t  is_fin;
    uint8_t  reserved[3];
    void*    cnx;             // picoquic_cnx_t* (for multi-connection)
    void*    stream_ctx;
} ring_entry_t;
```

- `head`/`tail` on separate cache lines (64-byte aligned) to avoid false sharing
- `data_arena` is a circular buffer for variable-length payload
- Producer: `atomic_store_explicit(&tail, new_tail, memory_order_release)`
- Consumer: `atomic_load_explicit(&tail, memory_order_acquire)`
- No CAS, no locks, no spinlocks

---

## API Mapping: aioquic → aiopquic

### Sans-IO Layer (`aiopquic.quic`)

| aioquic | aiopquic | Notes |
|---------|----------|-------|
| `QuicConnection(configuration)` | Same | Wraps picoquic_cnx_t |
| `QuicConfiguration` | Same | Maps to picoquic_create() params + transport params |
| `conn.receive_datagram(data, addr)` | Same | Feeds data via TX ring (or direct if same-thread fallback) |
| `conn.datagrams_to_send()` | Same | Drains outbound packet queue |
| `conn.next_event()` | Same | Drains RX ring entries, returns QuicEvent subclasses |
| `conn.send_stream_data(stream_id, data)` | Same | Writes to TX ring |
| `conn.get_next_available_stream_id()` | Same | |

### Events (`aiopquic.quic.events`)

| aioquic Event | picoquic Callback | Notes |
|---------------|-------------------|-------|
| `HandshakeCompleted` | `picoquic_callback_ready` | + ALPN from picoquic |
| `StreamDataReceived` | `picoquic_callback_stream_data/fin` | |
| `StreamReset` | `picoquic_callback_stream_reset` | |
| `ConnectionTerminated` | `picoquic_callback_close/application_close` | |
| `DatagramFrameReceived` | `picoquic_callback_datagram` | |
| `ConnectionIdIssued` | — | May need internal tracking |
| `PingAcknowledged` | — | Can implement via app_wakeup |

### asyncio Layer (`aiopquic.asyncio`)

| aioquic | aiopquic | Notes |
|---------|----------|-------|
| `connect(host, port, *, configuration, ...)` | Same signature | Uses picoquic_start_network_thread() |
| `serve(host, port, *, configuration, ...)` | Same signature | |
| `QuicConnectionProtocol` | Same | eventfd-based event processing |
| `protocol.create_stream()` | Same → `(StreamReader, StreamWriter)` | |
| `protocol.wait_connected()` | Same | |

### HTTP/3 Layer (`aiopquic.h3`)

| aioquic | aiopquic | Notes |
|---------|----------|-------|
| `H3Connection(quic)` | Same | Pure Python, operates on QuicConnection |
| `h3.send_headers(stream_id, headers)` | Same | |
| `h3.send_data(stream_id, data)` | Same | |
| `h3.handle_event(event)` | Same → H3Event | |
| QPACK | Use `pylsqpack` | Same dependency as aioquic |

---

## Project Structure

```
aiopquic/
├── pyproject.toml                  # Package metadata + build deps
├── setup.py                        # Cython.Build extensions, links against picoquic
├── build_picoquic.sh               # Builds picoquic + picotls via cmake
├── bootstrap_python.sh             # Python 3.14 + uv setup (exists)
├── .python-version                 # 3.14.3 (exists)
├── third_party/
│   └── picoquic/                   # git submodule (includes picotls, deps)
├── src/
│   └── aiopquic/
│       ├── __init__.py             # Version, top-level exports
│       ├── _binding/
│       │   ├── c/
│       │   │   ├── spsc_ring.h     # SPSC ring buffer (C11 atomics)
│       │   │   ├── spsc_ring.c
│       │   │   ├── callback.h      # picoquic callback → ring bridge
│       │   │   └── callback.c
│       │   ├── picoquic.pxd        # Cython declarations for picoquic.h
│       │   ├── picoquic_loop.pxd   # Cython declarations for packet_loop.h
│       │   ├── spsc_ring.pxd       # Cython declarations for ring buffer
│       │   ├── _transport.pyx      # Main Cython module: lifecycle, thread, rings
│       │   └── _transport.pyi      # Type stubs for IDE support
│       ├── quic/
│       │   ├── __init__.py
│       │   ├── connection.py       # QuicConnection (API-compat with aioquic)
│       │   ├── configuration.py    # QuicConfiguration dataclass
│       │   ├── events.py           # QuicEvent subclasses
│       │   └── stream.py           # Stream state tracking
│       ├── h3/
│       │   ├── __init__.py
│       │   ├── connection.py       # H3Connection
│       │   └── events.py           # H3Event subclasses
│       └── asyncio/
│           ├── __init__.py         # Exports connect, serve, QuicConnectionProtocol
│           ├── client.py           # connect() async context manager
│           ├── server.py           # serve() function
│           └── protocol.py         # QuicConnectionProtocol + stream adapters
└── tests/
    ├── test_spsc_ring.py           # Ring buffer correctness + stress tests
    ├── test_transport.py           # Cython transport lifecycle
    ├── test_connection.py          # QuicConnection API tests
    ├── test_asyncio.py             # connect/serve integration tests
    ├── test_h3.py                  # HTTP/3 tests
    ├── test_interop.py             # Interop with aioquic client/server
    └── bench/
        ├── bench_throughput.py     # Throughput benchmarks
        └── bench_latency.py        # Latency benchmarks
```

---

## Implementation Phases

### Phase 1: Build System + C Foundation

**Goal**: picoquic compiles as part of the Python package, SPSC ring works.

1. Add picoquic as git submodule in `third_party/picoquic`
2. Create `build_picoquic.sh`:
   - Runs cmake to build picoquic + picotls as static libraries
   - Installs headers + libs into `third_party/picoquic/build/`
3. Create `setup.py` with `Cython.Build`:
   - `Extension("aiopquic._binding._transport", ...)` linking against built picoquic
4. Create `pyproject.toml` with package metadata + build deps
4. Implement `spsc_ring.h/c`:
   - `spsc_ring_create()`, `spsc_ring_destroy()`
   - `spsc_ring_push()`, `spsc_ring_pop()`
   - `spsc_ring_push_with_data()` (copies payload into arena)
   - `spsc_ring_peek_data()` (returns pointer into arena, no copy)
   - C11 atomics, cache-line aligned
5. Implement `callback.h/c`:
   - `aiopquic_stream_cb()` — the picoquic callback function
   - Writes events + data to RX ring
   - Signals eventfd
6. Write `picoquic.pxd` — Cython extern declarations for picoquic.h key types/functions
7. Write `spsc_ring.pxd` — Cython extern declarations for ring buffer
8. Write minimal `_transport.pyx`:
   - Can create/destroy a picoquic context
   - Ring buffer accessible from Python
9. Tests: `test_spsc_ring.py` — correctness, multi-threaded stress

**Deliverable**: `pip install -e .` builds everything, ring buffer passes tests.

### Phase 2: Threaded Transport Core

**Goal**: picoquic runs in background thread, events flow to Python.

1. Implement `_transport.pyx` `TransportContext` cdef class:
   - `start(port, cert, key, alpn)` → calls `picoquic_create()` + `picoquic_start_network_thread()`
   - `stop()` → calls `picoquic_delete_network_thread()` + `picoquic_free()`
   - `eventfd` creation, asyncio `add_reader()` registration
   - RX ring drain method returning list of `(stream_id, event_type, data_memoryview)`
2. Implement loop callback (`picoquic_packet_loop_cb_fn`):
   - `picoquic_packet_loop_wake_up` → drain TX ring, call picoquic APIs
   - `picoquic_packet_loop_ready` → signal Python that transport is ready
3. Implement `aiopquic_stream_cb()` fully:
   - Handle all `picoquic_call_back_event_t` variants
   - Map to ring entries
   - For `prepare_to_send`: read from TX ring, provide to `picoquic_provide_stream_data_buffer()`
   - For `prepare_datagram`: read from TX datagram ring
4. Client connection: `create_client_connection(host, port, sni, alpn)` method
5. Server accept: auto-accept via default callback, notify Python via RX ring
6. Tests: create context, connect loopback, send/receive stream data

**Deliverable**: Can open a QUIC connection, send/receive bytes on streams.

### Phase 3: Python API Layer (Sans-IO Compatible)

**Goal**: `aiopquic.quic` module with aioquic-compatible classes.

1. `aiopquic/quic/events.py`:
   - `QuicEvent` base, `HandshakeCompleted`, `StreamDataReceived`, `StreamReset`,
     `ConnectionTerminated`, `DatagramFrameReceived`, `ConnectionIdIssued`, etc.
2. `aiopquic/quic/configuration.py`:
   - `QuicConfiguration` dataclass matching aioquic's fields
   - Maps to picoquic_create() params + picoquic_tp_t
3. `aiopquic/quic/connection.py`:
   - `QuicConnection` wrapping `TransportContext`
   - `send_stream_data(stream_id, data, end_stream=False)`
   - `next_event() → Optional[QuicEvent]`
   - `get_next_available_stream_id(is_unidirectional=False)`
   - `close(error_code=0, reason_phrase="")`
   - Datagram send/receive methods
4. `aiopquic/quic/stream.py`:
   - Internal stream state tracking
   - Flow control awareness
5. Tests: unit tests for all QuicConnection methods

**Deliverable**: `QuicConnection` API works, compatible with aioquic's interface.

### Phase 4: asyncio Integration

**Goal**: `connect()`, `serve()`, `QuicConnectionProtocol` — the high-level API.

1. `aiopquic/asyncio/protocol.py`:
   - `QuicConnectionProtocol`:
     - Wraps `QuicConnection` + `TransportContext`
     - `eventfd` reader registered on asyncio loop
     - `_process_events()` drains RX ring on eventfd signal
     - `create_stream(is_unidirectional=False) → (StreamReader, StreamWriter)`
     - `wait_connected()`, `wait_closed()`
     - `transmit()` → writes to TX ring + wake_up
   - `QuicStreamAdapter` (asyncio.Transport for streams)
2. `aiopquic/asyncio/client.py`:
   - `connect(host, port, *, configuration, create_protocol, ...)` async context manager
   - DNS resolution, socket setup delegated to picoquic
3. `aiopquic/asyncio/server.py`:
   - `serve(host, port, *, configuration, create_protocol, ...)`
   - Accepts connections via picoquic's server mode
4. Tests: full client/server integration, multiple streams, connection lifecycle

**Deliverable**: Can use `async with connect(...)` and `serve(...)` like aioquic.

### Phase 5: HTTP/3

**Goal**: `aiopquic.h3` module.

1. `aiopquic/h3/connection.py`:
   - `H3Connection(quic_connection)` — pure Python, same API as aioquic
   - Stream management (control, QPACK encoder/decoder, request streams)
   - Frame encoding/decoding (DATA, HEADERS, SETTINGS, GOAWAY, etc.)
   - Integration with `pylsqpack` for QPACK
2. `aiopquic/h3/events.py`:
   - `HeadersReceived`, `DataReceived`, `PushPromiseReceived`, `DatagramReceived`
3. Tests: H3 client/server, header compression, settings exchange

**Deliverable**: Working HTTP/3 client and server.

### Phase 6: Polish + Interop

**Goal**: Drop-in compatibility verified, performance benchmarked.

1. Interop tests:
   - aiopquic client ↔ aioquic server
   - aioquic client ↔ aiopquic server
   - aiopquic client ↔ aiopquic server
2. Performance benchmarks:
   - Throughput (bulk transfer)
   - Latency (small messages)
   - Memory usage
   - Compare against aioquic baseline
3. API compatibility audit:
   - Verify all public aioquic APIs are implemented
   - Verify event types match
   - Verify error handling matches
4. Documentation: usage examples, migration guide from aioquic
5. CI/CD: GitHub Actions for build + test on Linux

---

## Build Dependencies

| Dependency | Purpose | Source |
|-----------|---------|--------|
| picoquic | QUIC C library | git submodule |
| picotls | TLS 1.3 (used by picoquic) | bundled with picoquic |
| OpenSSL 3.x | Crypto backend for picotls | system or vendored |
| Python 3.14+ | Free-threaded runtime | uv install |
| Cython 3.1+ | C extension compiler | pip |
| setuptools | Python build backend | pip |
| pylsqpack | QPACK for H3 | pip |

## Risk / Open Questions

1. **picoquic submodule depth** — picoquic depends on picotls which depends on OpenSSL. Need to verify the CMake build handles the full dependency chain cleanly.

2. **Arena sizing** — The SPSC data arena size determines max in-flight data. Need to make this configurable. Default: 4MB per ring (8MB total per connection).

3. **Multi-connection support** — Each picoquic context can handle many connections, but they share one thread. Ring entries include `cnx` pointer to demux. Python side maintains a `{cnx_ptr: QuicConnection}` mapping.

4. **eventfd availability** — Linux-only. For macOS/BSD, fall back to `pipe()` or `kqueue` user events. For initial implementation, Linux-only is fine (WSL2 supports eventfd).

5. **H3 reuse** — Evaluate whether aioquic's `h3/` module can be used directly (it only depends on QuicConnection's public API). If so, Phase 5 becomes simpler.

6. **Cython free-threading maturity** — Cython 3.1 free-threading support is still evolving. May need workarounds for edge cases. The SPSC ring is pure C (no Cython codegen on the hot path), so this is lower risk than it appears.
