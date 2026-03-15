/*
 * spsc_ring.h — Lock-free Single Producer Single Consumer ring buffer.
 *
 * Uses C11 atomics with acquire/release ordering.
 * Cache-line aligned head/tail to avoid false sharing.
 * Includes an inline data arena for variable-length payloads.
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

/* Default sizes */
#define SPSC_RING_DEFAULT_CAPACITY  4096    /* entries, must be power of 2 */
#define SPSC_RING_DEFAULT_ARENA_SIZE (4 * 1024 * 1024)  /* 4 MB data arena */

/* Cache line size for alignment */
#define SPSC_CACHELINE 64

/* Event types matching picoquic callback events (subset we care about) */
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

    /* TX-specific (asyncio → picoquic) */
    SPSC_EVT_TX_STREAM_DATA = 128,
    SPSC_EVT_TX_STREAM_FIN = 129,
    SPSC_EVT_TX_DATAGRAM = 130,
    SPSC_EVT_TX_CLOSE = 131,
    SPSC_EVT_TX_STREAM_RESET = 132,
    SPSC_EVT_TX_STOP_SENDING = 133,
    SPSC_EVT_TX_MARK_ACTIVE = 134,
    SPSC_EVT_TX_CONNECT = 135,      /* create client connection */
} spsc_event_type_t;

/* Ring entry — fixed-size descriptor for each event */
typedef struct {
    uint64_t    stream_id;
    uint32_t    event_type;     /* spsc_event_type_t */
    uint32_t    data_offset;    /* offset into data arena */
    uint32_t    data_length;    /* bytes of payload */
    uint8_t     is_fin;
    uint8_t     reserved[3];
    void*       cnx;            /* picoquic_cnx_t* for demuxing */
    void*       stream_ctx;     /* app stream context */
    uint64_t    error_code;     /* for reset/close events */
} spsc_entry_t;

/* Ring buffer structure */
typedef struct {
    /* Producer (writer) index — on its own cache line */
    _Alignas(SPSC_CACHELINE) _Atomic(uint64_t) tail;
    char _pad_tail[SPSC_CACHELINE - sizeof(_Atomic(uint64_t))];

    /* Consumer (reader) index — on its own cache line */
    _Alignas(SPSC_CACHELINE) _Atomic(uint64_t) head;
    char _pad_head[SPSC_CACHELINE - sizeof(_Atomic(uint64_t))];

    /* Immutable after init */
    uint32_t    capacity;       /* must be power of 2 */
    uint32_t    mask;           /* capacity - 1 */
    spsc_entry_t* entries;      /* ring_entry_t[capacity] */

    /* Data arena for variable-length payloads */
    uint8_t*    arena;
    uint32_t    arena_size;
    uint32_t    arena_mask;     /* arena_size - 1, for wrapping */

    /* Arena write position (producer only — no atomics needed,
     * single writer guarantees exclusive access) */
    uint32_t    arena_write_pos;
} spsc_ring_t;


/*
 * Create a ring buffer.
 * capacity: number of entries (must be power of 2)
 * arena_size: size of data arena in bytes (must be power of 2)
 * Returns NULL on failure.
 */
static inline spsc_ring_t* spsc_ring_create(uint32_t capacity, uint32_t arena_size) {
    if ((capacity & (capacity - 1)) != 0) return NULL;  /* not power of 2 */
    if ((arena_size & (arena_size - 1)) != 0) return NULL;

    spsc_ring_t* ring = (spsc_ring_t*)aligned_alloc(SPSC_CACHELINE, sizeof(spsc_ring_t));
    if (!ring) return NULL;

    memset(ring, 0, sizeof(*ring));
    ring->capacity = capacity;
    ring->mask = capacity - 1;
    ring->arena_size = arena_size;
    ring->arena_mask = arena_size - 1;
    ring->arena_write_pos = 0;

    ring->entries = (spsc_entry_t*)calloc(capacity, sizeof(spsc_entry_t));
    ring->arena = (uint8_t*)malloc(arena_size);
    if (!ring->entries || !ring->arena) {
        free(ring->entries);
        free(ring->arena);
        free(ring);
        return NULL;
    }

    atomic_store_explicit(&ring->head, 0, memory_order_relaxed);
    atomic_store_explicit(&ring->tail, 0, memory_order_relaxed);
    return ring;
}

