/*
 * h3wt_callback.h — H3/WebTransport adapter callbacks.
 *
 * Bridges picoquic's per-stream picohttp_call_back_event_t events
 * (delivered by the h3zero/picowt machinery) into the aiopquic SPSC
 * ring as SPSC_EVT_WT_* events for the asyncio thread to consume.
 *
 * One wt_session_ctx_t per WebTransport session. The session points
 * back to the parent aiopquic_ctx_t so the callback can push events
 * to the shared rx_ring; the rest of the picowt machinery lives in
 * picoquic (h3_ctx, control_stream_ctx) and we hold opaque pointers.
 *
 * Copyright (c) 2026, aiopquic contributors. BSD-3-Clause license.
 */

#ifndef AIOPQUIC_H3WT_CALLBACK_H
#define AIOPQUIC_H3WT_CALLBACK_H

#include "callback.h"
#include "spsc_ring.h"

#include <picoquic.h>
#include <h3zero_common.h>
#include <pico_webtransport.h>

#include <string.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Packed payload for SPSC_EVT_TX_WT_OPEN. Stored as data_buf:
 * header + sni + path. ALPN is always "h3" for WT, not carried.
 */
typedef struct {
    struct sockaddr_in addr;
    uint16_t sni_len;
    uint16_t path_len;
    /* followed by: sni_len bytes of SNI, then path_len bytes of path */
} aiopquic_wt_open_params_t;

/*
 * Per-WT-stream RX context side-table. Mirrors raw QUIC's
 * picoquic_set_app_stream_ctx-attached aiopquic_stream_ctx_t — but
 * WT streams are owned by h3zero (its stream_ctx slot is taken), so
 * we keep our per-stream context in a side-table on the WT session.
 *
 * Open-addressed hash table with linear probing. O(1) lookup
 * regardless of total streams ever seen — critical at multi-Gbps
 * churn where the table can grow to 10K+ entries inside a 20s window.
 * Power-of-two slot count for fast masking. Capacity 4096 supports
 * up to ~3000 simultaneously-active streams with low load factor;
 * resized dynamically if it ever fills.
 */
#define AIOPQUIC_WT_STREAM_TABLE_INIT (4096u)
#define AIOPQUIC_WT_STREAM_SLOT_EMPTY     (~(uint64_t)0)
#define AIOPQUIC_WT_STREAM_SLOT_TOMBSTONE (~(uint64_t)1)

typedef struct st_aiopquic_wt_stream_slot {
    uint64_t                  stream_id;  /* EMPTY/TOMBSTONE sentinels */
    aiopquic_stream_ctx_t*    sc;
} aiopquic_wt_stream_slot_t;

typedef struct st_aiopquic_wt_stream_table {
    aiopquic_wt_stream_slot_t*  slots;
    uint32_t                    capacity;   /* power of two */
    uint32_t                    mask;
    uint32_t                    count;      /* live entries */
    uint32_t                    tombstones;
} aiopquic_wt_stream_table_t;

static inline int aiopquic_wt_stream_table_init(
        aiopquic_wt_stream_table_t* t) {
    t->capacity = AIOPQUIC_WT_STREAM_TABLE_INIT;
    t->mask = t->capacity - 1;
    t->count = 0;
    t->tombstones = 0;
    t->slots = (aiopquic_wt_stream_slot_t*)calloc(
        t->capacity, sizeof(*t->slots));
    if (!t->slots) return -1;
    for (uint32_t i = 0; i < t->capacity; i++) {
        t->slots[i].stream_id = AIOPQUIC_WT_STREAM_SLOT_EMPTY;
    }
    return 0;
}

static inline void aiopquic_wt_stream_table_destroy(
        aiopquic_wt_stream_table_t* t) {
    if (!t || !t->slots) return;
    for (uint32_t i = 0; i < t->capacity; i++) {
        if (t->slots[i].stream_id != AIOPQUIC_WT_STREAM_SLOT_EMPTY
                && t->slots[i].stream_id != AIOPQUIC_WT_STREAM_SLOT_TOMBSTONE
                && t->slots[i].sc) {
            aiopquic_stream_ctx_destroy(t->slots[i].sc);
        }
    }
    free(t->slots);
    t->slots = NULL;
}

/* Hash: stream_id is already monotonically increasing by 4; xor-fold
 * to spread the bottom bits across the slot space. */
static inline uint32_t aiopquic_wt_stream_hash(uint64_t stream_id,
                                                  uint32_t mask) {
    uint64_t h = stream_id * 0x9E3779B97F4A7C15ULL;
    h ^= h >> 32;
    return (uint32_t)h & mask;
}

static inline int aiopquic_wt_stream_table_rehash(
        aiopquic_wt_stream_table_t* t, uint32_t new_capacity);

