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
#include <time.h>

/* Inline helper for ns-resolution monotonic timestamps. CLOCK_MONOTONIC
 * is a single time-base across worker thread and asyncio thread on
 * Linux/macOS, making counter-event ordering directly comparable.
 * Defined here (rather than in callback.h) because stream_ctx.h is
 * included by callback.h — putting it here keeps the timestamp helper
 * usable from both header trees. */
static inline uint64_t aiopquic_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

typedef struct {
    aiopquic_stream_buf_t* tx;
    aiopquic_stream_buf_t* rx;
    /* Cumulative bytes Python has drained from the RX ring. Atomic
     * because the picoquic worker thread reads it from inside the
     * stream_data callback to decide when to extend MAX_STREAM_DATA,
     * while Python writes it after each drain_rx pop. Acquire/release
     * ordering ensures the worker sees Python's drain progress. */
    _Atomic(uint64_t) rx_consumed;
    /* Bytes that drain_rx has popped from sc->rx and wrapped into a
     * StreamChunk for delivery to the Python consumer, but whose
     * StreamChunk has NOT yet been released (last memoryview ref
     * dropped → __dealloc__ fired).
     *
     * Incremented by StreamChunk._wrap (asyncio thread); decremented
     * by StreamChunk.__dealloc__ (asyncio thread). Read by the picoquic
     * worker inside the SPSC_EVT_TX_OPEN_FLOW_CONTROL handler to
     * compute the effective MAX_STREAM_DATA grant:
     *   effective_free = max(0, sc->rx_buf_free - bytes_pending_release)
     * Acquire/release ordering ensures the worker sees Python's
     * release progress.
     *
     * Conflates two populations the application may produce:
     *   (1) bytes in the dispatch pipeline (parser still chewing)
     *   (2) bytes the app intentionally retained beyond delivery
     *       (archiving, deferred processing)
     * aiopquic cannot distinguish without an explicit release API
     * (deferred); both manifest as live-StreamChunk bytes and both
     * propagate as peer-side backpressure — which is correct: peer
     * flow naturally matches actual end-to-end consumption rate. */
    _Atomic(uint64_t) bytes_pending_release;
    /* Last MAX_STREAM_DATA limit advertised by the worker — kept here
     * (not in Python) so the worker-side hysteresis check needs no
     * additional state. Written only by the picoquic worker. */
    _Atomic(uint64_t) rx_credit_limit;
    /* Hysteresis gate for asyncio-side fc_credit push: peer's
     * consumed_offset (sc->rx_consumed) the last time we actually
     * pushed an SPSC_EVT_TX_OPEN_FLOW_CONTROL. Skip subsequent
     * pushes until consumed advances by at least
     * AIOPQUIC_RX_FC_HYSTERESIS_BYTES (default = ring_cap / 4).
     * Asyncio thread reads + writes; relaxed semantics OK because
     * a missed push just defers credit by one event — the next
     * event will catch up. */
    _Atomic(uint64_t) last_fc_push_consumed;
    /* Edge-triggered TX backpressure signal. Python sets this to 1
     * when a send_data attempt returns 0 (sc->tx full). The picoquic
     * worker thread CAS-clears it from 1->0 after popping bytes in
     * prepare_to_send; the CAS winner emits one SPSC event so the
     * blocked Python writer wakes and retries. Only one event fires
     * per fill->drain cycle. */
    _Atomic(uint32_t) tx_drain_pending;
    /* fin/reset arrived; free wrapper after final drain. */
    uint8_t  pending_destroy;
    /* Observability counters (single-writer; relaxed semantics).
     * Aligned with the connection-global ring_event counters in
     * aiopquic_ctx_t so the SIGUSR2 dump can correlate per-stream
     * sc->tx drain activity with cnx-wide ring activity at the same
     * monotonic timestamp. */
    uint64_t cnt_drain_arms;        /* Python armed sc->tx_drain_pending */
    uint64_t cnt_drain_fires;       /* worker fired SPSC_EVT_STREAM_TX_DRAINED */
    uint64_t cnt_drain_dropped;     /* CAS won but rx_ring push failed → re-arm */
    uint64_t last_drain_arm_ns;     /* CLOCK_MONOTONIC of last arm */
    uint64_t last_drain_fire_ns;    /* CLOCK_MONOTONIC of last fire */
    /* Reference count for lifecycle management. Starts at 1 (the
     * "stream lifetime" ref, dropped via SPSC_EVT_STREAM_DESTROY or
     * explicit teardown). Each live StreamChunk holds +1 (so a
     * chunk outliving the stream FIN keeps sc alive for __dealloc__
     * to safely touch sc->bytes_pending_release). Each pending
     * SPSC_EVT_TX_OPEN_FLOW_CONTROL event in flight holds +1 (so
     * the worker's handler can safely deref sc even if all chunks
     * dealloc'd after the push).
     *
     * aiopquic_stream_ctx_destroy (the public free entry point) is
     * the unref operation: decrements refcount and frees only when
     * the count hits zero. All call sites can use destroy() with
     * "drop my reference" semantics. */
    _Atomic(uint32_t) refcount;
} aiopquic_stream_ctx_t;