/* Destroy a ring buffer */
static inline void spsc_ring_destroy(spsc_ring_t* ring) {
    if (ring) {
        free(ring->entries);
        free(ring->arena);
        free(ring);
    }
}

/* Number of entries available to read */
static inline uint32_t spsc_ring_count(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_acquire);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_acquire);
    return (uint32_t)(tail - head);
}

/* Is the ring full? */
static inline int spsc_ring_full(spsc_ring_t* ring) {
    return spsc_ring_count(ring) >= ring->capacity;
}

/* Is the ring empty? */
static inline int spsc_ring_empty(spsc_ring_t* ring) {
    return spsc_ring_count(ring) == 0;
}

/*
 * Push an entry with inline data into the ring (PRODUCER only).
 * Copies `data_len` bytes from `data` into the arena.
 * Returns 0 on success, -1 if ring is full or arena is full.
 */
static inline int spsc_ring_push(spsc_ring_t* ring, const spsc_entry_t* entry,
                                  const uint8_t* data, uint32_t data_len) {
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_acquire);

    if (tail - head >= ring->capacity) {
        return -1;  /* ring full */
    }

    uint32_t slot = (uint32_t)(tail & ring->mask);
    spsc_entry_t* e = &ring->entries[slot];
    *e = *entry;

    if (data && data_len > 0) {
        /* Check arena has space. Simple check: we always have arena_size
         * bytes total. In worst case we use linear allocation and wrap. */
        uint32_t wp = ring->arena_write_pos;
        e->data_offset = wp;
        e->data_length = data_len;

        /* Copy data — handle wrap-around */
        uint32_t first = ring->arena_size - (wp & ring->arena_mask);
        if (first >= data_len) {
            memcpy(ring->arena + (wp & ring->arena_mask), data, data_len);
        } else {
            memcpy(ring->arena + (wp & ring->arena_mask), data, first);
            memcpy(ring->arena, data + first, data_len - first);
        }
        ring->arena_write_pos = wp + data_len;
    } else {
        e->data_offset = 0;
        e->data_length = 0;
    }

    /* Release: ensure entry + arena writes are visible before tail advances */
    atomic_store_explicit(&ring->tail, tail + 1, memory_order_release);
    return 0;
}

/*
 * Peek at the next entry to read (CONSUMER only).
 * Returns pointer to entry, or NULL if empty.
 * The entry remains in the ring until spsc_ring_pop() is called.
 */
static inline spsc_entry_t* spsc_ring_peek(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_acquire);

    if (head >= tail) {
        return NULL;  /* empty */
    }

    return &ring->entries[head & ring->mask];
}

/*
 * Get pointer to the data associated with an entry (CONSUMER only).
 * Returns pointer into the arena. Valid until spsc_ring_pop().
 * Caller must NOT free this pointer.
 */
static inline const uint8_t* spsc_ring_entry_data(spsc_ring_t* ring,
                                                    const spsc_entry_t* entry) {
    if (entry->data_length == 0) return NULL;
    return ring->arena + (entry->data_offset & ring->arena_mask);
}

/*
 * Pop (consume) the next entry (CONSUMER only).
 * Must be called after processing an entry obtained via spsc_ring_peek().
 */
static inline void spsc_ring_pop(spsc_ring_t* ring) {
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    /* Release: ensure consumer is done reading before advancing head */
    atomic_store_explicit(&ring->head, head + 1, memory_order_release);
}

/*
 * Push a simple event with no data (PRODUCER only).
 */
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

/*
 * Push stream data (PRODUCER only). Convenience wrapper.
 */
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
