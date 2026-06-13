# aiopquic Performance

Throughput baselines, how to reproduce them, build-time performance
flags, and runtime deployment tuning. The TX/RX dataflow and
flow-control model behind these numbers — including the TX
backpressure parameters — are documented in [DATAFLOW.md](DATAFLOW.md).

## Throughput baselines

Sustained single-stream throughput, 30s steady-state, byte-verifying,
high-level asyncio API (`QuicConnection.send_stream_data`):

| platform | 1 KiB | 4 KiB | 16 KiB |
|---|---|---|---|
| AMD Ryzen 7 PRO 7840U / WSL2 / Linux 6.6 | 1,570 Mbps | 2,118 Mbps | 2,031 Mbps |
| Apple M-series / macOS Sonoma | 953 Mbps | 1,130 Mbps | 1,104 Mbps |

These are over local UDP loopback at the QUIC default MTU (~1,400 B).
**The realistic ceiling at that MTU is the kernel's per-syscall
sendmsg rate, not bandwidth.** On Ryzen WSL2, raw `iperf3 -u -l 1400`
over loopback maxes at **3.15 Gbps** (≈ 280 K syscalls/s); raise the
datagram size and it climbs cleanly — 4 KiB → 7.9, 8 KiB → 12.8,
32 KiB → 33.7 Gbps. So QUIC pinned at MTU is in a regime where the
syscall rate is the wall.

In that regime, here's where the layers land on Ryzen WSL2:

| layer | ss_mbps | of UDP@1400 ceiling |
|---|---|---|
| `iperf3 -u -l 1400` (raw UDP loopback) | 3,150 | 100 % |
| `picoquicdemo -a perf` (picoquic over UDP) | 2,184 | 69 % |
| `aiopquic` lowlevel (SPSC ring + UDP) | 2,322 | 74 % |
| `aiopquic` highlevel (asyncio + SPSC + UDP) | 2,031 | 64 % |
| **`sim_link_bench`** (picoquic only, no kernel UDP) | **11,216** | — *(off-axis)* |

The asyncio wrapper costs ~10 % below the lowlevel SPSC path;
picoquic's own QUIC framing/encryption/ACK overhead accounts for
~25 % vs raw UDP. Both are normal for QUIC-over-loopback at MTU.

`sim_link_bench` (`tests/bench/sim_link/`) drives picoquic over its
`picoquictest_sim_link` simulated link — packets are routed in-process
between two `picoquic_quic_t` instances, no kernel UDP, no sockets, no
syscall-rate ceiling. It isolates picoquic protocol CPU cost from the
loopback wall and is platform-independent. The 11.2 Gbps number above
is what picoquic can do without any kernel involvement on this
hardware. Build with `./tests/bench/sim_link/build.sh` after
`./build_picoquic.sh`.

With a full protocol stack on top (aiomoqt's MoQT layer, 8 parallel
streams with per-group stream churn), the same hardware sustains
~2.9–3.1 Gbps end-to-end with bounded memory on both raw QUIC and
WebTransport — see aiomoqt's PERFORMANCE.md for those benches.

### Calibrate on your own hardware

```bash
# UDP-over-loopback path (what aiopquic users actually see)
pytest tests/bench/bench_baselines_highlevel.py -s -v          # 30s default
pytest tests/bench/bench_baselines_highlevel.py -s -v --duration=60

# Protocol-only reference (no kernel UDP)
PICOQUIC_SOLUTION_DIR=third_party/picoquic/ \
    tests/bench/sim_link/sim_link_bench --duration-s 30 --rate-gbps 100
```

Microbenches (ring lifecycle, stream churn, concurrent-streams short
bursts) live under `tests/bench/` for development reference. Their
reported numbers are *not* representative of sustained throughput —
short windows inflate numbers from warmup transients (a 100-stream
churn case at 256 B per stream measures ~1 ms of work, dominated by
setup cost).

## Test and bench suites

Tests pass on Linux and macOS. The interop suite is opt-in
(network-dependent).

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

## Performance build (opt-in)

Default builds use `CMAKE_BUILD_TYPE=Release` (`-O3 -DNDEBUG`),
portable across hosts. An opt-in env var layers on host-tuned
optimizations for local benching — not enabled in PyPI wheels:

```bash
# Host-tuned: Fusion AES-GCM (x86_64), DISABLE_DEBUG_PRINTF,
# -O3 -march=native -flto. Binary becomes machine-specific.
AIOPQUIC_PERF=1 ./build_picoquic.sh
```

