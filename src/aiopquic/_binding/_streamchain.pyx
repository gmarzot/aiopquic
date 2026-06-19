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

from cpython.bytes cimport (
    PyBytes_AsString, PyBytes_FromStringAndSize,
)
from cpython.buffer cimport (
    PyObject_GetBuffer, PyBuffer_Release, Py_buffer,
    PyBUF_SIMPLE,
)
from libc.stdint cimport uint8_t, uint64_t
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
    cdef bint _vi64

    def __cinit__(self, vi64=False):
        self._chunks = deque()
        self._chunk_idx = 0
        self._chunk_off = 0
        self._pos = 0
        self._total = 0
        self._save_chunk_idx = 0
        self._save_chunk_off = 0
        self._save_pos = 0
        self._vi64 = bool(vi64)

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

    @property
    def vi64(self) -> bool:
        """Variable-length-integer flavor for pull_vint: True = draft-18
        vi64, False = RFC9000 varint. Settable so a freshly-created chain
        can be tagged for the session's draft before the first read."""
        return self._vi64

    @vi64.setter
    def vi64(self, bint value):
        self._vi64 = value

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
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + n)
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
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + 1)
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
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + n)
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
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + 1)
        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef uint8_t first = (<uint8_t*>c.buf.buf)[self._chunk_off]
        cdef int prefix = first >> 6
        cdef Py_ssize_t nbytes = 1 << prefix  # 1, 2, 4, or 8
        if nbytes > avail:
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + nbytes)

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

    cpdef object pull_uint_vi64(self):
        """draft-18 vi64 varint (§1.4.1): leading-1-bits of the first byte
        give the length (1-9 bytes); bits after the first 0 plus subsequent
        bytes are the value, big-endian. Non-minimal encodings accepted."""
        cdef Py_ssize_t avail = self._total - self._pos
        if avail < 1:
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + 1)
        cdef _Chunk c = self._chunk_at(self._chunk_idx)
        cdef uint8_t first = (<uint8_t*>c.buf.buf)[self._chunk_off]
        cdef int k = 0
        while k < 8 and (first & (0x80 >> k)):
            k += 1
        cdef Py_ssize_t nbytes = k + 1
        if nbytes > avail:
            from aiopquic.exceptions import StreamUnderflow
            raise StreamUnderflow(self._pos, self._pos + nbytes)
        # pull_bytes consumes nbytes (incl. the peeked first) across chunks.
        cdef bytes raw = self.pull_bytes(nbytes)
        cdef uint64_t v = (<uint8_t>raw[0]) & <uint8_t>(0xFF >> (k + 1))
        cdef Py_ssize_t i
        for i in range(1, nbytes):
            v = (v << 8) | (<uint8_t>raw[i])
        return v

    cpdef object pull_vint(self):
        """Pull a variable-length integer in this chain's flavor (vi64 if
        self._vi64 else RFC9000 varint). One C branch — no codec lookup
        above."""
        if self._vi64:
            return self.pull_uint_vi64()
        return self.pull_uint_var()

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

    # --- subgroup-stream object body parse ------------------------

    cpdef object parse_object_subgroup(self,
                                       bint extensions_present,
                                       Py_ssize_t exts_len_limit):
        """Parse one subgroup-stream object body in Cython.

        Body format (common to d14/d16/d18+):
            delta varint
            extensions block (only if extensions_present):
                ext_block_len varint
                <ext_id varint, value>* where
                    ext_id even -> value is uint_var
                    ext_id odd  -> value is len-prefixed bytes
            payload_len varint
            if payload_len == 0: status varint
            else: payload bytes

        Returns tuple (object_id_delta, exts_or_None, status, payload).
        Underflow propagates StreamUnderflow; caller's chain.save() /
        rollback() at the outer level resets the cursor. Framer desync
        (ext_len > exts_len_limit) raises RuntimeError.
        """
        cdef object delta = self.pull_uint_var()
        cdef object exts = None
        cdef object ext_id
        cdef object ext_value
        cdef object exts_len_obj
        cdef Py_ssize_t exts_len
        cdef Py_ssize_t exts_end
        cdef object value_len_obj
        cdef Py_ssize_t value_len
        cdef object payload_len_obj
        cdef Py_ssize_t payload_len
        cdef object status
        cdef object payload

        if extensions_present:
            exts_len_obj = self.pull_uint_var()
            exts_len = <Py_ssize_t>exts_len_obj
            if exts_len > exts_len_limit:
                raise RuntimeError(
                    f"framer desync: ext_len={exts_len} exceeds "
                    f"limit {exts_len_limit}"
                )
            if exts_len > 0:
                exts_end = self._pos + exts_len
                exts = {}
                while self._pos < exts_end:
                    ext_id = self.pull_uint_var()
                    if not (ext_id & 1):
                        ext_value = self.pull_uint_var()
                    else:
                        value_len_obj = self.pull_uint_var()
                        value_len = <Py_ssize_t>value_len_obj
                        ext_value = self.pull_bytes(value_len)
                    exts[ext_id] = ext_value
                if not exts:
                    exts = None

        payload_len_obj = self.pull_uint_var()
        payload_len = <Py_ssize_t>payload_len_obj
        if payload_len == 0:
            status = self.pull_uint_var()
            payload = b""
        else:
            payload = self.pull_bytes(payload_len)
            status = 0  # ObjectStatus.NORMAL — caller maps int -> enum
        return (delta, exts, status, payload)

    cpdef object parse_object_subgroup_vi64(self,
                                            bint extensions_present,
                                            Py_ssize_t exts_len_limit):
        """draft-18 twin of parse_object_subgroup: identical body shape,
        vi64 codec (§1.4.1) instead of the RFC 9000 varint. Kept separate
        so the d14/d16 hot path is untouched. Returns
        (object_id_delta, exts_or_None, status, payload)."""
        cdef object delta = self.pull_uint_vi64()
        cdef object exts = None
        cdef object ext_id
        cdef object ext_value
        cdef object exts_len_obj
        cdef Py_ssize_t exts_len
        cdef Py_ssize_t exts_end
        cdef object value_len_obj
        cdef Py_ssize_t value_len
        cdef object payload_len_obj
        cdef Py_ssize_t payload_len
        cdef object status
        cdef object payload

        if extensions_present:
            exts_len_obj = self.pull_uint_vi64()
            exts_len = <Py_ssize_t>exts_len_obj
            if exts_len > exts_len_limit:
                raise RuntimeError(
                    f"framer desync: ext_len={exts_len} exceeds "
                    f"limit {exts_len_limit}"
                )
            if exts_len > 0:
                exts_end = self._pos + exts_len
                exts = {}
                while self._pos < exts_end:
                    ext_id = self.pull_uint_vi64()
                    if not (ext_id & 1):
                        ext_value = self.pull_uint_vi64()
                    else:
                        value_len_obj = self.pull_uint_vi64()
                        value_len = <Py_ssize_t>value_len_obj
                        ext_value = self.pull_bytes(value_len)
                    exts[ext_id] = ext_value
                if not exts:
                    exts = None

        payload_len_obj = self.pull_uint_vi64()
        payload_len = <Py_ssize_t>payload_len_obj
        if payload_len == 0:
            status = self.pull_uint_vi64()
            payload = b""
        else:
            payload = self.pull_bytes(payload_len)
            status = 0  # ObjectStatus.NORMAL — caller maps int -> enum
        return (delta, exts, status, payload)

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