/* Global lifecycle counters (process-wide). Used by dump_counters() to
 * detect sc leaks: cnt_sc_created - cnt_sc_destroyed = live sc count.
 * Single-writer-per-cell is NOT true here (multi-cnx), so atomic.
 * Single-TU build (_transport.c) lets us define storage directly. */
static _Atomic(uint64_t) aiopquic_cnt_sc_created = 0;
static _Atomic(uint64_t) aiopquic_cnt_sc_destroyed = 0;
static _Atomic(int64_t)  aiopquic_cnt_chunks_alive = 0;

static inline void aiopquic_chunks_alive_inc(void) {
    atomic_fetch_add_explicit(&aiopquic_cnt_chunks_alive, 1,
                               memory_order_relaxed);
}
static inline void aiopquic_chunks_alive_dec(void) {
    atomic_fetch_sub_explicit(&aiopquic_cnt_chunks_alive, 1,
                               memory_order_relaxed);
}
static inline uint64_t aiopquic_cnt_sc_created_load(void) {
    return atomic_load_explicit(&aiopquic_cnt_sc_created,
                                 memory_order_relaxed);
}
static inline uint64_t aiopquic_cnt_sc_destroyed_load(void) {
    return atomic_load_explicit(&aiopquic_cnt_sc_destroyed,
                                 memory_order_relaxed);
}
static inline int64_t aiopquic_cnt_chunks_alive_load(void) {
    return atomic_load_explicit(&aiopquic_cnt_chunks_alive,
                                 memory_order_relaxed);
}

/* sc->rx byte-ring in-flight tracker. Worker thread bumps on push;
 * Cython asyncio drain bumps on pop. Delta = bytes currently sitting
 * in sc->rx rings across ALL streams in the process. Reveals when
 * the RX ring layer is accumulating (consumer stalled) vs not. */
static _Atomic(uint64_t) aiopquic_cnt_sc_rx_bytes_pushed = 0;
static _Atomic(uint64_t) aiopquic_cnt_sc_rx_bytes_popped = 0;

static inline void aiopquic_sc_rx_bytes_pushed_add(uint64_t n) {
    atomic_fetch_add_explicit(&aiopquic_cnt_sc_rx_bytes_pushed, n,
                               memory_order_relaxed);
}
static inline void aiopquic_sc_rx_bytes_popped_add(uint64_t n) {
    atomic_fetch_add_explicit(&aiopquic_cnt_sc_rx_bytes_popped, n,
                               memory_order_relaxed);
}
static inline uint64_t aiopquic_cnt_sc_rx_bytes_pushed_load(void) {
    return atomic_load_explicit(&aiopquic_cnt_sc_rx_bytes_pushed,
                                 memory_order_relaxed);
}
static inline uint64_t aiopquic_cnt_sc_rx_bytes_popped_load(void) {
    return atomic_load_explicit(&aiopquic_cnt_sc_rx_bytes_popped,
                                 memory_order_relaxed);
}

static inline aiopquic_stream_ctx_t* aiopquic_stream_ctx_create(void) {
    aiopquic_stream_ctx_t* sc = (aiopquic_stream_ctx_t*)calloc(
        1, sizeof(aiopquic_stream_ctx_t));
    if (sc) {
        /* Initial "stream lifetime" reference. Released by either
         * SPSC_EVT_STREAM_DESTROY (raw QUIC pure receiver), the WT
         * link-release path, or any explicit Python stream_ctx_destroy
         * call. Additional refs come and go via _ref/_destroy as
         * StreamChunks are created and freed. */
        atomic_store_explicit(&sc->refcount, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&aiopquic_cnt_sc_created, 1,
                                   memory_order_relaxed);
    }
    return sc;
}