static inline aiopquic_stream_ctx_t* aiopquic_wt_stream_table_find(
        aiopquic_wt_stream_table_t* t, uint64_t stream_id) {
    uint32_t i = aiopquic_wt_stream_hash(stream_id, t->mask);
    for (uint32_t probe = 0; probe <= t->mask; probe++) {
        uint32_t slot = (i + probe) & t->mask;
        uint64_t sid = t->slots[slot].stream_id;
        if (sid == AIOPQUIC_WT_STREAM_SLOT_EMPTY) return NULL;
        if (sid == stream_id) return t->slots[slot].sc;
        /* tombstone: keep probing */
    }
    return NULL;
}

static inline int aiopquic_wt_stream_table_insert(
        aiopquic_wt_stream_table_t* t, uint64_t stream_id,
        aiopquic_stream_ctx_t* sc) {
    /* Resize at 50% load to keep linear-probe runs short. */
    if ((t->count + t->tombstones) * 2 >= t->capacity) {
        if (aiopquic_wt_stream_table_rehash(t, t->capacity * 2) != 0) {
            return -1;
        }
    }
    uint32_t i = aiopquic_wt_stream_hash(stream_id, t->mask);
    for (uint32_t probe = 0; probe <= t->mask; probe++) {
        uint32_t slot = (i + probe) & t->mask;
        uint64_t sid = t->slots[slot].stream_id;
        if (sid == AIOPQUIC_WT_STREAM_SLOT_EMPTY) {
            t->slots[slot].stream_id = stream_id;
            t->slots[slot].sc = sc;
            t->count++;
            return 0;
        }
        if (sid == AIOPQUIC_WT_STREAM_SLOT_TOMBSTONE) {
            t->slots[slot].stream_id = stream_id;
            t->slots[slot].sc = sc;
            t->count++;
            t->tombstones--;
            return 0;
        }
        if (sid == stream_id) {
            /* Already present — should not happen for first-time
             * insert. Caller bug; overwrite anyway. */
            t->slots[slot].sc = sc;
            return 0;
        }
    }
    return -1; /* Table truly full — caller should never see this. */
}

static inline void aiopquic_wt_stream_table_remove(
        aiopquic_wt_stream_table_t* t, uint64_t stream_id) {
    uint32_t i = aiopquic_wt_stream_hash(stream_id, t->mask);
    for (uint32_t probe = 0; probe <= t->mask; probe++) {
        uint32_t slot = (i + probe) & t->mask;
        uint64_t sid = t->slots[slot].stream_id;
        if (sid == AIOPQUIC_WT_STREAM_SLOT_EMPTY) return;
        if (sid == stream_id) {
            if (t->slots[slot].sc) {
                aiopquic_stream_ctx_destroy(t->slots[slot].sc);
                t->slots[slot].sc = NULL;
            }
            t->slots[slot].stream_id = AIOPQUIC_WT_STREAM_SLOT_TOMBSTONE;
            t->count--;
            t->tombstones++;
            return;
        }
    }
}

static inline int aiopquic_wt_stream_table_rehash(
        aiopquic_wt_stream_table_t* t, uint32_t new_capacity) {
    aiopquic_wt_stream_slot_t* old_slots = t->slots;
    uint32_t old_capacity = t->capacity;
    aiopquic_wt_stream_slot_t* new_slots =
        (aiopquic_wt_stream_slot_t*)calloc(
            new_capacity, sizeof(*new_slots));
    if (!new_slots) return -1;
    for (uint32_t i = 0; i < new_capacity; i++) {
        new_slots[i].stream_id = AIOPQUIC_WT_STREAM_SLOT_EMPTY;
    }
    t->slots = new_slots;
    t->capacity = new_capacity;
    t->mask = new_capacity - 1;
    uint32_t saved_count = 0;
    t->count = 0;
    t->tombstones = 0;
    for (uint32_t i = 0; i < old_capacity; i++) {
        uint64_t sid = old_slots[i].stream_id;
        if (sid != AIOPQUIC_WT_STREAM_SLOT_EMPTY
                && sid != AIOPQUIC_WT_STREAM_SLOT_TOMBSTONE) {
            (void)aiopquic_wt_stream_table_insert(t, sid, old_slots[i].sc);
            saved_count++;
        }
    }
    free(old_slots);
    return 0;
}

/*
 * Per-WebTransport-session context. Lives for the lifetime of the
 * WT session. Allocated when the asyncio side requests a WT session
 * open; freed on session-closed event after Python releases its
 * reference.
 */
