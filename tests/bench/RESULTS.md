# aiopquic — benchmark results

Reference numbers for the v0.2.0 pull-model transport. Reproduce with:

```
pytest tests/bench/bench_latency_floor.py        --benchmark-disable -s
pytest tests/bench/bench_throughput_pullmodel.py --benchmark-disable -s
pytest tests/bench/bench_small_object_rate.py    --benchmark-disable -s
```

All measurements: loopback, single stream, 1 MiB symmetric per-stream
rings, single-process, x86_64. Real-network numbers (RTT > 0) add
wire RTT to all measurements; the transport overhead floor remains
~100 µs on top of RTT.

## Headline

| metric | value |
|---|---|
| Throughput | **2.5 Gbps** sustained |
| Object rate | **294,271 obj/s** at 1 KB (~340 ns per object) |
| Latency floor | **52 µs** single-object round-trip, 1 KB |
| Sub-ms p50 holds to | **1.5 Gbps** at 4 KB objects |
| Byte conservation | 0 loss / 0 corruption across 1.47 M objects |

## Object rate — saturating push

Producer pushes back-to-back; consumer drains continuously. Each
object carries a 24-byte header (seq + build timestamp + magic) for
verification. Bench: `bench_small_object_rate.py`.

| object size | objs/sec | throughput | p50 | p90 | p99 | max |
|:--|--:|--:|--:|--:|--:|--:|
| **1 KB** | **294,271** | 2.4 Gbps | 124 µs | 339 µs | 961 µs | 8.6 ms |
| 4 KB | 69,745 | 2.3 Gbps | 3.7 ms | 4.3 ms | 5.6 ms | 7.9 ms |
| 16 KB | 19,186 | 2.5 Gbps | 3.4 ms | 4.1 ms | 5.8 ms | 8.7 ms |

The 1 KB result — **~300 K objects/sec at sub-ms p50** — is the
latency-friendly regime: each push is ≪ ring capacity so the ring
stays mostly empty even at line rate. 4 / 16 KB hit the
saturation-bufferbloat regime where p50 = ring fill time
(1 MB / 300 MB/s ≈ 3.3 ms physics floor), not transport overhead.

## Latency floor — single-object round-trip

Push one object, fully drain at the consumer, push the next. Zero
queueing — pure transport overhead per object. Bench:
`bench_latency_floor.py::test_bench_single_object_rtt`.

| object size | min | p50 | p90 | p99 | max | rate |
|:--|--:|--:|--:|--:|--:|--:|
| 1 KB | 52 µs | 104 µs | 153 µs | 225 µs | 811 µs | 8,677 /s |
| 4 KB | 32 µs | 126 µs | 179 µs | 249 µs | 515 µs | 7,374 /s |
| 16 KB | 85 µs | 186 µs | 271 µs | 410 µs | 852 µs | 5,038 /s |

This is the request/reply lower bound — what aiopquic delivers when
zero work is queued.

## Latency at sub-saturation rates

4 KB objects, monotonic-clock pacing, 3 s per row. Ring stays at
4–8 KB. Bench:
`bench_latency_floor.py::test_bench_sub_saturation_sustained`.

| target | achieved | objs/sec | p50 | p90 | p99 | avg ring |
|--:|--:|--:|--:|--:|--:|--:|
| 50 Mbps | 50 Mbps | 1,526 | 150 µs | 263 µs | 397 µs | 4 KB |
| 100 Mbps | 100 Mbps | 3,052 | 136 µs | 213 µs | 295 µs | 4 KB |
| 500 Mbps | 500 Mbps | 15,257 | 97 µs | 163 µs | 270 µs | 4 KB |

1 KB pacing variants:

| target | achieved | objs/sec | p50 | p90 | p99 | avg ring |
|--:|--:|--:|--:|--:|--:|--:|
| 50 Mbps | 50 Mbps | 6,103 | 99 µs | 150 µs | 226 µs | 1 KB |
| 100 Mbps | 100 Mbps | 12,206 | 80 µs | 134 µs | 486 µs | 1 KB |

