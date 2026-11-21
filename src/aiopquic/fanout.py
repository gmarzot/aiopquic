"""Public re-export of the Cython fanout API.

`DestSet` holds destinations of one TransportContext;
`TransportContext.fanout()` / `fanout_datagram()` write to all of them in
one call and refuse a destination with one of the FANOUT_* codes.
"""

from aiopquic._binding._transport import (
    DestSet,
    FANOUT_EVENT_RING_FULL,
    FANOUT_GONE,
    FANOUT_NO_MEMORY,
    FANOUT_QUIC_DATAGRAM,
    FANOUT_QUIC_STREAM,
    FANOUT_RING_FULL,
    FANOUT_TOO_LARGE,
    FANOUT_WT_DATAGRAM,
    FANOUT_WT_STREAM,
)

__all__ = [
    "DestSet",
    "FANOUT_EVENT_RING_FULL",
    "FANOUT_GONE",
    "FANOUT_NO_MEMORY",
    "FANOUT_QUIC_DATAGRAM",
    "FANOUT_QUIC_STREAM",
    "FANOUT_RING_FULL",
    "FANOUT_TOO_LARGE",
    "FANOUT_WT_DATAGRAM",
    "FANOUT_WT_STREAM",
]