# --- subgroup-stream object body encode ---------------------------
#
# Symmetric counterpart to StreamChain.parse_object_subgroup. Builds the
# wire bytes for one object body in a single Cython call — eliminates
# the per-object ObjectHeader + Buffer allocations and the Python-frame
# cost of _extensions_encode + serialize on the publisher hot path.

cdef inline Py_ssize_t _varint_size(uint64_t v):
    if v < 64:
        return 1
    elif v < 16384:
        return 2
    elif v < 1073741824:
        return 4
    else:
        return 8


cdef inline Py_ssize_t _varint_write(uint8_t* dst, uint64_t v):
    if v < 64:
        dst[0] = <uint8_t>v
        return 1
    elif v < 16384:
        dst[0] = <uint8_t>((v >> 8) | 0x40)
        dst[1] = <uint8_t>(v & 0xff)
        return 2
    elif v < 1073741824:
        dst[0] = <uint8_t>((v >> 24) | 0x80)
        dst[1] = <uint8_t>((v >> 16) & 0xff)
        dst[2] = <uint8_t>((v >> 8) & 0xff)
        dst[3] = <uint8_t>(v & 0xff)
        return 4
    else:
        dst[0] = <uint8_t>((v >> 56) | 0xc0)
        dst[1] = <uint8_t>((v >> 48) & 0xff)
        dst[2] = <uint8_t>((v >> 40) & 0xff)
        dst[3] = <uint8_t>((v >> 32) & 0xff)
        dst[4] = <uint8_t>((v >> 24) & 0xff)
        dst[5] = <uint8_t>((v >> 16) & 0xff)
        dst[6] = <uint8_t>((v >> 8) & 0xff)
        dst[7] = <uint8_t>(v & 0xff)
        return 8


