/*
 * callback.h — picoquic callback function that bridges events to SPSC ring.
 *
 * This runs in the picoquic network thread. It receives all stream/connection
 * events from picoquic and writes them into the RX SPSC ring buffer for
 * consumption by the asyncio thread.
 *
 * Each event with payload owns a malloc'd buffer; ownership transfers to
 * the consumer at pop time (Python wraps it in a StreamChunk). No arena
 * memory is involved — the ring is a pure entry table.
 *
 * Copyright (c) 2026, aiopquic contributors. BSD-3-Clause license.
 */

#ifndef AIOPQUIC_CALLBACK_H
#define AIOPQUIC_CALLBACK_H

#include "spsc_ring.h"
#include "stream_buf.h"
#include "stream_ctx.h"
#include <picoquic.h>
#include <picoquic_packet_loop.h>

/* Fallback per-stream RX byte ring capacity when the configured
 * max_stream_data hasn't been threaded through (e.g., raw transport
 * tests bypassing QuicConfiguration). Production code paths use the
 * configured window via aiopquic_ctx_t.rx_ring_cap. Power of two. */
#define AIOPQUIC_RX_RING_CAP_DEFAULT (1u << 20)

/* MAX_STREAM_DATA hysteresis: advance peer credit when ≥ 1/4 of the
 * ring has been drained since the last update. Matches picoquic's
 * own receive_window_threshold cadence and bounds the rate of
 * MAX_STREAM_DATA frames under sustained drain. */
#define AIOPQUIC_RX_FC_THRESHOLD_DIV 4

#include <fcntl.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* O(N_connections) walk over picoquic's live-cnx list. Used as the
 * guard before dispatching any TX_* SPSC event into a `cnx*` from
 * the entry — the cnx may have been freed between the Python push
 * and our pop (peer CLOSE_CONNECTION, app-side close, timeout). Any
 * deref into a freed cnx is a UAF, surfacing as a NULL-page fault
 * inside picoquic when a struct field is loaded.
 *
 * TODO(0.3.5+): replace with a generation-counter lookup if aiomoqt
 * grows into a relay role with many concurrent cnxs. For the
 * current publisher/client shape (1–2 cnxs) the walk is one or two
 * pointer compares per event — effectively free. Cost scales O(N)
 * and starts to matter past ~50 cnxs at megapacket-per-second
 * event rates. */
static inline int aiopquic_cnx_is_alive(picoquic_quic_t* quic,
                                         picoquic_cnx_t* cnx) {
    picoquic_cnx_t* cur = picoquic_get_first_cnx(quic);
    while (cur) {
        if (cur == cnx) {
            return 1;
        }
        cur = picoquic_get_next_cnx(cur);
    }
    return 0;
}
#include <netinet/in.h>
#include <arpa/inet.h>

/* Cached AIOPQUIC_RX_LOG env flag — resolved once at first use so the
 * overflow-diagnostic fprintf paths don't repeatedly hit getenv()
 * under sustained overflow (libc env lookups are not cheap when fired
 * from inside the picoquic worker callback). 0=unset, 1=set, -1=not
 * yet probed. Atomic only to ensure the first-probe write is visible
 * across threads — there is no correctness issue if two threads race
 * the probe; both write the same value. */
static _Atomic(int) _aiopquic_rx_log_cached = -1;

static inline int aiopquic_rx_log_enabled(void) {
    int v = atomic_load_explicit(&_aiopquic_rx_log_cached,
                                  memory_order_relaxed);
    if (v < 0) {
        const char* s = getenv("AIOPQUIC_RX_LOG");
        v = (s && *s && *s != '0') ? 1 : 0;
        atomic_store_explicit(&_aiopquic_rx_log_cached, v,
                              memory_order_relaxed);
    }
    return v;
}

#ifdef __linux__
#include <sys/eventfd.h>
#endif

/*
 * Packed struct for SPSC_EVT_TX_CONNECT data payload.
 * Stored in the entry's data_buf as: header + sni + alpn.
 */
