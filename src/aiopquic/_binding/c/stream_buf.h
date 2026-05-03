/*
 * Per-stream byte ring buffer for the PULL-model send path.
 *
 * Producer (Python via push_to_stream_buf): writes bytes into the ring
 * up to capacity. Returns # bytes accepted; 0 means full = backpressure.
 *
 * Consumer (picoquic-pthread inside aiopquic_stream_cb when picoquic
 * fires picoquic_callback_prepare_to_send): drains bytes up to the
 * frame budget picoquic gives us, calls picoquic_provide_stream_data_buffer.
 *
 * Synchronization: single producer (Python main thread holding GIL), single
 * consumer (picoquic-pthread). Atomic head/tail with acquire/release ordering.
 *
 * The "ring" is implemented as a contiguous byte array with monotonically
 * increasing head/tail indexes; modulo by capacity gives the slot. capacity
 * MUST be a power of two so we can mask.
 *
 * fin_pending: producer may set this to indicate "the next time the ring is
 * fully drained, signal FIN to picoquic". Consumer reads this when ring
 * goes empty in the prepare_to_send callback.
 */
#pragma once

#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    uint8_t* buf;
    uint32_t capacity;          /* power of two */
    uint32_t mask;
    _Atomic(uint64_t) tail;     /* producer-advanced; total bytes written */
    _Atomic(uint64_t) head;     /* consumer-advanced; total bytes read */
    _Atomic(int)      fin_pending;  /* 1 = mark FIN when ring drains */
    /* Byte-conservation diagnostic: rolling FNV-1a 32-bit hash updated on
     * push and on pop. By design (single-producer/single-consumer FIFO)
     * these MUST be equal once both sides have processed the same byte
     * count. A mismatch proves a bug in the ring. Gated on hash_enabled
     * to keep the hot path zero-cost when not investigating. */
    uint8_t  hash_enabled;
    uint32_t push_hash;
    uint32_t pop_hash;
} aiopquic_stream_buf_t;

#define AIOPQUIC_FNV1A_INIT 0x811c9dc5u
#define AIOPQUIC_FNV1A_PRIME 0x01000193u

static inline uint32_t aiopquic_fnv1a_step(uint32_t h, const uint8_t* p,
                                            uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        h ^= p[i];
        h *= AIOPQUIC_FNV1A_PRIME;
    }
    return h;
}

/* Round up to the next power of two ≥ n, capped at 1<<31 to keep
 * arithmetic in uint32_t. Callers pass the configured window
 * (e.g., max_stream_data) verbatim; the ring sizes itself. */
static inline uint32_t aiopquic_ceil_pow2_u32(uint32_t n) {
    if (n == 0) return 1;
    if ((n & (n - 1)) == 0) return n;
    /* clz is undefined on 0; we already returned. */
    uint32_t bits = (uint32_t)(32 - __builtin_clz(n));
    if (bits >= 32) return 1u << 31;
    return 1u << bits;
}

static inline aiopquic_stream_buf_t* aiopquic_stream_buf_create(
        uint32_t capacity) {
    /* Power-of-two is an internal sizing constraint — callers can pass
     * any positive size and the ring rounds up. */
    capacity = aiopquic_ceil_pow2_u32(capacity);
    if (capacity == 0) {
        return NULL;
    }
    aiopquic_stream_buf_t* sb =
        (aiopquic_stream_buf_t*)calloc(1, sizeof(aiopquic_stream_buf_t));
    if (!sb) return NULL;
    sb->buf = (uint8_t*)malloc(capacity);
    if (!sb->buf) { free(sb); return NULL; }
    sb->capacity = capacity;
    sb->mask = capacity - 1;
    atomic_store_explicit(&sb->head, 0, memory_order_relaxed);
    atomic_store_explicit(&sb->tail, 0, memory_order_relaxed);
    atomic_store_explicit(&sb->fin_pending, 0, memory_order_relaxed);
    /* Hash gate: env-driven so it can be flipped without rebuild.
     * AIOPQUIC_TX_HASH=1 enables push/pop FNV-1a accumulators. */
    {
        const char* h = getenv("AIOPQUIC_TX_HASH");
        sb->hash_enabled = (h && *h && *h != '0') ? 1 : 0;
    }
    sb->push_hash = AIOPQUIC_FNV1A_INIT;
    sb->pop_hash = AIOPQUIC_FNV1A_INIT;
    return sb;
}

