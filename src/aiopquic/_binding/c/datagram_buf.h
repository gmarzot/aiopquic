/*
 * Per-connection datagram TX record ring — PULL-model send path.
 *
 * Producer (Python via TransportContext.dgram_send): pushes whole
 * records, all-or-nothing. A record is [u32 payload_len][payload].
 * Returns 0 on ring-full = backpressure (mirrors stream_buf push
 * semantics), -1 if the payload exceeds max_record (datagram frames
 * cannot be fragmented, so an oversize record could never drain).
 *
 * Consumer (picoquic worker in picoquic_callback_prepare_datagram):
 * peeks the head record's length; if it fits the offered packet space,
 * copies it straight into picoquic's datagram buffer and pops. If it
 * does not fit the current packet's remaining space it stays queued
 * for the next packet (producer-side max_record enforcement guarantees
 * it fits a fresh packet).
 *
 * Bounded capacity IS the backpressure.
 *
 * Synchronization: single producer (Python thread), single consumer
 * (picoquic worker). Atomic head/tail with acquire/release ordering,
 * same discipline as stream_buf.h.
 *
 * Lifetime: refcounted. The Python connection object holds one ref;
 * the worker's cnx->ring table holds one from insert until the cnx
 * close callback (or ctx destroy sweep). Last unref frees.
 */
#pragma once

#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "stream_buf.h"   /* aiopquic_ceil_pow2_u32 */

#define AIOPQUIC_DGRAM_REC_HDR 4u

typedef struct {
    uint8_t* buf;
    uint32_t capacity;          /* power of two */
    uint32_t mask;
    uint32_t max_record;        /* producer-enforced payload byte cap */
    _Atomic(uint64_t) tail;     /* producer-advanced; total bytes written */
    _Atomic(uint64_t) head;     /* consumer-advanced; total bytes read */
    _Atomic(uint32_t) refcnt;
    /* Edge-trigger backpressure flag, mirrors sc->tx_drain_pending:
     * producer sets 1 after a full push; worker CAS-clears after a pop
     * and emits one SPSC_EVT_DATAGRAM_TX_DRAINED so a blocked writer
     * can retry. */
    _Atomic(uint32_t) drain_pending;
    /* Counters, single-writer each (producer / worker). */
    uint64_t records_pushed;    /* producer */
    uint64_t push_full;         /* producer: rejected, ring full */
    uint64_t push_oversize;     /* producer: rejected, > max_record */
    uint64_t records_popped;    /* worker */
    uint64_t bytes_popped;      /* worker: payload bytes provided */
    uint64_t head_deferred;     /* worker: head didn't fit this packet */
} aiopquic_dgram_buf_t;

static inline aiopquic_dgram_buf_t* aiopquic_dgram_buf_create(
        uint32_t capacity, uint32_t max_record) {
    capacity = aiopquic_ceil_pow2_u32(capacity);
    if (capacity == 0 || max_record == 0) return NULL;
    /* A single record (header + payload) must fit the ring, with room
     * to spare so one max-size record can't deadlock a full ring. */
    if (capacity < 2 * (AIOPQUIC_DGRAM_REC_HDR + max_record)) {
        capacity = aiopquic_ceil_pow2_u32(
            2 * (AIOPQUIC_DGRAM_REC_HDR + max_record));
    }
    aiopquic_dgram_buf_t* db =
        (aiopquic_dgram_buf_t*)calloc(1, sizeof(aiopquic_dgram_buf_t));
    if (!db) return NULL;
    db->buf = (uint8_t*)malloc(capacity);
    if (!db->buf) { free(db); return NULL; }
    db->capacity = capacity;
    db->mask = capacity - 1;
    db->max_record = max_record;
    atomic_store_explicit(&db->head, 0, memory_order_relaxed);
    atomic_store_explicit(&db->tail, 0, memory_order_relaxed);
    atomic_store_explicit(&db->refcnt, 1, memory_order_relaxed);
    atomic_store_explicit(&db->drain_pending, 0, memory_order_relaxed);
    return db;
}

static inline void aiopquic_dgram_buf_addref(aiopquic_dgram_buf_t* db) {
    atomic_fetch_add_explicit(&db->refcnt, 1, memory_order_relaxed);
}

static inline void aiopquic_dgram_buf_unref(aiopquic_dgram_buf_t* db) {
    if (!db) return;
    if (atomic_fetch_sub_explicit(&db->refcnt, 1,
                                  memory_order_acq_rel) == 1) {
        free(db->buf);
        free(db);
    }
}