typedef struct {
    struct sockaddr_in addr;
    uint16_t sni_len;
    uint16_t alpn_len;
    /* followed by: sni_len bytes of SNI, then alpn_len bytes of ALPN */
} aiopquic_connect_params_t;

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    spsc_ring_t*    rx_ring;
    spsc_ring_t*    tx_ring;
    int             eventfd;        /* readable fd asyncio watches (eventfd
                                       on Linux, pipe read end elsewhere) */
    int             wake_write_fd;  /* fd the network thread writes to
                                       (== eventfd on Linux; pipe write end
                                       elsewhere) */
    picoquic_quic_t* quic;
    /* Per-stream RX byte ring capacity. Set at start() time from the
     * QuicConfiguration's max_stream_data so the ring can hold the
     * full peer-allowed in-flight window (the spec promises peer
     * never sends more before MAX_STREAM_DATA is extended). Rounded
     * up to a power of two by the Cython binding before being stored
     * here. */
    uint32_t        rx_ring_cap;
    /* Forensic counters — incremented from the picoquic worker thread
     * as it processes TX events. The asyncio thread reads them via
     * Cython properties to verify per-event accounting. Plain ints,
     * read with relaxed semantics (only one writer). */
    uint64_t        worker_mark_active_processed;
    uint64_t        worker_prepare_to_send_calls;
    uint64_t        worker_prepare_to_send_pulled_bytes;
    /* RX-side: count of spsc_ring_push failures on rx_ring (event
     * ring full). On stream_data callbacks the BYTES were already
     * pushed to sc->rx; the dropped EVENT means asyncio is not told
     * those bytes arrived → small streams whose only events drop
     * are silently lost. THIS WAS THE STREAM-LOSS BUG ROOT CAUSE. */
    uint64_t        worker_rx_event_drops;
    uint64_t        worker_rx_event_drops_stream_data;
    /* Notify-coalescing state. eventfd is level-triggered; multiple
     * write()s coalesce to one wake-up at the consumer, but each
     * write is still a syscall. At 250K events/sec on a stream-churn
     * workload that's a quarter-million unnecessary syscalls/sec on
     * the picoquic worker thread. Track whether the consumer has
     * acknowledged the last notify (= drained) and skip the syscall
     * when there's already a pending notification.
     *
     * Set to 1 by aiopquic_notify_rx (worker) on first push since
     * last drain. Cleared to 0 by aiopquic_clear_rx (consumer) when
     * it reads the eventfd. Producer-only-sets, single-writer
     * semantics — atomic exchange used to detect the 0→1 edge. */
    uint32_t        rx_notify_pending;
    /* Same coalescing on TX side: skip picoquic_wake_up_network_thread
     * unless the worker has drained since our last wake. The worker
     * sets this back to 0 whenever it observes the tx_ring drained
     * to empty (after popping all entries) so the next producer push
     * triggers a wake. */
    uint32_t        tx_wake_pending;
    /* Per-stream RX byte ring overflow counter. Replaces the
     * fprintf(stderr,...) on full from the previous diagnostic —
     * libc stdio holds a global lock and stalls the picoquic worker
     * under load. Set AIOPQUIC_RX_LOG=1 in the env to re-enable a
     * one-shot stderr line per overflow event. */
    uint64_t        worker_rx_byte_ring_overflow;
} aiopquic_ctx_t;

static inline aiopquic_ctx_t* aiopquic_ctx_create(uint32_t ring_capacity) {
    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)calloc(1, sizeof(aiopquic_ctx_t));
    if (!ctx) return NULL;

    ctx->rx_ring = spsc_ring_create(ring_capacity);
    ctx->tx_ring = spsc_ring_create(ring_capacity);
    if (!ctx->rx_ring || !ctx->tx_ring) {
        spsc_ring_destroy(ctx->rx_ring);
        spsc_ring_destroy(ctx->tx_ring);
        free(ctx);
        return NULL;
    }