Per-platform behavior:

| Flag effect                   | Linux x86_64 | Linux ARM64 | macOS arm64 | macOS x86_64 |
|-------------------------------|:------------:|:-----------:|:-----------:|:------------:|
| `-O3 -DNDEBUG` (always on)    |      ✓       |      ✓      |      ✓      |      ✓       |
| `DISABLE_DEBUG_PRINTF`        |      ✓       |      ✓      |      ✓      |      ✓       |
| Fusion AES-GCM (CPUID-dispatched) | ✓        |      –      |      –      |      ✓       |
| `-march=native` / `-mcpu=native` + `-flto` | ✓ | ✓     |      ✓      |      ✓       |

### Experimental: `AIOPQUIC_IO_URING=1` (DORMANT)

io_uring scaffolding is in the tree (`third_party/liburing` submodule,
picoquic patch, setup.py linkage). Enabling it builds
`picoquic_packet_loop_uring` into `libpicoquic-core.a` and statically
links `liburing.a` into the Cython extension:

```bash
AIOPQUIC_IO_URING=1 ./build_picoquic.sh   # auto-fetches + builds liburing-2.7
uv pip install -e '.[dev]'                 # re-cythonize with PICOQUIC_WITH_IO_URING define
```

**This currently has no runtime effect.** aiopquic's worker thread
uses its own callback/SPSC-ring path and does not invoke
`picoquic_packet_loop_uring`. The scaffolding is preserved so the
worker can be migrated to io_uring later without re-discovering the
build recipe (liburing submodule pin, picoquic header patch for
kernel-uapi conflicts, ABI-critical define propagation through
setup.py).

Linux-only. Compatible CPU architectures: x86_64, ARM64. Build will
hard-error if `AIOPQUIC_IO_URING=1` is set on macOS / BSD / Windows.

> **ABI note:** `picoquic_network_thread_ctx_t` and
> `picoquic_socket_ctx_t` have conditional fields gated on
> `PICOQUIC_WITH_IO_URING`. The build-script + setup.py propagate the
> define to both picoquic-core *and* the Cython extension. A mismatch
> silently shifts `thread_is_ready` and other field offsets — the
> network thread appears to never become ready. Don't enable
> WITH_IO_URING in picoquic without also defining
> PICOQUIC_WITH_IO_URING in the Cython build.

## Runtime deployment guidance

These are runtime tunings, separate from build-time flags above. PyPI
wheels ship with portable perf flags baked in; these apply on top of
any binary. Transport-level configuration (TX budgets, ring
capacities, congestion control, flow-control windows) is covered in
[DATAFLOW.md](DATAFLOW.md) §Configuration parameters.

### Subscriber / fan-out tuning

Two different "fan-out" shapes with different walls — don't confuse
them:

**Per-process fan-out (N separate subscriber processes on one host)**
is the common shape, and the wall is **host resources, not
per-connection transport limits.** Each subscriber to a single track
holds only a couple of uni streams at a time, so the transport limits
below are ample; what bites first is N processes contending for CPU,
sockets, and memory:

- A scheduling gap stalls a process's asyncio drain → flow control
  back-pressures its sender to silence → the now-idle connection drops
  on the idle timeout. Mitigate with `keep_alive_interval` (below).
- **Socket buffers are already large.** aiopquic requests a 64 MiB UDP
  send+receive buffer via picoquic (`socket_buffer_size`); it is *not*
  the kernel default. But Linux **clamps** the request to
  `net.core.rmem_max` / `wmem_max` (and doubles internally), so the
  *effective* buffer is `min(64 MiB, rmem_max)`. On a stock box
  (`rmem_max` ~200 KB) that clamp is the real receive buffer — so the
  lever is the **host sysctl**, not an aiopquic setting. Raise
  `rmem_max`/`wmem_max` (kernel prereqs below) to actually get a large
  buffer. Per-socket memory then is the clamped value × N processes —
  size `rmem_max` with that product in mind for high process counts.
  To *lower* the per-socket request (cap memory under heavy
  process fan-out), set `QuicConfiguration.socket_buffer_size` (bytes;
  default 64 MiB).
- The socket is owned by picoquic's network thread with its own
  recv loop — aiopquic does not use asyncio's datagram transport, so
  the "one recvfrom per loop wakeup" pitfall does not apply.
- GSO (send-side segmentation offload) is on by default on Linux
  (`AIOPQUIC_GSO`, `AIOPQUIC_SEND_LENGTH_MAX`). Receive-side GRO
  coalescing is *not* enabled in picoquic today — a potential
  recv-path improvement tracked upstream.