/* Take an additional reference. Called when a StreamChunk wraps
 * bytes whose lifetime might exceed the stream's, or when an SPSC
 * event is queued whose handler needs sc to still be alive when
 * it runs. */
static inline void aiopquic_stream_ctx_ref(aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    atomic_fetch_add_explicit(&sc->refcount, 1, memory_order_acquire);
}

/* Python-side arm for the per-stream sc->tx drain signal. Mirrors
 * the connection-global aiopquic_arm_tx_ring_drain_pending in
 * callback.h. Use when the Python writer wants to wait for sc->tx
 * to drain at a soft threshold (e.g. a byte-budget cap below the
 * hard sc->tx ring capacity) rather than waiting for the natural
 * BufferError at full capacity. */
static inline void aiopquic_arm_stream_tx_drain_pending(
        aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    sc->cnt_drain_arms++;
    sc->last_drain_arm_ns = aiopquic_now_ns();
    atomic_store_explicit(&sc->tx_drain_pending, 1,
                          memory_order_release);
}

static inline void aiopquic_clear_stream_tx_drain_pending(
        aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    atomic_store_explicit(&sc->tx_drain_pending, 0,
                          memory_order_release);
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

/* Drop one reference; free tx/rx rings and the wrapper itself only
 * when this was the LAST reference. Idempotent on NULL. Safe to
 * call from any thread (asyncio drain, picoquic worker, StreamChunk
 * __dealloc__) because the refcount manipulation is atomic and the
 * "is this the final ref" test uses the return value of
 * atomic_fetch_sub. Replacement for the old free-immediately
 * semantics; existing callers transparently get the right behavior
 * (initial-ref-only callers free immediately; multi-ref callers
 * release lazily). */
static inline void aiopquic_stream_ctx_destroy(aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    uint32_t prev = atomic_fetch_sub_explicit(&sc->refcount, 1,
                                               memory_order_acq_rel);
    if (prev != 1) {
        /* Other refs still outstanding; defer actual free until they
         * each drop in turn. */
        return;
    }
    if (sc->tx) aiopquic_stream_buf_destroy(sc->tx);
    if (sc->rx) aiopquic_stream_buf_destroy(sc->rx);
    free(sc);
    atomic_fetch_add_explicit(&aiopquic_cnt_sc_destroyed, 1,
                               memory_order_relaxed);
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

/* Per-stream "bytes pending release" accessors. Producer is
 * StreamChunk._wrap (asyncio thread); consumer is StreamChunk.__dealloc__
 * (same thread). Worker thread reads via load() during FC handler. */
static inline void aiopquic_stream_ctx_pending_add(
        aiopquic_stream_ctx_t* sc, uint64_t n) {
    if (!sc || n == 0) return;
    atomic_fetch_add_explicit(&sc->bytes_pending_release, n,
                              memory_order_release);
}

static inline void aiopquic_stream_ctx_pending_sub(
        aiopquic_stream_ctx_t* sc, uint64_t n) {
    if (!sc || n == 0) return;
    atomic_fetch_sub_explicit(&sc->bytes_pending_release, n,
                              memory_order_release);
}

static inline uint64_t aiopquic_stream_ctx_pending_load(
        aiopquic_stream_ctx_t* sc) {
    if (!sc) return 0;
    return atomic_load_explicit(&sc->bytes_pending_release,
                                memory_order_acquire);
}

/* last_fc_push_consumed accessors. Hysteresis state for the
 * asyncio-side _push_fc_credit gate in _transport.pyx. */
static inline uint64_t aiopquic_stream_ctx_last_fc_push_consumed_load(
        aiopquic_stream_ctx_t* sc) {
    if (!sc) return 0;
    return atomic_load_explicit(&sc->last_fc_push_consumed,
                                memory_order_relaxed);
}

static inline void aiopquic_stream_ctx_last_fc_push_consumed_store(
        aiopquic_stream_ctx_t* sc, uint64_t v) {
    if (!sc) return;
    atomic_store_explicit(&sc->last_fc_push_consumed, v,
                          memory_order_relaxed);
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
            sc->cnt_drain_arms++;
            sc->last_drain_arm_ns = aiopquic_now_ns();
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
