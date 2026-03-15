"""QUIC configuration — matches qh3.quic.configuration API."""

from dataclasses import dataclass, field


@dataclass
class QuicConfiguration:
    """Configuration for a QUIC connection."""
    alpn_protocols: list[str] | None = None
    idle_timeout: float = 60.0
    is_client: bool = True
    max_data: int = 1048576
    max_stream_data: int = 1048576
    max_datagram_frame_size: int | None = None
    server_name: str | None = None
    certificate_file: str | None = None
    private_key_file: str | None = None
    verify_mode: int | None = None

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
