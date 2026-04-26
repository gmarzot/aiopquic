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
#include <picoquic.h>
#include <picoquic_packet_loop.h>

#include <string.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#ifdef __linux__
#include <sys/eventfd.h>
#include <unistd.h>
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
    int             eventfd;
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
#else
    ctx->eventfd = -1;
#endif

    return ctx;
}

static inline void aiopquic_ctx_destroy(aiopquic_ctx_t* ctx) {
    if (ctx) {
#ifdef __linux__
        if (ctx->eventfd >= 0) close(ctx->eventfd);
#endif
        spsc_ring_destroy(ctx->rx_ring);
        spsc_ring_destroy(ctx->tx_ring);
        free(ctx);
    }
}

static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx) {
#ifdef __linux__
    uint64_t val = 1;
    (void)write(ctx->eventfd, &val, sizeof(val));
#endif
}

static inline void aiopquic_clear_rx(aiopquic_ctx_t* ctx) {
#ifdef __linux__
    uint64_t val;
    (void)read(ctx->eventfd, &val, sizeof(val));
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

    /* TX-side callback: drain TX ring entry into picoquic's frame buffer. */
    if (fin_or_event == picoquic_callback_prepare_to_send) {
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
        entry.error_code = picoquic_get_remote_stream_error(cnx, stream_id);
    }

    int ret = spsc_ring_push(ctx->rx_ring, &entry, bytes, (uint32_t)length);
    if (ret == 0) {
        aiopquic_notify_rx(ctx);
    }
    /* If push failed (ring full / OOM), the event is dropped. picoquic will
     * keep the bytes in its reassembly chain and re-deliver on the next
     * scheduling tick once the consumer drains. */

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