static inline void aiopquic_stream_buf_destroy(aiopquic_stream_buf_t* sb) {
    if (!sb) return;
    if (sb->buf) free(sb->buf);
    free(sb);
}

/* Push bytes into the ring. Returns # bytes accepted (0..len). 0 means
 * the ring is full — caller should retry later. */
static inline uint32_t aiopquic_stream_buf_push(
        aiopquic_stream_buf_t* sb,
        const uint8_t* data, uint32_t len) {
    uint64_t tail = atomic_load_explicit(&sb->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&sb->head, memory_order_acquire);
    uint32_t free_bytes = sb->capacity - (uint32_t)(tail - head);
    if (free_bytes == 0) return 0;
    uint32_t to_write = (len < free_bytes) ? len : free_bytes;

    /* May wrap. Two memcpys at most. */
    uint32_t off = (uint32_t)(tail & sb->mask);
    uint32_t first = sb->capacity - off;
    if (first > to_write) first = to_write;
    memcpy(sb->buf + off, data, first);
    if (first < to_write) {
        memcpy(sb->buf, data + first, to_write - first);
    }
    if (sb->hash_enabled) {
        sb->push_hash = aiopquic_fnv1a_step(sb->push_hash, data, to_write);
    }
    atomic_store_explicit(&sb->tail, tail + to_write, memory_order_release);
    return to_write;
}

/* Drain up to max_bytes from the ring into out. Returns # bytes consumed. */
static inline uint32_t aiopquic_stream_buf_pop(
        aiopquic_stream_buf_t* sb,
        uint8_t* out, uint32_t max_bytes) {
    uint64_t head = atomic_load_explicit(&sb->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&sb->tail, memory_order_acquire);
    uint32_t available = (uint32_t)(tail - head);
    if (available == 0) return 0;
    uint32_t to_read = (max_bytes < available) ? max_bytes : available;

    uint32_t off = (uint32_t)(head & sb->mask);
    uint32_t first = sb->capacity - off;
    if (first > to_read) first = to_read;
    memcpy(out, sb->buf + off, first);
    if (first < to_read) {
        memcpy(out + first, sb->buf, to_read - first);
    }
    if (sb->hash_enabled) {
        sb->pop_hash = aiopquic_fnv1a_step(sb->pop_hash, out, to_read);
    }
    atomic_store_explicit(&sb->head, head + to_read, memory_order_release);
    return to_read;
}

/* Number of bytes currently buffered (tail - head). */
static inline uint32_t aiopquic_stream_buf_used(
        aiopquic_stream_buf_t* sb) {
    uint64_t tail = atomic_load_explicit(&sb->tail, memory_order_acquire);
    uint64_t head = atomic_load_explicit(&sb->head, memory_order_acquire);
    return (uint32_t)(tail - head);
}

/* Free space remaining. */
static inline uint32_t aiopquic_stream_buf_free(
        aiopquic_stream_buf_t* sb) {
    return sb->capacity - aiopquic_stream_buf_used(sb);
}

static inline void aiopquic_stream_buf_set_fin(aiopquic_stream_buf_t* sb) {
    atomic_store_explicit(&sb->fin_pending, 1, memory_order_release);
}

static inline int aiopquic_stream_buf_fin_pending(aiopquic_stream_buf_t* sb) {
    return atomic_load_explicit(&sb->fin_pending, memory_order_acquire);
}

/* Lifetime cumulative push count (bytes ever pushed; never decremented). */
static inline uint64_t aiopquic_stream_buf_pushed(aiopquic_stream_buf_t* sb) {
    return atomic_load_explicit(&sb->tail, memory_order_acquire);
}

/* Lifetime cumulative pop count (bytes ever popped to picoquic). */
static inline uint64_t aiopquic_stream_buf_popped(aiopquic_stream_buf_t* sb) {
    return atomic_load_explicit(&sb->head, memory_order_acquire);
}

/* Diagnostic hashes. Both AIOPQUIC_FNV1A_INIT when hash_enabled=0 or
 * no bytes have moved through. */
static inline uint32_t aiopquic_stream_buf_push_hash(
        aiopquic_stream_buf_t* sb) {
    return sb->push_hash;
}

static inline uint32_t aiopquic_stream_buf_pop_hash(
        aiopquic_stream_buf_t* sb) {
    return sb->pop_hash;
}
