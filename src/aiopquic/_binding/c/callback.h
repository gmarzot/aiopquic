/*
 * callback.h — picoquic callback function that bridges events to SPSC ring.
 *
 * This runs in the picoquic network thread. It receives all stream/connection
 * events from picoquic and writes them into the RX SPSC ring buffer for
 * consumption by the asyncio thread.
 *
 * Copyright (c) 2026, aiopquic contributors. BSD-3-Clause license.
 */

#ifndef AIOPQUIC_CALLBACK_H
#define AIOPQUIC_CALLBACK_H

#include "spsc_ring.h"
#include <picoquic.h>
#include <picoquic_internal.h>
#include <picoquic_packet_loop.h>

#include <string.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <fcntl.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/eventfd.h>
#endif

/*
 * Packed struct for SPSC_EVT_TX_CONNECT data payload.
 * Stored in the ring arena as the data for a CONNECT entry.
 */
typedef struct {
    struct sockaddr_in addr;      /* destination address (IPv4) */
    uint16_t sni_len;             /* length of SNI string */
    uint16_t alpn_len;            /* length of ALPN string */
    /* followed by: sni_len bytes of SNI, then alpn_len bytes of ALPN */
} aiopquic_connect_params_t;

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Context passed to the picoquic callback and loop callback.
 * Holds references to both RX and TX rings plus the eventfd.
 */
typedef struct {
    spsc_ring_t*    rx_ring;        /* picoquic → asyncio (events + data) */
    spsc_ring_t*    tx_ring;        /* asyncio → picoquic (send commands) */
    int             eventfd;        /* readable fd asyncio watches (eventfd on Linux,
                                       pipe read end elsewhere — self-pipe trick) */
    int             wake_write_fd;  /* fd the network thread writes to (== eventfd on
                                       Linux; pipe write end elsewhere) */
    picoquic_quic_t* quic;          /* back-reference to quic context */
} aiopquic_ctx_t;

/*
 * Create the bridge context. Returns NULL on failure.
 */
static inline aiopquic_ctx_t* aiopquic_ctx_create(
        uint32_t ring_capacity, uint32_t arena_size) {
    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)calloc(1, sizeof(aiopquic_ctx_t));
    if (!ctx) return NULL;

    ctx->rx_ring = spsc_ring_create(ring_capacity, arena_size);
    ctx->tx_ring = spsc_ring_create(ring_capacity, arena_size);
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
    /* Self-pipe trick: pipe[0] is read end (asyncio watches), pipe[1] is
     * write end (network thread signals). pipe2() isn't on macOS, so set
     * O_NONBLOCK | O_CLOEXEC via fcntl after creation. */
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

/* Destroy the bridge context */
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

/* Signal the asyncio thread that there are events to read.
 * Linux: 8-byte counter increment on the eventfd.
 * Other: single byte to the pipe write end (coalesced by drain). */
static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx) {
#ifdef __linux__
    uint64_t val = 1;
    (void)write(ctx->wake_write_fd, &val, sizeof(val));
#else
    uint8_t b = 1;
    (void)write(ctx->wake_write_fd, &b, 1);
#endif
}

/* Clear the wake fd (called by asyncio thread after draining the RX ring).
 * Linux: one 8-byte read returns and zeros the accumulated count.
 * Other: drain the pipe until EAGAIN — many notifies coalesce into one wake. */
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

