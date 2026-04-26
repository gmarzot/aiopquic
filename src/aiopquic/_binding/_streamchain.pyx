# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: freethreading_compatible=True
"""Cython StreamChain — chained-buffer accumulator with rollback.

Drop-in replacement for the pure-Python StreamChain. Same API
(extend/pull_uint_var/pull_bytes/tell/seek/save/rollback/commit/
data_slice/capacity/__len__) so callers (aiomoqt's MoQT parser)
need no changes.

Storage: deque of buffer-protocol objects (bytes, bytearray, or
memoryview, including memoryview backed by an aiopquic StreamChunk).
Each chunk is held via a C-level Py_buffer view; the deque keeps
the source object alive via Py_INCREF on each PyObject_GetBuffer.

Fast path for pull_uint_var / pull_bytes / pull_uint stays in C:
peek first byte from current chunk's uint8_t* via cached pointer,
advance the cursor, no Python dispatch. Cross-boundary slow path
walks chunks via a memcpy loop into a fresh PyBytes.

Save/rollback/commit semantics:
  save()      — anchor at current pos.
  rollback()  — restore to last save.
  commit()    — drop chunks before cursor; reset tell/save to 0.
"""

from collections import deque

from cpython.bytes cimport PyBytes_FromStringAndSize
from cpython.buffer cimport (
    PyObject_GetBuffer, PyBuffer_Release, Py_buffer,
    PyBUF_SIMPLE,
)
from libc.stdint cimport uint8_t
from libc.string cimport memcpy


cdef class _Chunk:
    """Wraps one buffer-protocol object as a C-accessible byte view.

    On creation, calls PyObject_GetBuffer to pin the source's bytes;
    on deallocation, releases the view. Holding _Chunk in the deque
    keeps the underlying buffer alive across parser yield points.
    """
    cdef Py_buffer buf
    cdef bint has_buf

    def __cinit__(self):
        self.has_buf = False

    @staticmethod
    cdef _Chunk create(object src) except *:
        cdef _Chunk c = _Chunk.__new__(_Chunk)
        if PyObject_GetBuffer(src, &c.buf, PyBUF_SIMPLE) != 0:
            raise BufferError("StreamChain: extend source not "
                              "buffer-protocol compatible")
        c.has_buf = True
        return c

    def __dealloc__(self):
        if self.has_buf:
            PyBuffer_Release(&self.buf)
            self.has_buf = False


