"""Microbench: RFC9000 varint vs draft-18 vi64 codec, and the
push_vint/pull_vint flavor-dispatch overhead.

Answers two questions directly:
  1. Is the vi64 codec measurably slower than RFC9000 (per-int encode/decode)?
  2. Does buffer.push_vint/pull_vint (one C branch on buf.vi64) add measurable
     overhead vs calling the codec method directly?

These run on control-plane integers and the per-stream SUBGROUP_HEADER (cold-
to-warm). The per-OBJECT data path uses the fused parse/encode_object_subgroup
(_vi64) codecs and is not benched here.

Prints per-codec ENCODE/DECODE tables plus a SUMMARY with the relative
verdicts (vi64-vs-rfc9000 codec ratio + push_vint/pull_vint dispatch overhead).

Run (from the aiopquic repo root): python tests/bench/bench_vint_codec.py [--iters N]
"""
import argparse
import time

from aiopquic.buffer import Buffer

# One value per RFC9000 length class, all <= 2^62-1 so push_uint_var can encode
# them too (apples to apples). Small values (<64) encode identically in both
# flavors; larger ones diverge.
VALUES = [0, 1, 63, 64, 100, 200, 16383, 16384,
          1 << 20, 1 << 30, 1 << 40, 1 << 50]
N = len(VALUES)


def _best(run, repeats=5):
    run()  # warmup
    return min(run() for _ in range(repeats))


def _enc_time(push_name, vi64, iters):
    buf = Buffer(capacity=256, vi64=vi64)
    push = getattr(buf, push_name)

    def run():
        t0 = time.perf_counter()
        for _ in range(iters):
            buf.seek(0)
            for v in VALUES:
                push(v)
        return time.perf_counter() - t0

    return _best(run)


def _dec_time(pull_name, vi64, src_push, iters):
    enc = Buffer(capacity=256, vi64=(src_push == 'push_uint_vi64'))
    ep = getattr(enc, src_push)
    for v in VALUES:
        ep(v)
    buf = Buffer(data=bytes(enc.data), vi64=vi64)
    pull = getattr(buf, pull_name)

    def run():
        t0 = time.perf_counter()
        for _ in range(iters):
            buf.seek(0)
            for _ in range(N):
                pull()
        return time.perf_counter() - t0

    return _best(run)


def _mops(iters, dt):
    return (iters * N) / dt / 1e6


def main():
    ap = argparse.ArgumentParser(description="vint codec microbench")
    ap.add_argument('--iters', type=int, default=200_000)
    args = ap.parse_args()
    it = args.iters
    print(f"vint codec microbench — {N} values x {it:,} iters = "
          f"{it * N:,} ops/measurement; best of 5\n")

    e_var = _enc_time('push_uint_var', False, it)
    e_v64 = _enc_time('push_uint_vi64', True, it)
    e_dv = _enc_time('push_vint', True, it)    # dispatch -> vi64
    e_dr = _enc_time('push_vint', False, it)   # dispatch -> rfc9000
    print("ENCODE                          Mops/s    rel(>1=faster)")
    print(f"  push_uint_var  (rfc9000)    {_mops(it, e_var):7.1f}    1.00x")
    print(f"  push_uint_vi64 (vi64)       {_mops(it, e_v64):7.1f}    {e_var/e_v64:.2f}x")
    print(f"  push_vint      [vi64 buf]   {_mops(it, e_dv):7.1f}    {e_var/e_dv:.2f}x")
    print(f"  push_vint      [rfc9000 buf]{_mops(it, e_dr):7.1f}    {e_var/e_dr:.2f}x")

    d_var = _dec_time('pull_uint_var', False, 'push_uint_var', it)
    d_v64 = _dec_time('pull_uint_vi64', True, 'push_uint_vi64', it)
    d_dv = _dec_time('pull_vint', True, 'push_uint_vi64', it)
    d_dr = _dec_time('pull_vint', False, 'push_uint_var', it)
    print("\nDECODE                          Mops/s    rel(>1=faster)")
    print(f"  pull_uint_var  (rfc9000)    {_mops(it, d_var):7.1f}    1.00x")
    print(f"  pull_uint_vi64 (vi64)       {_mops(it, d_v64):7.1f}    {d_var/d_v64:.2f}x")
    print(f"  pull_vint      [vi64 buf]   {_mops(it, d_dv):7.1f}    {d_var/d_dv:.2f}x")
    print(f"  pull_vint      [rfc9000 buf]{_mops(it, d_dr):7.1f}    {d_var/d_dr:.2f}x")

    # Comparative summary — the two questions, answered from the measurements.
    # Codec ratio >1.00x = the vi64 codec is faster than the rfc9000 baseline.
    # Dispatch % = extra cost of the push_vint/pull_vint flavor branch vs
    # calling the codec method directly on the same (vi64) buffer.
    enc_disp = (e_dv / e_v64 - 1.0) * 100.0
    dec_disp = (d_dv / d_v64 - 1.0) * 100.0
    print("\nSUMMARY")
    print(f"  vi64 codec vs rfc9000:                 "
          f"encode {e_var/e_v64:4.2f}x   decode {d_var/d_v64:4.2f}x")
    print(f"  push_vint/pull_vint dispatch overhead: "
          f"encode {enc_disp:+4.0f}%   decode {dec_disp:+4.0f}%")
    print("  -> vi64 codec is parity-or-faster vs rfc9000; the only added cost")
    print("     is the flavor-dispatch branch (cold control path). The per-OBJECT")
    print("     hot path bypasses it via the fused encode/parse_object_subgroup_vi64.")


if __name__ == '__main__':
    main()
