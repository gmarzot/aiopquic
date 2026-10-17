"""Interop Runner endpoints (qns/) against themselves over localhost."""
import asyncio
import filecmp
import importlib.util
import os

import pytest

from .conftest import _free_port

QNS = os.path.join(os.path.dirname(__file__), "..", "..", "qns")
KB = 1024
MB = 1024 * KB


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"qns_{name}", os.path.join(QNS, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hq = _load("endpoint")
wt = _load("wt_endpoint")


def _files(directory, sizes) -> list[str]:
    os.makedirs(directory, exist_ok=True)
    names = []
    for i, size in enumerate(sizes):
        name = f"file{i}"
        with open(os.path.join(directory, name), "wb") as f:
            f.write(os.urandom(size))
        names.append(name)
    return names


def _assert_same(src, dst, names) -> None:
    for name in names:
        assert os.path.isfile(os.path.join(dst, name)), name
        assert filecmp.cmp(os.path.join(src, name), os.path.join(dst, name),
                           shallow=False), name


@pytest.mark.parametrize("testcase,sizes", [
    ("handshake", [1 * KB]),
    pytest.param("transfer", [2 * MB, 3 * MB, 5 * MB], marks=pytest.mark.xfail(
        reason="client RX ring overflows under 256 KiB stream windows",
        strict=False)),
    ("multiconnect", [1 * KB] * 3),
    # More requests than the server's initial stream limit (512).
    ("transfer", [32] * 1200),
])
async def test_hq(tmp_path, cert_paths, testcase, sizes):
    www, downloads = tmp_path / "www", tmp_path / "downloads"
    names = _files(www, sizes)
    os.makedirs(downloads)
    port = _free_port()
    server = await hq.start_server(port, str(www), cert_paths["cert"],
                                   cert_paths["key"])
    try:
        urls = [f"https://127.0.0.1:{port}/{n}" for n in names]
        await asyncio.wait_for(
            hq.run_client(testcase, urls, str(downloads)), 30)
    finally:
        server.close()
    _assert_same(www, downloads, names)


@pytest.mark.parametrize("client_case,server_case,sizes", [
    ("transfer-unidirectional-receive", "transfer", [100 * KB, 2 * MB]),
    ("transfer-bidirectional-receive", "transfer", [100 * KB, 2 * MB]),
    ("transfer-datagram-receive", "transfer", [600 + 2 * i for i in range(50)]),
    ("transfer", "transfer-unidirectional-send", [100 * KB, 2 * MB]),
    ("transfer", "transfer-bidirectional-send", [100 * KB, 2 * MB]),
    ("transfer", "transfer-datagram-send", [600 + 2 * i for i in range(50)]),
])
async def test_wt_transfer(tmp_path, cert_paths, client_case, server_case,
                           sizes):
    dirs = {k: tmp_path / k for k in ("sw", "sd", "cw", "cd")}
    for d in dirs.values():
        os.makedirs(d / "ep")
    port = _free_port()
    if server_case == "transfer":
        names = _files(dirs["sw"] / "ep", sizes)
        urls = [f"https://127.0.0.1:{port}/ep/{n}" for n in names]
        server_requests = []
        src, dst = dirs["sw"] / "ep", dirs["cd"] / "ep"
    else:
        names = _files(dirs["cw"] / "ep", sizes)
        urls = [f"https://127.0.0.1:{port}/ep"]
        server_requests = [f"ep/{n}" for n in names]
        src, dst = dirs["cw"] / "ep", dirs["sd"] / "ep"
    server, done = await wt.start_server(
        server_case, port, str(dirs["sw"]), str(dirs["sd"]),
        cert_paths["cert"], cert_paths["key"], ["p1"], server_requests)
    try:
        await asyncio.wait_for(wt.run_client(
            client_case, urls, ["p1"], str(dirs["cw"]), str(dirs["cd"])), 30)
        await asyncio.wait_for(done, 10)
    finally:
        await server.aclose()
    _assert_same(src, dst, names)


async def test_wt_handshake(tmp_path, cert_paths):
    sw, sd, cd = tmp_path / "sw", tmp_path / "sd", tmp_path / "cd"
    os.makedirs(sw / "ep")
    os.makedirs(sd)
    os.makedirs(cd)
    port = _free_port()
    server, done = await wt.start_server(
        "handshake", port, str(sw), str(sd), cert_paths["cert"],
        cert_paths["key"], ["b", "y", "x"], [])
    try:
        await asyncio.wait_for(wt.run_client(
            "handshake", [f"https://127.0.0.1:{port}/ep"], ["a", "x", "y"],
            str(tmp_path), str(cd)), 30)
        await asyncio.wait_for(done, 10)
    finally:
        await server.aclose()
    client = (cd / "negotiated_protocol.txt").read_text()
    assert client in ("x", "y")
    assert (sd / "negotiated_protocol.txt").read_text() == client


@pytest.mark.parametrize("module,role,testcase", [
    (hq, "client", "chacha20"),
    (hq, "server", "retry"),
    (hq, "server", "http3"),
    (hq, "client", "unknown-random-slug"),
    (wt, "client", "transfer-datagram-send"),
    (wt, "server", "transfer-datagram-receive"),
    (wt, "server", "unknown-random-slug"),
])
def test_unsupported_exits_127(monkeypatch, module, role, testcase):
    monkeypatch.setenv("ROLE", role)
    monkeypatch.setenv("TESTCASE", testcase)
    assert module.main() == 127