cdef class StreamChain:
    """Chained-buffer accumulator with rollback. Cython, no GIL drops."""

    # deque of _Chunk; popleft on commit drops the underlying buffer.
    cdef object _chunks
    # Cached state of the cursor: which chunk + offset within it,
    # plus the cached pointer to that chunk's bytes for the fast path.
    cdef Py_ssize_t _chunk_idx
    cdef Py_ssize_t _chunk_off
    cdef Py_ssize_t _pos
    cdef Py_ssize_t _total
    # Single-level save/rollback anchor.
    cdef Py_ssize_t _save_chunk_idx
    cdef Py_ssize_t _save_chunk_off
    cdef Py_ssize_t _save_pos

    def __cinit__(self):
        self._chunks = deque()
        self._chunk_idx = 0
        self._chunk_off = 0
        self._pos = 0
        self._total = 0
        self._save_chunk_idx = 0
        self._save_chunk_off = 0
        self._save_pos = 0

    # --- ingest --------------------------------------------------

    def extend(self, data):
        """Append a chunk to the end. Accepts bytes, bytearray,
        memoryview, or any other buffer-protocol object."""
        if data is None:
            return
        cdef _Chunk c = _Chunk.create(data)
        if c.buf.len == 0:
            return  # _Chunk dealloc releases the empty view
        self._chunks.append(c)
        self._total += c.buf.len

    def push_bytes(self, data):
        """qh3.Buffer-compatible alias for extend()."""
        self.extend(data)

    # --- query ---------------------------------------------------

    @property
    def capacity(self):
        return self._total

    def __len__(self):
        return self._total - self._pos

    def tell(self):
        return self._pos

    cdef inline _Chunk _chunk_at(self, Py_ssize_t i):
        # deque[i] is O(min(i, n-i)) — fine for small _chunks.
        # Common case: i==_chunk_idx, very small index.
        return <_Chunk>self._chunks[i]

    # --- pull ----------------------------------------------------

    cpdef bytes pull_bytes(self, Py_ssize_t n):
        """Pull n bytes; return a bytes object."""
        if n < 0:
            raise ValueError("StreamChain.pull_bytes negative")
        cdef Py_ssize_t avail = self._total - self._pos
        if n > avail:
            from aiomoqt.messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + n)
        if n == 0:
            return b""

        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef Py_ssize_t chunk_avail = c.buf.len - self._chunk_off
        cdef bytes out
        cdef uint8_t* src
        cdef uint8_t* dst
        cdef Py_ssize_t take, written

        if n <= chunk_avail:
            # Fast path: single chunk
            src = <uint8_t*>c.buf.buf + self._chunk_off
            out = PyBytes_FromStringAndSize(<char*>src, n)
            self._chunk_off += n
            self._pos += n
            if self._chunk_off >= c.buf.len:
                self._chunk_idx += 1
                self._chunk_off = 0
            return out

        # Slow path: cross-boundary, allocate target and walk chunks
        out = PyBytes_FromStringAndSize(NULL, n)
        # Hack: PyBytes is meant to be immutable, but the
        # FromStringAndSize(NULL, n) idiom returns one whose buffer
        # we may write to once before exposing it. CPython documents
        # this pattern in the C API.
        dst = <uint8_t*>(<char*>out)
        written = 0
        while written < n:
            c = self._chunk_at(self._chunk_idx)
            src = <uint8_t*>c.buf.buf + self._chunk_off
            chunk_avail = c.buf.len - self._chunk_off
            take = n - written
            if take > chunk_avail:
                take = chunk_avail
            memcpy(dst + written, src, take)
            written += take
            self._chunk_off += take
            self._pos += take
            if self._chunk_off >= c.buf.len:
                self._chunk_idx += 1
                self._chunk_off = 0
        return out

    cpdef int pull_uint8(self) except? -1:
        cdef Py_ssize_t avail = self._total - self._pos
        if avail < 1:
            from aiomoqt.messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + 1)
        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef int b = (<uint8_t*>c.buf.buf)[self._chunk_off]
        self._chunk_off += 1
        self._pos += 1
        if self._chunk_off >= c.buf.len:
            self._chunk_idx += 1
            self._chunk_off = 0
        return b

    cpdef int pull_uint16(self) except? -1:
        return self._pull_uint_n(2)

    cpdef long pull_uint32(self) except? -1:
        return self._pull_uint_n(4)

    cpdef object pull_uint64(self):
        return self._pull_uint_n_obj(8)

    cdef long _pull_uint_n(self, Py_ssize_t n) except? -1:
        cdef Py_ssize_t avail = self._total - self._pos
        if n > avail:
            from aiomoqt.messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + n)
        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef Py_ssize_t chunk_avail = c.buf.len - self._chunk_off
        cdef uint8_t* p
        cdef long result = 0
        cdef Py_ssize_t i
        if n <= chunk_avail:
            p = <uint8_t*>c.buf.buf + self._chunk_off
            for i in range(n):
                result = (result << 8) | p[i]
            self._chunk_off += n
            self._pos += n
            if self._chunk_off >= c.buf.len:
                self._chunk_idx += 1
                self._chunk_off = 0
            return result
        # Slow path
        cdef bytes raw = self.pull_bytes(n)
        for i in range(n):
            result = (result << 8) | (<uint8_t>raw[i])
        return result

    cdef object _pull_uint_n_obj(self, Py_ssize_t n):
        # 64-bit may exceed C long on 32-bit; route through Python int.
        cdef bytes raw = self.pull_bytes(n)
        return int.from_bytes(raw, "big")

    def pull_uint(self, int n):
        if n == 1:
            return self.pull_uint8()
        if n == 2:
            return self.pull_uint16()
        if n == 4:
            return self.pull_uint32()
        if n == 8:
            return self.pull_uint64()
        cdef bytes raw = self.pull_bytes(n)
        return int.from_bytes(raw, "big")

    cpdef object pull_uint_var(self):
        """QUIC variable-length integer (RFC 9000 §16)."""
        cdef Py_ssize_t avail = self._total - self._pos
        if avail < 1:
            from aiomoqt.messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + 1)
        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef uint8_t first = (<uint8_t*>c.buf.buf)[self._chunk_off]
        cdef int prefix = first >> 6
        cdef Py_ssize_t nbytes = 1 << prefix  # 1, 2, 4, or 8
        if nbytes > avail:
            from aiomoqt.messages.base import MOQTUnderflow
            raise MOQTUnderflow(self._pos, self._pos + nbytes)

        cdef Py_ssize_t chunk_avail = c.buf.len - self._chunk_off
        cdef uint8_t* p
        cdef Py_ssize_t i
        cdef object result_obj  # for 8-byte path

        if nbytes == 1:
            self._chunk_off += 1
            self._pos += 1
            if self._chunk_off >= c.buf.len:
                self._chunk_idx += 1
                self._chunk_off = 0
            return first & 0x3F

        if nbytes <= chunk_avail:
            # Fast path: all bytes in one chunk
            p = <uint8_t*>c.buf.buf + self._chunk_off
            if nbytes == 2:
                result = (((<long>p[0]) & 0x3F) << 8) | p[1]
            elif nbytes == 4:
                result = (
                    (((<long>p[0]) & 0x3F) << 24)
                    | ((<long>p[1]) << 16)
                    | ((<long>p[2]) << 8)
                    |  (<long>p[3])
                )
            else:  # 8
                result_obj = (
                    (((<long>p[0]) & 0x3F) << 56)
                    | ((<long>p[1]) << 48)
                    | ((<long>p[2]) << 40)
                    | ((<long>p[3]) << 32)
                    | ((<long>p[4]) << 24)
                    | ((<long>p[5]) << 16)
                    | ((<long>p[6]) << 8)
                    |  (<long>p[7])
                )
                self._chunk_off += 8
                self._pos += 8
                if self._chunk_off >= c.buf.len:
                    self._chunk_idx += 1
                    self._chunk_off = 0
                return result_obj
            self._chunk_off += nbytes
            self._pos += nbytes
            if self._chunk_off >= c.buf.len:
                self._chunk_idx += 1
                self._chunk_off = 0
            return result

        # Slow path: cross-boundary
        cdef bytes raw = self.pull_bytes(nbytes)
        cdef long acc = (<uint8_t>raw[0]) & 0x3F
        for i in range(1, nbytes):
            acc = (acc << 8) | (<uint8_t>raw[i])
        return acc

    # --- save / rollback / commit ---------------------------------

    def save(self):
        self._save_chunk_idx = self._chunk_idx
        self._save_chunk_off = self._chunk_off
        self._save_pos = self._pos

    def rollback(self):
        self._chunk_idx = self._save_chunk_idx
        self._chunk_off = self._save_chunk_off
        self._pos = self._save_pos

    def commit(self):
        """Drop fully-consumed chunks; reset tell to 0; reset save."""
        cdef _Chunk c
        cdef object src_obj
        cdef object new_view
        cdef Py_ssize_t tail_len

        # Pop chunks fully behind the cursor — popleft is O(1) on deque.
        while self._chunk_idx > 0:
            old = self._chunks.popleft()  # _Chunk dealloc releases buf
            self._total -= (<_Chunk>old).buf.len
            self._pos -= (<_Chunk>old).buf.len
            self._chunk_idx -= 1
        # Trim the leading offset of the remaining first chunk, if any.
        if self._chunk_off > 0 and len(self._chunks) > 0:
            c = self._chunk_at(0)
            # Carve a memoryview over the still-live tail of the buffer
            # and reseat the chunk on the new shorter view. The
            # original chunk's _Chunk dealloc releases its full buffer
            # once superseded.
            src_obj = <object>c.buf.obj
            tail_len = c.buf.len - self._chunk_off
            new_view = memoryview(src_obj)[
                self._chunk_off:self._chunk_off + tail_len
            ]
            self._chunks.popleft()
            self._chunks.appendleft(_Chunk.create(new_view))
            self._total -= self._chunk_off
            self._pos -= self._chunk_off
            self._chunk_off = 0
        # _pos is now the cursor relative to the new chunks[0] start; if
        # we got here cleanly that's 0.
        self._save_chunk_idx = 0
        self._save_chunk_off = 0
        self._save_pos = 0

    # --- data_slice -----------------------------------------------

    def data_slice(self, Py_ssize_t start, Py_ssize_t end):
        """Return bytes[start:end). Used by logging/error paths."""
        if start < 0 or end > self._total or start > end:
            raise ValueError(
                f"StreamChain.data_slice [{start}, {end}) "
                f"in [0, {self._total})"
            )
        if start == end:
            return b""

        # Walk chunks to the byte at 'start', then memcpy out to 'end'
        cdef Py_ssize_t n = end - start
        cdef bytes out = PyBytes_FromStringAndSize(NULL, n)
        cdef uint8_t* dst = <uint8_t*>(<char*>out)
        cdef Py_ssize_t running = 0
        cdef Py_ssize_t idx = 0
        cdef Py_ssize_t off = 0
        cdef Py_ssize_t i
        cdef Py_ssize_t take, avail
        cdef Py_ssize_t written = 0
        cdef _Chunk c
        cdef Py_ssize_t total_chunks = len(self._chunks)
        for i in range(total_chunks):
            c = self._chunk_at(i)
            if running + c.buf.len > start:
                idx = i
                off = start - running
                break
            running += c.buf.len
        while written < n:
            c = self._chunk_at(idx)
            avail = c.buf.len - off
            take = n - written
            if take > avail:
                take = avail
            memcpy(dst + written, <uint8_t*>c.buf.buf + off, take)
            written += take
            off = 0
            idx += 1
        return out

    # --- seek (kept for compatibility with re_buf reset path) -----

    def seek(self, Py_ssize_t pos):
        if pos < 0 or pos > self._total:
            raise ValueError(
                f"StreamChain.seek out of range: {pos} not in "
                f"[0, {self._total}]"
            )
        cdef Py_ssize_t running = 0
        cdef Py_ssize_t i
        cdef Py_ssize_t total_chunks = len(self._chunks)
        cdef _Chunk c
        for i in range(total_chunks):
            c = self._chunk_at(i)
            if running + c.buf.len > pos:
                self._chunk_idx = i
                self._chunk_off = pos - running
                self._pos = pos
                return
            running += c.buf.len
        self._chunk_idx = total_chunks
        self._chunk_off = 0
        self._pos = pos
