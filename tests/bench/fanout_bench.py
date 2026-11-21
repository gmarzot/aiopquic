"""fanout() against per-destination tx_send_atomic() calls.

Producer cost only: the transport never starts, so no worker drains the
rings and every stream stays armed after its first write (the steady state
of a stream mid-group). Rings are emptied between rounds, outside timing.

    python tests/bench/fanout_bench.py [--dests 150 1000] [--rounds 20]
"""
import argparse
import time

from aiopquic._binding._transport import (
    DestSet, TransportContext, FANOUT_QUIC_STREAM,
    stream_buf_pop_to_bytes, stream_ctx_create, stream_ctx_get_tx,
)

RING_CAP = 64 * 1024
FAKE_CNX = 1    # never dereferenced: no worker runs


def _drain(scs):
    for sc in scs:
        tx = stream_ctx_get_tx(sc)
        if tx:
            stream_buf_pop_to_bytes(tx, RING_CAP)


def bench(n, size, calls, rounds):
    t = TransportContext(tx_ring_cap=4096)
    data = b"\xab" * size
    fan = [stream_ctx_create() for _ in range(n)]
    per = [stream_ctx_create() for _ in range(n)]
    ds = DestSet(t)
    for i, sc in enumerate(fan):
        ds._add((t, FANOUT_QUIC_STREAM, FAKE_CNX, 4 * i + 3, sc, RING_CAP, None))
    dests = [(4 * i + 3, sc) for i, sc in enumerate(per)]
    send = t.tx_send_atomic
    # Warm-up arms every stream (one MARK each).
    assert t.fanout(ds, data) is None
    for sid, sc in dests:
        assert send(sid, data, False, FAKE_CNX, sc, RING_CAP) == 0
    _drain(fan)
    _drain(per)

    fan_ns = per_ns = 0
    for _ in range(rounds):
        t0 = time.perf_counter_ns()
        for _ in range(calls):
            if t.fanout(ds, data) is not None:
                raise RuntimeError("fanout refused a destination")
        fan_ns += time.perf_counter_ns() - t0
        t0 = time.perf_counter_ns()
        for _ in range(calls):
            for sid, sc in dests:
                if send(sid, data, False, FAKE_CNX, sc, RING_CAP) != 0:
                    raise RuntimeError("tx_send_atomic refused")
        per_ns += time.perf_counter_ns() - t0
        _drain(fan)
        _drain(per)
    total = calls * rounds
    return fan_ns / total / 1e3, per_ns / total / 1e3


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dests", type=int, nargs="+", default=[150, 1000])
    ap.add_argument("--size", type=int, default=1000, help="bytes per write")
    ap.add_argument("--rounds", type=int, default=20)
    args = ap.parse_args()
    calls = max(1, (RING_CAP // 2) // args.size)
    print(f"{'dests':>6} {'fanout us':>10} {'per-dest us':>12} {'ratio':>6} "
          f"{'fanout ns/dest':>15}")
    for n in args.dests:
        fan, per = bench(n, args.size, calls, args.rounds)
        print(f"{n:>6} {fan:>10.1f} {per:>12.1f} {per / fan:>6.1f} "
              f"{fan * 1e3 / n:>15.1f}")


if __name__ == "__main__":
    main()
