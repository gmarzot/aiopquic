"""aioquic server that puts stream frames after its CONNECTION_CLOSE.

picoquic decodes a packet's remaining frames after an APPLICATION_CLOSE, so
an aiopquic client sees the close event and then stream events for the
same connection, often in one drain batch. Runs the aioquic server and the
aiopquic client in one process; the test runs it as a subprocess, so a
native crash fails the test rather than the test run.

  python aioquic_close_coalesced.py MODE PORT CERT KEY

MODE is what follows the close in the same 1-RTT packet:
  none       nothing
  data       STREAM on the client's stream 0, at its next offset
  reset      RESET_STREAM on stream 0
  newstream  STREAM on server stream 1, never opened before
  unifin     the FIN of server uni stream 3, which already carried data, so
             the client releases the stream behind the close

Output: one JSON line with the client's events in order and the
process-wide live stream context and chunk counts after teardown.
"""
import asyncio
import gc
import json
import ssl
import sys

from aioquic import tls
from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol as AioquicProtocol
from aioquic.quic import connection as aioquic_connection
from aioquic.quic.configuration import QuicConfiguration as AioquicConfig
from aioquic.quic.events import StreamDataReceived as AioquicData

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (
    ConnectionTerminated, StreamDataReceived, StreamReset,
)

ALPN = "coalesced-close"
HELLO = b"hello"
TAIL = b"world"

MODE, PORT, CERT, KEY = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

_write_close = aioquic_connection.QuicConnection._write_connection_close_frame


def _close_then_frames(self, builder, epoch, error_code, frame_type,
                       reason_phrase):
    _write_close(self, builder=builder, epoch=epoch, error_code=error_code,
                 frame_type=frame_type, reason_phrase=reason_phrase)
    if epoch != tls.Epoch.ONE_RTT:
        return
    if MODE == "data":
        buf = builder.start_frame(0x0E, capacity=32 + len(TAIL))  # OFF|LEN
        buf.push_uint_var(0)
        buf.push_uint_var(len(HELLO))
        buf.push_uint_var(len(TAIL))
        buf.push_bytes(TAIL)
    elif MODE == "newstream":
        buf = builder.start_frame(0x0A, capacity=32 + len(TAIL))  # LEN
        buf.push_uint_var(1)
        buf.push_uint_var(len(TAIL))
        buf.push_bytes(TAIL)
    elif MODE == "unifin":
        buf = builder.start_frame(0x0F, capacity=32 + len(TAIL))  # OFF|LEN|FIN
        buf.push_uint_var(3)
        buf.push_uint_var(len(HELLO))
        buf.push_uint_var(len(TAIL))
        buf.push_bytes(TAIL)
    elif MODE == "reset":
        buf = builder.start_frame(0x04, capacity=32)
        buf.push_uint_var(0)
        buf.push_uint_var(7)
        buf.push_uint_var(len(HELLO))


aioquic_connection.QuicConnection._write_connection_close_frame = (
    _close_then_frames)


class Server(AioquicProtocol):
    def quic_event_received(self, event):
        if isinstance(event, AioquicData) and event.stream_id == 0:
            self._quic.send_stream_data(0, HELLO, end_stream=False)
            if MODE == "unifin":
                self._quic.send_stream_data(3, HELLO, end_stream=False)
            self.transmit()
            asyncio.get_event_loop().call_later(0.2, self._close)

    def _close(self):
        self._quic.close(error_code=0x10, reason_phrase="bye")
        self.transmit()


events = []


class Client(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            events.append(["data", event.stream_id, bytes(event.data).decode(),
                           event.end_stream])
        elif isinstance(event, StreamReset):
            events.append(["reset", event.stream_id, event.error_code])
        elif isinstance(event, ConnectionTerminated):
            events.append(["terminated", event.error_code])
        super().quic_event_received(event)


async def main():
    scfg = AioquicConfig(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT, KEY)
    server = await serve("127.0.0.1", PORT, configuration=scfg,
                         create_protocol=Server)
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN])
    cfg.verify_mode = ssl.CERT_NONE
    counters = {}
    try:
        async with connect("127.0.0.1", PORT, configuration=cfg,
                           create_protocol=Client) as client:
            quic = client._quic
            quic.send_stream_data(0, b"ping")
            for _ in range(500):
                if any(ev[0] == "terminated" for ev in events):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            counters = dict(quic._transport.counters)
    finally:
        server.close()
    gc.collect()
    total = TransportContext().counters
    print(json.dumps({
        "events": events,
        "sc_alive": total["sc_alive_total"],
        "chunks_alive": total["chunks_alive_total"],
        "stream_cb_after_close": counters.get("stream_cb_after_close", 0),
    }), flush=True)


asyncio.run(main())
