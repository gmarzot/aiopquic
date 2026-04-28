/*
 * spsc_ring.h — Lock-free Single Producer Single Consumer ring buffer.
 *
 * Uses C11 atomics with acquire/release ordering.
 * Cache-line aligned head/tail to avoid false sharing.
 *
 * Each entry carries an owned data buffer (data_buf) allocated by the
 * producer. Ownership transfers to the consumer at pop time; consumer
 * is responsible for freeing data_buf (or transferring ownership
 * elsewhere, as drain_rx does to StreamChunk).
 *
 * spsc_ring_destroy walks any unread entries and frees their data_buf
 * to avoid leaks on shutdown.
 *
 * Copyright (c) 2026, aiopquic contributors. BSD-3-Clause license.
 */

#ifndef AIOPQUIC_SPSC_RING_H
#define AIOPQUIC_SPSC_RING_H

#include <stdint.h>
#include <stddef.h>
#include <stdatomic.h>
#include <string.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SPSC_RING_DEFAULT_CAPACITY  4096    /* entries, must be power of 2 */
#define SPSC_CACHELINE              64

typedef enum {
    SPSC_EVT_STREAM_DATA = 0,
    SPSC_EVT_STREAM_FIN = 1,
    SPSC_EVT_STREAM_RESET = 2,
    SPSC_EVT_STOP_SENDING = 3,
    SPSC_EVT_CLOSE = 4,
    SPSC_EVT_APP_CLOSE = 5,
    SPSC_EVT_READY = 6,
    SPSC_EVT_ALMOST_READY = 7,
    SPSC_EVT_DATAGRAM = 8,
    SPSC_EVT_DATAGRAM_ACKED = 9,
    SPSC_EVT_DATAGRAM_LOST = 10,
    SPSC_EVT_PATH_AVAILABLE = 11,
    SPSC_EVT_PATH_SUSPENDED = 12,
    SPSC_EVT_PATH_DELETED = 13,
    SPSC_EVT_PACING_CHANGED = 14,

    SPSC_EVT_TX_STREAM_DATA = 128,
    SPSC_EVT_TX_STREAM_FIN = 129,
    SPSC_EVT_TX_DATAGRAM = 130,
    SPSC_EVT_TX_CLOSE = 131,
    SPSC_EVT_TX_STREAM_RESET = 132,
    SPSC_EVT_TX_STOP_SENDING = 133,
    SPSC_EVT_TX_MARK_ACTIVE = 134,
    SPSC_EVT_TX_CONNECT = 135,

    /* WebTransport-side TX commands (asyncio → picoquic). cnx field
     * carries aiopquic_wt_session_t* for these; the loop callback
     * downcasts and invokes the appropriate picowt_* function. */
    SPSC_EVT_TX_WT_OPEN = 136,          /* picowt_prepare_client_cnx + picowt_connect */
    SPSC_EVT_TX_WT_CREATE_STREAM = 137, /* picowt_create_local_stream(bidir flag in is_fin) */
    SPSC_EVT_TX_WT_CLOSE = 138,         /* picowt_send_close_session_message */
    SPSC_EVT_TX_WT_DRAIN = 139,         /* picowt_send_drain_session_message */
    SPSC_EVT_TX_WT_RESET_STREAM = 140,  /* picowt_reset_stream */
    SPSC_EVT_TX_WT_DEREGISTER = 141,    /* picowt_deregister + free wt_session */
    SPSC_EVT_TX_WT_STOP_SENDING = 142,  /* picoquic_request_stop_sending on WT stream */

    /* WebTransport (H3) — picoquic thread → asyncio thread. The
     * `cnx` field carries the picoquic_cnx_t*; `stream_id` is the
     * WT control stream for session events, or the WT stream for
     * stream events. error_code carries WT error code for refused/
     * closed/reset/stop_sending. data_buf carries reason text for
     * close events, payload for stream/datagram events. */
    SPSC_EVT_WT_SESSION_READY = 64,        /* CONNECT accepted by peer */
    SPSC_EVT_WT_SESSION_REFUSED = 65,      /* CONNECT refused */
    SPSC_EVT_WT_SESSION_CLOSED = 66,       /* CLOSE_WEBTRANSPORT_SESSION received */
    SPSC_EVT_WT_SESSION_DRAINING = 67,     /* DRAIN_WEBTRANSPORT_SESSION received */
    SPSC_EVT_WT_STREAM_DATA = 68,          /* data on a WT stream */
    SPSC_EVT_WT_STREAM_FIN = 69,           /* FIN on a WT stream */
    SPSC_EVT_WT_STREAM_RESET = 70,         /* peer reset a WT stream */
    SPSC_EVT_WT_STOP_SENDING = 71,         /* peer asked us to stop sending */
    SPSC_EVT_WT_DATAGRAM = 72,             /* WT datagram received */
    SPSC_EVT_WT_NEW_STREAM = 73,           /* peer opened a new WT stream */
    SPSC_EVT_WT_STREAM_CREATED = 74,       /* ack of TX_WT_CREATE_STREAM
                                              with assigned stream_id */
    SPSC_EVT_WT_NEW_SESSION = 75,          /* server-side: peer's CONNECT
                                              accepted; new WT session
                                              created. data = path bytes;
                                              cnx = picoquic_cnx_t*;
                                              stream_ctx = aiopquic_wt_session_t* */
} spsc_event_type_t;

