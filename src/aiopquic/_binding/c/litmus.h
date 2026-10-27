/*
 * Lost-wake litmus harness (test only).
 *
 * Each round puts one wake protocol in the state just before a race,
 * runs the two sides on two threads with a random skew, and checks the
 * outcome: a side that will wait must be woken, or work must not be left
 * where neither side will look. Returns the number of lost wakes.
 */
#ifndef AIOPQUIC_LITMUS_H
#define AIOPQUIC_LITMUS_H

#include "callback.h"
#include <pthread.h>
#include <sched.h>

enum {
    AIOPQUIC_LITMUS_RX_NOTIFY = 0,   /* notify_rx vs clear_rx */
    AIOPQUIC_LITMUS_TX_WAKE = 1,     /* tx_wake_set_pending vs worker clear */
    AIOPQUIC_LITMUS_RX_EVENT = 2,    /* rx_event_pending arm vs clear */
    AIOPQUIC_LITMUS_TX_DRAIN = 3,    /* send_data arm vs worker take */
    AIOPQUIC_LITMUS_TX_RING = 4,     /* TX event ring arm vs worker drain */
    AIOPQUIC_LITMUS_DGRAM = 5,       /* datagram push arm vs worker take */
    AIOPQUIC_LITMUS_RX_DRAINED = 6,  /* late notify_rx for a drained entry vs clear_rx */
};

typedef struct {
    _Atomic(uint32_t) count;
    _Atomic(uint32_t) gen;
} aiopquic_litmus_barrier_t;

static inline void aiopquic_litmus_barrier_wait(aiopquic_litmus_barrier_t* b) {
    uint32_t g = atomic_load(&b->gen);
    if (atomic_fetch_add(&b->count, 1) == 1) {
        atomic_store(&b->count, 0);
        atomic_fetch_add(&b->gen, 1);
        return;
    }
    uint32_t spins = 0;
    while (atomic_load(&b->gen) == g) {
        if (++spins > 4096) { sched_yield(); spins = 0; }
    }
}

typedef struct {
    int kind;
    uint64_t rounds;
    aiopquic_litmus_barrier_t bar;
    aiopquic_ctx_t* ctx;
    aiopquic_stream_ctx_t* sc;
    aiopquic_dgram_buf_t* db;
    int waits;      /* producer will wait for a wake */
    int woke;       /* producer posted a wake itself */
    uint64_t lost;
} aiopquic_litmus_t;

static inline uint32_t aiopquic_litmus_rand(uint32_t* s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    *s = x;
    return x;
}

static inline void aiopquic_litmus_skew(uint32_t* s) {
    uint32_t n = aiopquic_litmus_rand(s) & 63;
    for (volatile uint32_t i = 0; i < n; i++) { }
}

static inline int aiopquic_litmus_fd_readable(aiopquic_ctx_t* ctx) {
    int got = 0;
#ifdef __linux__
    uint64_t v;
    if (read(ctx->eventfd, &v, sizeof(v)) > 0) got = 1;
#else
    uint8_t buf[64];
    while (read(ctx->eventfd, buf, sizeof(buf)) > 0) got = 1;
#endif
    return got;
}

static inline void aiopquic_litmus_ring_clear(spsc_ring_t* r) {
    while (spsc_ring_peek(r) != NULL) spsc_ring_pop(r);
}

static inline void aiopquic_litmus_tx_ring_fill(aiopquic_ctx_t* ctx) {
    spsc_entry_t e = {0};
    e.event_type = SPSC_EVT_TX_SET_STREAM_PRIORITY;
    while (spsc_ring_push(ctx->tx_event_ring, &e, NULL, 0) == 0) { }
}

static inline void aiopquic_litmus_dgram_fill(aiopquic_dgram_buf_t* db) {
    uint8_t rec[8] = {0};
    while (aiopquic_dgram_buf_used(db) + AIOPQUIC_DGRAM_REC_HDR + sizeof(rec)
           <= db->capacity) {
        (void)aiopquic_dgram_buf_push_record(db, rec, sizeof(rec));
    }
}

