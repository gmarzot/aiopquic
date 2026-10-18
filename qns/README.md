# Interop Runner endpoints

`endpoint.py` (hq-interop, HTTP/0.9) and `wt_endpoint.py` (WebTransport GET/PUSH) implement the
[QUIC Interop Runner](https://github.com/quic-interop/quic-interop-runner) endpoint contract on
aiopquic's public API. `Dockerfile` builds both on the network-simulator endpoint image: target
`quic` (default) and `webtransport`. Any other `TESTCASE` exits 127.

## QUIC cases

| `TESTCASE` | client | server |
|---|---|---|
| `handshake`, `transfer`, `multiconnect`, `ecn` | yes | yes |
| `retry` | yes (picoquic follows the Retry) | 127: Retry cannot be enabled |
| `resumption`, `zerortt` | 127: no session ticket store | 127 |
| `chacha20` | 127: no cipher suite selection | 127 |
| `keyupdate` | 127: no key update trigger | — |
| `v2` | 127: no version selection | 127 |
| `connectionmigration` | 127: no active migration | 127: no preferred address |
| `http3` | 127: no HTTP/3 request API | 127 |

The runner sends `handshake` for longrtt; `transfer` for multiplexing, amplificationlimit,
blackhole, transferloss, transfercorruption, rebind-port, rebind-addr, ipv6, goodput and
crosstraffic; `multiconnect` for handshakeloss and handshakecorruption. Sockets are IPv4 only:
the client exits 127 for an IPv6-only host, and the server is unreachable over IPv6.

The `transfer` client advertises 1 MiB connection and 256 KiB stream windows, so files larger
than the windows depend on window updates.

## WebTransport cases

| `TESTCASE` | client | server |
|---|---|---|
| `handshake` | yes | yes |
| `transfer` (answer requests) | yes | yes |
| `transfer-{unidirectional,bidirectional,datagram}-receive` | yes | — |
| `transfer-{unidirectional,bidirectional,datagram}-send` | — | yes |

`PROTOCOLS` sets `wt_available_protocols` on the client and `wt_supported_protocols` on the
server. One session per QUIC connection. The server accepts sessions on any path and serves
files by name from the endpoint directories under `/www`.

## Running locally

Build the wheel from a plain clone (in a git worktree the submodules' git dirs are outside the
cibuildwheel container), then the images:

```
uvx cibuildwheel==3.1.4 --platform linux --only cp312-manylinux_x86_64 --output-dir qns/wheelhouse .
```
```
docker build -t aiopquic-quic:ci --target quic qns
```
```
docker build -t aiopquic-webtransport:ci --target webtransport qns
```

In a quic-interop-runner checkout (Docker >= 28.1 and tshark), register the image and run:

```
jq '. + {aiopquic: {image: "aiopquic-quic:ci", url: "https://github.com/gmarzot/aiopquic", role: "both"}}' implementations_quic.json > i.json && mv i.json implementations_quic.json
```
```
python run.py -p quic -s aiopquic -c aiopquic -t onlyTests
```

For WebTransport use `implementations_webtransport.json`, `aiopquic-webtransport:ci` and
`-p webtransport`.

## Published images

`ghcr.io/gmarzot/aiopquic-qns` (QUIC) and `ghcr.io/gmarzot/aiopquic-wt-interop` (WebTransport),
amd64, built by `.github/workflows/qns-images.yml` from the aiopquic wheel on PyPI. Each release
is tagged with its version; `latest` is the newest stable release. The workflow runs after the
Release workflow, weekly, and on dispatch with a version.

## CI

`.github/workflows/interop.yml` runs aiopquic against itself and each peer in both roles, weekly,
on dispatch, and on pull requests that touch `qns/`. `tests/interop/test_qns_endpoint.py` runs
both endpoints against themselves over localhost.