typedef struct st_aiopquic_wt_session_t {
    aiopquic_ctx_t*               bridge;          /* shared rx/tx rings + eventfd */
    picoquic_cnx_t*               cnx;             /* per-session QUIC cnx */
    h3zero_callback_ctx_t*        h3_ctx;          /* picoquic-owned H3 ctx */
    h3zero_stream_ctx_t*          control_stream;  /* picoquic-owned control stream ctx */
    uint64_t                      control_stream_id;
    picowt_capsule_t              capsule;         /* incremental capsule accumulator */
    int                           session_ready;   /* CONNECT accepted */
    int                           session_closing; /* close/drain seen or initiated */
    aiopquic_wt_stream_table_t    stream_rx;       /* per-stream RX byte rings */
    /* Hot-path cache: the most recently accessed (sid, sc). On the
     * SP/MP stress workload the same stream is touched ~60+ times
     * in a row, so a single-slot cache hits the common case before
     * the hash lookup. */
    uint64_t                      stream_rx_cache_id;
    aiopquic_stream_ctx_t*        stream_rx_cache_sc;
} aiopquic_wt_session_t;

/*
 * Find (or create) the per-stream RX context for a WT stream. Called
 * by the picoquic worker thread on every post_data callback; safe
 * because the table is only modified by the worker. Returns NULL on
 * allocation failure.
 */
static inline aiopquic_stream_ctx_t* aiopquic_wt_session_find_or_create_stream_rx(
        aiopquic_wt_session_t* s, uint64_t stream_id) {
    /* Hot-path: same stream as last callback. */
    if (s->stream_rx_cache_sc && s->stream_rx_cache_id == stream_id) {
        return s->stream_rx_cache_sc;
    }
    aiopquic_stream_ctx_t* sc =
        aiopquic_wt_stream_table_find(&s->stream_rx, stream_id);
    if (sc) {
        s->stream_rx_cache_id = stream_id;
        s->stream_rx_cache_sc = sc;
        return sc;
    }
    sc = aiopquic_stream_ctx_create();
    if (!sc) return NULL;
    if (aiopquic_wt_stream_table_insert(&s->stream_rx, stream_id, sc) != 0) {
        aiopquic_stream_ctx_destroy(sc);
        return NULL;
    }
    s->stream_rx_cache_id = stream_id;
    s->stream_rx_cache_sc = sc;
    return sc;
}

/*
 * Lookup-only variant. Returns NULL if no per-stream RX context
 * exists for this stream_id. Used on stream_reset / stop_sending
 * paths where we don't want to lazy-allocate.
 */
static inline aiopquic_stream_ctx_t* aiopquic_wt_session_find_stream_rx(
        aiopquic_wt_session_t* s, uint64_t stream_id) {
    if (s->stream_rx_cache_sc && s->stream_rx_cache_id == stream_id) {
        return s->stream_rx_cache_sc;
    }
    return aiopquic_wt_stream_table_find(&s->stream_rx, stream_id);
}

/*
 * Remove + destroy the per-stream RX context for stream_id. Called
 * from picoquic worker thread on stream_reset / stop_sending /
 * post_fin once asyncio has drained. The caller must ensure no
 * outstanding ring entry still references this sc (Phase B path
 * pushes only one event per stream-data chunk, and asyncio drains
 * synchronously inside drain_rx).
 */
static inline void aiopquic_wt_session_remove_stream_rx(
        aiopquic_wt_session_t* s, uint64_t stream_id) {
    if (s->stream_rx_cache_id == stream_id) {
        s->stream_rx_cache_id = 0;
        s->stream_rx_cache_sc = NULL;
    }
    aiopquic_wt_stream_table_remove(&s->stream_rx, stream_id);
}

static inline aiopquic_wt_session_t* aiopquic_wt_session_create(
        aiopquic_ctx_t* bridge) {
    aiopquic_wt_session_t* s =
        (aiopquic_wt_session_t*)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->bridge = bridge;
    if (aiopquic_wt_stream_table_init(&s->stream_rx) != 0) {
        free(s);
        return NULL;
    }
    return s;
}

static inline void aiopquic_wt_session_destroy(aiopquic_wt_session_t* s) {
    if (!s) return;
    picowt_release_capsule(&s->capsule);
    aiopquic_wt_stream_table_destroy(&s->stream_rx);
    free(s);
}

/*
 * Phase B WT push: notification event carrying a BORROWED pointer to
 * the per-stream aiopquic_stream_ctx_t. No inline byte copy — bytes
 * live in sc->rx and the consumer (asyncio drain_rx) pops them
 * directly. entry.data_length=0 signals "borrowed" to spsc_ring_pop
 * which then skips the free.
 *
 * Used for SPSC_EVT_WT_STREAM_DATA and SPSC_EVT_WT_STREAM_FIN where
 * we want true flow control: byte-ring fullness blocks the publisher
 * via lack of MAX_STREAM_DATA extension.
 */
