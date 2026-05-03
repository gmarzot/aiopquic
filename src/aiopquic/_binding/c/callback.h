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

/* Per-stream RX byte ring capacity. 1 MiB matches TX side; gives
 * ~5-10ms of in-flight data at 1-2 Gbps peer rate which is plenty of
 * pipelining headroom while picoquic's default flow-control window is
 * also at this scale. Power of two required. */
#define AIOPQUIC_RX_RING_CAP (1u << 20)

#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <netinet/in.h>
#include <arpa/inet.h>

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

/* Signal asyncio that there are RX events.
 * Linux: 8-byte counter increment on the eventfd.
 * Other: single byte to the pipe write end. */
static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx) {
#ifdef __linux__
    uint64_t val = 1;
    (void)write(ctx->wake_write_fd, &val, sizeof(val));
#else
    uint8_t b = 1;
    (void)write(ctx->wake_write_fd, &b, 1);
#endif
}

/* Clear the wake fd after asyncio drains the RX ring.
 * Linux: one read returns and zeros the eventfd counter.
 * Other: drain the pipe until EAGAIN — many notifies coalesce. */
static inline void aiopquic_clear_rx(aiopquic_ctx_t* ctx) {
#ifdef __linux__
    uint64_t val;
    (void)read(ctx->eventfd, &val, sizeof(val));
#else
    uint8_t buf[64];
    while (read(ctx->eventfd, buf, sizeof(buf)) > 0) {
        /* drain */
    }
#endif
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

    /* RX-path instrumentation.
     *   AIOPQUIC_RX_TRACE=1       first chunk per stream only
     *   AIOPQUIC_RX_TRACE=2/all   every chunk (flood)
     *   AIOPQUIC_RX_STRIDE=K      every chunk where (chunk_idx % K)==0
     *                             AND chunk_idx==0 AND fin chunk
     *                             (covers all streams; same K on Python)
     *   AIOPQUIC_RX_FOCUS=N       restrict logging to stream_id N
     * Each line carries stream/chunk/off/len/fin + head[16] (and tail[8]
     * when focused). Compare to aiomoqt's per-chunk log on the Python
     * side to localize cross-stream byte misattribution. */
    static int trace_checked = 0;
    static int trace_mode = 0;     /* 0=off, 1=first-only, 2=all */
    static int trace_stride = 0;   /* >0 means stride mode */
    static int focus_checked = 0;
    static int has_focus = 0;
    static uint64_t focus_stream = 0;
    if (!trace_checked) {
        const char* env = getenv("AIOPQUIC_RX_TRACE");
        if (env && *env && *env != '0') {
            trace_mode = (env[0] == '2' || strcmp(env, "all") == 0) ? 2 : 1;
        }
        const char* senv = getenv("AIOPQUIC_RX_STRIDE");
        if (senv && *senv) {
            int v = atoi(senv);
            if (v > 0) trace_stride = v;
        }
        trace_checked = 1;
    }
    if (!focus_checked) {
        const char* fenv = getenv("AIOPQUIC_RX_FOCUS");
        if (fenv && *fenv) {
            char* endp = NULL;
            unsigned long long v = strtoull(fenv, &endp, 10);
            if (endp && endp != fenv) {
                focus_stream = (uint64_t)v;
                has_focus = 1;
            }
        }
        focus_checked = 1;
    }
    if ((trace_mode > 0 || trace_stride > 0 || has_focus) &&
        (fin_or_event == picoquic_callback_stream_data ||
         fin_or_event == picoquic_callback_stream_fin) &&
        bytes != NULL && length > 0) {
        int is_focus_target = has_focus && (stream_id == focus_stream);
        if (!has_focus || is_focus_target) {
            /* Per-stream chunk index + cumulative byte offset.
             * 1024-bucket thread-local hash, stream_id+1 marks slot. */
            static __thread uint64_t seen[1024];
            static __thread uint32_t chunk_idx[1024];
            static __thread uint64_t byte_off[1024];
            unsigned bucket = (unsigned)(stream_id % 1024);
            int first_for_stream = (seen[bucket] != stream_id + 1);
            if (first_for_stream) {
                seen[bucket] = stream_id + 1;
                chunk_idx[bucket] = 0;
                byte_off[bucket] = 0;
            }
            uint32_t cidx = chunk_idx[bucket];
            uint64_t boff = byte_off[bucket];
            int is_fin = (fin_or_event == picoquic_callback_stream_fin) ? 1 : 0;
            int log_it = is_focus_target || (trace_mode == 2) ||
                         (trace_mode == 1 && first_for_stream);
            if (!log_it && trace_stride > 0) {
                if (cidx == 0 || is_fin || (cidx % (uint32_t)trace_stride) == 0) {
                    log_it = 1;
                }
            }
            if (log_it) {
                size_t hn = length < 16 ? length : 16;
                size_t tn = length < 8 ? length : 8;
                fprintf(stderr,
                        "[aiopquic_rx] stream=%llu chunk=%u off=%llu len=%zu fin=%d head=",
                        (unsigned long long)stream_id, cidx,
                        (unsigned long long)boff, length, is_fin);
                for (size_t i = 0; i < hn; i++) {
                    fprintf(stderr, "%02x", (unsigned)bytes[i]);
                }
                if ((is_focus_target || trace_stride > 0) && length > 16) {
                    fprintf(stderr, " tail=");
                    for (size_t i = length - tn; i < length; i++) {
                        fprintf(stderr, "%02x", (unsigned)bytes[i]);
                    }
                }
                fprintf(stderr, "\n");
            }
            chunk_idx[bucket] = cidx + 1;
            byte_off[bucket] = boff + length;
        }
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
        aiopquic_stream_ctx_t* sc = (aiopquic_stream_ctx_t*)stream_ctx;
        if (!sc) {
            sc = aiopquic_stream_ctx_create();
            if (!sc) {
                /* OOM — close connection rather than drop bytes. */
                return -1;
            }
            picoquic_set_app_stream_ctx(cnx, stream_id, sc);
            entry.stream_ctx = sc;
        }
        if (aiopquic_stream_ctx_ensure_rx(sc, AIOPQUIC_RX_RING_CAP) != 0) {
            return -1;
        }
        uint32_t pushed = aiopquic_stream_buf_push(
            sc->rx, bytes, (uint32_t)length);
        if (pushed != length) {
            /* Ring full despite credit-window sizing — flow control
             * violation by peer or our own bookkeeping bug. Better to
             * close noisily than to silently corrupt. */
            fprintf(stderr,
                    "[aiopquic_rx] stream=%llu RX ring overflow: "
                    "pushed %u of %zu (free=%u). Peer flow-control "
                    "violation OR app didn't drain in time.\n",
                    (unsigned long long)stream_id, pushed, length,
                    aiopquic_stream_buf_free(sc->rx));
            return -1;
        }
        if (fin_or_event == picoquic_callback_stream_fin) {
            aiopquic_stream_buf_set_fin(sc->rx);
        }
        /* Signal-only SPSC event: bytes live on sc->rx, can't be dropped. */
        ret = spsc_ring_push(ctx->rx_ring, &entry, NULL, 0);
    } else {
        ret = spsc_ring_push(ctx->rx_ring, &entry, bytes, (uint32_t)length);
    }
    if (ret == 0) {
        aiopquic_notify_rx(ctx);
    }
    /* For non-stream events (datagram, close, ready, etc.) the SPSC
     * push can still fail under extreme load. Those are control-plane
     * and rare; if a future deployment hits this we'll move them to a
     * second priority ring. */

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

                if (!cnx) {
                    spsc_ring_pop(ctx->tx_ring);
                    continue;
                }

                switch (entry->event_type) {
                    case SPSC_EVT_TX_MARK_ACTIVE: {
                        picoquic_mark_active_stream(cnx, entry->stream_id,
                                                     1, entry->stream_ctx);
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