/* Consumer thread, between rounds: reset to the pre-race state. */
static inline void aiopquic_litmus_setup(aiopquic_litmus_t* L) {
    aiopquic_ctx_t* ctx = L->ctx;
    L->waits = 0;
    L->woke = 0;
    switch (L->kind) {
    case AIOPQUIC_LITMUS_RX_NOTIFY:
        aiopquic_litmus_ring_clear(ctx->rx_event_ring);
        (void)aiopquic_litmus_fd_readable(ctx);
        __atomic_store_n(&ctx->rx_notify_pending, 1, __ATOMIC_SEQ_CST);
        break;
    case AIOPQUIC_LITMUS_TX_WAKE:
        aiopquic_litmus_ring_clear(ctx->tx_event_ring);
        __atomic_store_n(&ctx->tx_wake_pending, 1, __ATOMIC_SEQ_CST);
        break;
    case AIOPQUIC_LITMUS_RX_EVENT: {
        uint32_t used = aiopquic_stream_buf_used(L->sc->rx);
        uint8_t tmp[64];
        while (used > 0) {
            uint32_t k = used < sizeof(tmp) ? used : (uint32_t)sizeof(tmp);
            used -= aiopquic_stream_buf_pop(L->sc->rx, tmp, k);
        }
        atomic_store(&L->sc->rx_event_pending, 1);
        break;
    }
    case AIOPQUIC_LITMUS_TX_DRAIN: {
        uint8_t fill[64] = {0};
        while (aiopquic_stream_buf_free(L->sc->tx) > 0) {
            uint32_t f = aiopquic_stream_buf_free(L->sc->tx);
            (void)aiopquic_stream_buf_push(L->sc->tx, fill,
                                           f < sizeof(fill) ? f : (uint32_t)sizeof(fill));
        }
        atomic_store(&L->sc->tx_drain_pending, 0);
        break;
    }
    case AIOPQUIC_LITMUS_TX_RING:
        aiopquic_litmus_ring_clear(ctx->rx_event_ring);
        (void)aiopquic_litmus_fd_readable(ctx);
        aiopquic_litmus_tx_ring_fill(ctx);
        __atomic_store_n(&ctx->tx_event_ring_drain_pending, 0, __ATOMIC_SEQ_CST);
        __atomic_store_n(&ctx->rx_notify_pending, 0, __ATOMIC_SEQ_CST);
        break;
    case AIOPQUIC_LITMUS_DGRAM:
        aiopquic_litmus_dgram_fill(L->db);
        atomic_store(&L->db->drain_pending, 0);
        break;
    case AIOPQUIC_LITMUS_RX_DRAINED: {
        /* An earlier notify flipped pending and wrote the wake fd. */
        aiopquic_litmus_ring_clear(ctx->rx_event_ring);
        (void)aiopquic_litmus_fd_readable(ctx);
        __atomic_store_n(&ctx->rx_notify_pending, 1, __ATOMIC_SEQ_CST);
#ifdef __linux__
        uint64_t one = 1;
        (void)!write(ctx->wake_write_fd, &one, sizeof(one));
#else
        uint8_t b = 1;
        (void)!write(ctx->wake_write_fd, &b, 1);
#endif
        break;
    }
    }
}