#ifdef __linux__
    ctx->eventfd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (ctx->eventfd < 0) {
        spsc_ring_destroy(ctx->rx_ring);
        spsc_ring_destroy(ctx->tx_ring);
        free(ctx);
        return NULL;
    }
    ctx->wake_write_fd = ctx->eventfd;
#else
    /* Self-pipe trick: pipe[0] is the read end (asyncio watches),
     * pipe[1] is the write end (network thread signals). pipe2() isn't
     * available on macOS, so set O_NONBLOCK | FD_CLOEXEC via fcntl. */
    int p[2];
    if (pipe(p) < 0) {
        spsc_ring_destroy(ctx->rx_ring);
        spsc_ring_destroy(ctx->tx_ring);
        free(ctx);
        return NULL;
    }
    for (int i = 0; i < 2; i++) {
        int flags = fcntl(p[i], F_GETFL, 0);
        if (flags >= 0) (void)fcntl(p[i], F_SETFL, flags | O_NONBLOCK);
        int fd_flags = fcntl(p[i], F_GETFD, 0);
        if (fd_flags >= 0) (void)fcntl(p[i], F_SETFD, fd_flags | FD_CLOEXEC);
    }
    ctx->eventfd = p[0];
    ctx->wake_write_fd = p[1];
#endif

    return ctx;
}

static inline void aiopquic_ctx_destroy(aiopquic_ctx_t* ctx) {
    if (ctx) {
        if (ctx->eventfd >= 0) close(ctx->eventfd);
        if (ctx->wake_write_fd >= 0 && ctx->wake_write_fd != ctx->eventfd) {
            close(ctx->wake_write_fd);
        }
        spsc_ring_destroy(ctx->rx_ring);
        spsc_ring_destroy(ctx->tx_ring);
        free(ctx);
    }
}

/* TX wake-coalescing: producer-side helper. Returns 1 if a wake is
 * already pending (caller should skip the picoquic_wake_up syscall),
 * 0 if this caller should write the wake. The worker clears the flag
 * back to 0 when it finishes draining the TX ring. Single-producer
 * (asyncio thread) so atomic exchange is sufficient. */
static inline int aiopquic_tx_wake_set_pending(aiopquic_ctx_t* ctx) {
    return __atomic_exchange_n(&ctx->tx_wake_pending, 1,
                                 __ATOMIC_RELEASE);
}

/* Signal asyncio that there are RX events.
 * Coalesced: write the wake fd only on the 0→1 transition of
 * rx_notify_pending. The consumer (aiopquic_clear_rx) restores the
 * flag to 0 after it has drained its ring snapshot AND re-armed if
 * the ring is still non-empty, so any push that races against the
 * consumer's drain is guaranteed to wake asyncio at least once.
 * ACQ_REL pairs with the consumer's RELEASE store on the same flag
 * and ACQUIRE on the ring entries. */
static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx) {
    if (__atomic_exchange_n(&ctx->rx_notify_pending, 1,
                              __ATOMIC_ACQ_REL) != 0) {
        return;
    }
#ifdef __linux__
    uint64_t val = 1;
    (void)write(ctx->wake_write_fd, &val, sizeof(val));
#else
    uint8_t b = 1;
    (void)write(ctx->wake_write_fd, &b, 1);
#endif
}

/* Consumer-side wake-fd clear + re-arm. Caller (drain_rx) MUST have
 * already drained as many ring entries as it intends to consume in
 * this cycle BEFORE calling this. We then:
 *   1. RELEASE-store rx_notify_pending=0 so the NEXT producer push
 *      will write the wake fd (the producer's exchange sees 0→1).
 *   2. Drain the wake-fd counter (Linux: one read; other: read until
 *      EAGAIN).
 *   3. Re-arm by writing the wake fd if the ring is still non-empty.
 *      Covers the race where a producer pushed an entry between the
 *      consumer's peek-empty and pending=0, observing pending=1 and
 *      thus skipping its own wake write — without re-arm those
 *      stranded entries would never wake asyncio again. */