cdef inline Py_ssize_t _vi64_size(uint64_t v):
    if v < (<uint64_t>1 << 7): return 1
    elif v < (<uint64_t>1 << 14): return 2
    elif v < (<uint64_t>1 << 21): return 3
    elif v < (<uint64_t>1 << 28): return 4
    elif v < (<uint64_t>1 << 35): return 5
    elif v < (<uint64_t>1 << 42): return 6
    elif v < (<uint64_t>1 << 49): return 7
    elif v < (<uint64_t>1 << 56): return 8
    else: return 9


cdef inline Py_ssize_t _vi64_write(uint8_t* dst, uint64_t v):
    """draft-18 vi64 (§1.4.1), minimal length. Mirrors Buffer.push_uint_vi64."""
    cdef Py_ssize_t n = _vi64_size(v)
    cdef Py_ssize_t i
    if n == 9:
        dst[0] = 0xFF
        for i in range(1, 9):
            dst[i] = <uint8_t>((v >> (8 * (8 - i))) & 0xff)
    else:
        dst[0] = <uint8_t>((~(0xFF >> (n - 1)) & 0xFF) | (v >> (8 * (n - 1))))
        for i in range(1, n):
            dst[i] = <uint8_t>((v >> (8 * (n - 1 - i))) & 0xff)
    return n


cpdef bytes encode_object_subgroup(
    object delta, object exts, int status,
    bytes payload, bint extensions_present):
    """Build one subgroup-stream object body in Cython.

    Wire format (common to d14/d16/d18+):
        delta varint
        if extensions_present: ext_block_len varint + <ext_id, value>*
            ext_id even -> value varint
            ext_id odd  -> len-prefixed bytes
        if status == 0 (NORMAL) and payload non-empty:
            payload_len varint + payload bytes
        else:
            payload_len=0 varint + status varint
        Per spec: extensions are written only on NORMAL-with-payload
        objects; non-NORMAL/empty objects carry an empty ext block.

    Args:
        delta: object_id - prev_object_id - 1 (or object_id for first obj)
        exts: dict[int, int|bytes] or None
        status: int (0 = NORMAL; caller maps ObjectStatus -> int)
        payload: bytes
        extensions_present: bool

    Returns: bytes ready to push on the stream (concatenated wire form).
    """
    cdef uint64_t delta_v = <uint64_t>delta
    cdef Py_ssize_t payload_len = <Py_ssize_t>len(payload)
    cdef Py_ssize_t total_size = 0
    cdef Py_ssize_t ext_block_size = 0
    cdef Py_ssize_t value_len
    cdef bint write_exts
    cdef object ext_id
    cdef object ext_value
    cdef uint64_t ext_id_v
    cdef uint64_t v_v

    write_exts = extensions_present and status == 0 and exts is not None

    total_size += _varint_size(delta_v)
    if extensions_present:
        if write_exts:
            for ext_id, ext_value in exts.items():
                ext_id_v = <uint64_t>ext_id
                ext_block_size += _varint_size(ext_id_v)
                if not (ext_id_v & 1):
                    ext_block_size += _varint_size(<uint64_t>ext_value)
                else:
                    value_len = <Py_ssize_t>len(ext_value)
                    ext_block_size += (
                        _varint_size(<uint64_t>value_len) + value_len)
        total_size += _varint_size(<uint64_t>ext_block_size) + ext_block_size

    if status == 0 and payload_len > 0:
        total_size += _varint_size(<uint64_t>payload_len) + payload_len
    else:
        total_size += _varint_size(0) + _varint_size(<uint64_t>status)

    cdef bytes out = PyBytes_FromStringAndSize(NULL, total_size)
    cdef uint8_t* p = <uint8_t*>PyBytes_AsString(out)
    cdef Py_ssize_t off = 0

    off += _varint_write(p + off, delta_v)
    if extensions_present:
        off += _varint_write(p + off, <uint64_t>ext_block_size)
        if write_exts:
            for ext_id, ext_value in exts.items():
                ext_id_v = <uint64_t>ext_id
                off += _varint_write(p + off, ext_id_v)
                if not (ext_id_v & 1):
                    off += _varint_write(p + off, <uint64_t>ext_value)
                else:
                    value_len = <Py_ssize_t>len(ext_value)
                    off += _varint_write(p + off, <uint64_t>value_len)
                    memcpy(p + off,
                           <const char*>PyBytes_AsString(ext_value),
                           value_len)
                    off += value_len

    if status == 0 and payload_len > 0:
        off += _varint_write(p + off, <uint64_t>payload_len)
        memcpy(p + off, <const char*>PyBytes_AsString(payload), payload_len)
        off += payload_len
    else:
        off += _varint_write(p + off, 0)
        off += _varint_write(p + off, <uint64_t>status)

    return out