/* Producer side of the race. */
static inline void aiopquic_litmus_produce(aiopquic_litmus_t* L) {
    aiopquic_ctx_t* ctx = L->ctx;
    spsc_entry_t e = {0};
    switch (L->kind) {
    case AIOPQUIC_LITMUS_RX_NOTIFY:
        e.event_type = SPSC_EVT_READY;
        (void)aiopquic_rx_push(ctx, &e, NULL, 0);
        break;
    case AIOPQUIC_LITMUS_TX_WAKE:
        e.event_type = SPSC_EVT_TX_SET_STREAM_PRIORITY;
        (void)spsc_ring_push(ctx->tx_event_ring, &e, NULL, 0);
        L->woke = aiopquic_tx_wake_set_pending(ctx) == 0;
        break;
    case AIOPQUIC_LITMUS_RX_EVENT: {
        uint8_t b[8] = {0};
        (void)aiopquic_stream_buf_push(L->sc->rx, b, sizeof(b));
        L->woke = aiopquic_stream_ctx_rx_event_pending_arm(L->sc);
        break;
    }
    case AIOPQUIC_LITMUS_TX_DRAIN: {
        uint8_t b[16] = {0};
        L->waits = aiopquic_stream_ctx_send_data(
            L->sc, b, sizeof(b), L->sc->tx->capacity, 0) == 0;
        break;
    }
    case AIOPQUIC_LITMUS_TX_RING:
        /* The TransportContext.send_stream_data pre-check. */
        if (spsc_ring_count(ctx->tx_event_ring) >= ctx->tx_event_ring->capacity) {
            L->waits = aiopquic_arm_tx_event_ring_drain_pending(ctx);
        }
        break;
    case AIOPQUIC_LITMUS_DGRAM: {
        uint8_t b[8] = {0};
        L->waits = aiopquic_dgram_buf_push_record(L->db, b, sizeof(b)) == 0;
        break;
    }
    case AIOPQUIC_LITMUS_RX_DRAINED: {
        /* Push and notify are separate steps: the consumer can drain the
         * entry before its notify runs. The delay spans clear_rx. */
        static uint32_t seed = 0x6a09e667u;
        e.event_type = SPSC_EVT_READY;
        (void)spsc_ring_push(ctx->rx_event_ring, &e, NULL, 0);
        uint32_t n = aiopquic_litmus_rand(&seed) & 2047;
        for (volatile uint32_t i = 0; i < n; i++) { }
        aiopquic_notify_rx(ctx);
        break;
    }
    }
}

/* Consumer side of the race; returns 1 on a lost wake. */
static inline int aiopquic_litmus_consume(aiopquic_litmus_t* L) {
    aiopquic_ctx_t* ctx = L->ctx;
    switch (L->kind) {
    case AIOPQUIC_LITMUS_RX_NOTIFY:
        aiopquic_clear_rx(ctx);
        return 0;
    case AIOPQUIC_LITMUS_TX_WAKE: {
        aiopquic_tx_wake_clear(ctx);
        int saw = spsc_ring_peek(ctx->tx_event_ring) != NULL;
        return 0x10 | saw;
    }
    case AIOPQUIC_LITMUS_RX_EVENT: {
        aiopquic_stream_ctx_rx_event_pending_clear(L->sc);
        int saw = aiopquic_stream_buf_used(L->sc->rx) > 0;
        return 0x10 | saw;
    }
    case AIOPQUIC_LITMUS_TX_DRAIN: {
        uint8_t tmp[64];
        uint32_t used = aiopquic_stream_buf_used(L->sc->tx);
        while (used > 0) {
            uint32_t k = used < sizeof(tmp) ? used : (uint32_t)sizeof(tmp);
            used -= aiopquic_stream_buf_pop(L->sc->tx, tmp, k);
        }
        return 0x20 | aiopquic_stream_ctx_tx_drain_take(L->sc);
    }
    case AIOPQUIC_LITMUS_TX_RING:
        while (spsc_ring_peek(ctx->tx_event_ring) != NULL) {
            spsc_ring_pop(ctx->tx_event_ring);
            aiopquic_maybe_fire_tx_event_ring_drained(ctx);
        }
        aiopquic_tx_event_ring_drained_final(ctx);
        return 0;
    case AIOPQUIC_LITMUS_DGRAM: {
        uint8_t tmp[64];
        uint32_t len;
        while ((len = aiopquic_dgram_buf_peek_len(L->db)) > 0) {
            aiopquic_dgram_buf_pop_into(L->db, tmp, len);
        }
        return 0x20 | aiopquic_dgram_buf_drain_take(L->db);
    }
    case AIOPQUIC_LITMUS_RX_DRAINED:
        while (spsc_ring_peek(ctx->rx_event_ring) == NULL) { }
        spsc_ring_pop(ctx->rx_event_ring);
        aiopquic_clear_rx(ctx);
        return 0;
    }
    return 0;
}