typedef struct {
    uint64_t    stream_id;
    uint32_t    event_type;
    uint32_t    data_length;
    uint8_t     is_fin;
    uint8_t     reserved[7];
    void*       data_buf;       /* owned malloc'd payload (NULL if no data) */
    void*       cnx;
    void*       stream_ctx;
    uint64_t    error_code;
} spsc_entry_t;

typedef struct {
    _Alignas(SPSC_CACHELINE) _Atomic(uint64_t) tail;
    char _pad_tail[SPSC_CACHELINE - sizeof(_Atomic(uint64_t))];

    _Alignas(SPSC_CACHELINE) _Atomic(uint64_t) head;
    char _pad_head[SPSC_CACHELINE - sizeof(_Atomic(uint64_t))];

    uint32_t    capacity;
    uint32_t    mask;
    spsc_entry_t* entries;
} spsc_ring_t;


static inline spsc_ring_t* spsc_ring_create(uint32_t capacity) {
    if ((capacity & (capacity - 1)) != 0) return NULL;

    spsc_ring_t* ring = (spsc_ring_t*)aligned_alloc(SPSC_CACHELINE, sizeof(spsc_ring_t));
    if (!ring) return NULL;

    memset(ring, 0, sizeof(*ring));
    ring->capacity = capacity;
    ring->mask = capacity - 1;
    ring->entries = (spsc_entry_t*)calloc(capacity, sizeof(spsc_entry_t));
    if (!ring->entries) {
        free(ring);
        return NULL;
    }

    atomic_store_explicit(&ring->head, 0, memory_order_relaxed);
    atomic_store_explicit(&ring->tail, 0, memory_order_relaxed);
    return ring;
}

/* Destroy a ring buffer; frees any pending entries' data_buf. */
static inline void spsc_ring_destroy(spsc_ring_t* ring) {
    if (!ring) return;
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_relaxed);
    while (head < tail) {
        spsc_entry_t* e = &ring->entries[head & ring->mask];
        if (e->data_buf) {
            free(e->data_buf);
            e->data_buf = NULL;
        }
        head++;
    }
    free(ring->entries);
    free(ring);
}

static inline uint32_t spsc_ring_count(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_acquire);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_acquire);
    return (uint32_t)(tail - head);
}

static inline int spsc_ring_full(spsc_ring_t* ring) {
    return spsc_ring_count(ring) >= ring->capacity;
}

static inline int spsc_ring_empty(spsc_ring_t* ring) {
    return spsc_ring_count(ring) == 0;
}

/*
 * Push an entry with payload data into the ring (PRODUCER only).
 * If data_len > 0, allocates a fresh buffer, copies data into it,
 * and stores the buffer pointer in the entry's data_buf. Ownership
 * transfers to the consumer at pop time.
 *
 * Returns 0 on success, -1 if the ring is full (allocated buffer is freed),
 * -2 on allocation failure.
 */