static inline void aiopquic_wt_push_event_with_sc(
        aiopquic_wt_session_t* s,
        uint32_t event_type,
        uint64_t stream_id,
        aiopquic_stream_ctx_t* sc,
        uint8_t is_fin) {
    spsc_entry_t entry = {0};
    entry.event_type = event_type;
    entry.stream_id = stream_id;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;     /* session ptr for asyncio routing */
    entry.data_buf = sc;      /* borrowed; freed by session_destroy */
    entry.data_length = 0;    /* sentinel: do not free on pop */
    entry.is_fin = is_fin;
    int ret = spsc_ring_push_borrowed(s->bridge->rx_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        /* RX EVENT RING FULL — the bytes are in sc->rx but the
         * notification event dropped. asyncio will only catch up on
         * the next event for the same stream that DOES make it into
         * the ring (drain_rx will pop available sc->rx bytes then).
         * This matches the raw-QUIC sc->rx invariant. */
        s->bridge->worker_rx_event_drops++;
        if (event_type == SPSC_EVT_WT_STREAM_DATA) {
            s->bridge->worker_rx_event_drops_stream_data++;
        }
        if (aiopquic_rx_log_enabled()
                && s->bridge->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] WT EVENT RING FULL (sc): drop "
                "stream=%llu evt=%u (rx_ring entries=%u)\n",
                (unsigned long long)stream_id, event_type,
                spsc_ring_count(s->bridge->rx_ring));
        }
    }
}

/*
 * Push a WT event to the bridge's rx_ring. data may be NULL/0 for
 * pure events. Caller (picoquic thread) must ensure session is set.
 */
static inline void aiopquic_wt_push_event(
        aiopquic_wt_session_t* s,
        uint32_t event_type,
        uint64_t stream_id,
        uint64_t error_code,
        const uint8_t* data,
        uint32_t data_len) {
    spsc_entry_t entry = {0};
    entry.event_type = event_type;
    entry.stream_id = stream_id;
    entry.error_code = error_code;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;       /* session ptr for demux */
    int ret = spsc_ring_push(s->bridge->rx_ring, &entry, data, data_len);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        /* RX EVENT RING FULL — bytes for WT events live ONLY in
         * the ring entry (no per-stream fallback), so a drop here
         * is a hard data loss. Surface via shared counter; log
         * one-shot under AIOPQUIC_RX_LOG=1. */
        s->bridge->worker_rx_event_drops++;
        if (event_type == SPSC_EVT_WT_STREAM_DATA) {
            s->bridge->worker_rx_event_drops_stream_data++;
        }
        if (aiopquic_rx_log_enabled()
                && s->bridge->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] WT EVENT RING FULL: drop "
                "stream=%llu evt=%u (rx_ring entries=%u)\n",
                (unsigned long long)stream_id, event_type,
                spsc_ring_count(s->bridge->rx_ring));
        }
    }
}

/*
 * The picohttp_post_data_cb_fn registered via picowt_connect.
 * Runs in the picoquic network thread.
 *
 * Translates picohttp_call_back_event_t → SPSC_EVT_WT_* and pushes
 * to rx_ring. Special-cases the control stream: data on the control
 * stream is capsule bytes; we feed it to picowt_receive_capsule and
 * surface CLOSE/DRAIN as session events instead of stream events.
 */
