# cython: language_level=3

from libc.stdint cimport uint8_t, uint32_t, uint64_t

cdef extern from "c/spsc_ring.h":
    enum:
        SPSC_EVT_STREAM_DATA
        SPSC_EVT_STREAM_FIN
        SPSC_EVT_STREAM_RESET
        SPSC_EVT_STOP_SENDING
        SPSC_EVT_CLOSE
        SPSC_EVT_APP_CLOSE
        SPSC_EVT_READY
        SPSC_EVT_ALMOST_READY
        SPSC_EVT_DATAGRAM
        SPSC_EVT_DATAGRAM_ACKED
        SPSC_EVT_DATAGRAM_LOST
        SPSC_EVT_PATH_AVAILABLE
        SPSC_EVT_PATH_SUSPENDED
        SPSC_EVT_PATH_DELETED
        SPSC_EVT_PACING_CHANGED
        SPSC_EVT_TX_STREAM_DATA
        SPSC_EVT_TX_STREAM_FIN
        SPSC_EVT_TX_DATAGRAM
        SPSC_EVT_TX_CLOSE
        SPSC_EVT_TX_STREAM_RESET
        SPSC_EVT_TX_STOP_SENDING
        SPSC_EVT_TX_MARK_ACTIVE
        SPSC_EVT_TX_CONNECT
        SPSC_EVT_TX_WT_OPEN
        SPSC_EVT_TX_WT_CREATE_STREAM
        SPSC_EVT_TX_WT_CLOSE
        SPSC_EVT_TX_WT_DRAIN
        SPSC_EVT_TX_WT_RESET_STREAM
        SPSC_EVT_TX_WT_DEREGISTER

    ctypedef struct spsc_entry_t:
        uint64_t    stream_id
        uint32_t    event_type
        uint32_t    data_length
        uint8_t     is_fin
        void*       data_buf
        void*       cnx
        void*       stream_ctx
        uint64_t    error_code

    ctypedef struct spsc_ring_t:
        uint32_t    capacity
        uint32_t    mask

    spsc_ring_t* spsc_ring_create(uint32_t capacity)
    void spsc_ring_destroy(spsc_ring_t* ring)
    uint32_t spsc_ring_count(spsc_ring_t* ring)
    int spsc_ring_full(spsc_ring_t* ring)
    int spsc_ring_empty(spsc_ring_t* ring)
    int spsc_ring_push(spsc_ring_t* ring, const spsc_entry_t* entry,
                       const uint8_t* data, uint32_t data_len)
    spsc_entry_t* spsc_ring_peek(spsc_ring_t* ring)
    const uint8_t* spsc_ring_entry_data(spsc_ring_t* ring,
                                         const spsc_entry_t* entry)
    void spsc_ring_pop(spsc_ring_t* ring)
    void* spsc_ring_take_data(spsc_entry_t* entry)
    int spsc_ring_push_event(spsc_ring_t* ring, uint32_t event_type,
                              uint64_t stream_id, void* cnx, uint64_t error_code)
    int spsc_ring_push_stream_data(spsc_ring_t* ring, uint64_t stream_id,
                                    const uint8_t* data, uint32_t length,
                                    int is_fin, void* cnx, void* stream_ctx)
