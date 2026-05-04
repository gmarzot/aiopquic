"""aiopquic ↔ s2n-quic interop.

Skipped unless `S2N_QUIC_PERF` env var points to a built s2n-quic-qns
or s2n-quic-perf binary. Build instructions in tests/interop/README.md.

Both directions use s2n-quic's `s2n-quic-qns` interop runner OR the
`perf` example with --send and --receive byte counts. The s2n-quic
process emits its own per-stream summary; we cross-check against
aiopquic's stream_buf_stats / cumulative byte counter.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from .conftest import _free_port, counted_pad


S2N_BIN = os.environ.get("S2N_QUIC_PERF") or os.environ.get("S2N_QUIC_QNS")
ALPN = "perf"


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not S2N_BIN or not os.path.exists(S2N_BIN),
        reason="set S2N_QUIC_PERF or S2N_QUIC_QNS env var to a built "
               "s2n-quic-perf/s2n-quic-qns binary; see "
               "tests/interop/README.md",
    ),
]


# TODO: implement once Phase 2 RX path lands. Skeleton only — placeholder
# so the test runner discovers the file and the skip reason makes the
# missing dependency obvious.
@pytest.mark.skip(reason="implementation pending Phase 2 RX path")
async def test_aiopquic_client_s2n_server_sink(cert_paths):
    pass


@pytest.mark.skip(reason="implementation pending Phase 2 RX path")
async def test_s2n_client_aiopquic_server_sink(cert_paths):
    pass
