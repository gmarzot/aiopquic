"""aiopquic exception types.

Kept transport-level: aiopquic must not depend on any layer above
it (e.g. aiomoqt). Higher-level libraries may alias or subclass
these.
"""


class StreamUnderflow(Exception):
    """Raised by StreamChain pull_* / parse_* when fewer bytes are
    available than requested. Carries (pos, needed) so callers can
    rewind and await more data.
    """

    def __init__(self, pos: int, needed: int):
        self.pos = pos
        self.needed = needed


class LengthFieldError(OverflowError):
    """A decoded length field exceeds its bound, e.g. a value the
    stream cannot carry. Names the field, its value, the bound and the
    field's chain offset.
    """

    def __init__(self, field: str, value: int, limit: int, offset: int,
                 context: str = ""):
        msg = f"{field} {value} exceeds limit {limit} at offset {offset}"
        if context:
            msg += f" ({context})"
        super().__init__(msg)
        self.field = field
        self.value = value
        self.limit = limit
        self.offset = offset
