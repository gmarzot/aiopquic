"""Native picoquic test driver subprocess wrapper.

Runs a curated smoke list of picoquic_ct / picohttp_ct tests as
subprocesses to confirm picoquic itself is healthy on the currently
pinned submodule, independent of aiopquic's binding code.

Build the drivers first:
    ./build_picoquic_tests.sh

Then run:
    pytest -m native

Drivers must run from the picoquic source directory so relative
cert paths (./certs/cert.pem) resolve.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PICOQUIC_DIR = ROOT / "third_party" / "picoquic"
PICOQUIC_CT = PICOQUIC_DIR / "build" / "picoquic_ct"
PICOHTTP_CT = PICOQUIC_DIR / "build" / "picohttp_ct"


# Fast smoke tests — each well under a second. Picked to span:
# encoding/decoding, hashing, threading, byte streams, socket loop,
# basic logging. Excludes long-running interop / fuzzer / satellite
# tests; those are available via picoquic_ct directly when needed.
PICOQUIC_SMOKE = [
    "connection_id_print",
    "connection_id_parse",
    "error_name",
    "util_sprintf",
    "util_uint8_to_str",
    "util_memcmp",
    "picohash",
    "siphash",
    "bytestream",
    "picolog_basic",
    "sockloop_ipv4",
    "transport_param",
    "version_negotiation",
    "ack_loop",
    "frames_skip",
    "frames_parse",
    "datagram",
    "retry",
]

PICOHTTP_SMOKE = [
    "h3zero_integer",
    "h3zero_varint_stream",
    "h3zero_capsule",
    "qpack_huffman",
    "qpack_huffman_base",
    "h3zero_parse_qpack",
    "h3zero_prepare_qpack",
    "h3zero_user_agent",
    "h3zero_uri",
    "h3zero_url_template",
    "h3zero_wt_protocol_response",
    "demo_alpn",
    "demo_ticket",
]


def _run(driver: Path, tests: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(driver), *tests],
        cwd=str(PICOQUIC_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.native
class TestNativePicoquic:
    """Run native picoquic_ct smoke list as one batch."""

    @pytest.mark.skipif(
        not PICOQUIC_CT.exists(),
        reason="picoquic_ct not built; run ./build_picoquic_tests.sh",
    )
    def test_picoquic_ct_smoke(self):
        result = _run(PICOQUIC_CT, PICOQUIC_SMOKE)
        last = result.stdout.strip().splitlines()[-3:]
        assert result.returncode == 0, (
            f"picoquic_ct smoke failed (rc={result.returncode}):\n"
            f"{os.linesep.join(last)}"
        )

    @pytest.mark.skipif(
        not PICOHTTP_CT.exists(),
        reason="picohttp_ct not built; run ./build_picoquic_tests.sh",
    )
    def test_picohttp_ct_smoke(self):
        result = _run(PICOHTTP_CT, PICOHTTP_SMOKE)
        last = result.stdout.strip().splitlines()[-3:]
        assert result.returncode == 0, (
            f"picohttp_ct smoke failed (rc={result.returncode}):\n"
            f"{os.linesep.join(last)}"
        )