static inline void aiopquic_clear_rx(aiopquic_ctx_t* ctx) {
    __atomic_store_n(&ctx->rx_notify_pending, 0, __ATOMIC_RELEASE);
#ifdef __linux__
    uint64_t val;
    (void)read(ctx->eventfd, &val, sizeof(val));
#else
    uint8_t buf[64];
    while (read(ctx->eventfd, buf, sizeof(buf)) > 0) {
        /* drain */
    }
#endif
    if (!spsc_ring_empty(ctx->rx_ring)) {
#ifdef __linux__
        uint64_t one = 1;
        (void)write(ctx->wake_write_fd, &one, sizeof(one));
#else
        uint8_t b = 1;
        (void)write(ctx->wake_write_fd, &b, 1);
#endif
    }
}

/* WT TX-event dispatch hook. Defined in h3wt_callback.h. The loop
 * callback below routes any TX entry whose event type is a WT
 * command (TX_WT_OPEN/CREATE_STREAM/CLOSE/DRAIN/RESET_STREAM)
 * through this function. Returns 1 if handled, 0 if not a WT TX
 * event (caller falls through to normal dispatch). */
static int aiopquic_wt_handle_tx(picoquic_quic_t* quic,
                                   aiopquic_ctx_t* ctx,
                                   spsc_entry_t* entry);

static inline int aiopquic_map_event(picoquic_call_back_event_t ev) {
    switch (ev) {
        case picoquic_callback_stream_data:     return SPSC_EVT_STREAM_DATA;
        case picoquic_callback_stream_fin:      return SPSC_EVT_STREAM_FIN;
        case picoquic_callback_stream_reset:    return SPSC_EVT_STREAM_RESET;
        case picoquic_callback_stop_sending:    return SPSC_EVT_STOP_SENDING;
        case picoquic_callback_close:           return SPSC_EVT_CLOSE;
        case picoquic_callback_application_close: return SPSC_EVT_APP_CLOSE;
        case picoquic_callback_ready:           return SPSC_EVT_READY;
        case picoquic_callback_almost_ready:    return SPSC_EVT_ALMOST_READY;
        case picoquic_callback_datagram:        return SPSC_EVT_DATAGRAM;
        case picoquic_callback_datagram_acked:  return SPSC_EVT_DATAGRAM_ACKED;
        case picoquic_callback_datagram_lost:   return SPSC_EVT_DATAGRAM_LOST;
        case picoquic_callback_path_available:  return SPSC_EVT_PATH_AVAILABLE;
        case picoquic_callback_path_suspended:  return SPSC_EVT_PATH_SUSPENDED;
        case picoquic_callback_path_deleted:    return SPSC_EVT_PATH_DELETED;
        case picoquic_callback_pacing_changed:  return SPSC_EVT_PACING_CHANGED;
        default: return -1;
    }
}

/*
 * Picoquic stream/connection callback. Runs in the picoquic network thread.
 * Mandatory copy-out happens here: picoquic's stream-callback bytes have
 * callback-frame lifetime, so spsc_ring_push allocates a fresh buffer
 * and memcpys before publishing the entry.
 */