static inline int spsc_ring_push(spsc_ring_t* ring, const spsc_entry_t* entry,
                                  const uint8_t* data, uint32_t data_len) {
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_acquire);

    if (tail - head >= ring->capacity) {
        return -1;
    }

    void* buf = NULL;
    if (data && data_len > 0) {
        buf = malloc(data_len);
        if (!buf) {
            return -2;
        }
        memcpy(buf, data, data_len);
    }

    uint32_t slot = (uint32_t)(tail & ring->mask);
    spsc_entry_t* e = &ring->entries[slot];
    *e = *entry;
    e->data_buf = buf;
    e->data_length = (buf ? data_len : 0);

    /* Release: ensure entry + buf writes are visible before tail advances. */
    atomic_store_explicit(&ring->tail, tail + 1, memory_order_release);
    return 0;
}

/*
 * Peek at the next entry to read (CONSUMER only). Pointer remains
 * stable until spsc_ring_pop() is called.
 */
static inline spsc_entry_t* spsc_ring_peek(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_acquire);

    if (head >= tail) {
        return NULL;
    }

    return &ring->entries[head & ring->mask];
}

/*
 * Get pointer to the data associated with an entry (CONSUMER only).
 * Valid until spsc_ring_pop() (consumer free policy permitting).
 */
static inline const uint8_t* spsc_ring_entry_data(spsc_ring_t* ring,
                                                    const spsc_entry_t* entry) {
    (void)ring;
    return (const uint8_t*)entry->data_buf;
}

/*
 * Pop (consume) the next entry (CONSUMER only).
 * Caller takes ownership of entry->data_buf BEFORE calling pop, OR
 * passes free_data=1 to have the ring free it here. Use free_data=0
 * (and call spsc_ring_take_data first) when transferring ownership
 * (e.g., to a Python StreamChunk).
 */
static inline void spsc_ring_pop(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    spsc_entry_t* e = &ring->entries[head & ring->mask];
    /* Default: free any leftover data_buf. Consumers transferring
     * ownership must NULL the buffer pointer before calling pop. */
    if (e->data_buf) {
        free(e->data_buf);
        e->data_buf = NULL;
    }
    /* Release: ensure consumer is done reading before advancing head. */
    atomic_store_explicit(&ring->head, head + 1, memory_order_release);
}

/*
 * Take ownership of an entry's data_buf (CONSUMER only).
 * Returns the pointer and zeros it on the entry so spsc_ring_pop
 * won't free it. Caller becomes responsible for free().
 */
static inline void* spsc_ring_take_data(spsc_entry_t* entry) {
    void* p = entry->data_buf;
    entry->data_buf = NULL;
    entry->data_length = 0;
    return p;
}

/* Push a simple event with no data (PRODUCER only). */
static inline int spsc_ring_push_event(spsc_ring_t* ring,
                                        uint32_t event_type,
                                        uint64_t stream_id,
                                        void* cnx,
                                        uint64_t error_code) {
    spsc_entry_t entry = {0};
    entry.event_type = event_type;
    entry.stream_id = stream_id;
    entry.cnx = cnx;
    entry.error_code = error_code;
    return spsc_ring_push(ring, &entry, NULL, 0);
}

static inline int spsc_ring_push_stream_data(spsc_ring_t* ring,
                                              uint64_t stream_id,
                                              const uint8_t* data,
                                              uint32_t length,
                                              int is_fin,
                                              void* cnx,
                                              void* stream_ctx) {
    spsc_entry_t entry = {0};
    entry.event_type = is_fin ? SPSC_EVT_STREAM_FIN : SPSC_EVT_STREAM_DATA;
    entry.stream_id = stream_id;
    entry.is_fin = (uint8_t)is_fin;
    entry.cnx = cnx;
    entry.stream_ctx = stream_ctx;
    return spsc_ring_push(ring, &entry, data, length);
}

#ifdef __cplusplus
}
#endif

#endif /* AIOPQUIC_SPSC_RING_H */
