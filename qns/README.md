# Interop Runner endpoints

`endpoint.py` (hq-interop, HTTP/0.9) and `wt_endpoint.py` (WebTransport GET/PUSH) implement the
[QUIC Interop Runner](https://github.com/quic-interop/quic-interop-runner) endpoint contract on
aiopquic's public API. `Dockerfile` builds both on the network-simulator endpoint image: target
`quic` (default) and `webtransport`. Any other `TESTCASE` exits 127.

## QUIC cases

| `TESTCASE` | client | server |
|---|---|---|
| `handshake`, `transfer`, `multiconnect`, `ecn` | yes | yes |
| `retry` | yes (picoquic follows the Retry) | yes (`serve(retry=True)`) |
| `resumption`, `zerortt` | yes (`session_ticket_file`; 0-RTT requests written before the handshake) | yes |
| `chacha20` | yes (`cipher_suite=0x1303`) | yes |
| `keyupdate` | yes (`key_update_interval`) | — |
| `v2` | yes (`supported_versions`) | yes |
| `http3` | yes (`h3client.py`, QPACK via pylsqpack) | yes (`serve_webtransport(web_folder=)`) |
| `connectionmigration` | 127 | 127: no preferred address |

The runner sends `handshake` for longrtt; `transfer` for multiplexing, amplificationlimit,
blackhole, transferloss, transfercorruption, rebind-port, rebind-addr, ipv6, goodput and
crosstraffic; `multiconnect` for handshakeloss and handshakecorruption. The server binds both
IPv4 and IPv6 when the host has IPv6.

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