static int aiopquic_stream_cb(picoquic_cnx_t* cnx,
                               uint64_t stream_id,
                               uint8_t* bytes,
                               size_t length,
                               picoquic_call_back_event_t fin_or_event,
                               void* callback_ctx,
                               void* stream_ctx) {
    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)callback_ctx;
    if (!ctx) return -1;

    /* TX-side callback. Two paths:
     *
     * (1) PULL model: stream_ctx is a aiopquic_stream_buf_t* set by
     *     picoquic_mark_active_stream(cnx, sid, 1, sb). Drain bytes
     *     from the ring into picoquic's frame buffer up to the budget.
     *     If the ring drains to empty AND fin_pending is set, signal
     *     FIN; else keep the stream active.
     *
     * (2) PUSH model (legacy): stream_ctx is NULL. Drain a single
     *     SPSC TX entry (whatever is at the head of the shared TX
     *     ring). Only matches if the entry is for this stream_id.
     *     This path has no upstream backpressure — caller is
     *     responsible for not over-pushing into picoquic's internal
     *     queue. Retained for the existing send_stream_data API.
     */
    if (fin_or_event == picoquic_callback_prepare_to_send) {
        ctx->worker_prepare_to_send_calls++;
        if (stream_ctx) {
            aiopquic_stream_ctx_t* sc =
                (aiopquic_stream_ctx_t*)stream_ctx;
            aiopquic_stream_buf_t* sb = sc->tx;
            if (!sb) {
                (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
                return 0;
            }
            uint32_t want = (uint32_t)length;
            uint32_t avail = aiopquic_stream_buf_used(sb);
            uint32_t to_send = (avail < want) ? avail : want;
            int fin_after = aiopquic_stream_buf_fin_pending(sb);
            int is_fin = (fin_after && to_send == avail) ? 1 : 0;
            int is_still_active = (avail > to_send) ? 1 : 0;
            uint8_t* buf = picoquic_provide_stream_data_buffer(
                bytes, to_send, is_fin, is_still_active);
            if (buf && to_send > 0) {
                aiopquic_stream_buf_pop(sb, buf, to_send);
                ctx->worker_prepare_to_send_pulled_bytes += to_send;
                /* Edge-trigger TX backpressure: if a Python writer
                 * was blocked (set tx_drain_pending=1 after seeing
                 * sc->tx full), CAS-clear and emit one SPSC event
                 * so it can wake and retry. Single-shot per cycle —
                 * only the CAS winner pushes the event. */
                uint32_t expected = 1;
                if (atomic_compare_exchange_strong_explicit(
                        &sc->tx_drain_pending, &expected, 0,
                        memory_order_acq_rel, memory_order_relaxed)) {
                    spsc_entry_t drain_entry = {0};
                    drain_entry.event_type = SPSC_EVT_STREAM_TX_DRAINED;
                    drain_entry.stream_id = stream_id;
                    drain_entry.cnx = cnx;
                    drain_entry.stream_ctx = sc;
                    if (spsc_ring_push(ctx->rx_ring,
                                       &drain_entry, NULL, 0) == 0) {
                        aiopquic_notify_rx(ctx);
                    } else {
                        /* RX ring full — re-arm so Python re-attempts
                         * once it drains; the worker will retry next
                         * prepare_to_send. Better than losing the
                         * wakeup. */
                        atomic_store_explicit(
                            &sc->tx_drain_pending, 1,
                            memory_order_release);
                    }
                }
            }
            return 0;
        }

        spsc_entry_t* tx_entry = spsc_ring_peek(ctx->tx_ring);
        if (tx_entry && tx_entry->stream_id == stream_id &&
            (tx_entry->event_type == SPSC_EVT_TX_STREAM_DATA ||
             tx_entry->event_type == SPSC_EVT_TX_STREAM_FIN)) {
            const uint8_t* data = (const uint8_t*)tx_entry->data_buf;
            uint32_t to_send = tx_entry->data_length;
            if (to_send > length) to_send = (uint32_t)length;

            int is_fin = (tx_entry->event_type == SPSC_EVT_TX_STREAM_FIN);
            int is_still_active = !is_fin;

            uint8_t* buf = picoquic_provide_stream_data_buffer(
                bytes, to_send, is_fin, is_still_active);
            if (buf && data) {
                memcpy(buf, data, to_send);
            }
            spsc_ring_pop(ctx->tx_ring);
            return 0;
        }
        (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
        return 0;
    }

    int evt = aiopquic_map_event(fin_or_event);
    if (evt < 0) {
        return 0;
    }

    spsc_entry_t entry = {0};
    entry.event_type = (uint32_t)evt;
    entry.stream_id = stream_id;
    entry.cnx = cnx;
    entry.stream_ctx = stream_ctx;

    if (fin_or_event == picoquic_callback_stream_reset ||
        fin_or_event == picoquic_callback_stop_sending) {
        /* picoquic_get_remote_stream_error returns the RESET_STREAM
         * code; STOP_SENDING is stored in stream->remote_stop_error
         * (private to picoquic_internal.h, no public getter), so
         * STOP_SENDING events surface error_code=0 today. TODO: add
         * a small C helper that includes internal.h to recover it. */
        entry.error_code = picoquic_get_remote_stream_error(cnx, stream_id);
    } else if (fin_or_event == picoquic_callback_application_close) {
        entry.error_code = picoquic_get_application_error(cnx);
    } else if (fin_or_event == picoquic_callback_close) {
        entry.error_code = picoquic_get_remote_error(cnx);
    }

    /* Canonical RX path: stream_data / stream_fin bytes are pushed
     * synchronously into a per-stream byte ring on the wrapper. The
     * SPSC event delivered to Python carries ONLY the wrapper pointer
     * (no payload), so picoquic's "callback returns = bytes consumed"
     * contract is satisfied without ever risking a silent drop on
     * SPSC ring full.
     *
     * The Python consumer (connection.py RX drain) pops bytes from
     * sc->rx, builds StreamDataReceived events, and (Landing B)
     * advances peer credit via picoquic_open_flow_control. */
    int has_payload = (fin_or_event == picoquic_callback_stream_data ||
                       fin_or_event == picoquic_callback_stream_fin)
                      && bytes != NULL && length > 0;
    int ret;
    if (has_payload) {
        /* Physical RX ring sized at exactly the advertised window
         * (1x). picoquic's auto-FC extension is gated by
         * !stream->use_app_flow_control (frames.c:4638), and we set
         * use_app_flow_control=1 SYNCHRONOUSLY inside this first
         * stream_data callback before returning to picoquic's send
         * framer — so no auto-extend frame can be emitted after our
         * opt-in. Before our opt-in, the peer's max in-flight bytes
         * is bounded by the initial_max_stream_data transport
         * parameter; after, by advertise_cap. Neither produces 2x
         * overshoot, so the previous 2x headroom was unnecessary
         * memory traffic + cache footprint per stream. RX ring
         * overflow remains a hard error: a peer that overruns is a
         * spec-correct FLOW_CONTROL_ERROR connection close. */
        uint32_t advertise_cap = ctx->rx_ring_cap > 0
            ? ctx->rx_ring_cap
            : AIOPQUIC_RX_RING_CAP_DEFAULT;
        uint32_t physical_cap = advertise_cap;
        uint32_t fc_threshold = advertise_cap / AIOPQUIC_RX_FC_THRESHOLD_DIV;
        aiopquic_stream_ctx_t* sc = (aiopquic_stream_ctx_t*)stream_ctx;
        if (!sc) {
            sc = aiopquic_stream_ctx_create();
            if (!sc) {
                return -1;
            }
            picoquic_set_app_stream_ctx(cnx, stream_id, sc);
            entry.stream_ctx = sc;
            /* Opt into application-driven flow control. Initial credit
             * advertised to peer = advertise_cap. */
            (void)picoquic_set_app_flow_control(cnx, stream_id, 1);
            (void)picoquic_open_flow_control(cnx, stream_id, advertise_cap);
            aiopquic_stream_ctx_rx_credit_store(sc, advertise_cap);
        }
        if (aiopquic_stream_ctx_ensure_rx(sc, physical_cap) != 0) {
            return -1;
        }
        uint32_t pushed = aiopquic_stream_buf_push(
            sc->rx, bytes, (uint32_t)length);
        if (pushed != length) {
            /* Per-stream RX byte-ring overflow. Means the peer sent
             * more than its advertised flow-control window allowed —
             * a spec-correct FLOW_CONTROL_ERROR connection close.
             * Counter incremented from worker thread; opt-in stderr
             * via env AIOPQUIC_RX_LOG=1 (libc stdio holds a global
             * lock and stalls the worker thread under load, so the
             * default is silent). */
            ctx->worker_rx_byte_ring_overflow++;
            if (aiopquic_rx_log_enabled()) {
                fprintf(stderr,
                    "[aiopquic_rx] stream=%llu RX ring overflow: "
                    "pushed %u of %zu (free=%u, physical_cap=%u, "
                    "advertised=%u). Peer flow-control violation.\n",
                    (unsigned long long)stream_id, pushed, length,
                    aiopquic_stream_buf_free(sc->rx),
                    physical_cap, advertise_cap);
            }
            return -1;
        }
        if (fin_or_event == picoquic_callback_stream_fin) {
            aiopquic_stream_buf_set_fin(sc->rx);
        }
        uint64_t consumed = aiopquic_stream_ctx_rx_consumed_load(sc);
        uint64_t want_limit = consumed + advertise_cap;
        uint64_t cur_limit = aiopquic_stream_ctx_rx_credit_load(sc);
        if (want_limit > cur_limit + fc_threshold) {
            (void)picoquic_open_flow_control(cnx, stream_id, want_limit);
            aiopquic_stream_ctx_rx_credit_store(sc, want_limit);
        }
        ret = spsc_ring_push(ctx->rx_ring, &entry, NULL, 0);
    } else {
        ret = spsc_ring_push(ctx->rx_ring, &entry, bytes, (uint32_t)length);
    }
    if (ret == 0) {
        aiopquic_notify_rx(ctx);
    } else {
        /* RX EVENT RING FULL — the data is already in sc->rx (for
         * stream_data) but the notification event is gone. asyncio
         * will only learn about the bytes if a LATER event for the
         * same stream pushes successfully (drain_rx pops avail bytes
         * from sc->rx then). Short streams whose only events drop
         * are silently lost. Counter is exposed via Cython so callers
         * can detect the condition; opt-in stderr via AIOPQUIC_RX_LOG=1. */
        ctx->worker_rx_event_drops++;
        if (has_payload) {
            ctx->worker_rx_event_drops_stream_data++;
        }
        if (aiopquic_rx_log_enabled()
                && ctx->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] EVENT RING FULL: drop "
                "stream=%llu evt=%u (rx_ring entries=%u)\n",
                (unsigned long long)stream_id, entry.event_type,
                spsc_ring_count(ctx->rx_ring));
        }
    }

    return 0;
}

