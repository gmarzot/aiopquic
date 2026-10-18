"""qns/h3client.py against an aioquic HTTP/3 file server over localhost.

Run as a script, this module is the server: --port --cert --key --www.
"""
import asyncio
import filecmp
import importlib.util
import os
import socket
import ssl
import subprocess
import sys
from contextlib import closing

import pytest

QNS = os.path.join(os.path.dirname(__file__), "..", "..", "qns")
KB = 1024
MB = 1024 * KB


def _serve(argv: list[str]) -> None:
    import argparse

    from aioquic.asyncio import serve
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--www", required=True)
    args = ap.parse_args(argv)
    root = os.path.realpath(args.www)

    class FileServer(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._h3 = H3Connection(self._quic)

        def quic_event_received(self, event) -> None:
            for ev in self._h3.handle_event(event):
                if isinstance(ev, HeadersReceived):
                    self._respond(ev.stream_id, dict(ev.headers))

        def _respond(self, sid: int, headers: dict) -> None:
            path = headers.get(b":path", b"").decode().lstrip("/")
            target = os.path.realpath(os.path.join(root, path))
            if not target.startswith(root + os.sep) or not os.path.isfile(target):
                self._h3.send_headers(sid, [(b":status", b"404")], end_stream=True)
            else:
                with open(target, "rb") as f:
                    data = f.read()
                self._h3.send_headers(sid, [
                    (b":status", b"200"),
                    (b"content-length", str(len(data)).encode()),
                ])
                self._h3.send_data(sid, data, end_stream=True)
            self.transmit()

    async def main() -> None:
        cfg = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
        cfg.load_cert_chain(args.cert, args.key)
        await serve("127.0.0.1", args.port, configuration=cfg,
                    create_protocol=FileServer)
        print("ready", flush=True)
        await asyncio.Event().wait()

    asyncio.run(main())


def _load_h3client():
    spec = importlib.util.spec_from_file_location(
        "qns_h3client", os.path.join(QNS, "h3client.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("sizes", [
    [1 * KB, 1 * MB, 3 * MB],
    [1 * KB] * 20,
])
async def test_h3_download(tmp_path, cert_paths, sizes):
    pytest.importorskip("pylsqpack")
    pytest.importorskip("aioquic.h3")
    h3 = _load_h3client()
    from aiopquic.quic.configuration import QuicConfiguration

    www, downloads = tmp_path / "www", tmp_path / "downloads"
    os.makedirs(www)
    os.makedirs(downloads)
    names = []
    for i, size in enumerate(sizes):
        name = f"file{i}"
        (www / name).write_bytes(os.urandom(size))
        names.append(name)

    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "--port", str(port), "--cert", cert_paths["cert"],
        "--key", cert_paths["key"], "--www", str(www),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), 15)
        assert line.strip() == b"ready", (await proc.stderr.read()).decode()
        cfg = QuicConfiguration(is_client=True, verify_mode=ssl.CERT_NONE)
        urls = [f"https://127.0.0.1:{port}/{n}" for n in names]
        await asyncio.wait_for(h3.fetch_h3(urls, str(downloads), cfg), 30)
    finally:
        proc.kill()
        await proc.wait()
    for name in names:
        assert filecmp.cmp(www / name, downloads / name, shallow=False), name


async def test_h3_download_from_aiopquic_web_folder(tmp_path, cert_paths):
    """serve_webtransport(web_folder=) answers HTTP/3 GETs outside the WT path."""
    pytest.importorskip("pylsqpack")
    h3 = _load_h3client()
    from aiopquic.asyncio.webtransport import serve_webtransport
    from aiopquic.quic.configuration import QuicConfiguration

    www, downloads = tmp_path / "www", tmp_path / "downloads"
    os.makedirs(www)
    os.makedirs(downloads)
    names = []
    for i, size in enumerate([1 * KB, 1 * MB]):
        (www / f"file{i}").write_bytes(os.urandom(size))
        names.append(f"file{i}")
    port = _free_port()
    server = await serve_webtransport(
        "0.0.0.0", port, "/wt", handler=lambda session: None,
        cert_file=cert_paths["cert"], key_file=cert_paths["key"], web_folder=str(www))
    try:
        cfg = QuicConfiguration(is_client=True, verify_mode=ssl.CERT_NONE)
        urls = [f"https://127.0.0.1:{port}/{n}" for n in names]
        await asyncio.wait_for(h3.fetch_h3(urls, str(downloads), cfg), 30)
    finally:
        await server.aclose()
    for name in names:
        assert filecmp.cmp(www / name, downloads / name, shallow=False), name


if __name__ == "__main__":
    _serve(sys.argv[1:])
