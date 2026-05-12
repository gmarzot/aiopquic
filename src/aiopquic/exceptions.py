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
