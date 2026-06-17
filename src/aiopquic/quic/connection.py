"""QUIC connection — matches qh3.quic.connection API.

Wraps TransportContext (Cython/picoquic) and translates SPSC ring
events into QuicEvent objects that match qh3's event interface.
"""

import asyncio
import contextlib
import logging
import os
from collections import deque
from enum import IntEnum

logger = logging.getLogger(__name__)

from .configuration import QuicConfiguration
from .events import (
    QuicEvent, HandshakeCompleted, ConnectionTerminated,
    ProtocolNegotiated, StreamDataReceived, StreamReset,
    StopSendingReceived, DatagramFrameReceived,
)
from aiopquic._binding._transport import (
    TransportContext,
    tx_data_bytes_queued,
    stream_buf_create, stream_buf_destroy,
    stream_buf_push, stream_buf_used, stream_buf_free, stream_buf_set_fin,
    stream_buf_stats,
    stream_buf_pop_to_bytes,
    stream_ctx_create, stream_ctx_destroy,
    stream_ctx_ensure_tx, stream_ctx_ensure_rx,
    stream_ctx_get_tx, stream_ctx_get_rx,
    stream_ctx_rx_consumed,
    stream_ctx_send_data,
)


# Per-stream send-ring capacity for the PULL-model send path.
# 1 MiB gives ~5-10ms of in-flight data at 1-2 Gbps which is enough
# pipelining headroom without unbounded queueing. Power of two required.
# Per-stream sc->tx data-ring capacity (bytes). Fallback default when
# the QuicConnection's configuration does not override via
# QuicConfiguration.stream_ring_cap. Hard cap on bytes Python may push
# to a single stream's send queue before send_stream_data raises
# BufferError. Preserves QUIC per-stream independence (HOLB-free
# backpressure): a slow stream parks ITS producer only, not others.
_STREAM_RING_CAP = 1 << 20

# SPSC event type constants (must match spsc_ring.h)
_EVT_STREAM_DATA = 0
_EVT_STREAM_FIN = 1
_EVT_STREAM_RESET = 2
_EVT_STOP_SENDING = 3
_EVT_CLOSE = 4
_EVT_APP_CLOSE = 5
_EVT_READY = 6
_EVT_ALMOST_READY = 7
_EVT_DATAGRAM = 8
_EVT_STREAM_TX_DRAINED = 15
_EVT_STREAM_DESTROY = 17

# TX event types
_TX_STREAM_DATA = 128
_TX_STREAM_FIN = 129
_TX_DATAGRAM = 130
_TX_CLOSE = 131
_TX_STREAM_RESET = 132
_TX_STOP_SENDING = 133
_TX_MARK_ACTIVE = 134
_TX_CONNECT = 135


class QuicErrorCode(IntEnum):
    NO_ERROR = 0x0
    INTERNAL_ERROR = 0x1
    CONNECTION_REFUSED = 0x2
    FLOW_CONTROL_ERROR = 0x3
    STREAM_LIMIT_ERROR = 0x4
    STREAM_STATE_ERROR = 0x5
    FINAL_SIZE_ERROR = 0x6
    FRAME_ENCODING_ERROR = 0x7
    TRANSPORT_PARAMETER_ERROR = 0x8
    CONNECTION_ID_LIMIT_ERROR = 0x9
    PROTOCOL_VIOLATION = 0xA
    INVALID_TOKEN = 0xB
    APPLICATION_ERROR = 0xC
    CRYPTO_BUFFER_EXCEEDED = 0xD
    KEY_UPDATE_ERROR = 0xE
    AEAD_LIMIT_REACHED = 0xF
    VERSION_NEGOTIATION_ERROR = 0x11
    CRYPTO_ERROR = 0x100


def stream_is_unidirectional(stream_id: int) -> bool:
    """Returns True if the stream is unidirectional (bit 1 set)."""
    return bool(stream_id & 2)


