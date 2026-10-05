# aiopquic interop tests

Cross-stack tests against independent QUIC implementations.

## What runs where

| stack    | language | install                         | runs in CI? | role                                      |
|----------|----------|---------------------------------|-------------|-------------------------------------------|
| qh3      | Python   | `pip install qh3` (transitive)  | **yes**     | basic byte-conservation smoke (1MB/10MB) |
| aioquic  | Python   | `pip install aioquic`           | **yes**     | reference asyncio QUIC cross-check; strict WebTransport peer |

CI runs the pure-Python qh3 and aioquic tests by default — they need no extra
binaries; they're an optional developer-machine harness, not a release gate.

The package wheel does not ship any of these test peers. They live entirely
under `tests/interop/` and exist to validate aiopquic's transport against
genuinely independent QUIC stacks during dev and pre-release.

## Run

```
# Default (CI shape) — runs qh3 only:
pytest tests/interop/ -v
```

## qh3

Already a dev dependency (transitive via aiomoqt v0.8.x). Tests run
automatically. If `import qh3` fails, the test file is skipped with a
clear reason.


## Test surface coverage

| target  | test pattern                        | what it stresses                |
|---------|-------------------------------------|---------------------------------|
| qh3     | 1 stream × 1MB / 10MB transfer      | TX byte-conservation correctness|
| aioquic | stream transfers, both directions   | byte conservation               |
| aioquic WebTransport | session close; `reset_stream` | §6 teardown frames in each stream's direction; RESET_STREAM when reset_stream_at is not negotiated |

## Pass criteria

Every transfer test verifies:

1. **Handshake completes** within 5s.
2. **Byte conservation** per stream — bytes received == bytes sent.
3. **CRC32 equality** — rolling CRC32 of the deterministic counted-pad
   payload matches end-to-end.

Throughput floors are loose (>50 Mbps) — these are correctness tests, not
benchmarks. Performance regression tests live in `tests/bench/`.

The WebTransport tests (`test_aioquic_wt.py`) check the peer's view instead:
it never closes the connection during our teardown, and a reset arrives with
its code.
