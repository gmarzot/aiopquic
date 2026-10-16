# AGENTS.md

`aiopquic` — Python/Cython binding to picoquic: QUIC and WebTransport for asyncio. Each
`TransportContext` runs picoquic on its own network thread and talks to the asyncio thread over
SPSC rings. The API is shaped like aioquic/qh3 but is not a drop-in replacement.

This file ships at the repo root and inside the installed package (`aiopquic/AGENTS.md`).

## Using aiopquic from an agent

`uv pip install aiopquic`, Python >= 3.12. Wheels: cp312–cp314, Linux x86_64/aarch64
(manylinux_2_34, OpenSSL 3.5 bundled) and macOS arm64; elsewhere the sdist builds (cmake, a C
compiler, OpenSSL headers). No Windows, no free-threaded 3.14t, no `py.typed`. Pre-releases need
an explicit specifier (`aiopquic>=0.5.0a3`) or `--pre`. Bug reports carry
`python -m aiopquic.versions` (picoquic/picotls SHAs, the OpenSSL actually loaded).

Import from `aiopquic.quic.configuration`, `aiopquic.quic.events`, `aiopquic.asyncio`,
`aiopquic.asyncio.webtransport`, `aiopquic.buffer`, `aiopquic.streamchain`, `aiopquic.exceptions`,
`aiopquic.qlog`. From `aiopquic._binding._transport` take only `TransportContext` (to share one),
the `KEX_*` ids and `counters`; everything else there, and raw pointers, are test paths.

- Raw QUIC: `async with connect(host, port, configuration=cfg, create_protocol=P) as p:`;
  `await serve(host, port, configuration=cfg, create_protocol=P)`. `P` subclasses
  `QuicConnectionProtocol` and overrides `quic_event_received`.
- WebTransport: `async with connect_webtransport(host, port, path, sni=..., configuration=cfg) as
  wt:`, then `create_stream()`, `send_stream_data()`, `receive_stream_data(sid)`, `events()`.
  `serve_webtransport(host, port, path, handler=h, cert_file=..., key_file=..., configuration=cfg)`
  calls `h(session)` per CONNECT. `WebTransportError` is a `ConnectionError`.
- `serve_dispatch(...)` serves both on one UDP port by ALPN (`h3` is added to `alpn_protocols`).
  WebTransport subprotocols: `wt_available_protocols=` / `wt_supported_protocols=`. The server
  picks by its own order; an offer with no match is refused with HTTP 400.

**Lifecycle.** `TransportContext.start()` creates picoquic and its thread, once. `stop()` drains
the TX ring to picoquic (up to 50 ms) and joins the thread; it does not wait for the wire, so
`aclose()` first when the peer must see the close. `connect()` and `connect_webtransport()` own the
transport they create and exit through `aclose()`; with `transport=t` the caller owns `t`. A
WebTransport session built directly instead of through `connect_webtransport()` must
`await session.detach()` after `aclose()` and before `transport.stop()`, or its transport's
event dispatcher (and the wake fd) is held for the life of the process.

**Threading.** One network thread per `TransportContext`. Call everything on a transport, `stop()`
included, from its event loop's thread: each TX ring has one producer and the producer side relies
on the GIL.

**Certificates.** Clients verify the chain and name against certifi's roots by default. Private
CA: `QuicConfiguration.cafile` or `connect_webtransport(ca_file=)`. Accept anything:
`verify_mode=ssl.CERT_NONE` or `verify_peer=False`. Servers never verify clients.

**Congestion control.** Default `"bbr1"`; `None` is picoquic's `newreno`. Names, matched exactly:
`newreno` (alias `reno`), `cubic`, `dcubic`, `fast`, `bbr` (v3), `bbr1`, `prague`, `c4`. An unknown
name (`"BBR1"` among them) raises `ValueError` from `QuicConfiguration` and at transport start.

**Datagrams** are on by default. `send_datagram_frame(data)` returns `len(data)`, or 0 when the
record ring is full: wait on `get_datagram_tx_drain_event()` and resend the same payload.

**Priority.** `set_stream_priority(sid, p)`: 0 highest, 255 lowest, picoquic default 9; the low bit
picks round robin (even) or FIFO by stream id (odd) among equal priorities. A return of 1 means the
TX event ring was full and nothing applied.

**TX backpressure.** `send_stream_data()` is all-or-nothing: `BufferError` commits nothing, so
resend the same buffer; `ValueError` means one write exceeds `stream_ring_cap` (4 MiB).
`await send_stream_data_drained()` does the waiting. For a byte budget below ring-full use
`tx_data_ring_used(sid)` with `get_tx_data_drain_event(sid)` / `set_tx_data_drain_pending()`.
`tx_sent_bytes(sid)` is the stream's cumulative bytes picoquic has taken into packets: an offset
has left the ring once it is exceeded.