static int aiopquic_wt_path_callback(
        picoquic_cnx_t* cnx,
        uint8_t* bytes, size_t length,
        picohttp_call_back_event_t event,
        h3zero_stream_ctx_t* stream_ctx,
        void* path_app_ctx) {
    aiopquic_wt_session_t* s = (aiopquic_wt_session_t*)path_app_ctx;
    if (!s) return 0;

    int is_control = (stream_ctx != NULL &&
                       stream_ctx->stream_id == s->control_stream_id);
    uint64_t sid = stream_ctx ? stream_ctx->stream_id : 0;

    switch (event) {
    case picohttp_callback_connecting:
        /* Client-side: we just sent CONNECT. No event needed.
         * Acceptance comes via connect_accepted. */
        break;

    case picohttp_callback_connect_accepted:
        s->session_ready = 1;
        aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_READY,
                                s->control_stream_id, 0, NULL, 0);
        break;

    case picohttp_callback_connect_refused:
        aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                s->control_stream_id, 0, NULL, 0);
        break;

    case picohttp_callback_post_data:
        if (is_control) {
            /* Capsule bytes on the control stream. Feed the
             * accumulator; surface CLOSE/DRAIN as session events. */
            int rc = picowt_receive_capsule(cnx, bytes, bytes + length,
                                              &s->capsule);
            if (rc == 0 && s->capsule.h3_capsule.is_stored) {
                uint64_t ctype = s->capsule.h3_capsule.capsule_type;
                if (ctype == picowt_capsule_close_webtransport_session) {
                    aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                        s->control_stream_id, s->capsule.error_code,
                        s->capsule.error_msg, (uint32_t)s->capsule.error_msg_len);
                    s->session_closing = 1;
                } else if (ctype == picowt_capsule_drain_webtransport_session) {
                    aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_DRAINING,
                        s->control_stream_id, 0, NULL, 0);
                }
                /* Reset accumulator for next capsule. */
                picowt_release_capsule(&s->capsule);
                memset(&s->capsule, 0, sizeof(s->capsule));
            }
        } else {
            /* Phase B: per-stream byte ring + app-driven flow control.
             * Bytes go into sc->rx (owned by the WT session), not into
             * the SPSC ring entry itself. picoquic_open_flow_control
             * is extended ONLY as Python's drain advances `consumed`,
             * so a slow consumer back-pressures the publisher via
             * MAX_STREAM_DATA exhaustion — no UDP-buffer-overrun loss.
             */
            aiopquic_stream_ctx_t* sc =
                aiopquic_wt_session_find_or_create_stream_rx(s, sid);
            if (!sc) return -1;

            uint32_t advertise_cap =
                s->bridge->rx_ring_cap > 0
                    ? s->bridge->rx_ring_cap
                    : AIOPQUIC_RX_RING_CAP_DEFAULT;
            uint32_t fc_threshold = advertise_cap / AIOPQUIC_RX_FC_THRESHOLD_DIV;
            int first_touch = (sc->rx == NULL);
            if (first_touch) {
                /* Opt into application-driven flow control on this
                 * stream and prime the initial credit. picoquic's
                 * h3zero may already have advanced its own flow
                 * control internally; this overrides with our
                 * drain-driven value. */
                (void)picoquic_set_app_flow_control(cnx, sid, 1);
                (void)picoquic_open_flow_control(cnx, sid, advertise_cap);
                aiopquic_stream_ctx_rx_credit_store(sc, advertise_cap);
            }
            if (aiopquic_stream_ctx_ensure_rx(sc, advertise_cap) != 0) {
                return -1;
            }
            uint32_t pushed = aiopquic_stream_buf_push(
                sc->rx, bytes, (uint32_t)length);
            if (pushed != length) {
                /* Per-stream RX ring overflow: peer exceeded its
                 * advertised flow-control window. Spec-correct
                 * FLOW_CONTROL_ERROR connection close, but we let
                 * the worker continue and just count the event. */
                s->bridge->worker_rx_byte_ring_overflow++;
                if (aiopquic_rx_log_enabled()
                        && s->bridge->worker_rx_byte_ring_overflow <= 16) {
                    fprintf(stderr,
                        "[aiopquic_rx] WT stream=%llu RX ring overflow: "
                        "pushed %u of %zu (free=%u, advertise=%u). "
                        "Peer flow-control violation.\n",
                        (unsigned long long)sid, pushed, length,
                        aiopquic_stream_buf_free(sc->rx), advertise_cap);
                }
                return -1;
            }
            /* Hysteresis-bound MAX_STREAM_DATA extension: only when
             * drained consumed has advanced by more than fc_threshold
             * past the last advertised credit. */
            uint64_t consumed = aiopquic_stream_ctx_rx_consumed_load(sc);
            uint64_t want_limit = consumed + advertise_cap;
            uint64_t cur_limit = aiopquic_stream_ctx_rx_credit_load(sc);
            if (want_limit > cur_limit + fc_threshold) {
                (void)picoquic_open_flow_control(cnx, sid, want_limit);
                aiopquic_stream_ctx_rx_credit_store(sc, want_limit);
            }
            /* First-touch notification (sets the per-h3-stream
             * sentinel as before — see legacy path comment in the
             * Phase A code). */
            if (stream_ctx->post_received == 0) {
                aiopquic_wt_push_event(s, SPSC_EVT_WT_NEW_STREAM,
                                        sid, 0, NULL, 0);
                stream_ctx->post_received = 1;
            }
            /* Notification carries the per-stream sc; bytes are
             * already in sc->rx. */
            aiopquic_wt_push_event_with_sc(
                s, SPSC_EVT_WT_STREAM_DATA, sid, sc, 0);
        }
        break;

    case picohttp_callback_post_fin:
        if (is_control) {
            /* Control stream FIN — session is over. */
            if (length > 0) {
                /* Any leftover bytes preceding the FIN */
                int rc = picowt_receive_capsule(cnx, bytes,
                                                  bytes + length, &s->capsule);
                (void)rc;
            }
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                                    s->control_stream_id, 0, NULL, 0);
            s->session_closing = 1;
        } else {
            /* Phase B FIN: any tail bytes go into sc->rx like in
             * post_data; FIN flag is set on sc->rx so the consumer
             * sees end-of-stream after draining all bytes.
             * The notification event itself carries is_fin=1 so the
             * consumer knows this is the terminal event. */
            aiopquic_stream_ctx_t* sc =
                aiopquic_wt_session_find_or_create_stream_rx(s, sid);
            if (!sc) return -1;

            uint32_t advertise_cap =
                s->bridge->rx_ring_cap > 0
                    ? s->bridge->rx_ring_cap
                    : AIOPQUIC_RX_RING_CAP_DEFAULT;
            int first_touch = (sc->rx == NULL);
            if (first_touch) {
                (void)picoquic_set_app_flow_control(cnx, sid, 1);
                (void)picoquic_open_flow_control(cnx, sid, advertise_cap);
                aiopquic_stream_ctx_rx_credit_store(sc, advertise_cap);
            }
            if (aiopquic_stream_ctx_ensure_rx(sc, advertise_cap) != 0) {
                return -1;
            }
            if (length > 0) {
                uint32_t pushed = aiopquic_stream_buf_push(
                    sc->rx, bytes, (uint32_t)length);
                if (pushed != length) {
                    s->bridge->worker_rx_byte_ring_overflow++;
                    return -1;
                }
            }
            aiopquic_stream_buf_set_fin(sc->rx);

            if (stream_ctx->post_received == 0) {
                aiopquic_wt_push_event(s, SPSC_EVT_WT_NEW_STREAM,
                                        sid, 0, NULL, 0);
                stream_ctx->post_received = 1;
            }
            aiopquic_wt_push_event_with_sc(
                s, SPSC_EVT_WT_STREAM_FIN, sid, sc, 1);
        }
        break;

    case picohttp_callback_provide_data: {
        /* picoquic asking for TX bytes on stream sid. Drain TX ring. */
        spsc_entry_t* tx = spsc_ring_peek(s->bridge->tx_ring);
        if (tx && tx->stream_id == sid &&
            (tx->event_type == SPSC_EVT_TX_STREAM_DATA ||
             tx->event_type == SPSC_EVT_TX_STREAM_FIN)) {
            const uint8_t* data = (const uint8_t*)tx->data_buf;
            uint32_t to_send = tx->data_length;
            if (to_send > length) to_send = (uint32_t)length;
            int is_fin = (tx->event_type == SPSC_EVT_TX_STREAM_FIN);
            int still_active = !is_fin;
            uint8_t* buf = picoquic_provide_stream_data_buffer(
                bytes, to_send, is_fin, still_active);
            if (buf && data) memcpy(buf, data, to_send);
            spsc_ring_pop(s->bridge->tx_ring);
        } else {
            (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
        }
        break;
    }

    case picohttp_callback_post_datagram:
        aiopquic_wt_push_event(s, SPSC_EVT_WT_DATAGRAM,
                                s->control_stream_id, 0,
                                bytes, (uint32_t)length);
        break;

    case picohttp_callback_provide_datagram: {
        /* Drain TX ring for a TX_DATAGRAM if present. */
        spsc_entry_t* tx = spsc_ring_peek(s->bridge->tx_ring);
        if (tx && tx->event_type == SPSC_EVT_TX_DATAGRAM) {
            uint32_t to_send = tx->data_length;
            if (to_send > length) to_send = (uint32_t)length;
            void* buf = h3zero_provide_datagram_buffer(stream_ctx, to_send, 0);
            if (buf && tx->data_buf) memcpy(buf, tx->data_buf, to_send);
            spsc_ring_pop(s->bridge->tx_ring);
        }
        break;
    }

    case picohttp_callback_reset:
        if (is_control) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                                    s->control_stream_id,
                                    picoquic_get_remote_stream_error(cnx, sid),
                                    NULL, 0);
            s->session_closing = 1;
        } else {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_RESET,
                                    sid,
                                    picoquic_get_remote_stream_error(cnx, sid),
                                    NULL, 0);
        }
        break;

    case picohttp_callback_stop_sending:
        aiopquic_wt_push_event(s, SPSC_EVT_WT_STOP_SENDING,
                                sid,
                                picoquic_get_remote_stream_error(cnx, sid),
                                NULL, 0);
        break;

    case picohttp_callback_deregister:
        /* Session is being torn down. Last chance to surface. */
        if (!s->session_closing) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                                    s->control_stream_id, 0, NULL, 0);
            s->session_closing = 1;
        }
        break;

    case picohttp_callback_free:
        /* Stream context being freed by picoquic; nothing to do here.
         * Session destruction (aiopquic_wt_session_destroy) is driven
         * from the Python side after consuming the CLOSED event. */
        break;

    default:
        break;
    }
    return 0;
}

