"""aiopquic ↔ ngtcp2 interop — high-throughput, high-stream-count stress.

ngtcp2's HTTP/0.9 (h09) example client+server give us a canonical
QUIC-spec-compliant peer to drive aiopquic against. h09 is
request/response: client sends `GET /N\\r\\n`, server responds with
N bytes of payload. Multi-stream stress comes from `-n` (parallel
GETs in one connection).

Build instructions: tests/interop/README.md.

Test surface:
  - sustained throughput single stream (aiopquic ↔ h09)
  - high stream-count multi-stream concurrent transfer
  - byte-conservation per stream + aggregate
  - high-object-rate small-object pattern (many short streams)

Each test asserts:
  1. handshake completion
  2. exact bytes received per stream (h09 server returns deterministic
     content; aiopquic side computes CRC32 to verify byte-faithful RX)
  3. throughput floor (loose; correctness check, not benchmark)
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib

import pytest

from .conftest import _free_port


NGTCP2_CLIENT = os.environ.get("NGTCP2_CLIENT")
NGTCP2_SERVER = os.environ.get("NGTCP2_SERVER")
ALPN = "hq-interop"


def _bins_present() -> bool:
    return all([
        NGTCP2_CLIENT, NGTCP2_SERVER,
        os.path.exists(NGTCP2_CLIENT or ""),
        os.path.exists(NGTCP2_SERVER or ""),
    ])


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _bins_present(),
        reason="set NGTCP2_CLIENT and NGTCP2_SERVER to built h09client / "
               "h09server binaries; see tests/interop/README.md",
    ),
]


# ---------------------------------------------------------------------------
# h09 server harness — serves pad-pattern payloads for arbitrary GET /N.
# We control content so we can verify CRC client-side.
# ---------------------------------------------------------------------------
def _spawn_h09_server(port: int, cert: str, key: str, docroot: str):
    """h09server takes addr port cert key. Serves files from --htdocs PATH."""
    return subprocess.Popen(
        [NGTCP2_SERVER,
         "127.0.0.1", str(port), key, cert,
         "--htdocs", docroot,
         "--alpn", ALPN,
         "-q"],  # quiet
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_listen(proc, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        last = line
        if "Listening on" in line or "listening" in line.lower():
            return
    raise TimeoutError(f"h09 peer did not start within {timeout}s; "
                       f"last stderr: {last!r}")


# ---------------------------------------------------------------------------
# h09 client harness — aiopquic-server-as-target, h09client requests data.
# Used to exercise aiopquic TX path under high stream count.
# Returns parsed summary: {streams: [{bytes, crc, time_ms}, ...]}.
# ---------------------------------------------------------------------------
def _run_h09_client(host: str, port: int, paths: list[str],
                    parallel: int = 1, timeout: float = 30.0) -> dict:
    """Run h09client; collect summary. paths[i] like '/1048576' for 1MB GET."""
    args = [NGTCP2_CLIENT, host, str(port)] + paths + [
        "--alpn", ALPN, "-n", str(parallel), "-q",
    ]
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {
        "returncode": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }


@pytest.fixture
def docroot(tmp_path):
    """Create a docroot with files of various sizes for h09server to serve.
    Pad pattern matches the rest of the suite — byte i = i & 0xFF."""
    sizes = {
        "1MB": 1 * 1024 * 1024,
        "10MB": 10 * 1024 * 1024,
        "100MB": 100 * 1024 * 1024,
    }
    for name, sz in sizes.items():
        f = tmp_path / name
        # Write counted-pad in chunks so we don't allocate the full payload.
        chunk = bytes(i & 0xFF for i in range(min(sz, 4096)))
        with open(f, "wb") as fh:
            remaining = sz
            while remaining > 0:
                n = min(len(chunk), remaining)
                fh.write(chunk[:n])
                remaining -= n
    return str(tmp_path)


# ---------------------------------------------------------------------------
# CASE: aiopquic client GETs payload from ngtcp2 server (RX stress).
# This is the path that exercises our silent-drop hazard.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload_bytes", [
    1 * 1024 * 1024,
    10 * 1024 * 1024,
])
async def test_aiopquic_client_ngtcp2_server_get(cert_paths, docroot,
                                                  payload_bytes):
    pytest.skip("aiopquic h09 client adapter — pending")


@pytest.mark.parametrize("n_streams,per_stream_bytes", [
    (4,  1 * 1024 * 1024),    # 4 streams × 1MB = 4MB; small fan-out
    (16, 1 * 1024 * 1024),    # 16 streams × 1MB = 16MB; medium
    (64, 256 * 1024),          # 64 streams × 256KB = 16MB; high stream count
])
async def test_aiopquic_client_ngtcp2_server_multistream(cert_paths,
                                                          docroot,
                                                          n_streams,
                                                          per_stream_bytes):
    pytest.skip("aiopquic h09 client adapter — pending")


# ---------------------------------------------------------------------------
# CASE: ngtcp2 client GETs from aiopquic server (TX stress under high
# concurrent stream count).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_streams,per_stream_bytes", [
    (1,  10 * 1024 * 1024),   # single sustained transfer baseline
    (4,  4  * 1024 * 1024),
    (16, 1  * 1024 * 1024),
    (64, 256 * 1024),
])
async def test_ngtcp2_client_aiopquic_server_multistream(cert_paths,
                                                          n_streams,
                                                          per_stream_bytes):
    pytest.skip("aiopquic h09 server adapter — pending")


# ---------------------------------------------------------------------------
# CASE: high object rate — many short streams in quick succession.
# Stresses stream-context allocation/teardown and per-connection scheduling.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_streams,per_stream_bytes", [
    (1000, 4 * 1024),    # 1000 small streams of 4KB each
    (5000, 1 * 1024),    # 5000 tiny streams of 1KB each
])
async def test_ngtcp2_high_object_rate(cert_paths, docroot,
                                        n_streams, per_stream_bytes):
    pytest.skip("aiopquic h09 server adapter — pending")