## Layout

| Path | What |
|---|---|
| `src/aiopquic/quic/` | `QuicConfiguration` (every knob, its default and why), `QuicConnection`, `QuicEngine` (server demux), events |
| `src/aiopquic/asyncio/` | `connect`, `serve`, the protocol class, `dispatch.py` (one-port dual stack), `webtransport.py` |
| `src/aiopquic/buffer.py`, `streamchain.py` | re-exports of the Cython codecs: varint, draft-18 vi64, MoQT subgroup objects |
| `src/aiopquic/_binding/_transport.pyx` | `TransportContext`, `StreamChunk`, `WebTransportSessionState`, the RX drain loops |
| `src/aiopquic/_binding/c/` | worker-thread C: `callback.h` (raw callback, TX dispatch), `h3wt_callback.h` (WebTransport), ring and stream-context headers |
| `third_party/`, `patches/` | submodules picoquic, picotls, liburing; picoquic patches |
| `tests/` | pytest suite; `interop/` peer stacks; `bench/` opt-in |
| `DATAFLOW.md`, `PERFORMANCE.md` | TX/RX/flow-control model, counters; numbers, build flags, tuning |

## Setup and build

    ./bootstrap_python.sh         # .venv: uv-managed CPython 3.14, Cython
    source .venv/bin/activate
    ./build.sh                    # reconcile submodules, build native if stale, relink, verify
    uv pip install -e '.[dev]'    # test extras

- `build.sh` installs editable into the **active** venv and fails unless the imported `aiopquic`
  is this tree. Use one venv per worktree; never point another project's venv at a work tree.
- Re-run it after any checkout or submodule bump. `--check` is a read-only doctor; `--native` only
  compiles. `.build_state` fingerprints submodule SHAs, patch files, flavor, compiler and
  `OPENSSL_ROOT_DIR`; binding sources always relink.
- `AIOPQUIC_PERF=1` (default) is host-tuned and not portable; `AIOPQUIC_WHEEL_BUILD=1` is portable
  (CI, wheels).
- OpenSSL is the main performance variable. Linux wheels bundle 3.5; for parity build with
  `OPENSSL_ROOT_DIR=<3.5 prefix> ./build.sh`.
- Submodules are `ignore = dirty` because the build patches picoquic in place.
  `./update_submodules.sh` reports drift; `--advance` moves picoquic behind a rebuild and tests.

## Tests

    python -m pytest tests                                   # CI core tier
    python -m pytest tests -m native                         # picoquic's own suites
    python -m pytest tests/bench                             # opt-in benchmarks

- `interop` (public endpoints) and `native` are deselected by default; `-m native` or
  `-m interop` selects them.
- Ports come from `tests/_ports.next_port()`. `tests/interop/` peers need
  `pip install aioquic 'qh3<2'`.
- `tests/wt_close_scenarios.py` and `tests/dgram_release_scenarios.py` are not collected: their
  tests run each scenario in a fresh process, so process-wide counters start at zero and a native
  crash takes only the child.
- Every fix needs a test that fails without it: no vacuous passes, no fixed sleeps racing slow
  runners, no process-wide counter reads that earlier tests can skew.

## CI and releases

- `cicd.yml` runs on pull requests and on push to `main` only: a working branch gets CI once a PR
  is open from it. Jobs: `core` (ubuntu and macos × 3.12–3.14), `native`, `peer interop`,
  `benchmark` (continue-on-error).
- `release.yml` (`workflow_dispatch`, `version` = `N.N.N[{a,b,rc}N]`) tags the dispatched ref's
  HEAD, tests the tag, builds the sdist and wheels, publishes to TestPyPI then PyPI, and creates
  the GitHub release.
- Flow: work lands on `gmarzot-X.Y.Z`; the release PR is squash-merged to `main`, `release.yml`
  runs on `main` so the tag is the squash commit, then `main` is merged back into the working
  branch. Check what shipped with `git diff <commit> <tag>`, not ancestry.

## Conventions

- Comments say what the code does or why a non-obvious choice is necessary, in as few words as
  possible: no process narration, issue references or history. Same for YAML and shell.
- Product Python has no synchronous waits or polling sleeps; every wait ends on an event or on
  picoquic's timers.
- CHANGELOG.md: one bullet per change, stating the behavior; fixes start `Fix:`.
- Commits: `area: short statement` (`wt`, `quic`, `tls`, `rings`, `stream`, `dgram`, `dispatch`,
  `codec`, `tests`, `ci`, `deps`, `docs`).