/*
 * Server-side WT path callback. Registered in the picoquic engine's
 * picohttp_server_parameters_t::path_table with path_app_ctx set to
 * the bridge (aiopquic_ctx_t*). h3zero invokes this on a CONNECT
 * landing at the registered path; we accept the WT session,
 * allocate a per-session aiopquic_wt_session_t, re-point the
 * stream's callback to aiopquic_wt_path_callback (the per-session
 * handler used by the client), declare the stream prefix so peer-
 * opened WT streams within this session route through us, and push
 * SPSC_EVT_WT_NEW_SESSION so the asyncio side can construct a
 * Python WebTransportServerSession.
 */
static int aiopquic_wt_server_path_callback(
        picoquic_cnx_t* cnx,
        uint8_t* path_bytes, size_t path_len,
        picohttp_call_back_event_t event,
        h3zero_stream_ctx_t* stream_ctx,
        void* path_app_ctx) {
    aiopquic_ctx_t* bridge = (aiopquic_ctx_t*)path_app_ctx;
    if (!bridge || !stream_ctx) return -1;

    if (event != picohttp_callback_connect) {
        /* Anything other than the CONNECT-arrival event on the
         * path-table entry is unexpected; per-session events should
         * have been re-routed to aiopquic_wt_path_callback by the
         * stream_ctx->path_callback override below. */
        return 0;
    }

    h3zero_callback_ctx_t* h3_ctx =
        (h3zero_callback_ctx_t*)picoquic_get_callback_context(cnx);
    if (!h3_ctx) return -1;

    aiopquic_wt_session_t* s = aiopquic_wt_session_create(bridge);
    if (!s) return -1;
    s->cnx = cnx;
    s->h3_ctx = h3_ctx;
    s->control_stream = stream_ctx;
    s->control_stream_id = stream_ctx->stream_id;
    s->session_ready = 1;

    /* Re-point this stream so all further events (post_data,
     * post_fin, capsules, reset, stop_sending, deregister, ...) go
     * to the per-session handler with the session ctx. */
    stream_ctx->path_callback = aiopquic_wt_path_callback;
    stream_ctx->path_callback_ctx = s;

    /* Register the prefix so peer-opened WT streams within this WT
     * session are dispatched to our per-session callback. */
    (void)h3zero_declare_stream_prefix(
        h3_ctx, s->control_stream_id,
        aiopquic_wt_path_callback, s);

    /* Surface the new session to Python. cnx + session ptr come from
     * the spsc_entry fields; path bytes ride in the data buffer so
     * the asyncio side can demux by path when multiple are served. */
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_NEW_SESSION;
    entry.stream_id = s->control_stream_id;
    entry.cnx = cnx;
    entry.stream_ctx = s;
    int rc = spsc_ring_push(bridge->rx_ring, &entry,
                             path_bytes, (uint32_t)path_len);
    if (rc == 0) aiopquic_notify_rx(bridge);
    return 0;
}

