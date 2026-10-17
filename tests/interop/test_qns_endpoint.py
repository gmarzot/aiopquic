"""Interop Runner endpoint (qns/) against itself over localhost."""
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


@pytest.mark.parametrize("module,role,testcase", [
    (hq, "client", "chacha20"),
    (hq, "server", "retry"),
    (hq, "server", "http3"),
    (hq, "client", "unknown-random-slug"),
])
def test_unsupported_exits_127(monkeypatch, module, role, testcase):
    monkeypatch.setenv("ROLE", role)
    monkeypatch.setenv("TESTCASE", testcase)
    assert module.main() == 127
