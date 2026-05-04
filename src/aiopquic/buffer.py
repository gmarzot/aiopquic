"""Public re-export of the Cython Buffer.

aiomoqt's serializer/parser imports `from aiopquic.buffer import
Buffer, BufferReadError`. The binding lives in
aiopquic._binding._buffer (Cython); this shim keeps the import path
stable.
"""

from aiopquic._binding._buffer import Buffer, BufferReadError

__all__ = ["Buffer", "BufferReadError"]
