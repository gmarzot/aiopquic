"""aiopquic — High-performance asyncio QUIC/HTTP3, picoquic-backed."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("aiopquic")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

del _pkg_version, PackageNotFoundError
