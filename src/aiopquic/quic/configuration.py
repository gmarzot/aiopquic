"""QUIC configuration — matches qh3.quic.configuration API."""

from dataclasses import dataclass, field


@dataclass
class QuicConfiguration:
    """Configuration for a QUIC connection.

    Defaults tuned for streaming-media workloads (MoQT, WT). Override
    individual fields where a different policy is needed.
    """
    alpn_protocols: list[str] | None = None
    # QUIC idle timeout (seconds). 10s balances prompt dead-peer
    # detection against scheduling jitter. Safe at this value because
    # the aggregate TX gate and the per-new-stream cooperative yield
    # keep the asyncio loop responsive even under unpaced producers —
    # setup-phase exchanges can no longer be starved long enough to
    # trip it. Raise for long-quiescent control-plane sessions.
    # QUIC idle timeout in seconds: the connection closes if no packet
    # is exchanged for this long. 30 s matches picoquic's default and
    # leaves headroom for a flow-controlled receiver whose consumer
    # briefly stalls (a lower value drops such connections — enable
    # keep_alive_interval below for active liveness instead of relying
    # on a short idle timeout).
    idle_timeout: float = 30.0
    # QUIC keep-alive interval in seconds. None/0 = disabled (default).
    # When set, the peer sends PING frames at this interval to hold an
    # otherwise-quiet connection open past idle_timeout — important for
    # a flow-controlled receiver whose consumer stalls and
    # back-pressures the sender to silence (the connection would
    # otherwise idle-time-out and drop). Pick well under idle_timeout
    # (e.g. idle_timeout/3). App opts in; nothing is sent when None.
    keep_alive_interval: float | None = None
    # UDP socket SO_RCVBUF/SO_SNDBUF request in bytes. None = aiopquic's
    # 64 MiB default. The kernel clamps the effective value to
    # net.core.rmem_max / wmem_max (Linux also doubles internally), so
    # the real ceiling is the host sysctl. Lower this to cap per-socket
    # memory when running many sockets (e.g. high subscriber-process
    # fan-out, where N × clamped-buffer adds up).
    socket_buffer_size: int | None = None
    is_client: bool = True
    # Initial flow-control windows advertised to the peer at handshake.
    # The peer is bound by spec to never send more than max_stream_data
    # bytes unconsumed on a single stream (and max_data across all
    # streams) until we extend MAX_STREAM_DATA. The C-side per-stream
    # RX byte ring is sized to match this advertised cap at allocation
    # time, so the spec-permitted worst case (peer fills the entire
    # window before we drain a byte) is handled correctly. As the
    # consumer drains bytes the picoquic worker thread extends the
    # cap via picoquic_open_flow_control. Higher caps tolerate larger
    # peer bursts before backpressure kicks in; lower caps reduce
    # per-stream memory and bufferbloat.
    max_data: int = 16 * 1024 * 1024
    max_stream_data: int = 16 * 1024 * 1024
    # 65535: the QUIC max for the DATAGRAM frame extension (RFC 9221).
    # Enables datagrams by default (h3zero only advertises h3_datagram
    # + webtransport_max_sessions when this is non-zero).
    max_datagram_frame_size: int | None = 65535
    server_name: str | None = None
    certificate_file: str | None = None
    private_key_file: str | None = None
    verify_mode: int | None = None
    # NSS Key Log Format file (Wireshark-compatible). When set, picoquic
    # writes TLS secrets per connection so packet captures can be
    # decrypted offline. Honors the SSLKEYLOGFILE env var as a default.
    secrets_log_file: str | None = None
    # Directory for picoquic qlog traces (one JSON file per connection,
    # named by initial CID: CC state, RTT, FC frames). Directory must
    # already exist. The AIOPQUIC_QLOG_DIR env var serves as a
    # shell-level fallback when this is None.
    qlog_dir: str | None = None
    # Congestion-control algorithm for picoquic to use on this transport.
    # Default "bbr1": loss-tolerant (loss-based CCs collapse on the
    # GIL-induced loss blips common on loopback and loaded hosts) and
    # free of the picoquic "bbr" (v3) sub-128µs-RTT cwnd freeze
    # (upstream #2118). Other values: "newreno", "cubic", "bbr",
    # "prague" (L4S/ECN), "dcubic", "fast". None defers to picoquic's
    # compile-time default (newreno). The string is passed verbatim to
    # picoquic_set_default_congestion_algorithm_by_name; an unknown
    # name falls back to the compile-time default.
    congestion_control_algorithm: str | None = "bbr1"
    # SPSC TX/RX event ring capacity (entries — must be a power of 2).
    # Each event is an ~64 B notification between the picoquic worker
    # thread and the asyncio drain. None defers to the compile-time
    # defaults (TX 2048, RX 16384 — callback.h). Data-ready
    # notifications are coalesced to at most one outstanding per
    # stream, so RX occupancy scales with live stream count plus
    # lifecycle events, not packet rate. Bump for workloads with very
    # large live-stream counts; lower to shave memory.
    event_ring_capacity: int | None = None
    # Per-stream sc->tx data-ring capacity (bytes). Hard cap on bytes
    # Python may push to a single stream's send queue before
    # send_stream_data raises BufferError. Preserves QUIC stream
    # independence (HOLB-free backpressure). Constraint: must be
    # >= max object size on this connection. App-layer soft caps
    # (aiomoqt tx_max_inflight_bytes) MUST be below this to engage.
    stream_ring_cap: int = 4 * 1024 * 1024
    # Initial MAX_STREAMS advertised to peer at handshake — RFC 9000
    # §4.6 / §19.11: cumulative cap on highest stream ID the peer may
    # open (the peer extends with new MAX_STREAMS frames as streams
    # complete on its side). This is INITIAL credit; healthy
    # operation flows freely as the peer extends. The cap only bites
    # when the peer stops extending (blackhole disconnect): then
    # picoquic rejects further opens once the credit is consumed,
    # bounding stream-count growth during the idle_timeout window.
    # Lower for memory-constrained multi-tenant servers; raise for
    # workloads with very high stream-churn rates.
    max_streams_uni: int = 512
    max_streams_bidi: int = 512
    # Aggregate cap on bytes committed to per-stream TX data rings but
    # not yet pulled by the picoquic worker (process-wide; see
    # aiopquic._binding._transport.tx_data_bytes_queued). Bounds
    # producer run-ahead that per-stream caps structurally miss:
    # short-stream churn resets the per-stream budget every rollover,
    # so an unpaced producer can spread unbounded backlog across
    # thousands of fresh streams while QUIC's own throttles (CC, FC,
    # MAX_STREAMS) all read green — CC paces the wire, not the app;
    # peer FC credits track delivery, which keeps up with the wire.
    # Enforced at stream-creation boundaries (WT create_stream, raw
    # QUIC first write to a new stream) with park/resume hysteresis
    # (park above cap, resume below cap/2). Steady-state added
    # latency ≈ 0.75 × cap / drain rate: 4 MiB ≈ 8 ms at ~3 Gbps.
    # Size ≥ path BDP to avoid starving the wire (4 MiB covers
    # 1 Gbps × 33 ms); raise for high-BDP WAN paths, lower for
    # tighter latency budgets. None or 0 disables.
    tx_max_queued_bytes: int | None = 4 * 1024 * 1024

    def load_cert_chain(self, certfile: str, keyfile: str | None = None,
                        password: str | None = None) -> None:
        """Load certificate and private key files."""
        self.certificate_file = certfile
        if keyfile is not None:
            self.private_key_file = keyfile

    def load_verify_locations(self, cafile: str | None = None,
                              capath: str | None = None,
                              cadata: bytes | None = None) -> None:
        """Load CA certificates for peer verification."""
        pass  # picoquic uses system CA store by default