/*
 * WT TX-event dispatch — called from aiopquic_loop_cb in callback.h.
 * Returns 1 if event was a recognized WT command, 0 otherwise.
 * The caller is responsible for spsc_ring_pop on the entry.
 */
static int aiopquic_wt_handle_tx(picoquic_quic_t* quic,
                                   aiopquic_ctx_t* ctx,
                                   spsc_entry_t* entry) {
    aiopquic_wt_session_t* s = (aiopquic_wt_session_t*)entry->cnx;

    switch (entry->event_type) {
    case SPSC_EVT_TX_WT_OPEN: {
        const uint8_t* raw = (const uint8_t*)entry->data_buf;
        if (!s || !raw ||
            entry->data_length < sizeof(aiopquic_wt_open_params_t)) {
            return 1;
        }
        const aiopquic_wt_open_params_t* p =
            (const aiopquic_wt_open_params_t*)raw;
        char sni_buf[256];
        char path_buf[1024];
        size_t offset = sizeof(aiopquic_wt_open_params_t);

        if (p->sni_len == 0 || p->sni_len >= sizeof(sni_buf) ||
            offset + p->sni_len > entry->data_length) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                    0, 1, NULL, 0);
            return 1;
        }
        memcpy(sni_buf, raw + offset, p->sni_len);
        sni_buf[p->sni_len] = '\0';
        offset += p->sni_len;

        if (p->path_len == 0 || p->path_len >= sizeof(path_buf) ||
            offset + p->path_len > entry->data_length) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                    0, 2, NULL, 0);
            return 1;
        }
        memcpy(path_buf, raw + offset, p->path_len);
        path_buf[p->path_len] = '\0';

        picoquic_cnx_t* cnx = NULL;
        h3zero_callback_ctx_t* h3_ctx = NULL;
        h3zero_stream_ctx_t* control_stream = NULL;

        int rc = picowt_prepare_client_cnx(
            quic, (struct sockaddr*)&p->addr,
            &cnx, &h3_ctx, &control_stream,
            picoquic_current_time(), sni_buf);
        if (rc != 0 || !cnx || !h3_ctx || !control_stream) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                    0, 3, NULL, 0);
            return 1;
        }

        s->cnx = cnx;
        s->h3_ctx = h3_ctx;
        s->control_stream = control_stream;
        s->control_stream_id = control_stream->stream_id;

        rc = picowt_connect(cnx, h3_ctx, control_stream,
                              sni_buf, path_buf,
                              aiopquic_wt_path_callback,
                              (void*)s, NULL);
        if (rc == 0) {
            /* picowt_prepare_client_cnx + picowt_connect leave the
             * cnx inert; pull the trigger so picoquic kicks off the
             * TLS handshake on the next packet loop tick. */
            rc = picoquic_start_client_cnx(cnx);
        }
        if (rc != 0) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                    s->control_stream_id, 4, NULL, 0);
        }
        return 1;
    }

    case SPSC_EVT_TX_WT_CREATE_STREAM: {
        if (!s || !s->cnx || !s->h3_ctx) return 1;
        int is_bidir = entry->is_fin;
        h3zero_stream_ctx_t* new_stream = picowt_create_local_stream(
            s->cnx, is_bidir, s->h3_ctx, s->control_stream_id);
        if (!new_stream) {
            /* Push 0 sid to indicate failure; Python side surfaces
             * an exception. */
            aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_CREATED,
                                    0, 1, NULL, 0);
        } else {
            /* picowt_create_local_stream wires the OUTBOUND prefix
             * but doesn't register a path_callback. For bidi, the
             * peer's reply lands on the inbound side and h3zero needs
             * a callback to route the bytes back to us. wt_baton sets
             * this in wt_baton_relay (see picohttp/wt_baton.c:181). */
            new_stream->path_callback = aiopquic_wt_path_callback;
            new_stream->path_callback_ctx = s;
            aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_CREATED,
                                    new_stream->stream_id, 0, NULL, 0);
        }
        return 1;
    }

    case SPSC_EVT_TX_WT_CLOSE: {
        if (!s || !s->cnx || !s->control_stream) return 1;
        const char* msg = (const char*)entry->data_buf;
        char buf[256];
        if (msg && entry->data_length > 0 &&
            entry->data_length < sizeof(buf)) {
            memcpy(buf, msg, entry->data_length);
            buf[entry->data_length] = '\0';
            picowt_send_close_session_message(
                s->cnx, s->control_stream,
                (uint32_t)entry->error_code, buf);
        } else {
            picowt_send_close_session_message(
                s->cnx, s->control_stream,
                (uint32_t)entry->error_code, "");
        }
        s->session_closing = 1;
        return 1;
    }

    case SPSC_EVT_TX_WT_DRAIN: {
        if (!s || !s->cnx || !s->control_stream) return 1;
        picowt_send_drain_session_message(s->cnx, s->control_stream);
        return 1;
    }

    case SPSC_EVT_TX_WT_RESET_STREAM: {
        if (!s || !s->cnx) return 1;
        /* The receiving-side stream_ctx was registered by the H3
         * machinery; we look it up by stream_id via h3zero. */
        h3zero_stream_ctx_t* st =
            h3zero_find_stream(s->h3_ctx, entry->stream_id);
        if (st) {
            picowt_reset_stream(s->cnx, st, entry->error_code);
        }
        return 1;
    }

    case SPSC_EVT_TX_WT_STOP_SENDING: {
        if (!s || !s->cnx) return 1;
        picoquic_stop_sending(s->cnx, entry->stream_id,
                               entry->error_code);
        return 1;
    }

    case SPSC_EVT_TX_WT_DEREGISTER: {
        /* Python is releasing this session. picowt_deregister
         * unregisters the WT prefix from h3_ctx so picoquic stops
         * dispatching to our path callback; safe to free the
         * wt_session afterward. The actual cnx + h3_ctx are owned
         * by picoquic and cleaned up on connection close. */
        if (s) {
            if (s->cnx && s->h3_ctx && s->control_stream) {
                picowt_deregister(s->cnx, s->h3_ctx, s->control_stream);
            }
            aiopquic_wt_session_destroy(s);
        }
        return 1;
    }

    default:
        return 0;
    }
}

#ifdef __cplusplus
}
#endif

#endif /* AIOPQUIC_H3WT_CALLBACK_H */
