# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: freethreading_compatible=True
"""Cython Buffer — single contiguous byte buffer with QUIC varint codecs.

Drop-in replacement for qh3.Buffer (Rust). Same API surface
(push_uint8/16/32/64/var, push_bytes, pull_uint8/16/32/64/var,
pull_bytes, tell, seek, capacity, data, data_slice) so callers
(aiomoqt's MoQT parser/serializer) need no changes.

Single cursor: both push and pull advance the same _pos. Capacity is
fixed for read-mode (Buffer(data=)) and grows on demand for write-mode
(Buffer(capacity=)).
"""

from cpython.bytes cimport PyBytes_FromStringAndSize
from libc.stdint cimport uint8_t, uint64_t
from libc.stdlib cimport malloc, realloc, free
from libc.string cimport memcpy


class BufferReadError(Exception):
    """Raised when an attempt is made to read past the end of a buffer."""


cdef class Buffer:
    """Single-cursor byte buffer with QUIC varint codecs.

    Wraps a heap-allocated uint8_t* so push/pull primitives operate
    on raw pointers — no Python attribute traffic, no GIL boundary
    crossing on the hot path.
    """

    cdef uint8_t* _buf
    cdef Py_ssize_t _capacity
    cdef Py_ssize_t _pos
    cdef bint _growable
    cdef bint _own_buf

    def __cinit__(self, capacity=None, data=None):
        cdef const uint8_t* src
        cdef Py_ssize_t n
        cdef bytes b
        self._buf = NULL
        self._pos = 0
        self._growable = False
        self._own_buf = False
        if data is not None:
            b = bytes(data)
            n = len(b)
            self._buf = <uint8_t*>malloc(n if n > 0 else 1)
            if self._buf is NULL:
                raise MemoryError()
            self._own_buf = True
            self._capacity = n
            if n > 0:
                src = <const uint8_t*>b
                memcpy(self._buf, src, n)
        else:
            n = capacity if capacity is not None else 1024
            if n <= 0:
                n = 1024
            self._buf = <uint8_t*>malloc(n)
            if self._buf is NULL:
                raise MemoryError()
            self._own_buf = True
            self._capacity = n
            self._growable = True

    def __dealloc__(self):
        if self._own_buf and self._buf is not NULL:
            free(self._buf)
            self._buf = NULL

    # --- read-only state ----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def data(self):
        return PyBytes_FromStringAndSize(<const char*>self._buf, self._pos)

    cpdef Py_ssize_t tell(self):
        return self._pos

    def seek(self, Py_ssize_t pos):
        if pos < 0 or pos > self._capacity:
            raise BufferReadError(f"seek out of bounds: {pos}")
        self._pos = pos

    def data_slice(self, Py_ssize_t start, Py_ssize_t end):
        if start < 0 or end > self._capacity or start > end:
            raise BufferReadError(
                f"data_slice out of bounds: [{start}, {end})")
        return PyBytes_FromStringAndSize(
            <const char*>(self._buf + start), end - start)

    cpdef bint eof(self):
        return self._pos >= self._capacity

    # --- internal grow ------------------------------------------------

    cdef int _grow(self, Py_ssize_t need) except -1:
        cdef Py_ssize_t new_cap
        cdef uint8_t* new_buf
        if not self._growable:
            raise BufferReadError("write past fixed capacity")
        new_cap = self._capacity * 2
        if new_cap < need:
            new_cap = need
        new_buf = <uint8_t*>realloc(self._buf, new_cap)
        if new_buf is NULL:
            raise MemoryError()
        self._buf = new_buf
        self._capacity = new_cap
        return 0

    # --- pull primitives ----------------------------------------------

    cpdef int pull_uint8(self) except? -1:
        if self._pos + 1 > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef int v = self._buf[self._pos]
        self._pos += 1
        return v

    cpdef int pull_uint16(self) except? -1:
        if self._pos + 2 > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef uint8_t* p = self._buf + self._pos
        cdef int v = (<int>p[0] << 8) | <int>p[1]
        self._pos += 2
        return v

    cpdef long pull_uint32(self) except? -1:
        if self._pos + 4 > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef uint8_t* p = self._buf + self._pos
        cdef long v = ((<long>p[0]) << 24) | ((<long>p[1]) << 16) | \
                      ((<long>p[2]) << 8) | <long>p[3]
        self._pos += 4
        return v

    cpdef object pull_uint64(self):
        if self._pos + 8 > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef bytes raw = PyBytes_FromStringAndSize(
            <const char*>(self._buf + self._pos), 8)
        self._pos += 8
        return int.from_bytes(raw, "big")

    def pull_uint(self, int n):
        if n == 1: return self.pull_uint8()
        if n == 2: return self.pull_uint16()
        if n == 4: return self.pull_uint32()
        if n == 8: return self.pull_uint64()
        if self._pos + n > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef bytes raw = PyBytes_FromStringAndSize(
            <const char*>(self._buf + self._pos), n)
        self._pos += n
        return int.from_bytes(raw, "big")

    cpdef object pull_uint_var(self):
        if self._pos + 1 > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef uint8_t first = self._buf[self._pos]
        cdef int n = 1 << (first >> 6)
        if self._pos + n > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef uint8_t* p = self._buf + self._pos
        cdef object result_obj
        cdef long result
        if n == 1:
            self._pos += 1
            return first & 0x3F
        if n == 2:
            result = (((<long>p[0]) & 0x3F) << 8) | <long>p[1]
            self._pos += 2
            return result
        if n == 4:
            result = (
                (((<long>p[0]) & 0x3F) << 24)
                | ((<long>p[1]) << 16)
                | ((<long>p[2]) << 8)
                | (<long>p[3])
            )
            self._pos += 4
            return result
        # n == 8: route through Python int to handle 32-bit longs
        cdef bytes raw = PyBytes_FromStringAndSize(<const char*>p, 8)
        self._pos += 8
        result_obj = int.from_bytes(raw, "big") & ((1 << 62) - 1)
        return result_obj

    def pull_bytes(self, Py_ssize_t n):
        if n < 0:
            raise BufferReadError("negative read length")
        if self._pos + n > self._capacity:
            raise BufferReadError("read out of bounds")
        cdef bytes out = PyBytes_FromStringAndSize(
            <const char*>(self._buf + self._pos), n)
        self._pos += n
        return out

    # --- push primitives ----------------------------------------------

    cdef inline int _check_push(self, Py_ssize_t need) except -1:
        if not self._growable:
            raise BufferReadError("write to fixed-capacity buffer")
        if self._pos + need > self._capacity:
            self._grow(self._pos + need)
        return 0

    cpdef int push_uint8(self, int v) except -1:
        self._check_push(1)
        self._buf[self._pos] = <uint8_t>(v & 0xFF)
        self._pos += 1
        return 0

    cpdef int push_uint16(self, int v) except -1:
        self._check_push(2)
        cdef uint8_t* p = self._buf + self._pos
        p[0] = <uint8_t>((v >> 8) & 0xFF)
        p[1] = <uint8_t>(v & 0xFF)
        self._pos += 2
        return 0

    cpdef int push_uint32(self, long v) except -1:
        self._check_push(4)
        cdef uint8_t* p = self._buf + self._pos
        p[0] = <uint8_t>((v >> 24) & 0xFF)
        p[1] = <uint8_t>((v >> 16) & 0xFF)
        p[2] = <uint8_t>((v >> 8) & 0xFF)
        p[3] = <uint8_t>(v & 0xFF)
        self._pos += 4
        return 0

    def push_uint64(self, v):
        self._check_push(8)
        cdef bytes raw = int(v).to_bytes(8, "big")
        memcpy(self._buf + self._pos, <const char*>raw, 8)
        self._pos += 8
        return 0

    def push_uint(self, int n, v):
        if n == 1: return self.push_uint8(<int>v)
        if n == 2: return self.push_uint16(<int>v)
        if n == 4: return self.push_uint32(<long>v)
        if n == 8: return self.push_uint64(v)
        self._check_push(n)
        cdef bytes raw = int(v).to_bytes(n, "big")
        memcpy(self._buf + self._pos, <const char*>raw, n)
        self._pos += n
        return 0

    cpdef int push_uint_var(self, object v_obj) except -1:
        cdef uint64_t v = <uint64_t>v_obj
        cdef int n
        cdef uint8_t prefix
        if v < (<uint64_t>1 << 6):
            n = 1
            prefix = 0
        elif v < (<uint64_t>1 << 14):
            n = 2
            prefix = 0x40
        elif v < (<uint64_t>1 << 30):
            n = 4
            prefix = 0x80
        elif v < (<uint64_t>1 << 62):
            n = 8
            prefix = 0xC0
        else:
            raise ValueError(f"varint value too large: {v_obj}")
        self._check_push(n)
        cdef uint8_t* p = self._buf + self._pos
        if n == 1:
            p[0] = <uint8_t>(v & 0x3F) | prefix
        elif n == 2:
            p[0] = <uint8_t>((v >> 8) & 0x3F) | prefix
            p[1] = <uint8_t>(v & 0xFF)
        elif n == 4:
            p[0] = <uint8_t>((v >> 24) & 0x3F) | prefix
            p[1] = <uint8_t>((v >> 16) & 0xFF)
            p[2] = <uint8_t>((v >> 8) & 0xFF)
            p[3] = <uint8_t>(v & 0xFF)
        else:  # 8
            p[0] = <uint8_t>((v >> 56) & 0x3F) | prefix
            p[1] = <uint8_t>((v >> 48) & 0xFF)
            p[2] = <uint8_t>((v >> 40) & 0xFF)
            p[3] = <uint8_t>((v >> 32) & 0xFF)
            p[4] = <uint8_t>((v >> 24) & 0xFF)
            p[5] = <uint8_t>((v >> 16) & 0xFF)
            p[6] = <uint8_t>((v >> 8) & 0xFF)
            p[7] = <uint8_t>(v & 0xFF)
        self._pos += n
        return 0

    cpdef int push_bytes(self, object data) except -1:
        cdef Py_ssize_t n
        cdef const uint8_t* src
        cdef bytes b
        if isinstance(data, (bytes, bytearray)):
            n = len(data)
            if n == 0:
                return 0
            if self._pos + n > self._capacity:
                self._grow(self._pos + n)
            b = bytes(data) if isinstance(data, bytearray) else data
            src = <const uint8_t*>(<bytes>b)
            memcpy(self._buf + self._pos, src, n)
        else:
            b = bytes(data)
            n = len(b)
            if n == 0:
                return 0
            if self._pos + n > self._capacity:
                self._grow(self._pos + n)
            src = <const uint8_t*>b
            memcpy(self._buf + self._pos, src, n)
        self._pos += n
        return 0