/* After both sides finished: 1 if a wake was lost. */
static inline int aiopquic_litmus_check(aiopquic_litmus_t* L, int c) {
    aiopquic_ctx_t* ctx = L->ctx;
    switch (L->kind) {
    case AIOPQUIC_LITMUS_RX_NOTIFY: {
        int pending = !spsc_ring_empty(ctx->rx_event_ring);
        return pending && !aiopquic_litmus_fd_readable(ctx);
    }
    case AIOPQUIC_LITMUS_TX_WAKE:
    case AIOPQUIC_LITMUS_RX_EVENT:
        return !L->woke && !(c & 1);
    case AIOPQUIC_LITMUS_TX_DRAIN:
    case AIOPQUIC_LITMUS_DGRAM:
        return L->waits && !(c & 1);
    case AIOPQUIC_LITMUS_RX_DRAINED: {
        /* pending=1 with no wake to read: every later push skips its write. */
        int pending = __atomic_load_n(&ctx->rx_notify_pending, __ATOMIC_SEQ_CST);
        return pending && !aiopquic_litmus_fd_readable(ctx);
    }
    case AIOPQUIC_LITMUS_TX_RING: {
        int fired = 0;
        spsc_entry_t* e;
        while ((e = spsc_ring_peek(ctx->rx_event_ring)) != NULL) {
            if (e->event_type == SPSC_EVT_TX_EVENT_RING_DRAINED) fired = 1;
            spsc_ring_pop(ctx->rx_event_ring);
        }
        return L->waits && !fired;
    }
    }
    return 0;
}

static void* aiopquic_litmus_producer(void* arg) {
    aiopquic_litmus_t* L = (aiopquic_litmus_t*)arg;
    uint32_t seed = 0x9e3779b9u;
    for (uint64_t i = 0; i < L->rounds; i++) {
        aiopquic_litmus_barrier_wait(&L->bar);   /* state ready */
        aiopquic_litmus_skew(&seed);
        aiopquic_litmus_produce(L);
        aiopquic_litmus_barrier_wait(&L->bar);   /* both done */
    }
    return NULL;
}

/* Runs `rounds` rounds of `kind`; returns lost wakes, or -1 on setup
 * failure. Blocks the caller until both threads finish. */
static inline int64_t aiopquic_litmus_run(int kind, uint64_t rounds) {
    aiopquic_litmus_t L;
    memset(&L, 0, sizeof(L));
    L.kind = kind;
    L.rounds = rounds;
    L.ctx = aiopquic_ctx_create(16, 64, 50);
    L.sc = aiopquic_stream_ctx_create();
    L.db = aiopquic_dgram_buf_create(64, 8);
    if (!L.ctx || !L.sc || !L.db
            || aiopquic_stream_ctx_ensure_tx(L.sc, 64) != 0
            || aiopquic_stream_ctx_ensure_rx(L.sc, 64) != 0) {
        aiopquic_ctx_destroy(L.ctx);
        aiopquic_stream_ctx_destroy(L.sc);
        if (L.db) aiopquic_dgram_buf_unref(L.db);
        return -1;
    }
    /* No worker runs: rx_push wakes go to the eventfd only. */
    L.ctx->thread_ctx = NULL;
    pthread_t th;
    aiopquic_litmus_setup(&L);
    if (pthread_create(&th, NULL, aiopquic_litmus_producer, &L) != 0) {
        aiopquic_ctx_destroy(L.ctx);
        aiopquic_stream_ctx_destroy(L.sc);
        aiopquic_dgram_buf_unref(L.db);
        return -1;
    }
    uint32_t seed = 0x2545f491u;
    for (uint64_t i = 0; i < rounds; i++) {
        aiopquic_litmus_barrier_wait(&L.bar);
        aiopquic_litmus_skew(&seed);
        int c = aiopquic_litmus_consume(&L);
        aiopquic_litmus_barrier_wait(&L.bar);
        L.lost += (uint64_t)aiopquic_litmus_check(&L, c);
        aiopquic_litmus_setup(&L);
    }
    pthread_join(th, NULL);
    int64_t lost = (int64_t)L.lost;
    aiopquic_ctx_destroy(L.ctx);
    aiopquic_stream_ctx_destroy(L.sc);
    aiopquic_dgram_buf_unref(L.db);
    return lost;
}

#endif /* AIOPQUIC_LITMUS_H */
