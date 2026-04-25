"""Native picoquic test driver subprocess wrapper.

Runs picoquic_ct + picohttp_ct as subprocesses, exercising every
non-slow test compiled into the binaries (~552 tests total, ~48s).
This catches regressions in picoquic itself when the submodule is
bumped, independent of aiopquic's binding.

Drivers are built as part of ./build_picoquic.sh so this runs in
default `pytest` if the build succeeded.

Slow categories are excluded via `-x`: stress/fuzz, cnx_stress,
cnx_ddos, satellite_*, ddos_amplification_*, key_rotation_stress,
eccf_corrupted_fuzz (picoquic_ct); http_stress, h3zero_*_fuzz,
h3zero_satellite, picowt_baton_long (picohttp_ct). Run those
directly with picoquic_ct/picohttp_ct when you want them.

Drivers run with cwd = picoquic source dir so relative cert paths
(./certs/cert.pem) resolve.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PICOQUIC_DIR = ROOT / "third_party" / "picoquic"
PICOQUIC_CT = PICOQUIC_DIR / "build" / "picoquic_ct"
PICOHTTP_CT = PICOQUIC_DIR / "build" / "picohttp_ct"


# Tests that take many seconds to minutes individually — excluded
# from the regression smoke. Run them on demand via the binary.
PICOQUIC_SLOW = [
    "stress",
    "fuzz",
    "fuzz_initial",
    "cnx_stress",
    "cnx_ddos",
    "key_rotation_stress",
    "eccf_corrupted_fuzz",
    "ddos_amplification_0rtt",
    "ddos_amplification_8k",
    "satellite_basic",
    "satellite_seeded",
    "satellite_loss",
    "satellite_loss_fc",
    "satellite_jitter",
    "satellite_medium",
    "satellite_preemptive_fc",
    "satellite_small",
    "satellite_small_up",
    "satellite_bbr1",
    "satellite_cubic_seeded",
    "satellite_cubic_loss",
    "satellite_dcubic_seeded",
    "satellite_prague_seeded",
]

PICOHTTP_SLOW = [
    "http_stress",
    "h3zero_qpack_fuzz",
    "h3zero_stream_fuzz",
    "h3zero_satellite",
    "picowt_baton_long",
]


def _run(driver: Path, exclude: list[str]) -> subprocess.CompletedProcess:
    args = [str(driver)]
    for name in exclude:
        args.extend(["-x", name])
    return subprocess.run(
        args,
        cwd=str(PICOQUIC_DIR),
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.native
class TestNativePicoquic:
    """Native picoquic_ct and picohttp_ct full smoke (slow excluded)."""

    @pytest.mark.skipif(
        not PICOQUIC_CT.exists(),
        reason="picoquic_ct not built; run ./build_picoquic.sh",
    )
    def test_picoquic_ct(self):
        result = _run(PICOQUIC_CT, PICOQUIC_SLOW)
        last = result.stdout.strip().splitlines()[-3:]
        assert result.returncode == 0, (
            f"picoquic_ct failed (rc={result.returncode}):\n"
            f"{os.linesep.join(last)}\n"
            f"stderr tail: {result.stderr[-2000:]}"
        )

    @pytest.mark.skipif(
        not PICOHTTP_CT.exists(),
        reason="picohttp_ct not built; run ./build_picoquic.sh",
    )
    def test_picohttp_ct(self):
        result = _run(PICOHTTP_CT, PICOHTTP_SLOW)
        last = result.stdout.strip().splitlines()[-3:]
        assert result.returncode == 0, (
            f"picohttp_ct failed (rc={result.returncode}):\n"
            f"{os.linesep.join(last)}\n"
            f"stderr tail: {result.stderr[-2000:]}"
        )
