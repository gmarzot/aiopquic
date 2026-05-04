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
            /* First data on a peer-opened stream: announce it before
             * delivering payload. h3zero increments post_received only
             * after this callback returns, so post_received == 0 is a
             * clean first-touch marker. */
            if (stream_ctx->post_received == 0) {
                aiopquic_wt_push_event(s, SPSC_EVT_WT_NEW_STREAM,
                                        sid, 0, NULL, 0);
            }
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
            if (stream_ctx->post_received == 0) {
                aiopquic_wt_push_event(s, SPSC_EVT_WT_NEW_STREAM,
                                        sid, 0, NULL, 0);
            }
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
