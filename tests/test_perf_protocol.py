"""QUIC perf protocol (tests/bench/perf.py) over loopback, and against picoquicdemo."""

import asyncio
import os
import re
import struct
import subprocess

import pytest

from aiopquic.asyncio.client import connect
from aiopquic.quic.events import StreamDataReceived, StreamReset

from tests.bench.perf import (
    UNBOUNDED, PerfClientProtocol, client_configuration, run_client,
    server_configuration, start_server,
)
from tests._ports import next_port

CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "third_party", "picoquic", "certs")
CERT_FILE = os.path.join(CERTS_DIR, "cert.pem")
KEY_FILE = os.path.join(CERTS_DIR, "key.pem")
CA_FILE = os.path.join(CERTS_DIR, "test-ca.crt")
SNI = "test.example.com"
PICOQUICDEMO = os.path.join(os.path.dirname(__file__), "..", "third_party", "picoquic",
                            "build", "picoquicdemo")

pytestmark = pytest.mark.timeout(30)


def _client_cfg():
    return client_configuration(server_name=SNI, cafile=CA_FILE, cc_algo="newreno")


@pytest.fixture
async def perf_server():
    port = next_port()
    conns = []
    server = await start_server(port, server_configuration(CERT_FILE, KEY_FILE, "newreno"),
                                conns, host="127.0.0.1")
    try:
        yield port, conns
    finally:
        server.close()


@pytest.mark.parametrize("upload,download", [
    (0, 1 << 20),
    (1 << 20, 0),
    (300_000, 700_001),
    (0, 0),
], ids=["download", "upload", "both", "zero"])
async def test_single_request(perf_server, upload, download):
    port, conns = perf_server
    r = await run_client("127.0.0.1", port, _client_cfg(), upload=upload, download=download)
    assert r.requests == 1
    assert r.bytes_down == download
    assert r.bytes_up == 8 + upload
    srv = conns[0]
    assert srv.requests == 1
    assert srv.bytes_received == 8 + upload
    assert srv.bytes_sent == download


async def test_concurrent_requests(perf_server):
    port, conns = perf_server
    r = await run_client("127.0.0.1", port, _client_cfg(), upload=10_000, download=50_000,
                         streams=8, requests=40)
    assert r.requests == 40
    assert r.bytes_down == 40 * 50_000
    assert r.bytes_up == 40 * (8 + 10_000)
    assert conns[0].requests == 40
    assert conns[0].bytes_received == r.bytes_up


async def test_uni_request(perf_server):
    port, conns = perf_server
    async with connect("127.0.0.1", port, configuration=_client_cfg(),
                       create_protocol=PerfClientProtocol) as proto:
        sizes = await asyncio.gather(proto.request(1000, 200_000, uni=True),
                                     proto.request(0, 200_000, uni=True),
                                     proto.request(500, 0, uni=True))
        assert sizes == [200_000, 200_000, 0]
        assert proto.bytes_received == 400_000
    assert conns[0].requests == 3


class _RawClient(PerfClientProtocol):
    def __init__(self, quic, **kw):
        super().__init__(quic, **kw)
        self.data = {}
        self.fin = set()
        self.reset = asyncio.Event()
        self.changed = asyncio.Event()

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            self.data[event.stream_id] = self.data.get(event.stream_id, 0) + len(event.data)
            if event.end_stream:
                self.fin.add(event.stream_id)
            self.changed.set()
        elif isinstance(event, StreamReset):
            self.reset.set()


async def _until(proto, cond):
    while not cond():
        proto.changed.clear()
        await proto.changed.wait()


async def test_short_header_gets_bare_fin(perf_server):
    port, conns = perf_server
    async with connect("127.0.0.1", port, configuration=_client_cfg(),
                       create_protocol=_RawClient) as proto:
        sid = proto._quic.get_next_available_stream_id()
        proto._quic.send_stream_data(sid, b"\x00\x00\x01", end_stream=True)
        await asyncio.wait_for(_until(proto, lambda: sid in proto.fin), 5)
        assert proto.data[sid] == 0
    assert conns[0].requests == 0


async def test_unbounded_request_stops_on_stop_sending(perf_server):
    port, conns = perf_server
    async with connect("127.0.0.1", port, configuration=_client_cfg(),
                       create_protocol=_RawClient) as proto:
        sid = proto._quic.get_next_available_stream_id()
        proto._quic.send_stream_data(sid, struct.pack("!Q", UNBOUNDED), end_stream=True)
        await asyncio.wait_for(_until(proto, lambda: proto.data.get(sid, 0) >= 256_000), 5)
        proto._quic.stop_stream(sid, 0)
        await asyncio.wait_for(proto.reset.wait(), 5)
        assert sid not in proto.fin
    assert conns[0].requests == 1


_need_demo = pytest.mark.skipif(not os.access(PICOQUICDEMO, os.X_OK),
                                reason="picoquicdemo not built (./build.sh)")


@pytest.mark.native
@_need_demo
async def test_interop_picoquicdemo_server(tmp_path):
    port = next_port()
    proc = await asyncio.create_subprocess_exec(
        PICOQUICDEMO, "-p", str(port), "-c", CERT_FILE, "-k", KEY_FILE, "-G", "newreno", "-1",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=tmp_path)
    try:
        # Initial retransmission covers the server's startup.
        r = await run_client("127.0.0.1", port, _client_cfg(), upload=256 * 1024,
                             download=1 << 20)
        assert r.requests == 1
        assert r.bytes_down == 1 << 20
        assert r.bytes_up == 8 + 256 * 1024
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@pytest.mark.native
@_need_demo
async def test_interop_picoquicdemo_client(perf_server, tmp_path):
    port, conns = perf_server
    # picoquic's post size includes the 8-byte header.
    scenario = f"*1:{256 * 1024 + 8}:{1 << 20};"
    proc = await asyncio.create_subprocess_exec(
        PICOQUICDEMO, "-a", "perf", "-n", SNI, "-t", CA_FILE,
        "-G", "newreno", "-D", "127.0.0.1", str(port), scenario,
        stdout=asyncio.subprocess.PIPE, stderr=subprocess.STDOUT, cwd=tmp_path)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    text = out.decode(errors="replace")
    assert proc.returncode == 0, text
    assert re.search(r"^Nb_transactions: 1$", text, re.M), text
    assert re.search(rf"^Download_bytes: {1 << 20}$", text, re.M), text
    assert re.search(rf"^Upload_bytes: {256 * 1024 + 8}$", text, re.M), text
    assert conns[0].requests == 1
    assert conns[0].bytes_received == 256 * 1024 + 8
    assert conns[0].bytes_sent == 1 << 20
