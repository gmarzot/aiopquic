/*
 * stream_ctx.h — per-stream wrapper holding both TX and RX byte rings.
 *
 * picoquic gives applications a single stream_ctx slot per stream
 * (settable via picoquic_set_app_stream_ctx() or as v_stream_ctx in
 * picoquic_mark_active_stream()). For bidirectional streams we need both
 * TX and RX rings, so the slot points at this wrapper rather than at a
 * raw aiopquic_stream_buf_t. Each direction's ring is allocated lazily —
 * unidirectional streams only ever populate one side.
 *
 * Lifecycle:
 *   - First contact (RX path's first stream_data callback OR TX path's
 *     send_stream_data): aiopquic_stream_ctx_get_or_create binds a fresh
 *     wrapper to the stream slot.
 *   - aiopquic_stream_ctx_ensure_tx / _ensure_rx allocate the side ring
 *     on demand (idempotent).
 *   - On stream_reset / stop_sending: mark pending_destroy; the TX ring
 *     can be freed immediately (sender abandoned), RX waits for Python
 *     to drain.
 *   - On stream_fin: same; mark pending_destroy and let drain complete.
 *   - aiopquic_stream_ctx_destroy frees both rings + wrapper.
 *
 * All ring access remains single-producer/single-consumer per direction:
 *   TX: Python pushes, picoquic worker pops in prepare_to_send.
 *   RX: picoquic worker pushes in stream_data callback, Python pops.
 * Memory ordering is handled inside aiopquic_stream_buf_t.
 */
#pragma once

#include "stream_buf.h"
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct {
    aiopquic_stream_buf_t* tx;
    aiopquic_stream_buf_t* rx;
    /* Cumulative bytes Python has drained from the RX ring. Atomic
     * because the picoquic worker thread reads it from inside the
     * stream_data callback to decide when to extend MAX_STREAM_DATA,
     * while Python writes it after each drain_rx pop. Acquire/release
     * ordering ensures the worker sees Python's drain progress. */
    _Atomic(uint64_t) rx_consumed;
    /* Last MAX_STREAM_DATA limit advertised by the worker — kept here
     * (not in Python) so the worker-side hysteresis check needs no
     * additional state. Written only by the picoquic worker. */
    _Atomic(uint64_t) rx_credit_limit;
    /* Edge-triggered TX backpressure signal. Python sets this to 1
     * when a send_data attempt returns 0 (sc->tx full). The picoquic
     * worker thread CAS-clears it from 1->0 after popping bytes in
     * prepare_to_send; the CAS winner emits one SPSC event so the
     * blocked Python writer wakes and retries. Only one event fires
     * per fill->drain cycle. */
    _Atomic(uint32_t) tx_drain_pending;
    /* fin/reset arrived; free wrapper after final drain. */
    uint8_t  pending_destroy;
} aiopquic_stream_ctx_t;

static inline aiopquic_stream_ctx_t* aiopquic_stream_ctx_create(void) {
    return (aiopquic_stream_ctx_t*)calloc(1, sizeof(aiopquic_stream_ctx_t));
}

static inline int aiopquic_stream_ctx_ensure_tx(aiopquic_stream_ctx_t* sc,
                                                 uint32_t capacity) {
    if (!sc) return -1;
    if (sc->tx) return 0;
    sc->tx = aiopquic_stream_buf_create(capacity);
    return sc->tx ? 0 : -1;
}

static inline int aiopquic_stream_ctx_ensure_rx(aiopquic_stream_ctx_t* sc,
                                                 uint32_t capacity) {
    if (!sc) return -1;
    if (sc->rx) return 0;
    sc->rx = aiopquic_stream_buf_create(capacity);
    return sc->rx ? 0 : -1;
}

static inline void aiopquic_stream_ctx_destroy(aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    if (sc->tx) aiopquic_stream_buf_destroy(sc->tx);
    if (sc->rx) aiopquic_stream_buf_destroy(sc->rx);
    free(sc);
}

/* Atomic accessors for cross-thread fields. Cython sees plain uint64_t
 * (no _Atomic in the .pyx cdef extern); call these from both Python and
 * C sides for proper memory ordering. */
static inline uint64_t aiopquic_stream_ctx_rx_consumed_load(
        aiopquic_stream_ctx_t* sc) {
    return atomic_load_explicit(&sc->rx_consumed, memory_order_acquire);
}

static inline void aiopquic_stream_ctx_rx_consumed_add(
        aiopquic_stream_ctx_t* sc, uint64_t delta) {
    atomic_fetch_add_explicit(&sc->rx_consumed, delta, memory_order_release);
}

static inline uint64_t aiopquic_stream_ctx_rx_credit_load(
        aiopquic_stream_ctx_t* sc) {
    return atomic_load_explicit(&sc->rx_credit_limit, memory_order_acquire);
}

static inline void aiopquic_stream_ctx_rx_credit_store(
        aiopquic_stream_ctx_t* sc, uint64_t value) {
    atomic_store_explicit(&sc->rx_credit_limit, value, memory_order_release);
}

/* Combined send-data fast path. Atomic from Python's perspective:
 * lazy-allocates the TX ring on first call, atomically pushes `data`
 * (all-or-nothing — never commits a partial accept), optionally sets
 * FIN. Pull model unchanged: bytes go into the SPSC TX ring; picoquic
 * still pulls at wire rate via prepare_to_send.
 *
 * Returns:
 *    1  pushed all `len` bytes (and set FIN if requested);
 *    0  no bytes pushed because ring full (caller must retry);
 *   -1  allocation / NULL-arg failure (caller raises).
 *
 * The all-or-nothing guarantee is the reason this exists as one C
 * call rather than the Python-level ensure/free/push triple: with
 * Python control between free-check and push, a writer thread cannot
 * race the underlying ring (single-producer model already guarantees
 * that), but ALSO the partial-accept window of stream_buf_push gets
 * eliminated — `len > free_bytes` is detected and rejected before
 * any tail advance happens, so a caller catching "not all pushed"
 * can safely retry the entire `data` buffer.
 */
static inline int aiopquic_stream_ctx_send_data(
        aiopquic_stream_ctx_t* sc,
        const uint8_t* data, uint32_t len,
        uint32_t capacity, uint8_t set_fin) {
    if (!sc) return -1;
    if (!sc->tx) {
        sc->tx = aiopquic_stream_buf_create(capacity);
        if (!sc->tx) return -1;
    }
    if (len > 0) {
        /* All-or-nothing: pre-check free_bytes against full request,
         * then call push only when the whole request fits. push is
         * single-producer so there is no concurrent writer to consume
         * the room between the check and the push. */
        uint64_t tail = atomic_load_explicit(&sc->tx->tail,
                                             memory_order_relaxed);
        uint64_t head = atomic_load_explicit(&sc->tx->head,
                                             memory_order_acquire);
        uint32_t free_bytes = sc->tx->capacity - (uint32_t)(tail - head);
        if (free_bytes < len) {
            /* Arm the edge-trigger so the worker emits one
             * SPSC_EVT_STREAM_TX_DRAINED when it next drains bytes
             * from sc->tx. Caller awaits on the matching asyncio
             * Event instead of polling sleep. */
            atomic_store_explicit(&sc->tx_drain_pending, 1,
                                  memory_order_release);
            return 0;
        }
        uint32_t accepted = aiopquic_stream_buf_push(sc->tx, data, len);
        if (accepted != len) return 0;  /* defensive; shouldn't happen */
    }
    if (set_fin) {
        aiopquic_stream_buf_set_fin(sc->tx);
    }
    return 1;
}