## Latency-vs-rate sweep — finds the bufferbloat knee

4 KB objects, 2 s per step. Bench:
`bench_latency_floor.py::test_bench_latency_vs_rate`.

| target | achieved | objs/sec | p50 | p90 | p99 | avg ring |
|--:|--:|--:|--:|--:|--:|--:|
| 10 Mbps | 10 Mbps | 305 | 232 µs | 526 µs | 825 µs | 4 KB |
| 50 Mbps | 50 Mbps | 1,526 | 162 µs | 293 µs | 451 µs | 4 KB |
| 100 Mbps | 100 Mbps | 3,052 | 141 µs | 227 µs | 365 µs | 4 KB |
| 250 Mbps | 250 Mbps | 7,629 | 109 µs | 184 µs | 7.7 ms | 8 KB |
| 500 Mbps | 500 Mbps | 15,258 | 101 µs | 170 µs | 318 µs | 5 KB |
| 1000 Mbps | 1000 Mbps | 30,516 | 99 µs | 220 µs | 5.1 ms | 21 KB |
| 1500 Mbps | 1500 Mbps | 45,774 | 97 µs | 203 µs | 521 µs | 8 KB |
| 2000 Mbps | 2091 Mbps | 63,826 | 152 µs | 979 µs | 3.7 ms | **303 KB ← knee** |

p50 stays under 200 µs from 50 Mbps through 1.5 Gbps. Bufferbloat
knee starts at ~2 Gbps where the ring fills past 300 KB.

## Sustained throughput — saturating pull-model

Bench: `bench_throughput_pullmodel.py`. 1 MiB symmetric rings, single
stream, 5 s sustained, 64 KB chunks each carrying a verification
header.

| duration | sent | recv | rate | p50 lag | p99 lag | integrity |
|--:|--:|--:|--:|--:|--:|--:|
| 1 s | 4,887 chunks (320 MB) | 4,886 | 2.59 Gbps | 3.5 ms | 5.0 ms | gaps=0 dupes=0 magic-mismatch=0 |
| 5 s | 24,658 chunks (1.62 GB) | 24,657 | 2.59 Gbps | 3.5 ms | 5.4 ms | gaps=0 dupes=0 magic-mismatch=0 |

## What the numbers mean

- **Sub-saturation operating points** (rates below the knee): the
  per-stream ring stays small; latency is dominated by wire RTT +
  asyncio loop tick + parser dispatch — order of 100 µs on loopback.
- **Saturation:** the ring fills, and latency = ring_size / drain_rate.
  This is bufferbloat, not transport overhead. Smaller rings → lower
  saturation latency (at the cost of more frequent backpressure).
- **Object rate ceiling:** ~300 K obj/s at 1 KB, ~80 K obj/s at 4 KB,
  ~20 K obj/s at 16 KB. Per-object overhead is a small constant
  (~340 ns at 1 KB, ~14 µs at 4 KB) on top of the bytes-on-wire cost.
- **Sizing knob:** `QuicConfiguration.max_stream_data` controls both
  the peer-advertised flow-control window and the per-stream RX byte
  ring. Default 1 MiB is the sweet spot for low-latency media; lower
  for tighter latency bounds, higher for tolerating bursty peers.
- **Congestion control:** `QuicConfiguration.congestion_control_algorithm`
  defers to picoquic's default (newreno). Set to `"cubic"`, `"bbr"`,
  `"bbr1"`, `"prague"`, `"dcubic"`, or `"fast"` per-deployment.
- **Verification:** per-object 64-bit magic + monotonic sequence on
  every bench; sustained-throughput run additionally checks no gaps,
  no duplicates, no out-of-order at saturation. Zero loss / zero
  corruption across 1.47 M objects in the small-object bench at
  2.4 Gbps sustained.