/* Ring byte helpers — offsets may wrap; two memcpys at most. */
static inline void aiopquic_dgram_ring_write(
        aiopquic_dgram_buf_t* db, uint64_t pos,
        const uint8_t* src, uint32_t n) {
    uint32_t off = (uint32_t)(pos & db->mask);
    uint32_t first = db->capacity - off;
    if (first > n) first = n;
    memcpy(db->buf + off, src, first);
    if (first < n) memcpy(db->buf, src + first, n - first);
}

static inline void aiopquic_dgram_ring_read(
        aiopquic_dgram_buf_t* db, uint64_t pos,
        uint8_t* dst, uint32_t n) {
    uint32_t off = (uint32_t)(pos & db->mask);
    uint32_t first = db->capacity - off;
    if (first > n) first = n;
    memcpy(dst, db->buf + off, first);
    if (first < n) memcpy(dst + first, db->buf, n - first);
}

/* Producer. Returns 1 = accepted, 0 = ring full (retry later),
 * -1 = payload exceeds max_record (permanent, do not retry). */
static inline int aiopquic_dgram_buf_push_record(
        aiopquic_dgram_buf_t* db, const uint8_t* data, uint32_t len) {
    if (len > db->max_record) {
        db->push_oversize++;
        return -1;
    }
    uint64_t tail = atomic_load_explicit(&db->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&db->head, memory_order_acquire);
    uint32_t free_bytes = db->capacity - (uint32_t)(tail - head);
    if (free_bytes < AIOPQUIC_DGRAM_REC_HDR + len) {
        db->push_full++;
        return 0;
    }
    uint8_t hdr[AIOPQUIC_DGRAM_REC_HDR];
    hdr[0] = (uint8_t)(len & 0xff);
    hdr[1] = (uint8_t)((len >> 8) & 0xff);
    hdr[2] = (uint8_t)((len >> 16) & 0xff);
    hdr[3] = (uint8_t)((len >> 24) & 0xff);
    aiopquic_dgram_ring_write(db, tail, hdr, AIOPQUIC_DGRAM_REC_HDR);
    aiopquic_dgram_ring_write(db, tail + AIOPQUIC_DGRAM_REC_HDR, data, len);
    atomic_store_explicit(&db->tail, tail + AIOPQUIC_DGRAM_REC_HDR + len,
                          memory_order_release);
    db->records_pushed++;
    return 1;
}

/* Consumer: payload length of the head record, 0 if empty. */
static inline uint32_t aiopquic_dgram_buf_peek_len(
        aiopquic_dgram_buf_t* db) {
    uint64_t head = atomic_load_explicit(&db->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&db->tail, memory_order_acquire);
    if (tail - head < AIOPQUIC_DGRAM_REC_HDR) return 0;
    uint8_t hdr[AIOPQUIC_DGRAM_REC_HDR];
    aiopquic_dgram_ring_read(db, head, hdr, AIOPQUIC_DGRAM_REC_HDR);
    return (uint32_t)hdr[0] | ((uint32_t)hdr[1] << 8)
         | ((uint32_t)hdr[2] << 16) | ((uint32_t)hdr[3] << 24);
}

/* Consumer: copy the head record's payload (exactly `len` bytes, from a
 * prior peek) into out and consume it. */
static inline void aiopquic_dgram_buf_pop_into(
        aiopquic_dgram_buf_t* db, uint8_t* out, uint32_t len) {
    uint64_t head = atomic_load_explicit(&db->head, memory_order_relaxed);
    aiopquic_dgram_ring_read(db, head + AIOPQUIC_DGRAM_REC_HDR, out, len);
    atomic_store_explicit(&db->head, head + AIOPQUIC_DGRAM_REC_HDR + len,
                          memory_order_release);
    db->records_popped++;
    db->bytes_popped += len;
}

static inline uint32_t aiopquic_dgram_buf_used(aiopquic_dgram_buf_t* db) {
    uint64_t tail = atomic_load_explicit(&db->tail, memory_order_acquire);
    uint64_t head = atomic_load_explicit(&db->head, memory_order_acquire);
    return (uint32_t)(tail - head);
}

/* Producer: arm the edge-trigger drain signal after a full push, so the
 * worker's next pop emits SPSC_EVT_DATAGRAM_TX_DRAINED. */
static inline void aiopquic_dgram_buf_arm_drain(aiopquic_dgram_buf_t* db) {
    atomic_store_explicit(&db->drain_pending, 1, memory_order_release);
}

/* ---------------------------------------------------------------------
 * cnx -> ring table. WORKER-THREAD-ONLY access (insert on
 * MARK_DATAGRAM_READY, lookup on prepare_datagram, remove on cnx close,
 * sweep on ctx destroy) — no locking needed. Open addressing with
 * tombstones; grows by rehash at ~70% load.
 * ------------------------------------------------------------------- */

#define AIOPQUIC_DGRAM_SLOT_TOMBSTONE ((void*)(uintptr_t)1)