- Diagnose with `ss -uanm` (per-socket `rb`=rcvbuf, `d`=drops) and
  `netstat -su | grep -A1 Udp:` (`RcvbufErrors` / `InErrors`).

**Per-connection fan-out (one subscriber, a high-subgroup-count or
many-track session)** is where the transport limits matter — each
subgroup is its own uni stream:

- **`max_streams_uni`** (`QuicConfiguration`, default 512) — the
  MAX_STREAMS credit granted to the peer. picoquic auto-replenishes it
  as streams complete, so a typical few-subgroup track never
  approaches 512. It only bites if the cumulative open rate outruns
  replenishment (very high subgroup count, or streams that open but
  never close — a leak). If raised, bound the memory: worst-case RX ≈
  `max_streams_uni × max_stream_data` (512 × 16 MiB = 8 GiB;
  8192 × 16 MiB = 128 GiB) — raise concurrency and lower
  `max_stream_data` together.
- **`max_data` / `max_stream_data`** — QUIC flow-control credit (max
  unacked bytes the peer may send, per-connection / per-stream). Size
  for BDP × concurrency.

### Quiet-connection liveness (keep-alive)

A flow-controlled receiver whose consumer stalls back-pressures the
sender to silence; the now-idle connection then hits the idle timeout
(default 30 s) and drops. Set `QuicConfiguration.keep_alive_interval`
(seconds, e.g. `idle_timeout/3`) so PING frames hold it open through
the stall. Off by default — opt in on subscribers/control connections
that must survive long quiet periods. See DATAFLOW.md.

### Host kernel prerequisites (deployment, not aiopquic's job)

Socket-buffer tuning rides on these ceilings — set them where
high-pps fan-out runs (persist via `/etc/sysctl.d/`; WSL/containers
reset on restart):

```bash
net.core.rmem_max    # >= 2x your SO_RCVBUF target (Linux doubles + clamps)
net.core.wmem_max    # >= 2x SO_SNDBUF target
net.core.netdev_max_backlog = 10000   # 1000 default is low for fan-out
# net.ipv4.udp_mem — global cap across all UDP sockets; bites only when
# many sockets back up at once
```

### jemalloc for tail-latency reduction

The default `glibc` allocator's per-thread arenas + occasional
coalescing show up as max-latency outliers under sustained
high-throughput workloads. Preloading `jemalloc` measurably tightens
the tail:

```bash
# Debian/Ubuntu:
sudo apt install libjemalloc2
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 python -m your_app

# Fedora/RHEL:
sudo dnf install jemalloc
LD_PRELOAD=/usr/lib64/libjemalloc.so.2 python -m your_app
```

Validated improvement on a representative aiopquic sustained workload
(Ryzen 7 PRO, Linux loopback): `sd 7.1 ms → 4.3 ms`,
`max 437 ms → 310 ms`, throughput unchanged. Effect is most visible at
multi-Gbps over 60+ second runs; small workloads see no difference.

### GSO and send-length-max

GSO (UDP segmentation offload) is **already enabled by default on
Linux** with `send_length_max=65535` (max kernel-coalesced stride). No
user action needed. macOS / FreeBSD default to GSO off — picoquic's
per-datagram `sendmsg` path is used instead. Env overrides:

```bash
AIOPQUIC_GSO=0                  # force off (diagnostic only)
AIOPQUIC_SEND_LENGTH_MAX=8192   # cap kernel-coalesced buffer (Linux GSO on)
```

### qlog / textlog tracing

Off by default — at multi-Gbps rates, writing qlog roughly halves
throughput, so don't measure with it on. Enable via
`QuicConfiguration.qlog_dir` (or the `AIOPQUIC_QLOG_DIR` env
fallback); picoquic writes one `.qlog` file per connection into that
directory (it must already exist). `AIOPQUIC_TEXTLOG_FILE` enables the
much lighter human-readable per-packet text log. For
production-compatible visibility, prefer the counters
(`TransportContext.counters`, SIGUSR2 dump).

### TX wake threshold

The TX SPSC event ring's drain-wake threshold defaults to 50% —
producer is signalled to resume only after ≥ half the queued events
have drained. Overridable to tune for latency vs. context-switch
overhead:

```bash
AIOPQUIC_TX_RING_WAKE_PCT=25    # wake earlier (lower per-send latency, more context switches)
AIOPQUIC_TX_RING_WAKE_PCT=75    # wake later (more batching, slightly higher latency)
```