class QuicConnection:
    """QUIC connection wrapping picoquic via TransportContext.

    Presents the same public API as qh3.quic.connection.QuicConnection
    so that higher layers (H3Connection, aiomoqt) can use it unchanged.

    Two construction modes:
    - Client / standalone: QuicConnection(configuration=cfg). Owns its
      own TransportContext; calls _start_transport() then connect().
    - Engine-spawned (server side): QuicConnection(configuration=cfg,
      engine=engine, cnx_ptr=cnx). Shares engine's transport; cnx_ptr
      is the picoquic cnx pointer for this peer. Engine routes events
      to this connection's _events queue; no own drain.
    """

    def __init__(self, *, configuration: QuicConfiguration,
                 engine: "QuicEngine | None" = None,
                 cnx_ptr: int = 0):
        self._configuration = configuration
        self._engine = engine
        if engine is not None:
            # Engine-spawned: share engine's transport, no own drain
            self._transport = engine._transport
            self._cnx_ptr = cnx_ptr
            self._connected = True
            self._closed = False
        else:
            self._transport: TransportContext | None = None
            self._cnx_ptr = 0
            self._connected = False
            self._closed = False
        # deque, not list: next_event() pops from the head per event
        # while the consumer parses — list.pop(0) is O(n) memmove,
        # which under a receive backlog turns consumption quadratic
        # (434K-entry list measured 15s+ delivery latency and a
        # GC/memmove death spiral on the 2-process subscriber).
        self._events: deque[QuicEvent] = deque()
        self._next_bidi_id: int = 0 if configuration.is_client else 1
        self._next_uni_id: int = 2 if configuration.is_client else 3
        # H3Connection compatibility
        self._quic_logger = None
        self._remote_max_datagram_frame_size = (
            configuration.max_datagram_frame_size
        )
        # Per-stream wrapper map. Each stream_ctx_t holds {tx, rx} byte
        # rings + RX flow-control state and is bound to picoquic's
        # app_stream_ctx slot. Lazy-created on first contact (TX from
        # send_stream_data, RX from picoquic stream_data callback).
        # Destroyed via lifecycle hooks (Landing C); for now leak until
        # process exit to avoid use-after-free with the picoquic worker.
        self._stream_ctxs: dict[int, int] = {}
        # Per-stream "sc->tx drained" events for event-driven TX
        # backpressure. Set by _handle_raw_event when the picoquic
        # worker fires SPSC_EVT_STREAM_TX_DRAINED for a stream;
        # awaited by aiomoqt's stream_write_drain on BufferError.
        # Lazy-created on first reference from either side.
        self._stream_tx_drain_events: dict[int, asyncio.Event] = {}

    @property
    def configuration(self) -> QuicConfiguration:
        return self._configuration

    @property
    def closed(self) -> bool:
        """True once this connection has seen close/app-close. The
        worker no longer drains TX rings; sends become no-ops, so
        callers in produce loops should stop writing."""
        return self._closed

    def _start_transport(self, port: int = 0) -> None:
        """Create and start the TransportContext."""
        if self._transport is not None:
            return
        cfg = self._configuration
        # SPSC event ring capacity: configurable via QuicConfiguration.
        # None defers to the Cython compile-time default. High-rate
        # stream-churn workloads (multi-Gbps with many short streams)
        # need this larger to avoid silent stream_data event drops on
        # the receiver side.
        if cfg.event_ring_capacity is not None:
            self._transport = TransportContext(
                ring_capacity=cfg.event_ring_capacity)
        else:
            self._transport = TransportContext()
        # SSLKEYLOGFILE env var as a fallback when configuration didn't
        # set secrets_log_file explicitly — matches the convention used
        # by curl, openssl s_client, and Chromium-based tooling.
        keylog = cfg.secrets_log_file or os.environ.get('SSLKEYLOGFILE')
        # Per-stream RX byte ring capacity matches the configured
        # max_stream_data window (spec-bound peer-allowed in-flight
        # maximum on a single stream). The C-side allocator rounds up
        # to a power of two internally — caller-side just passes the
        # configured window verbatim.
        self._transport.start(
            port=port,
            cert_file=cfg.certificate_file,
            key_file=cfg.private_key_file,
            # Single configured ALPN keeps the simple default_alpn path
            # (byte-identical to before). Multiple ALPNs switch to the
            # negotiation path: client offers the list (request_alpn_list),
            # server selects (alpn_select_fn) — both require default_alpn
            # NULL, which start() sets when alpn_list is given.
            alpn=(cfg.alpn_protocols[0]
                  if cfg.alpn_protocols and len(cfg.alpn_protocols) == 1
                  else None),
            alpn_list=(list(cfg.alpn_protocols)
                       if cfg.alpn_protocols and len(cfg.alpn_protocols) > 1
                       else None),
            is_client=cfg.is_client,
            idle_timeout_ms=int(cfg.idle_timeout * 1000),
            max_datagram_frame_size=(cfg.max_datagram_frame_size or 0),
            keylog_filename=keylog,
            rx_data_ring_cap=cfg.max_stream_data,
            congestion_control_algorithm=cfg.congestion_control_algorithm,
            initial_max_data=cfg.max_data,
            initial_max_streams_uni=getattr(cfg, 'max_streams_uni', 0) or 0,
            initial_max_streams_bidi=getattr(cfg, 'max_streams_bidi', 0) or 0,
            keep_alive_interval_ms=int((cfg.keep_alive_interval or 0)
                                        * 1000),
            socket_buffer_size=(cfg.socket_buffer_size or 0),
            qlog_dir=cfg.qlog_dir,
        )

    def connect(self, addr: tuple[str, int], now: float = 0.0) -> None:
        """Initiate a client connection to the given address."""
        if self._transport is None:
            self._start_transport(port=0)
        cfg = self._configuration
        host, port = addr
        self._transport.create_client_connection(
            host, port,
            sni=cfg.server_name or host,
            # Single ALPN: set it on the cnx (simple path). Multiple:
            # leave NULL so picoquic asks our request_alpn_list callback,
            # which offers the full list start() stashed on the bridge.
            alpn=(cfg.alpn_protocols[0]
                  if cfg.alpn_protocols and len(cfg.alpn_protocols) == 1
                  else None),
        )

    @property
    def eventfd(self) -> int:
        """File descriptor for asyncio add_reader() registration."""
        if self._transport is None:
            return -1
        return self._transport.eventfd

    @property
    def bytes_sent(self) -> int:
        """Cumulative bytes this cnx has placed on the wire (picoquic-
        accounting). Differs from bytes-queued: send_stream_data only
        appends to picoquic's per-stream send buffer; bytes_sent is
        the on-wire count after cwnd/pacing has done its work."""
        from aiopquic._binding._transport import cnx_data_sent
        return cnx_data_sent(self._cnx_ptr)

    @property
    def bytes_received(self) -> int:
        """Cumulative bytes this cnx has received from the wire."""
        from aiopquic._binding._transport import cnx_data_received
        return cnx_data_received(self._cnx_ptr)

    def _drain_and_convert(self) -> None:
        """Drain SPSC ring and convert to QuicEvent objects.

        Routes through the Cython `drain_rx_callback` so per-entry
        events are appended directly to `self._events` without the
        intermediate list-of-tuples that drain_rx() builds.
        """
        if self._transport is None:
            return
        self._transport.drain_rx_callback(self._handle_raw_event)

    def _negotiated_alpn(self, cnx_ptr):
        """The ALPN TLS actually negotiated for this cnx, falling back
        to the first configured ALPN if picoquic can't report it.

        Reporting the configured `alpn_protocols[0]` was correct only
        while exactly one ALPN was offered; with a multi-version offer
        the first-offered protocol is not necessarily the one the peer
        selected, so the version derived from it would be wrong.
        """
        ptr = cnx_ptr or self._cnx_ptr
        if ptr and self._transport is not None:
            try:
                alpn = self._transport.get_negotiated_alpn(ptr)
                if alpn:
                    return alpn
            except Exception:
                pass
        # Fall back to the configured ALPN only when exactly one was
        # offered (then it is unambiguously the negotiated one). With a
        # multi-version offer, guessing alpn_protocols[0] could report the
        # wrong draft, so return None and let the caller decide.
        cfg = self._configuration
        if cfg.alpn_protocols and len(cfg.alpn_protocols) == 1:
            return cfg.alpn_protocols[0]
        return None

    def _handle_raw_event(self, evt_type, stream_id, data, is_fin,
                          error_code, cnx_ptr, _stream_ctx_ptr,
                          _sc_ptr) -> None:
        """Per-entry handler invoked from drain_rx_callback.

        For STREAM_DATA / STREAM_FIN events on streams owned by an
        aiopquic_stream_ctx_t wrapper, the Cython side has already
        popped bytes from the wrapper's RX ring into `data` AND
        atomically advanced sc->rx_consumed. The picoquic worker reads
        that counter on its next stream_data callback for the same
        stream and extends MAX_STREAM_DATA — backpressure is a side
        effect of worker packet processing, no Python dispatch needed.
        """
        if evt_type == _EVT_STREAM_DATA or evt_type == _EVT_STREAM_FIN:
            if _stream_ctx_ptr and stream_id not in self._stream_ctxs:
                self._stream_ctxs[stream_id] = _stream_ctx_ptr
            self._events.append(StreamDataReceived(
                stream_id=stream_id,
                data=data if data is not None else memoryview(b""),
                end_stream=(evt_type == _EVT_STREAM_FIN),
            ))
        elif evt_type == _EVT_STREAM_RESET:
            # Abnormal stream end (peer RESET_STREAM) — surface the code
            # so drop-under-load investigations see the actual reason
            # rather than inferring it from the relay's logs.
            logger.warning("stream %d reset by peer: error=%d",
                           stream_id, error_code)
            self._events.append(StreamReset(
                stream_id=stream_id,
                error_code=error_code,
            ))
        elif evt_type == _EVT_STOP_SENDING:
            self._events.append(StopSendingReceived(
                stream_id=stream_id,
                error_code=error_code,
            ))
        elif evt_type == _EVT_CLOSE or evt_type == _EVT_APP_CLOSE:
            self._closed = True
            # Log the close so drop-under-load investigations can read
            # the actual cause: transport vs application close + the
            # error code. error_code 0 is a clean close (debug);
            # anything else is abnormal (warning).
            _kind = ("app" if evt_type == _EVT_APP_CLOSE
                     else "transport")
            if error_code:
                logger.warning("connection closed (%s): error=%d",
                               _kind, error_code)
            else:
                logger.debug("connection closed (%s, clean)", _kind)
            self._events.append(ConnectionTerminated(
                error_code=error_code,
            ))
            # Wake any producer parked in send_stream_data_drained /
            # stream_write_drain waiting on per-stream or ring drain
            # events. After close the worker stops invoking drain
            # callbacks for this cnx, so without these explicit sets
            # the waiter would deadlock until process exit. Producer
            # wakes, loops, observes _closed and exits cleanly.
            for ev in self._stream_tx_drain_events.values():
                ev.set()
            ring_ev = getattr(self._transport, '_tx_event_ring_drain_event', None)
            if ring_ev is not None:
                ring_ev.set()
            # picoquic's close callback has fired — worker has stopped
            # invoking app callbacks for this cnx's streams. Safe to
            # free per-stream wrappers; without this they'd leak until
            # process exit.
            self._destroy_stream_ctxs()
            # Zero the cached cnx ptr; picoquic frees the cnx_t shortly
            # after this callback. cnx_data_sent / cnx_data_received /
            # path_quality already short-circuit on cnx_ptr == 0, so
            # any subsequent observer call returns cleanly rather than
            # dereferencing a freed pointer.
            self._cnx_ptr = 0
        elif evt_type == _EVT_READY:
            if cnx_ptr != 0:
                self._cnx_ptr = cnx_ptr
                self._connected = True
                alpn = self._negotiated_alpn(cnx_ptr)
                self._events.append(HandshakeCompleted(
                    alpn_protocol=alpn,
                ))
                self._events.append(ProtocolNegotiated(
                    alpn_protocol=alpn,
                ))
        elif evt_type == _EVT_ALMOST_READY:
            if cnx_ptr != 0:
                self._cnx_ptr = cnx_ptr
        elif evt_type == _EVT_DATAGRAM:
            self._events.append(DatagramFrameReceived(
                data=data if data is not None else memoryview(b""),
            ))
        elif evt_type == _EVT_STREAM_TX_DRAINED:
            # Picoquic worker drained sc->tx after Python had armed
            # tx_drain_pending. Wake the blocked writer.
            event = self._stream_tx_drain_events.get(stream_id)
            if event is None:
                event = asyncio.Event()
                self._stream_tx_drain_events[stream_id] = event
            event.set()
        elif evt_type == _EVT_STREAM_DESTROY:
            # Stream fully retired by picoquic — drop our cached
            # pointer so the dict doesn't accumulate stale entries.
            # The C side has already dropped the stream-lifetime ref
            # by the time this fires; don't deref.
            # Wake any producer parked in await sc_event.wait() with
            # the about-to-be-popped Event still in hand. Without this
            # set(), the waiter would deadlock — the popped Event is
            # no longer reachable from this dict so no STREAM_TX_DRAINED
            # will ever set it again. Mirrors the _EVT_CLOSE handler.
            ev = self._stream_tx_drain_events.get(stream_id)
            if ev is not None:
                ev.set()
            self._stream_ctxs.pop(stream_id, None)
            self._stream_tx_drain_events.pop(stream_id, None)

    def get_tx_data_drain_event(self, stream_id: int) -> asyncio.Event:
        """Return the asyncio.Event signalling that the picoquic
        worker has drained bytes from this stream's sc->tx after the
        ring was last reported full.

        Lazy-creates the Event on first reference. Caller pattern:
            event = quic.get_tx_data_drain_event(sid)
            event.clear()
            try:
                quic.send_stream_data(sid, data, end_stream)
            except BufferError:
                await event.wait()
                continue  # retry
        """
        event = self._stream_tx_drain_events.get(stream_id)
        if event is None:
            event = asyncio.Event()
            self._stream_tx_drain_events[stream_id] = event
        return event

    def tx_data_ring_used(self, stream_id: int) -> int:
        """Per-stream sc->tx bytes-in-flight — the load-bearing
        backpressure signal in the pull model.

        Returns the count of bytes currently queued in this stream's
        sc->tx ring waiting for the picoquic worker to pull them onto
        the wire. Companion of set_tx_data_drain_pending +
        get_tx_data_drain_event: read used → if over budget, arm the
        per-stream drain-pending flag and await the per-stream event.
        Unknown stream_id returns 0.

        Conceptually `used` is how full the per-stream byte pipe is
        between Python (producer) and picoquic (consumer):
          - bytes you've pushed via send_stream_data but not yet on
            the wire
          - drained monotonically by the worker; wake via the
            per-stream sc->tx drain event
          - cap_minus_used = headroom before next push raises
            BufferError
          - used / wire_rate ≈ latency this queue adds above wire RTT
        """
        sc_ptr = self._stream_ctxs.get(stream_id, 0)
        if not sc_ptr or self._transport is None:
            return 0
        return self._transport.tx_data_ring_used(sc_ptr)

    def set_tx_data_drain_pending(self, stream_id: int) -> None:
        """Arm the per-stream sc->tx_drain_pending flag so the next
        worker drain of this stream's sc->tx fires
        SPSC_EVT_STREAM_TX_DRAINED — even if sc->tx wasn't full.
        Pair with get_tx_data_drain_event(stream_id) for the canonical
        clear-arm-recheck-wait pattern against a byte budget below
        sc->tx full. No-op on unknown stream_id."""
        sc_ptr = self._stream_ctxs.get(stream_id, 0)
        if not sc_ptr or self._transport is None:
            return
        self._transport.set_tx_data_drain_pending(sc_ptr)

    def clear_tx_data_drain_pending(self, stream_id: int) -> None:
        """Clear the per-stream sc->tx_drain_pending flag. Use when
        the producer observed the byte budget cleared between arm and
        wait (the race-recovery branch of clear-arm-recheck-wait).
        No-op on unknown stream_id."""
        sc_ptr = self._stream_ctxs.get(stream_id, 0)
        if not sc_ptr or self._transport is None:
            return
        self._transport.clear_tx_data_drain_pending(sc_ptr)

    def path_quality(self) -> dict:
        """Snapshot of picoquic path-quality metrics for this cnx.

        Returns a dict with cwnd (cwin), bytes_in_transit, smoothed
        rtt, pacing_rate, lost packet counts, bytes sent/received.
        The load-bearing CC + FC observability surface — use to
        detect BBR freeze (cwin flat + bytes_in_transit pegged at
        cwin for many RTT) or other CC pathologies without parsing
        qlog.

        Returns empty dict if the cnx is not yet open or the
        transport is gone."""
        if self._cnx_ptr == 0 or self._transport is None:
            return {}
        return self._transport.path_quality(self._cnx_ptr)

    def tx_event_ring_fill(self) -> float:
        """Current TX event-ring fill ratio in [0.0, 1.0],
        connection-global, for backpressure-aware yielding from tight
        send loops.

        BufferError from `send_stream_data` is the hard backpressure
        signal — ring full, caller must await the drain event. This
        method is the soft signal: ratio of pending events to ring
        capacity. Callers can yield (e.g., `await asyncio.sleep(0)`)
        when the ratio exceeds a threshold to give the picoquic worker
        thread cycles to drain. Count-based yields starve the worker
        on fast Python paths (notably Linux with UDP GSO, where the
        Python side can outrun a single sendmsg-per-batch worker).
        """
        if self._transport is None:
            return 0.0
        cap = self._transport.tx_event_ring_capacity
        if not cap:
            return 0.0
        return self._transport.tx_event_ring_count / cap

    def next_event(self) -> QuicEvent | None:
        """Dequeue next event from the connection.

        Drains the SPSC ring only when the local queue is EMPTY —
        consume-to-empty before re-ingesting. Draining on every call
        let the worker refill _events faster than the consumer parsed
        whenever wire-rate exceeded parse-rate, growing the queue
        without bound (each StreamDataReceived pins its chunk: ~4 GB
        measured on a 2-process subscriber). Bounding ingest to one
        drain batch per empty also lets the dormant FC chain engage:
        un-popped bytes stay in sc->rx, rx_consumed stalls, the worker
        stops extending MAX_STREAM_DATA, and the peer slows to the
        consumer's actual parse rate. No lost wakeups: the worker
        notifies the eventfd per push and clear_rx re-arms when
        entries remain after a drain."""
        if self._engine is None and not self._events:
            self._drain_and_convert()
        if self._events:
            return self._events.popleft()
        return None

    def _enqueue_raw(self, evt_type: int, stream_id: int, data,
                     is_fin: bool, error_code: int,
                     stream_ctx_ptr: int) -> None:
        """Engine-side: append a raw event tuple as a QuicEvent.

        Routes one drained SPSC entry into this connection's queue.
        Mirrors the conversion logic in _drain_and_convert. Called by
        QuicEngine after demuxing by cnx_ptr.
        """
        if evt_type == _EVT_STREAM_DATA:
            if stream_ctx_ptr and stream_id not in self._stream_ctxs:
                self._stream_ctxs[stream_id] = stream_ctx_ptr
            self._events.append(StreamDataReceived(
                stream_id=stream_id,
                data=data if data is not None else memoryview(b""),
                end_stream=False,
            ))
        elif evt_type == _EVT_STREAM_FIN:
            if stream_ctx_ptr and stream_id not in self._stream_ctxs:
                self._stream_ctxs[stream_id] = stream_ctx_ptr
            self._events.append(StreamDataReceived(
                stream_id=stream_id,
                data=data if data is not None else memoryview(b""),
                end_stream=True,
            ))
        elif evt_type == _EVT_STREAM_RESET:
            self._events.append(StreamReset(
                stream_id=stream_id, error_code=error_code,
            ))
        elif evt_type == _EVT_STOP_SENDING:
            self._events.append(StopSendingReceived(
                stream_id=stream_id, error_code=error_code,
            ))
        elif evt_type in (_EVT_CLOSE, _EVT_APP_CLOSE):
            self._closed = True
            self._events.append(ConnectionTerminated(error_code=error_code))
            # Wake parked drain waiters — see matching block ~line 256.
            for ev in self._stream_tx_drain_events.values():
                ev.set()
            ring_ev = getattr(self._transport, '_tx_event_ring_drain_event', None)
            if ring_ev is not None:
                ring_ev.set()
            self._destroy_stream_ctxs()
            # See companion block: zero cnx ptr so observer accessors
            # short-circuit cleanly rather than UAF on the freed cnx.
            self._cnx_ptr = 0
        elif evt_type == _EVT_READY:
            alpn = self._negotiated_alpn(0)
            self._events.append(HandshakeCompleted(alpn_protocol=alpn))
            self._events.append(ProtocolNegotiated(alpn_protocol=alpn))
        elif evt_type == _EVT_DATAGRAM:
            self._events.append(DatagramFrameReceived(
                data=data if data is not None else memoryview(b""),
            ))
        elif evt_type == _EVT_STREAM_TX_DRAINED:
            # Engine-side mirror of _handle_raw_event's STREAM_TX_DRAINED
            # case. Picoquic worker drained sc->tx after Python had armed
            # tx_drain_pending; wake the blocked writer on this stream.
            # Without this branch the engine-routed dispatch silently
            # dropped the wake and producers parked in stream_write_drain
            # forever.
            event = self._stream_tx_drain_events.get(stream_id)
            if event is None:
                event = asyncio.Event()
                self._stream_tx_drain_events[stream_id] = event
            event.set()
        elif evt_type == _EVT_STREAM_DESTROY:
            self._stream_ctxs.pop(stream_id, None)
            self._stream_tx_drain_events.pop(stream_id, None)

    def _get_or_create_stream_ctx(self, stream_id: int) -> int:
        """Lazy-allocate the per-stream wrapper. Both TX and RX paths
        funnel through here so a single wrapper covers bidi streams."""
        # Post-close: do not lazy-allocate. _destroy_stream_ctxs ran in
        # the close handler and any fresh sc inserted here has no
        # cleanup path (the close handler won't fire again). Returning
        # 0 signals the caller to short-circuit cleanly.
        if self._closed:
            return 0
        sc = self._stream_ctxs.get(stream_id)
        if sc is None:
            sc = stream_ctx_create()
            self._stream_ctxs[stream_id] = sc
        return sc

    def _destroy_stream_ctxs(self) -> None:
        """Free all per-stream wrappers + their TX/RX byte rings.
        Idempotent — safe to call multiple times. Called when the
        connection-close callback has fired (picoquic worker has
        stopped invoking app callbacks for this cnx's streams).
        Per-stream early cleanup (before connection close) requires
        picoquic_unlink_app_stream_ctx via SPSC dispatch; deferred to
        a future landing — bounded leak per connection until then."""
        if not self._stream_ctxs:
            return
        for sc in self._stream_ctxs.values():
            stream_ctx_destroy(sc)
        self._stream_ctxs.clear()

    def send_stream_data(self, stream_id: int, data: bytes,
                         end_stream: bool = False) -> None:
        """Send data on a stream — PULL-model with real backpressure.

        Calls the atomic Cython send primitive that bundles per-stream-
        ring push + MARK_ACTIVE event push + worker wake-up under a
        single GIL hold. All-or-nothing: on BufferError, no bytes are
        committed to sc->tx, so the caller can retry the SAME data
        buffer without risking duplicated bytes on the wire.

        Raises BufferError on any retryable backpressure (TX event ring
        full or per-stream send ring full); raises MemoryError on
        allocation failure.

        Post-close: returns cleanly without committing or raising.
        Mirrors the WT-side self-protective gate so producers racing
        teardown do not orphan a fresh sc per call.
        """
        if self._closed:
            return
        sc = self._get_or_create_stream_ctx(stream_id)
        if sc == 0:
            return
        rc = self._transport.tx_send_atomic(
            stream_id,
            data if data is not None else b"",
            end_stream,
            self._cnx_ptr,
            sc,
            getattr(self._configuration, 'stream_ring_cap',
                    _STREAM_RING_CAP),
        )
        if rc == 1:
            raise BufferError(
                f"TX event ring full (stream={stream_id})"
            )
        if rc == 2:
            raise BufferError(
                f"per-stream send ring full "
                f"(stream={stream_id}, need={len(data) if data else 0})"
            )
        if rc < 0:
            raise MemoryError(
                f"send_stream_data alloc failed (stream={stream_id})"
            )

    async def send_stream_data_drained(self, stream_id: int, data: bytes,
                                         end_stream: bool = False,
                                         *,
                                         soft_yield_at: float = 0.5,
                                         hard_wait_at: float = 0.9) -> None:
        """Send with built-in TX-ring backpressure for raw QUIC.

        Composes send_stream_data + get_tx_data_drain_event + tx_event_ring_fill
        and the connection-global tx_event_ring_drain_event so every caller
        gets worker-thread-aware pacing without copying the heuristic.
        Mirrors aiopquic.asyncio.WebTransportSession.send_stream_data_drained.

        Layers:
          - hard ring-saturation guard: await the connection-global
            tx_event_ring_drain_event when tx_event_ring_fill > hard_wait_at
            (default 0.9). Uses the clear-arm-recheck-wait pattern so
            there is no lost wakeup if the worker drains the ring
            between our threshold check and the wait.
          - send: atomic push to sc->tx + MARK_ACTIVE under one GIL hold.
          - soft post-send yield: await asyncio.sleep(0) when
            tx_event_ring_fill > soft_yield_at (default 0.5).
          - BufferError retry: await whichever drain event the Cython
            side armed (sc->tx-full arms per-stream event; TX-ring-full
            arms connection-global event). asyncio.wait FIRST_COMPLETED
            on both wakes us on whichever fires.

        Application-level policies (byte budgets, fairness) belong
        above this layer. The canonical per-stream byte-budget
        primitive trio is:
          - tx_data_ring_used(stream_id) — data-ring queue depth
            (where the payload actually lives in pull mode)
          - set_tx_data_drain_pending(stream_id) — request a wake
            on next sc->tx drain below sc->tx-full
          - get_tx_data_drain_event(stream_id) — the per-stream wake
        Pair these in a clear-arm-recheck-wait loop above the call
        to this helper.
        """
        # Aggregate TX gate at the stream-creation boundary: raw QUIC
        # creates the per-stream sc lazily on first write, so "sid not
        # yet in _stream_ctxs" IS stream creation. Park while the
        # process-wide queued-TX budget (QuicConfiguration.
        # tx_max_queued_bytes) is exceeded; resume below cap/2. The
        # green-light is connection drain capacity — any stream's
        # drain frees budget — so one wedged stream can never stall
        # the gate. Bounds producer run-ahead that per-stream caps
        # miss under short-stream churn (fresh stream = fresh budget).
        if stream_id not in self._stream_ctxs:
            cap = getattr(self._configuration, 'tx_max_queued_bytes',
                          4 * 1024 * 1024)
            if cap and tx_data_bytes_queued() > cap:
                low = cap // 2
                while not self._closed and tx_data_bytes_queued() > low:
                    await asyncio.sleep(0.002)
            if self._closed:
                # Yield before the no-op return: an await that never
                # suspends starves the event loop — a produce loop
                # calling this on a closed cnx would otherwise spin
                # at 100% with cancellation undeliverable.
                await asyncio.sleep(0)
                return
            # Cooperative yield at the stream-creation boundary — the
            # raw-QUIC mirror of WT create_stream's worker round-trip
            # suspension. The raw send path otherwise never truly
            # suspends under an unpaced producer, so this process's
            # OWN eventfd drain starves: housekeeping events
            # (STREAM_DESTROY/released) drop off the full ring and
            # scs leak — measured 110K alive / 94K dropped destroy
            # events on a 2-process publisher. One loop turn per
            # stream (~600/s at full churn) services the drain and
            # any co-located consumer.
            await asyncio.sleep(0)
        sc_event = self.get_tx_data_drain_event(stream_id)
        ring_event = self._transport.tx_event_ring_drain_event
        while True:
            # Close-time guard: _EVT_CLOSE / _EVT_APP_CLOSE handlers
            # set every per-stream event and the ring event explicitly
            # to unpark waiters. Without this check the producer would
            # observe set() events, fall through, call send_stream_data
            # on a closed cnx, and propagate that failure into the
            # caller. Return cleanly instead. STREAM_DESTROY also sets
            # the per-stream event before popping it (see handler).
            # The sleep(0) guarantees this coroutine suspends at least
            # once even on a dead cnx — without it an unpaced produce
            # loop never yields, the event loop never runs again, and
            # task cancellation can never be delivered (100% spin).
            if self._closed:
                await asyncio.sleep(0)
                return
            # Connection-global ring pressure: tx_event_ring_fill reads the
            # SPSC TX event ring. Wait on the connection-global ring
            # event, NOT the per-stream sc->tx event (which is only
            # armed when sc->tx fills).
            if self.tx_event_ring_fill() > hard_wait_at:
                ring_event.clear()
                self._transport.arm_tx_event_ring_drain_pending()
                if self.tx_event_ring_fill() <= hard_wait_at:
                    self._transport.clear_tx_event_ring_drain_pending()
                    continue
                await ring_event.wait()
                continue
            # Clear both events BEFORE the send. If the worker fires
            # them during our send_stream_data call (race between the
            # Cython arming and us catching the exception), the set
            # will land AFTER our clear and be captured on the wait.
            # Clearing AFTER the BufferError loses that wakeup.
            sc_event.clear()
            ring_event.clear()
            try:
                self.send_stream_data(stream_id, data, end_stream=end_stream)
                if self.tx_event_ring_fill() > soft_yield_at:
                    await asyncio.sleep(0)
                return
            except BufferError:
                # Cython side armed the appropriate signal:
                # - sc->tx full → per-stream sc->tx_drain_pending
                # - TX event ring full → connection-global tx_event_ring_drain_pending
                # Events were cleared BEFORE the call so no lost wakeup.
                sc_wait = asyncio.create_task(sc_event.wait())
                ring_wait = asyncio.create_task(ring_event.wait())
                done, pending = await asyncio.wait(
                    [sc_wait, ring_wait],
                    return_when=asyncio.FIRST_COMPLETED)
                # Cancellation hygiene: await the cancelled task under
                # suppress so the loop reaps it now instead of leaking
                # a pending Task until the next scheduler turn (matters
                # under teardown storms).
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                # Re-check close after the wait — the close handler
                # may have set our events to unpark us cleanly.
                if self._closed:
                    return

    def send_datagram_frame(self, data: bytes) -> None:
        """Send a datagram frame."""
        self._transport.push_tx_event(
            _TX_DATAGRAM, 0,
            data=data, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def get_stream_buf_stats(self, stream_id: int):
        """Return (pushed, popped, push_hash, pop_hash) for the per-stream
        TX byte ring. pushed and popped are cumulative byte totals; in a
        clean run they are equal at stream close. push_hash and pop_hash
        are FNV-1a accumulators (only meaningful when AIOPQUIC_TX_HASH=1
        was set when the ring was created); equal hashes prove byte-for-
        byte conservation through the ring. Returns None if the stream
        has no per-stream wrapper or no TX ring on that wrapper.
        """
        sc = self._stream_ctxs.get(stream_id)
        if sc is None:
            return None
        sb = stream_ctx_get_tx(sc)
        if not sb:
            return None
        return stream_buf_stats(sb)

    def get_next_available_stream_id(self, is_unidirectional: bool = False) -> int:
        """Allocate the next stream ID."""
        if is_unidirectional:
            stream_id = self._next_uni_id
            self._next_uni_id += 4
        else:
            stream_id = self._next_bidi_id
            self._next_bidi_id += 4
        return stream_id

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        """Reset a stream with the given error code."""
        self._transport.push_tx_event(
            _TX_STREAM_RESET, stream_id,
            error_code=error_code, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def stop_stream(self, stream_id: int, error_code: int) -> None:
        """Send STOP_SENDING on a stream."""
        self._transport.push_tx_event(
            _TX_STOP_SENDING, stream_id,
            error_code=error_code, cnx_ptr=self._cnx_ptr,
        )
        self._transport.wake_up()

    def close(self, error_code: int = 0, frame_type: int | None = None,
              reason_phrase: str = "") -> None:
        """Close the connection."""
        if self._transport is not None and not self._closed:
            self._transport.push_tx_event(
                _TX_CLOSE, 0,
                error_code=error_code, cnx_ptr=self._cnx_ptr,
            )
            self._transport.wake_up()
            self._closed = True
        # NOTE: per-stream send rings are intentionally NOT destroyed
        # here. picoquic-pthread may still hold stream_ctx pointers to
        # them; freeing now risks use-after-free. Proper lifecycle
        # requires an explicit deactivate→free dance via picoquic
        # mark_active_stream(active=0) before destroy. Until that lands,
        # buffers are reclaimed at process exit. Acceptable for tests
        # and short-lived sessions; leaks per long-lived connection
        # otherwise — TODO before production ship.

    def stop(self) -> None:
        """Stop the transport entirely.

        For engine-spawned connections this is a no-op; the engine
        owns the transport and shuts it down on engine.close().
        """
        if self._engine is not None:
            return
        if self._transport is not None:
            self._transport.stop()
            self._transport = None


class QuicEngine:
    """Server-side multiplexer over a single picoquic engine.

    Owns the TransportContext, drains SPSC events, and routes them by
    cnx pointer to a per-connection QuicConnection. On the first READY
    event for a new cnx, calls create_protocol(connection) to spawn a
    fresh user protocol bound to that connection.

    The engine is what serve() returns to the user. Lifetime of all
    connections is bound to the engine (engine.close() tears them
    down). Used only for the server case; client connect() wraps a
    standalone QuicConnection.
    """

    def __init__(self, *, configuration: QuicConfiguration,
                 create_protocol, stream_handler=None):
        self._configuration = configuration
        self._create_protocol = create_protocol
        self._stream_handler = stream_handler
        self._transport: TransportContext | None = None
        self._connections: dict[int, QuicConnection] = {}
        self._protocols: dict[int, "object"] = {}

    def _start_transport(self, port: int = 0) -> None:
        if self._transport is not None:
            return
        cfg = self._configuration
        if cfg.event_ring_capacity is not None:
            self._transport = TransportContext(
                ring_capacity=cfg.event_ring_capacity)
        else:
            self._transport = TransportContext()
        keylog = cfg.secrets_log_file or os.environ.get('SSLKEYLOGFILE')
        self._transport.start(
            port=port,
            cert_file=cfg.certificate_file,
            key_file=cfg.private_key_file,
            # Single configured ALPN keeps the simple default_alpn path
            # (byte-identical to before). Multiple ALPNs switch to the
            # negotiation path: client offers the list (request_alpn_list),
            # server selects (alpn_select_fn) — both require default_alpn
            # NULL, which start() sets when alpn_list is given.
            alpn=(cfg.alpn_protocols[0]
                  if cfg.alpn_protocols and len(cfg.alpn_protocols) == 1
                  else None),
            alpn_list=(list(cfg.alpn_protocols)
                       if cfg.alpn_protocols and len(cfg.alpn_protocols) > 1
                       else None),
            is_client=cfg.is_client,
            idle_timeout_ms=int(cfg.idle_timeout * 1000),
            max_datagram_frame_size=(cfg.max_datagram_frame_size or 0),
            keylog_filename=keylog,
            rx_data_ring_cap=cfg.max_stream_data,
            congestion_control_algorithm=cfg.congestion_control_algorithm,
            initial_max_data=cfg.max_data,
            initial_max_streams_uni=getattr(cfg, 'max_streams_uni', 0) or 0,
            initial_max_streams_bidi=getattr(cfg, 'max_streams_bidi', 0) or 0,
            keep_alive_interval_ms=int((cfg.keep_alive_interval or 0)
                                        * 1000),
            socket_buffer_size=(cfg.socket_buffer_size or 0),
            qlog_dir=cfg.qlog_dir,
        )

    @property
    def eventfd(self) -> int:
        if self._transport is None:
            return -1
        return self._transport.eventfd

    def drain_and_route(self) -> None:
        """Drain SPSC events and route to per-cnx protocols.

        For each event:
          - cnx_ptr == 0: engine-level (transport ready); ignored here.
          - cnx_ptr unknown: first READY for a new cnx — spawn
            QuicConnection + protocol via create_protocol().
          - cnx_ptr known: enqueue raw event on that connection, then
            drive the bound protocol to drain it.
        """
        if self._transport is None:
            return
        raw_events = self._transport.drain_rx()
        for (evt_type, stream_id, data, is_fin, error_code,
             cnx_ptr, stream_ctx_ptr, _sc_ptr) in raw_events:
            if cnx_ptr == 0:
                continue  # engine-level event, not per-cnx
            conn = self._connections.get(cnx_ptr)
            if conn is None:
                conn = QuicConnection(
                    configuration=self._configuration,
                    engine=self, cnx_ptr=cnx_ptr,
                )
                self._connections[cnx_ptr] = conn
                proto = self._create_protocol(
                    conn, stream_handler=self._stream_handler,
                )
                # Engine owns the eventfd reader; bind protocol to the
                # loop without adding a second reader (which would
                # replace ours and break demux for all other cnx).
                import asyncio as _asyncio
                proto._loop = _asyncio.get_event_loop()
                self._protocols[cnx_ptr] = proto
            conn._enqueue_raw(evt_type, stream_id, data, is_fin,
                              error_code, stream_ctx_ptr)
            proto = self._protocols[cnx_ptr]
            proto._process_events()
            if evt_type in (_EVT_CLOSE, _EVT_APP_CLOSE):
                self._connections.pop(cnx_ptr, None)
                self._protocols.pop(cnx_ptr, None)

    def close(self) -> None:
        """Tear down all connections and stop the transport.

        Order matters: stop the transport (joins the picoquic worker
        thread, then runs picoquic_free which fires picoquic_callback_close
        on the per-cnx callback for every cnx) BEFORE clearing
        self._connections / self._protocols. picoquic_free's close
        callbacks push _EVT_CLOSE events to the SPSC RX ring; those
        events carry per-stream wrapper (aiopquic_stream_ctx_t*)
        pointers picoquic still holds via app_stream_ctx. If we cleared
        Python state first, GC could destroy QuicConnection objects
        whose _stream_ctxs dicts hold the same wrapper pointers, and
        picoquic_free would then walk freed memory.

        Pre-2026-05-05 ordering (clear-then-stop) reproduced an
        intermittent SEGV in transport.stop on aiomoqt full pytest
        ~10% of runs.
        """
        # Push CLOSE events for any still-open cnxs; the worker will
        # drain these as it goes through teardown inside transport.stop.
        for proto in list(self._protocols.values()):
            try:
                proto.close()
            except Exception:
                pass
        # Stop transport first — joins worker, runs picoquic_free,
        # fires every close callback while Python wrappers are alive.
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
        # Now safe to drop Python state; picoquic is fully torn down.
        self._connections.clear()
        self._protocols.clear()
