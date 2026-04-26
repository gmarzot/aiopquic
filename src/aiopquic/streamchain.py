"""Public re-export of the Cython StreamChain.

aiomoqt's parser imports `from aiopquic.streamchain import StreamChain`.
The binding lives in aiopquic._binding._streamchain (Cython); this
shim keeps the import path stable.
"""

from aiopquic._binding._streamchain import StreamChain

__all__ = ["StreamChain"]
