"""QUIC configuration — matches qh3.quic.configuration API."""

from dataclasses import dataclass, field


@dataclass
class QuicConfiguration:
    """Configuration for a QUIC connection.

    Defaults tuned for streaming-media workloads (MoQT, WT). Override
    individual fields where a different policy is needed.
    """
    alpn_protocols: list[str] | None = None
    # QUIC idle timeout (seconds). 60s is the historic working default
    # that doesn't fire spuriously under loop-starved setup paths
    # (e.g. -r 0 max-rate producer hogging asyncio during MoQT
    # SUBSCRIBE / SubscribeOk handshake). Override downward if rapid
    # peer-dead detection is needed; only safe when the application
    # cannot starve setup-phase scheduling.
    idle_timeout: float = 60.0
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
    # Congestion-control algorithm for picoquic to use on this transport.
    # None defers to picoquic's compile-time default (newreno). Common
    # values: "newreno" (loss-based, default), "cubic" (loss-based,
    # widely deployed), "bbr" (delay-based, high-BDP friendly), "bbr1"
    # (older BBRv1), "prague" (L4S/ECN), "dcubic", "fast". The string
    # is passed verbatim to picoquic_set_default_congestion_algorithm_by_name;
    # an unknown name falls back to the compile-time default.
    congestion_control_algorithm: str | None = None
    # SPSC TX/RX event ring capacity (entries — must be a power of 2).
    # Each event is an ~64 B notification between the picoquic worker
    # thread and the asyncio drain. Sized for the worst-case burst of
    # stream_data / stream_fin notifications between asyncio drain
    # cycles. None defers to the compile-time default (262144 — about
    # 16 MiB per ring). Bump higher for sustained multi-Gbps workloads
    # with high stream-churn rates (e.g. one stream per video frame
    # group); lower to reduce memory footprint when stream rate is
    # low. Below ~16384 entries, multi-Gbps stream-churn workloads
    # exhibit silent stream-data event drops on the receiver — the
    # bytes are buffered on the per-stream ring, but the asyncio loop
    # is never notified about them, so short streams whose only
    # notifications fall in the overflow window are silently lost.
    event_ring_capacity: int | None = None
    # Per-stream sc->tx data-ring capacity (bytes). Hard cap on bytes
    # Python may push to a single stream's send queue before
    # send_stream_data raises BufferError. Preserves QUIC stream
    # independence (HOLB-free backpressure). Default 16 MiB sized to
    # accommodate per-stream BDP on Internet-grade paths: 500 Mbps
    # per-stream × 200 ms RTT = 12.5 MB BDP, plus jitter headroom.
    # Constraint: must be >= max object size on this connection.
    # App-layer soft caps (aiomoqt tx_max_inflight_bytes) MUST be
    # below this to engage; matching ring_cap = max_stream_data
    # keeps the Python queue and picoquic per-stream FC ceiling
    # symmetric.
    stream_ring_cap: int = 4 * 1024 * 1024
    # Initial MAX_STREAMS advertised to peer at handshake — RFC 9000
    # §4.6 / §19.11: cumulative cap on highest stream ID the peer may
    # open (the peer extends with new MAX_STREAMS frames as streams
    # complete on its side). So 256 is INITIAL credit; healthy
    # operation flows freely as the peer extends. The cap only bites
    # when the peer stops extending (blackhole disconnect): then
    # picoquic rejects further opens after 256 unique IDs are used,
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
    # latency ≈ cap / drain rate: 8 MiB ≈ 11-22 ms at ~3 Gbps
    # (measured p50 35 ms at 16 MiB on the 2-process churn bench).
    # Size ≥ path BDP to avoid starving the wire; raise for
    # high-BDP WAN paths, lower for tighter latency budgets.
    # None or 0 disables.
    tx_max_queued_bytes: int | None = 8 * 1024 * 1024

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