cpdef bytes encode_object_subgroup_vi64(
    object delta, object exts, int status,
    bytes payload, bint extensions_present):
    """draft-18 twin of encode_object_subgroup: identical body shape, vi64
    codec (§1.4.1). Kept separate so the d14/d16 hot path is untouched."""
    cdef uint64_t delta_v = <uint64_t>delta
    cdef Py_ssize_t payload_len = <Py_ssize_t>len(payload)
    cdef Py_ssize_t total_size = 0
    cdef Py_ssize_t ext_block_size = 0
    cdef Py_ssize_t value_len
    cdef bint write_exts
    cdef object ext_id
    cdef object ext_value
    cdef uint64_t ext_id_v

    write_exts = extensions_present and status == 0 and exts is not None

    total_size += _vi64_size(delta_v)
    if extensions_present:
        if write_exts:
            for ext_id, ext_value in exts.items():
                ext_id_v = <uint64_t>ext_id
                ext_block_size += _vi64_size(ext_id_v)
                if not (ext_id_v & 1):
                    ext_block_size += _vi64_size(<uint64_t>ext_value)
                else:
                    value_len = <Py_ssize_t>len(ext_value)
                    ext_block_size += (
                        _vi64_size(<uint64_t>value_len) + value_len)
        total_size += _vi64_size(<uint64_t>ext_block_size) + ext_block_size

    if status == 0 and payload_len > 0:
        total_size += _vi64_size(<uint64_t>payload_len) + payload_len
    else:
        total_size += _vi64_size(0) + _vi64_size(<uint64_t>status)

    cdef bytes out = PyBytes_FromStringAndSize(NULL, total_size)
    cdef uint8_t* p = <uint8_t*>PyBytes_AsString(out)
    cdef Py_ssize_t off = 0

    off += _vi64_write(p + off, delta_v)
    if extensions_present:
        off += _vi64_write(p + off, <uint64_t>ext_block_size)
        if write_exts:
            for ext_id, ext_value in exts.items():
                ext_id_v = <uint64_t>ext_id
                off += _vi64_write(p + off, ext_id_v)
                if not (ext_id_v & 1):
                    off += _vi64_write(p + off, <uint64_t>ext_value)
                else:
                    value_len = <Py_ssize_t>len(ext_value)
                    off += _vi64_write(p + off, <uint64_t>value_len)
                    memcpy(p + off,
                           <const char*>PyBytes_AsString(ext_value),
                           value_len)
                    off += value_len

    if status == 0 and payload_len > 0:
        off += _vi64_write(p + off, <uint64_t>payload_len)
        memcpy(p + off, <const char*>PyBytes_AsString(payload), payload_len)
        off += payload_len
    else:
        off += _vi64_write(p + off, 0)
        off += _vi64_write(p + off, <uint64_t>status)

    return out
