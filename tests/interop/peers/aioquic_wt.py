"""aioquic H3/WebTransport server peer for aiopquic WebTransport clients.

aioquic closes the connection on STOP_SENDING for a stream that is
receive-only at its end (RFC 9000 §19.5), and it does not negotiate
reset_stream_at, so a reset sent only as RESET_STREAM_AT never reaches it.
Spawned as a subprocess so it runs in its own asyncio loop. Accepts one
WebTransport session and opens one server-to-client uni stream with data.

Usage:
  python aioquic_wt.py --port 4433 --cert PATH --key PATH [--lifetime 10]
                       [--late-uni MS]

--late-uni opens another uni stream MS milliseconds after accepting the
session.

Ready line on stderr: "serving on 127.0.0.1:PORT".
Stdout, one JSON line per event:
  {"event": "wt_data", "stream_id": N, "bytes": N}   first bytes on a stream
  {"event": "stream_reset", "stream_id": N, "error_code": N}
  {"event": "stop_sending", "stream_id": N, "error_code": N}
  {"event": "late_uni", "stream_id": N}
  {"event": "terminated", "error_code": N, "frame_type": N, "reason": "..."}
Exits when the connection terminates or after --lifetime seconds.
"""
import argparse
import asyncio
import json
import sys

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import HeadersReceived, WebTransportStreamDataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import (
    ConnectionTerminated, ProtocolNegotiated, StopSendingReceived,
    StreamReset,
)


def emit(**fields):
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


class _H3WithWtEnabled(H3Connection):
    """Also advertises SETTINGS_WT_ENABLED (0x2c7cf000), which makes a
    draft-ietf-webtrans-http3-16 client switch to the webtransport-h3 token."""

    def _get_local_settings(self):
        settings = super()._get_local_settings()
        settings[0x2C7CF000] = 1
        return settings


class WebTransportPeer(QuicConnectionProtocol):
    def __init__(self, *args, done: asyncio.Event, late_uni_ms: float,
                 strict_protocol: bool = False,
                 wt_enabled_setting: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._done = done
        self._late_uni_ms = late_uni_ms
        self._strict_protocol = strict_protocol
        self._wt_enabled_setting = wt_enabled_setting
        self._h3 = None
        self._seen = set()

    def _open_late_uni(self, session_id):
        sid = self._h3.create_webtransport_stream(
            session_id, is_unidirectional=True)
        self._quic.send_stream_data(sid, b"L" * 500)
        self.transmit()
        emit(event="late_uni", stream_id=sid)

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            h3_class = (_H3WithWtEnabled if self._wt_enabled_setting
                        else H3Connection)
            self._h3 = h3_class(self._quic, enable_webtransport=True)
        elif isinstance(event, StreamReset):
            emit(event="stream_reset", stream_id=event.stream_id,
                 error_code=event.error_code)
        elif isinstance(event, StopSendingReceived):
            emit(event="stop_sending", stream_id=event.stream_id,
                 error_code=event.error_code)
        elif isinstance(event, ConnectionTerminated):
            emit(event="terminated", error_code=event.error_code,
                 frame_type=event.frame_type, reason=event.reason_phrase)
            self._done.set()
        if self._h3 is None:
            return
        for ev in self._h3.handle_event(event):
            if isinstance(ev, HeadersReceived):
                if dict(ev.headers).get(b":method") == b"CONNECT":
                    token = dict(ev.headers).get(b":protocol", b"")
                    emit(event="connect",
                         protocol=token.decode(errors="replace"))
                    if self._strict_protocol and token != b"webtransport":
                        # the token browsers send and proxygen requires
                        self._h3.send_headers(
                            ev.stream_id, [(b":status", b"400")],
                            end_stream=True)
                        self.transmit()
                        continue
                    self._h3.send_headers(ev.stream_id, [
                        (b":status", b"200"),
                        (b"sec-webtransport-http3-draft", b"draft02")])
                    sid = self._h3.create_webtransport_stream(
                        ev.stream_id, is_unidirectional=True)
                    self._quic.send_stream_data(sid, b"S" * 500)
                    self.transmit()
                    if self._late_uni_ms > 0:
                        asyncio.get_running_loop().call_later(
                            self._late_uni_ms / 1000, self._open_late_uni,
                            ev.stream_id)
            elif isinstance(ev, WebTransportStreamDataReceived):
                if ev.data and ev.stream_id not in self._seen:
                    self._seen.add(ev.stream_id)
                    emit(event="wt_data", stream_id=ev.stream_id,
                         bytes=len(ev.data))


async def main(args):
    cfg = QuicConfiguration(alpn_protocols=H3_ALPN, is_client=False,
                            max_datagram_frame_size=65536)
    cfg.load_cert_chain(args.cert, args.key)
    done = asyncio.Event()
    server = await serve(
        "127.0.0.1", args.port, configuration=cfg,
        create_protocol=lambda *a, **kw: WebTransportPeer(
            *a, done=done, late_uni_ms=args.late_uni,
            strict_protocol=args.strict_protocol,
            wt_enabled_setting=args.wt_enabled_setting, **kw))
    sys.stderr.write(f"serving on 127.0.0.1:{args.port}\n")
    sys.stderr.flush()
    try:
        await asyncio.wait_for(done.wait(), timeout=args.lifetime)
    except asyncio.TimeoutError:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--lifetime", type=float, default=10.0)
    p.add_argument("--late-uni", type=float, default=0.0)
    p.add_argument("--strict-protocol", action="store_true",
                   help="400 unless :protocol is webtransport")
    p.add_argument("--wt-enabled-setting", action="store_true",
                   help="advertise SETTINGS_WT_ENABLED (draft-16)")
    asyncio.run(main(p.parse_args()))
