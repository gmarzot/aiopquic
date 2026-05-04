"""Ring buffer microbenchmarks (no network).

Establishes a baseline for what the wrapper itself can sustain
independent of QUIC. Regressions here mean we hurt every
loopback/throughput test downstream.
"""
import pytest

from aiopquic._binding._transport import RingBuffer

SMALL_PAYLOAD = b"x" * 64
MED_PAYLOAD = b"x" * 1024


@pytest.mark.bench
@pytest.mark.parametrize("payload", [b"", SMALL_PAYLOAD, MED_PAYLOAD],
                         ids=["empty", "64B", "1KB"])
def test_bench_ring_push_pop(benchmark, payload):
    """Single push + pop round-trip for various payload sizes."""
    ring = RingBuffer(capacity=4096)

    def push_pop():
        ring.push(0, 0, data=payload)
        ring.pop()

    benchmark(push_pop)


@pytest.mark.bench
def test_bench_ring_batch_push(benchmark):
    """Push a batch of 64 entries then drain — sustained throughput."""
    ring = RingBuffer(capacity=4096)

    def batch():
        for i in range(64):
            ring.push(0, i, data=SMALL_PAYLOAD)
        for _ in range(64):
            ring.pop()

    benchmark(batch)
