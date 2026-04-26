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
 * Per-WebTransport-session context. Lives for the lifetime of the
 * WT session. Allocated when the asyncio side requests a WT session
 * open; freed on session-closed event after Python releases its
 * reference.
 */
typedef struct st_aiopquic_wt_session_t {
    aiopquic_ctx_t*           bridge;          /* shared rx/tx rings + eventfd */
    picoquic_cnx_t*           cnx;             /* per-session QUIC cnx */
    h3zero_callback_ctx_t*    h3_ctx;          /* picoquic-owned H3 ctx */
    h3zero_stream_ctx_t*      control_stream;  /* picoquic-owned control stream ctx */
    uint64_t                  control_stream_id;
    picowt_capsule_t          capsule;         /* incremental capsule accumulator */
    int                       session_ready;   /* CONNECT accepted */
    int                       session_closing; /* close/drain seen or initiated */
} aiopquic_wt_session_t;

static inline aiopquic_wt_session_t* aiopquic_wt_session_create(
        aiopquic_ctx_t* bridge) {
    aiopquic_wt_session_t* s =
        (aiopquic_wt_session_t*)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->bridge = bridge;
    return s;
}

static inline void aiopquic_wt_session_destroy(aiopquic_wt_session_t* s) {
    if (!s) return;
    picowt_release_capsule(&s->capsule);
    free(s);
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
            aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_DATA,
                                    sid, 0, bytes, (uint32_t)length);
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
            if (length > 0) {
                aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_DATA,
                                        sid, 0, bytes, (uint32_t)length);
            }
            aiopquic_wt_push_event(s, SPSC_EVT_WT_STREAM_FIN,
                                    sid, 0, NULL, 0);
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

#ifdef __cplusplus
}
#endif

#endif /* AIOPQUIC_H3WT_CALLBACK_H */