/*
 * Map picoquic callback event to our SPSC event type.
 * Returns -1 for events we don't handle.
 */
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
 * The main picoquic stream/connection callback.
 * Runs in the picoquic network thread.
 *
 * This is registered with picoquic_create() or picoquic_set_callback().
 * It receives ALL events and writes them to the RX ring.
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

    int evt = aiopquic_map_event(fin_or_event);
    if (evt < 0) {
        /* Events we don't handle yet — just return success */
        return 0;
    }

    /* For prepare_to_send, we handle it differently (TX path) */
    if (fin_or_event == picoquic_callback_prepare_to_send) {
        /* Read from TX ring and provide data to picoquic */
        spsc_entry_t* tx_entry = spsc_ring_peek(ctx->tx_ring);
        if (tx_entry && tx_entry->stream_id == stream_id &&
            (tx_entry->event_type == SPSC_EVT_TX_STREAM_DATA ||
             tx_entry->event_type == SPSC_EVT_TX_STREAM_FIN)) {
            const uint8_t* data = spsc_ring_entry_data(ctx->tx_ring, tx_entry);
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
        /* Nothing to send — deactivate */
        (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
        return 0;
    }

    /* Push event + data to RX ring */
    spsc_entry_t entry = {0};
    entry.event_type = (uint32_t)evt;
    entry.stream_id = stream_id;
    entry.cnx = cnx;
    entry.stream_ctx = stream_ctx;

    if (fin_or_event == picoquic_callback_stream_reset) {
        entry.error_code = picoquic_get_remote_stream_error(cnx, stream_id);
    } else if (fin_or_event == picoquic_callback_stop_sending) {
        /* picoquic stores STOP_SENDING's error code in stream->remote_stop_error;
         * no public getter exists, so reach into picoquic_internal.h. */
        picoquic_stream_head_t* s = picoquic_find_stream(cnx, stream_id);
        if (s != NULL) entry.error_code = s->remote_stop_error;
    } else if (fin_or_event == picoquic_callback_application_close) {
        entry.error_code = picoquic_get_application_error(cnx);
    } else if (fin_or_event == picoquic_callback_close) {
        entry.error_code = picoquic_get_remote_error(cnx);
    }

    int ret = spsc_ring_push(ctx->rx_ring, &entry, bytes, (uint32_t)length);
    if (ret == 0) {
        aiopquic_notify_rx(ctx);
    }

    return 0;
}

/*
 * Packet loop callback — handles wake-up events to drain the TX ring.
 * Runs in the picoquic network thread.
 */
static int aiopquic_loop_cb(picoquic_quic_t* quic,
                             picoquic_packet_loop_cb_enum cb_mode,
                             void* callback_ctx,
                             void* callback_argv) {
    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)callback_ctx;

    switch (cb_mode) {
        case picoquic_packet_loop_ready:
            ctx->quic = quic;
            /* Signal Python that the loop is ready */
            spsc_ring_push_event(ctx->rx_ring, SPSC_EVT_READY, 0, NULL, 0);
            aiopquic_notify_rx(ctx);
            break;

        case picoquic_packet_loop_wake_up:
            /* Drain TX ring — process send commands from asyncio thread */
            while (1) {
                spsc_entry_t* entry = spsc_ring_peek(ctx->tx_ring);
                if (!entry) break;

                picoquic_cnx_t* cnx = (picoquic_cnx_t*)entry->cnx;

                /* CONNECT doesn't have a cnx yet */
                if (entry->event_type == SPSC_EVT_TX_CONNECT) {
                    const uint8_t* raw = spsc_ring_entry_data(
                        ctx->tx_ring, entry);
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
                            0,  /* preferred version */
                            sni_ptr, alpn_ptr,
                            aiopquic_stream_cb,
                            (void*)ctx);
                        if (new_cnx) {
                            /* picoquic_create_client_cnx already calls
                             * picoquic_start_client_cnx internally.
                             * Notify Python with cnx ptr. */
                            spsc_entry_t resp = {0};
                            resp.event_type = SPSC_EVT_ALMOST_READY;
                            resp.cnx = new_cnx;
                            spsc_ring_push(ctx->rx_ring, &resp,
                                           NULL, 0);
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
                        const uint8_t* data = spsc_ring_entry_data(ctx->tx_ring, entry);
                        int is_fin = (entry->event_type == SPSC_EVT_TX_STREAM_FIN);
                        picoquic_add_to_stream(cnx, entry->stream_id,
                                                data, entry->data_length, is_fin);
                        spsc_ring_pop(ctx->tx_ring);
                        break;
                    }
                    case SPSC_EVT_TX_DATAGRAM: {
                        const uint8_t* data = spsc_ring_entry_data(ctx->tx_ring, entry);
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
