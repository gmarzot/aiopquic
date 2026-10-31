"""Close reason phrases between an aioquic server and an aiopquic client.

MODE "peer": the aioquic server closes; prints the client's ConnectionTerminated
events. MODE "ours": the aiopquic client closes; prints the server's.
"""
import asyncio
import json
import ssl
import sys

from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol as AioquicProtocol
from aioquic.quic.configuration import QuicConfiguration as AioquicConfig
from aioquic.quic.events import ConnectionTerminated as AioquicTerminated
from aioquic.quic.events import HandshakeCompleted as AioquicHandshake

from aiopquic.asyncio.client import connect
from aiopquic.asyncio.protocol import QuicConnectionProtocol
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import ConnectionTerminated

MODE, PORT, CERT, KEY = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
ALPN = "hq-interop"
server_events = []
client_events = []


class Server(AioquicProtocol):
    def quic_event_received(self, event):
        if isinstance(event, AioquicHandshake) and MODE == "peer":
            asyncio.get_event_loop().call_later(0.1, self._close)
        elif isinstance(event, AioquicTerminated):
            server_events.append([event.error_code, event.frame_type,
                                  event.reason_phrase])

    def _close(self):
        self._quic.close(error_code=0x10, reason_phrase="aioquic-bye")
        self.transmit()


class Client(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            client_events.append([event.error_code, event.frame_type,
                                  event.reason_phrase])


async def main():
    scfg = AioquicConfig(is_client=False, alpn_protocols=[ALPN])
    scfg.load_cert_chain(CERT, KEY)
    server = await serve("127.0.0.1", PORT, configuration=scfg,
                         create_protocol=Server)
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN])
    cfg.verify_mode = ssl.CERT_NONE
    try:
        async with connect("127.0.0.1", PORT, configuration=cfg,
                           create_protocol=Client) as client:
            if MODE == "ours":
                client._quic.close(error_code=9, reason_phrase="aiopquic-bye")
            mine = server_events if MODE == "ours" else client_events
            for _ in range(500):
                if mine:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.5)
    finally:
        server.close()
    print(json.dumps({"server": server_events, "client": client_events}),
          flush=True)


asyncio.run(main())