- `[tool.ruff]`: line-length 100, `E`/`F`/`W`, `E501` ignored; CI does not lint.

## Native-code rules

- The network thread owns all picoquic state; after `start()` every picoquic call runs there,
  reached through a TX event. picoquic's thread checks are compiled out: a cross-thread call is a
  silent race. `stop()` and `__dealloc__` act only after the join.
- asyncio never dereferences a cnx; accessors read snapshots the worker sends.
- asyncio alone produces `tx_event_ring`, each `sc->tx` and each datagram ring, and alone consumes
  `rx_event_ring` and `sc->rx`. Stream bytes never ride the event rings: TX posts MARK_ACTIVE and
  the worker pulls `sc->tx`; RX fills `sc->rx`. A full RX event ring spills to an order-preserving
  overflow. A full TX ring is `BufferError` or a return of 1.
- The worker never blocks, writes stderr only behind env flags read once, and uses picoquic's
  public API only. RESET_STREAM goes only where we send, STOP_SENDING only where we receive.
- An event that borrows an `sc`, link or ring is queued before the event that releases it: FIFO
  order on the TX ring is the lifetime guarantee. Datagram rings are refcounted (the Python object,
  the worker's cnx table, a WT session); a MARK holds no reference, so Python's release is a TX
  event (`DGRAM_RING_RELEASE`) handled behind every MARK queued before it.
- `aiopquic_stream_ctx_t` starts with the stream's reference; `aiopquic_stream_ctx_destroy()` is
  the unref and the last frees. Never free on our own FIN: picoquic calls back with the `sc` until
  `stream_released`.
- picoquic may free a cnx any time after its close callback. The dead-connection guard
  (`aiopquic_cnx_is_alive()`) runs before any TX event that names a cnx and drops the event on a
  miss; it compares addresses, so a new cnx at a freed one's address passes. WT TX events carry a
  session and test `s->cnx` instead; ring releases bypass the guard (the cnx is only a table key).
- Cython does not None-check typed `cdef` attributes, and `tp_clear` can clear them before
  `__dealloc__`: check first. New `aiopquic_ctx_t` fields go at the tail.

## picoquic patches

`build.sh --native` applies `patches/*.patch`, skipping any already applied. A patch that neither
applies nor reverse-applies stops the build: drop or regenerate it.

| Patch | What it does | Fate |
|---|---|---|
| `picoquic-wt-connect-protocol` | CONNECT `:protocol` is always `webtransport`; picowt sends `webtransport-h3` once the server advertises SETTINGS_WT_ENABLED, which proxygen and browsers refuse | Temporary: remove once they accept `webtransport-h3` |
| `picoquic-wt-connect-lenient` | A client sends CONNECT once the server's SETTINGS arrive, dropping vetoes deployed relays fail; `AIOPQUIC_WT_STRICT_CONNECT=1` restores the gate | Local |
| `picoquic-alpn-null-guard` | negotiated-ALPN lookup tolerates a NULL cnx | Upstream candidate |
| `picoquic-h3zero-null-file-guard` | h3zero's client skips `fwrite` on a NULL `FILE*` | Upstream candidate |
| `picoquic-reset-at-ack-delete` | The ack of a RESET_STREAM_AT retires a stream we only sent on; picoquic gated it on the receive-side `consumed_offset`, so the stream stayed until the connection closed | Local |
| `picoquic-stop-sending-error-getter` | `picoquic_get_remote_stop_sending_error()` returns the code of a received STOP_SENDING | Local |

## Known traps

- picoquic's `bbr` (v3) freezes cwnd below ~128 µs RTT: loopback, the one place not to use it.
  `newreno` or `cubic` give clean loopback timing; loss-based CCs collapse on the GIL-induced loss
  blips of a loaded host.
- `connect_webtransport()` / `serve_webtransport()` without `configuration=` run picoquic's
  defaults, not `QuicConfiguration`'s.
- The key-exchange order is process-wide: the last `start()` wins. ML-KEM shares need OpenSSL 3.5.
- IPv4 only: the `serve*` functions bind the IPv4 wildcard.
- The raw-QUIC `tx_max_queued_bytes` gate reads the process-wide queued total, so one stalled peer
  parks new streams on every raw connection; the WebTransport gate is per session.
- `max_stream_data` (16 MiB) is both the advertised window and the per-stream RX ring.
- A stalled consumer back-pressures the peer to silence and the connection idles out; set
  `keep_alive_interval`. On a closed raw connection `send_stream_data()` silently does nothing;
  check `quic.closed`.
- qlog roughly halves multi-Gbps throughput.
- macOS: no GSO; `AIOPQUIC_SEND_LENGTH_MAX` above `net.inet.udp.maxdgram` fails with EMSGSIZE.