typedef struct {
    void* cnx;
    aiopquic_dgram_buf_t* db;
} aiopquic_dgram_slot_t;

typedef struct {
    aiopquic_dgram_slot_t* slots;
    uint32_t cap;               /* power of two, 0 until first insert */
    uint32_t mask;
    uint32_t live;              /* live entries */
    uint32_t used;              /* live + tombstones */
} aiopquic_dgram_table_t;

static inline uint32_t aiopquic_dgram_hash_ptr(void* p) {
    uintptr_t v = (uintptr_t)p;
    v ^= v >> 33;
    v *= 0xff51afd7ed558ccdULL;
    v ^= v >> 33;
    return (uint32_t)v;
}

static inline int aiopquic_dgram_table_grow(aiopquic_dgram_table_t* t) {
    uint32_t new_cap = t->cap ? t->cap * 2 : 64;
    aiopquic_dgram_slot_t* ns = (aiopquic_dgram_slot_t*)calloc(
        new_cap, sizeof(aiopquic_dgram_slot_t));
    if (!ns) return -1;
    uint32_t new_mask = new_cap - 1;
    for (uint32_t i = 0; i < t->cap; i++) {
        void* c = t->slots[i].cnx;
        if (c == NULL || c == AIOPQUIC_DGRAM_SLOT_TOMBSTONE) continue;
        uint32_t j = aiopquic_dgram_hash_ptr(c) & new_mask;
        while (ns[j].cnx != NULL) j = (j + 1) & new_mask;
        ns[j] = t->slots[i];
    }
    free(t->slots);
    t->slots = ns;
    t->cap = new_cap;
    t->mask = new_mask;
    t->used = t->live;
    return 0;
}

/* Insert (idempotent for the same cnx). Takes a NEW worker-side ref on
 * db when a fresh entry is created; no-ops if cnx already present. */
static inline int aiopquic_dgram_table_put(
        aiopquic_dgram_table_t* t, void* cnx, aiopquic_dgram_buf_t* db) {
    if (t->cap == 0 || (t->used + 1) * 10 >= t->cap * 7) {
        if (aiopquic_dgram_table_grow(t) != 0) return -1;
    }
    uint32_t i = aiopquic_dgram_hash_ptr(cnx) & t->mask;
    int32_t first_tomb = -1;
    for (;;) {
        void* c = t->slots[i].cnx;
        if (c == cnx) return 0;             /* already registered */
        if (c == NULL) break;
        if (c == AIOPQUIC_DGRAM_SLOT_TOMBSTONE && first_tomb < 0) {
            first_tomb = (int32_t)i;
        }
        i = (i + 1) & t->mask;
    }
    if (first_tomb >= 0) {
        i = (uint32_t)first_tomb;
    } else {
        t->used++;
    }
    t->slots[i].cnx = cnx;
    t->slots[i].db = db;
    t->live++;
    aiopquic_dgram_buf_addref(db);
    return 0;
}

static inline aiopquic_dgram_buf_t* aiopquic_dgram_table_get(
        aiopquic_dgram_table_t* t, void* cnx) {
    if (t->cap == 0) return NULL;
    uint32_t i = aiopquic_dgram_hash_ptr(cnx) & t->mask;
    for (;;) {
        void* c = t->slots[i].cnx;
        if (c == cnx) return t->slots[i].db;
        if (c == NULL) return NULL;
        i = (i + 1) & t->mask;
    }
}

/* Remove + drop the worker-side ref. Safe to call for absent cnx. */
static inline void aiopquic_dgram_table_remove(
        aiopquic_dgram_table_t* t, void* cnx) {
    if (t->cap == 0) return;
    uint32_t i = aiopquic_dgram_hash_ptr(cnx) & t->mask;
    for (;;) {
        void* c = t->slots[i].cnx;
        if (c == cnx) {
            aiopquic_dgram_buf_unref(t->slots[i].db);
            t->slots[i].cnx = AIOPQUIC_DGRAM_SLOT_TOMBSTONE;
            t->slots[i].db = NULL;
            t->live--;
            return;
        }
        if (c == NULL) return;
        i = (i + 1) & t->mask;
    }
}

/* Drop every worker-side ref and free the slot array (ctx destroy). */
static inline void aiopquic_dgram_table_destroy(aiopquic_dgram_table_t* t) {
    for (uint32_t i = 0; i < t->cap; i++) {
        void* c = t->slots[i].cnx;
        if (c != NULL && c != AIOPQUIC_DGRAM_SLOT_TOMBSTONE) {
            aiopquic_dgram_buf_unref(t->slots[i].db);
        }
    }
    free(t->slots);
    t->slots = NULL;
    t->cap = t->mask = t->live = t->used = 0;
}
