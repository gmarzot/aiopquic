# sim_link_bench

Protocol-only picoquic throughput bench. Drives two `picoquic_quic_t`
instances over `picoquictest_sim_link` (no kernel UDP, no sockets) so
the measurement reflects only picoquic CPU cost — cross-platform
comparable, independent of the host's UDP loopback bandwidth.

Companion to `tests/bench/bench_baselines_*.py`. Those measure
`picoquic + UDP loopback`. `sim_link_bench` isolates picoquic from the
loopback ceiling — useful for answering "is the wall protocol or
kernel?"

## Build

```bash
./build_picoquic.sh                # builds picoquic static libs
./tests/bench/sim_link/build.sh    # builds sim_link_bench
```

## Run

```bash
PICOQUIC_SOLUTION_DIR=third_party/picoquic/ \
    tests/bench/sim_link/sim_link_bench \
        --duration-s 30 --warmup-s 2 \
        --rate-gbps 100 --rtt-us 1000
```

Output:

```
sim_link_bench obj=16384B
    warmup_recv=2796818475 warmup_mbps=11187.3
    ss_recv=42059172199 ss_mbps=11215.8 obj_per_s=85570
    overall_mbps=11214.0 sim_total_s=32.000
    rtt_us=1000 rate_gbps=100.00 rounds=122027063
```

`ss_mbps` is the steady-state number — bytes received during the
30-second window after the 2-second warmup, divided by the window.
That's the picoquic protocol-only throughput ceiling on this hardware.

## Reference numbers

| platform | ss_mbps |
|---|---|
| AMD Ryzen 7 PRO 7840U / WSL2 / Linux 6.6 | ~11,200 |

Compared to `bench_baselines_lowlevel.py` (same hardware, same
picoquic, but going through real UDP loopback): ~2,300 Mbps. The
~5× gap is kernel UDP loopback, not picoquic.

## Why simulated time

The simulator runs entirely in event-driven simulated time. Wall time
correlates with picoquic's CPU work but is not the throughput axis —
we're measuring "how fast does picoquic process packets" not "how
fast does this CPU run" (those are 1:1 only when no other CPU work is
involved). For a 30s simulated steady-state window at 11 Gbps, wall
clock is roughly 2 minutes of single-core CPU on Ryzen 7 PRO 7840U.

## What it does NOT measure

- Kernel UDP loopback bandwidth (use `bench_baselines_*.py` for that)
- Real-world network performance (use a relay)
- aiopquic Cython binding overhead (use `bench_baselines_lowlevel.py`)
- aiopquic asyncio wrapper overhead (use `bench_baselines_highlevel.py`)
