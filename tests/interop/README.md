# aiopquic interop tests

Cross-stack tests against independent QUIC implementations.

## What runs where

| stack    | language | install                         | runs in CI? | role                                      |
|----------|----------|---------------------------------|-------------|-------------------------------------------|
| qh3      | Python   | `pip install qh3` (transitive)  | **yes**     | basic byte-conservation smoke (1MB/10MB) |
| aioquic  | Python   | `pip install aioquic`           | **yes**     | reference asyncio QUIC cross-check        |
| ngtcp2   | C        | local build, env var pointer    | **no**      | high-throughput / multi-stream stress (optional) |

CI runs the pure-Python qh3 and aioquic tests by default — they need no extra
binaries. The ngtcp2 tests skip silently unless their env vars point to built
binaries; they're an optional developer-machine harness, not a release gate.

The package wheel does not ship any of these test peers. They live entirely
under `tests/interop/` and exist to validate aiopquic's transport against
genuinely independent QUIC stacks during dev and pre-release.

## Run

```
# Default (CI shape) — runs qh3 only:
pytest tests/interop/ -v

# Full local stress (after building the ngtcp2 binaries below):
NGTCP2_CLIENT=$HOME/src/ngtcp2/examples/h09client \
NGTCP2_SERVER=$HOME/src/ngtcp2/examples/h09server \
pytest tests/interop/ -v
```

## qh3

Already a dev dependency (transitive via aiomoqt v0.8.x). Tests run
automatically. If `import qh3` fails, the test file is skipped with a
clear reason.

## ngtcp2 (local-only)

ngtcp2's HTTP/0.9-over-QUIC examples (`h09client`, `h09server`) speak the
canonical IETF interop reference protocol. Build path on Ubuntu 22.04
using system gnutls (no custom OpenSSL needed):

```
sudo apt install libgnutls28-dev libev-dev pkg-config

git clone --depth 1 https://github.com/ngtcp2/nghttp3 ~/src/nghttp3
cd ~/src/nghttp3
autoreconf -i
./configure --enable-lib-only --prefix=$(pwd)/inst
make -j$(nproc) install

git clone --depth 1 --recurse-submodules https://github.com/ngtcp2/ngtcp2 ~/src/ngtcp2
cd ~/src/ngtcp2
autoreconf -i
./configure --with-gnutls --with-libnghttp3=$HOME/src/nghttp3/inst \
            PKG_CONFIG_PATH=$HOME/src/nghttp3/inst/lib/pkgconfig
make -j$(nproc)

export NGTCP2_CLIENT=$(pwd)/examples/h09client
export NGTCP2_SERVER=$(pwd)/examples/h09server
```

(BoringSSL or quictls are also supported; gnutls path above is the
shortest on stock Ubuntu.)

## Test surface coverage

| target  | test pattern                        | what it stresses                |
|---------|-------------------------------------|---------------------------------|
| qh3     | 1 stream × 1MB / 10MB transfer      | TX byte-conservation correctness|
| ngtcp2  | 1 stream × 100MB sustained          | aggregate throughput (>500 Mbps)|
| ngtcp2  | 16 streams × 1MB concurrent         | multi-stream scheduler          |
| ngtcp2  | 64 streams × 256KB concurrent       | high stream count               |
| ngtcp2  | 1000 streams × 4KB                  | high object rate / open+close   |

## Pass criteria

Every test verifies:

1. **Handshake completes** within 5s.
2. **Byte conservation** per stream — bytes received == bytes sent.
3. **CRC32 equality** — rolling CRC32 of the deterministic counted-pad
   payload matches end-to-end.

Throughput floors are loose (>50 Mbps) — these are correctness tests, not
benchmarks. Performance regression tests live in `tests/bench/`.