/*
 * Packet loop callback — handles wake-up events to drain the TX ring.
 */
static int aiopquic_loop_cb(picoquic_quic_t* quic,
                             picoquic_packet_loop_cb_enum cb_mode,
                             void* callback_ctx,
                             void* callback_argv) {
    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)callback_ctx;

    switch (cb_mode) {
        case picoquic_packet_loop_ready:
            ctx->quic = quic;
            spsc_ring_push_event(ctx->rx_ring, SPSC_EVT_READY, 0, NULL, 0);
            aiopquic_notify_rx(ctx);
            break;

        case picoquic_packet_loop_wake_up:
            /* Clear pending BEFORE draining. Race-safe ordering:
             * - If producer pushes BEFORE we clear: their exchange
             *   sees 1, skips wake — but their entry is in the ring,
             *   we'll see it on the next peek (still in the loop).
             * - If producer pushes AFTER we clear: their exchange
             *   sees 0, fires wake — picoquic re-enters this case.
             * - If producer pushes between our peek-empty and clear:
             *   their exchange could see 1 (stale) and skip — that's
             *   why we clear FIRST. Producer always sees 0 when the
             *   ring was actually drained. */
            __atomic_store_n(&ctx->tx_wake_pending, 0,
                              __ATOMIC_RELEASE);
            while (1) {
                spsc_entry_t* entry = spsc_ring_peek(ctx->tx_ring);
                if (!entry) break;

                picoquic_cnx_t* cnx = (picoquic_cnx_t*)entry->cnx;

                if (entry->event_type == SPSC_EVT_TX_CONNECT) {
                    const uint8_t* raw = (const uint8_t*)entry->data_buf;
                    if (raw && entry->data_length >=
                            sizeof(aiopquic_connect_params_t)) {
                        const aiopquic_connect_params_t* p =
                            (const aiopquic_connect_params_t*)raw;
                        const char* sni_ptr = NULL;
                        const char* alpn_ptr = NULL;
                        char sni_buf[256];
                        char alpn_buf[64];
                        size_t offset = sizeof(aiopquic_connect_params_t);

                        if (p->sni_len > 0 && p->sni_len < sizeof(sni_buf) &&
                            offset + p->sni_len <= entry->data_length) {
                            memcpy(sni_buf, raw + offset, p->sni_len);
                            sni_buf[p->sni_len] = '\0';
                            sni_ptr = sni_buf;
                            offset += p->sni_len;
                        }
                        if (p->alpn_len > 0 && p->alpn_len < sizeof(alpn_buf) &&
                            offset + p->alpn_len <= entry->data_length) {
                            memcpy(alpn_buf, raw + offset, p->alpn_len);
                            alpn_buf[p->alpn_len] = '\0';
                            alpn_ptr = alpn_buf;
                        }

                        picoquic_cnx_t* new_cnx = picoquic_create_client_cnx(
                            quic,
                            (struct sockaddr*)&p->addr,
                            picoquic_current_time(),
                            0,
                            sni_ptr, alpn_ptr,
                            aiopquic_stream_cb,
                            (void*)ctx);
                        if (new_cnx) {
                            spsc_entry_t resp = {0};
                            resp.event_type = SPSC_EVT_ALMOST_READY;
                            resp.cnx = new_cnx;
                            spsc_ring_push(ctx->rx_ring, &resp, NULL, 0);
                            aiopquic_notify_rx(ctx);
                        }
                    }
                    spsc_ring_pop(ctx->tx_ring);
                    continue;
                }

                /* Stale-cnx guard. Drops events whose cnx was freed
                 * between the Python push and this pop. Without this,
                 * any picoquic_* call below UAFs. See
                 * aiopquic_cnx_is_alive() comment for cost notes. */
                if (!cnx || !aiopquic_cnx_is_alive(quic, cnx)) {
                    spsc_ring_pop(ctx->tx_ring);
                    continue;
                }

                switch (entry->event_type) {
                    case SPSC_EVT_TX_MARK_ACTIVE: {
                        picoquic_mark_active_stream(cnx, entry->stream_id,
                                                     1, entry->stream_ctx);
                        ctx->worker_mark_active_processed++;
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_STREAM_DATA:
                    case SPSC_EVT_TX_STREAM_FIN: {
                        const uint8_t* data = (const uint8_t*)entry->data_buf;
                        int is_fin = (entry->event_type == SPSC_EVT_TX_STREAM_FIN);
                        picoquic_add_to_stream(cnx, entry->stream_id,
                                                data, entry->data_length, is_fin);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_DATAGRAM: {
                        const uint8_t* data = (const uint8_t*)entry->data_buf;
                        picoquic_queue_datagram_frame(cnx, entry->data_length, data);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_CLOSE: {
                        /* cnx liveness already verified by the outer
                         * aiopquic_cnx_is_alive() guard above. */
                        picoquic_close(cnx, entry->error_code);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_STREAM_RESET: {
                        picoquic_reset_stream(cnx, entry->stream_id, entry->error_code);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_STOP_SENDING: {
                        picoquic_stop_sending(cnx, entry->stream_id, entry->error_code);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_OPEN_FLOW_CONTROL: {
                        /* Asyncio thread asks us to advance the peer's
                         * MAX_STREAM_DATA limit for this stream. error_code
                         * carries the new absolute byte limit. Returns
                         * non-zero if stream not found or cnx not in ready
                         * state — both transient/recoverable. */
                        (void)picoquic_open_flow_control(
                            cnx, entry->stream_id, entry->error_code);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_SET_APP_FLOW_CONTROL: {
                        (void)picoquic_set_app_flow_control(
                            cnx, entry->stream_id, (int)entry->is_fin);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    default:
                        /* Unknown for raw-QUIC; route to WT dispatch
                         * which handles WT-specific TX commands. */
                        (void)aiopquic_wt_handle_tx(quic, ctx, entry);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                }
            }
            break;

        default:
            break;
    }

    return 0;
}

#ifdef __cplusplus
}
#endif

#endif /* AIOPQUIC_CALLBACK_H */
