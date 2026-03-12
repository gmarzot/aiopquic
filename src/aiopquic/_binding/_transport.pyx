# cython: language_level=3
# cython: freethreading_compatible = True
"""
_transport — Cython bridge between picoquic (C) and Python asyncio.

Manages the picoquic context lifecycle, SPSC ring buffers,
and the dedicated network thread.
"""

from libc.stdint cimport uint8_t, uint32_t, uint64_t, uintptr_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy

from aiopquic._binding.spsc_ring cimport (
    spsc_ring_t, spsc_ring_create, spsc_ring_destroy,
    spsc_ring_count, spsc_ring_empty, spsc_ring_peek,
    spsc_ring_entry_data, spsc_ring_pop, spsc_ring_push,
    spsc_entry_t,
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
    SPSC_EVT_STREAM_RESET, SPSC_EVT_STOP_SENDING,
    SPSC_EVT_CLOSE, SPSC_EVT_APP_CLOSE,
    SPSC_EVT_READY, SPSC_EVT_ALMOST_READY,
    SPSC_EVT_DATAGRAM,
    SPSC_EVT_TX_STREAM_DATA, SPSC_EVT_TX_STREAM_FIN,
    SPSC_EVT_TX_DATAGRAM, SPSC_EVT_TX_CLOSE,
    SPSC_EVT_TX_MARK_ACTIVE,
)

# C callback declarations
cdef extern from "c/callback.h":
    ctypedef struct aiopquic_ctx_t:
        spsc_ring_t* rx_ring
        spsc_ring_t* tx_ring
        int eventfd
        void* quic  # picoquic_quic_t*

    aiopquic_ctx_t* aiopquic_ctx_create(uint32_t ring_capacity, uint32_t arena_size)
    void aiopquic_ctx_destroy(aiopquic_ctx_t* ctx)
    void aiopquic_clear_rx(aiopquic_ctx_t* ctx)

    int aiopquic_stream_cb(void* cnx, uint64_t stream_id,
                            uint8_t* bytes, size_t length,
                            int fin_or_event,
                            void* callback_ctx, void* stream_ctx)
    int aiopquic_loop_cb(void* quic, int cb_mode,
                          void* callback_ctx, void* callback_argv)


# Default ring sizing
DEF DEFAULT_RING_CAPACITY = 4096
DEF DEFAULT_ARENA_SIZE = 4 * 1024 * 1024  # 4 MB


cdef class RingBuffer:
    """Python wrapper around the SPSC ring buffer for testing/inspection."""
    cdef spsc_ring_t* _ring
    cdef bint _owned

    def __cinit__(self, uint32_t capacity=DEFAULT_RING_CAPACITY,
                  uint32_t arena_size=DEFAULT_ARENA_SIZE):
        self._ring = spsc_ring_create(capacity, arena_size)
        self._owned = True
        if self._ring is NULL:
            raise MemoryError("Failed to create SPSC ring buffer")

    def __dealloc__(self):
        if self._owned and self._ring is not NULL:
            spsc_ring_destroy(self._ring)
            self._ring = NULL

    @property
    def capacity(self):
        return self._ring.capacity

    @property
    def count(self):
        return spsc_ring_count(self._ring)

    @property
    def empty(self):
        return spsc_ring_empty(self._ring) != 0

    def push(self, uint32_t event_type, uint64_t stream_id,
             bytes data=None, uint8_t is_fin=0, uint64_t error_code=0):
        """Push an entry into the ring (producer side)."""
        cdef spsc_entry_t entry
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0

        entry.event_type = event_type
        entry.stream_id = stream_id
        entry.is_fin = is_fin
        entry.cnx = NULL
        entry.stream_ctx = NULL
        entry.error_code = error_code
        entry.data_offset = 0
        entry.data_length = 0

        if data is not None:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        cdef int ret = spsc_ring_push(self._ring, &entry, data_ptr, data_len)
        if ret != 0:
            raise BufferError("Ring buffer is full")

    def pop(self):
        """Pop the next entry (consumer side). Returns (event_type, stream_id, data, error_code) or None."""
        cdef spsc_entry_t* entry = spsc_ring_peek(self._ring)
        if entry is NULL:
            return None

        cdef const uint8_t* data_ptr
        cdef bytes data = None

        if entry.data_length > 0:
            data_ptr = spsc_ring_entry_data(self._ring, entry)
            if data_ptr is not NULL:
                data = data_ptr[:entry.data_length]

        result = (entry.event_type, entry.stream_id, data,
                  entry.is_fin, entry.error_code)
        spsc_ring_pop(self._ring)
        return result


cdef class TransportContext:
    """
    Manages the picoquic context and network thread.

    This is the core bridge between the C picoquic library and Python.
    It owns the SPSC rings and the eventfd used for async notification.
    """
    cdef aiopquic_ctx_t* _ctx
    cdef bint _started

    def __cinit__(self, uint32_t ring_capacity=DEFAULT_RING_CAPACITY,
                  uint32_t arena_size=DEFAULT_ARENA_SIZE):
        self._ctx = aiopquic_ctx_create(ring_capacity, arena_size)
        self._started = False
        if self._ctx is NULL:
            raise MemoryError("Failed to create transport context")

    def __dealloc__(self):
        if self._ctx is not NULL:
            aiopquic_ctx_destroy(self._ctx)
            self._ctx = NULL

    @property
    def eventfd(self):
        """File descriptor for asyncio add_reader() registration."""
        return self._ctx.eventfd

    @property
    def rx_count(self):
        """Number of events pending in the RX ring."""
        return spsc_ring_count(self._ctx.rx_ring)

    def drain_rx(self, int max_events=256):
        """
        Drain events from the RX ring.

        Returns a list of (event_type, stream_id, data_bytes, is_fin, error_code).
        Called from the asyncio thread after eventfd signals.
        """
        aiopquic_clear_rx(self._ctx)

        events = []
        cdef int i
        cdef spsc_entry_t* entry
        cdef const uint8_t* data_ptr
        cdef bytes data

        for i in range(max_events):
            entry = spsc_ring_peek(self._ctx.rx_ring)
            if entry is NULL:
                break

            data = None
            if entry.data_length > 0:
                data_ptr = spsc_ring_entry_data(self._ctx.rx_ring, entry)
                if data_ptr is not NULL:
                    data = data_ptr[:entry.data_length]

            events.append((
                entry.event_type,
                entry.stream_id,
                data,
                entry.is_fin,
                entry.error_code,
                <uintptr_t>entry.cnx,
            ))
            spsc_ring_pop(self._ctx.rx_ring)

        return events

    def push_tx(self, uint32_t event_type, uint64_t stream_id,
                bytes data=None, uint64_t error_code=0,
                uintptr_t cnx_ptr=0):
        """
        Push a command into the TX ring (asyncio → picoquic thread).

        After pushing, caller should call wake_up() to signal the network thread.
        """
        cdef spsc_entry_t entry
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0

        entry.event_type = event_type
        entry.stream_id = stream_id
        entry.is_fin = 0
        entry.cnx = <void*>cnx_ptr
        entry.stream_ctx = NULL
        entry.error_code = error_code
        entry.data_offset = 0
        entry.data_length = 0

        if data is not None:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        cdef int ret = spsc_ring_push(self._ctx.tx_ring, &entry, data_ptr, data_len)
        if ret != 0:
            raise BufferError("TX ring buffer is full")
