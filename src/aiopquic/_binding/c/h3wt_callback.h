/*
 * h3wt_callback.h — H3/WebTransport adapter callbacks.
 *
 * Bridges picoquic's per-stream picohttp_call_back_event_t events
 * (delivered by the h3zero/picowt machinery) into the aiopquic SPSC
 * ring as SPSC_EVT_WT_* events for the asyncio thread to consume.
 *
 * One aiopquic_wt_session_t per WT session. Per-data-stream state
 * (aiopquic_stream_ctx_t + back-pointer to session) lives in
 * aiopquic_wt_stream_link_t, attached directly to h3zero's
 * stream_ctx->path_callback_ctx. Dispatch is O(0): the callback's
 * path_app_ctx IS the link (data streams) or the session (control
 * stream / session-level events) — no side-table lookup.
 *
 * path_callback_ctx is polymorphic between sessions and links.
 * A uint32_t kind tag at offset 0 of both structs discriminates and
 * doubles as a heap-corruption canary: freed-then-reused memory
 * won't read back AIOPQUIC_WT_CTX_SESSION or AIOPQUIC_WT_CTX_LINK,
 * so a wild path_app_ctx fails fast in dispatch rather than UB-ing
 * through the wrong type interpretation.
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
 * Discriminator tag for path_callback_ctx polymorphism. The tag also
 * acts as a heap canary — cleared to 0 on destroy so use-after-free
 * fails the dispatch check rather than re-interpreting bytes.
 */
typedef enum {
    AIOPQUIC_WT_CTX_SESSION = 0x57545301u,  /* 'WTS\1' */
    AIOPQUIC_WT_CTX_LINK    = 0x57544c01u,  /* 'WTL\1' */
} aiopquic_wt_ctx_kind_t;

/*
 * Packed payload for SPSC_EVT_TX_WT_OPEN. Stored as data_buf:
 *   header + sni + path + protocols.
 *
 * ALPN is always "h3" for WT, not carried.
 *
 * protocols is an OPTIONAL comma-separated list of subprotocol
 * identifiers to advertise in the WT-Available-Protocols header
 * (per WebTransport HTTP/3 spec §3.3). aiopquic treats it as an
 * opaque string — no interpretation; passed verbatim to
 * picowt_connect. Empty (protocols_len == 0) sends no header.
 */
typedef struct {
    struct sockaddr_in addr;
    uint16_t sni_len;
    uint16_t path_len;
    uint16_t protocols_len;
    /* followed by: sni_len bytes of SNI, then path_len bytes of path,
     * then protocols_len bytes of protocols. */
} aiopquic_wt_open_params_t;

/* Forward decls — link references session and vice versa via the
 * callback chain. */
struct st_aiopquic_wt_session_t;
typedef struct st_aiopquic_wt_session_t aiopquic_wt_session_t;

/*
 * Per-WT-stream link. Attached to h3zero's stream_ctx->path_callback_ctx
 * for data streams. The kind tag MUST be first — discriminator + canary.
 * Allocated once at first event for an inbound stream, or at create
 * time for an outbound stream we initiated. Freed by
 * picohttp_callback_free, which h3zero fires after the stream is gone
 * from picoquic's perspective.
 */
typedef struct st_aiopquic_wt_stream_link_t {
    uint32_t                  kind;        /* AIOPQUIC_WT_CTX_LINK */
    aiopquic_wt_session_t*    session;     /* back-pointer for routing */
    aiopquic_stream_ctx_t*    sc;          /* per-stream rx + tx rings */
    uint64_t                  stream_id;
} aiopquic_wt_stream_link_t;

/*
 * Per-WebTransport-session context. Lives for the lifetime of the
 * WT session. Allocated when the asyncio side requests a WT session
 * open (client) or on CONNECT arrival (server); freed on session-closed
 * event after Python releases its reference. kind tag MUST be first.
 */
struct st_aiopquic_wt_session_t {
    uint32_t                      kind;            /* AIOPQUIC_WT_CTX_SESSION */
    aiopquic_ctx_t*               bridge;          /* shared rx/tx rings + eventfd */
    picoquic_cnx_t*               cnx;             /* per-session QUIC cnx */
    h3zero_callback_ctx_t*        h3_ctx;          /* picoquic-owned H3 ctx */
    h3zero_stream_ctx_t*          control_stream;  /* picoquic-owned control stream ctx */
    uint64_t                      control_stream_id;
    picowt_capsule_t              capsule;         /* incremental capsule accumulator */
    int                           session_ready;   /* CONNECT accepted */
    int                           session_closing; /* close/drain seen or initiated */
    char*                         wt_protocol;     /* negotiated WT subprotocol (owned copy), NULL if none */
};

/*
 * Diagnostic helper — formats the cnx's logging CID as hex into a
 * static buffer (caller-private; not thread-safe for concurrent use).
 * Gated by AIOPQUIC_WT_DIAG to keep production output clean.
 */
static inline int aiopquic_wt_diag_enabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char* v = getenv("AIOPQUIC_WT_DIAG");
        cached = (v != NULL && v[0] != '\0' && v[0] != '0');
    }
    return cached;
}

static inline void aiopquic_wt_log_cnxid(const char* tag,
                                          picoquic_cnx_t* cnx,
                                          uint64_t sid) {
    if (!aiopquic_wt_diag_enabled() || cnx == NULL) return;
    picoquic_connection_id_t cid = picoquic_get_logging_cnxid(cnx);
    fprintf(stderr, "[wt-diag] %s cid=", tag);
    for (uint8_t i = 0; i < cid.id_len; i++) {
        fprintf(stderr, "%02x", cid.id[i]);
    }
    if (sid != UINT64_MAX) {
        fprintf(stderr, " sid=%llu", (unsigned long long)sid);
    }
    fprintf(stderr, "\n");
    fflush(stderr);
}

/* Copy a NUL-terminated string into a freshly malloc'd buffer
 * (self-contained; avoids depending on POSIX strdup feature macros).
 * Used to capture the negotiated WT subprotocol off picoquic-owned
 * memory so the session owns a copy independent of stream lifetime. */
static inline char* aiopquic_wt_strdup(const char* src) {
    if (src == NULL) return NULL;
    size_t n = strlen(src);
    char* d = (char*)malloc(n + 1);
    if (d != NULL) memcpy(d, src, n + 1);
    return d;
}

static inline aiopquic_wt_session_t* aiopquic_wt_session_create(
        aiopquic_ctx_t* bridge) {
    aiopquic_wt_session_t* s =
        (aiopquic_wt_session_t*)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->kind = AIOPQUIC_WT_CTX_SESSION;
    s->bridge = bridge;
    return s;
}

static inline void aiopquic_wt_session_destroy(aiopquic_wt_session_t* s) {
    if (!s) return;
    picowt_release_capsule(&s->capsule);
    free(s->wt_protocol);  /* free(NULL) is a no-op when unnegotiated */
    s->kind = 0;  /* clear canary so post-free dispatch fails fast */
    free(s);
}

static inline aiopquic_wt_stream_link_t* aiopquic_wt_stream_link_create(
        aiopquic_wt_session_t* s, uint64_t stream_id) {
    aiopquic_wt_stream_link_t* link =
        (aiopquic_wt_stream_link_t*)calloc(1, sizeof(*link));
    if (!link) return NULL;
    aiopquic_stream_ctx_t* sc = aiopquic_stream_ctx_create();
    if (!sc) {
        free(link);
        return NULL;
    }
    if (s && s->bridge) {
        s->bridge->cnt_sc_create_wt_link++;
    }
    link->kind = AIOPQUIC_WT_CTX_LINK;
    link->session = s;
    link->sc = sc;
    link->stream_id = stream_id;
    return link;
}

static inline void aiopquic_wt_stream_link_destroy(
        aiopquic_wt_stream_link_t* link) {
    if (!link) return;
    /* No session/bridge deref here: at cnx-teardown, session is freed
     * (h3wt_callback.h:~1115 / TX_WT_DEREGISTER) BEFORE every pending
     * LINK_RELEASE has been popped from rx_event_ring. Touching
     * link->session in this function = UAF on shutdown. The destroy
     * count is captured at call sites (close walker + drain_rx
     * LINK_RELEASE pop) where session validity is guaranteed. */
    if (link->sc) aiopquic_stream_ctx_destroy(link->sc);
    link->kind = 0;  /* clear canary */
    free(link);
}

/*
 * Resolve path_app_ctx into (session, link). Returns 0 on success,
 * -1 on a corrupted / unknown ctx (wild pointer, freed memory).
 * out_link is NULL for session-level events (CONNECT/control-stream).
 */
static inline int aiopquic_wt_ctx_resolve(
        void* path_app_ctx,
        aiopquic_wt_session_t** out_s,
        aiopquic_wt_stream_link_t** out_link) {
    if (!path_app_ctx) {
        *out_s = NULL;
        *out_link = NULL;
        return -1;
    }
    uint32_t kind = *(uint32_t*)path_app_ctx;
    if (kind == AIOPQUIC_WT_CTX_LINK) {
        aiopquic_wt_stream_link_t* link =
            (aiopquic_wt_stream_link_t*)path_app_ctx;
        *out_link = link;
        *out_s = link->session;
        return 0;
    }
    if (kind == AIOPQUIC_WT_CTX_SESSION) {
        *out_link = NULL;
        *out_s = (aiopquic_wt_session_t*)path_app_ctx;
        return 0;
    }
    *out_s = NULL;
    *out_link = NULL;
    return -1;
}

/*
 * Ensure a per-stream link is attached to stream_ctx. On first touch
 * (path_callback_ctx still points at the session via the prefix
 * declaration), allocate a fresh link, install it as path_callback_ctx,
 * and set *out_first_touch = 1 so the caller emits NEW_STREAM.
 * Subsequent calls return the existing link with *out_first_touch = 0.
 */
static inline aiopquic_wt_stream_link_t* aiopquic_wt_ensure_link(
        aiopquic_wt_session_t* s,
        aiopquic_wt_stream_link_t* maybe_link,
        h3zero_stream_ctx_t* stream_ctx,
        uint64_t stream_id,
        int* out_first_touch) {
    if (maybe_link) {
        *out_first_touch = 0;
        return maybe_link;
    }
    aiopquic_wt_stream_link_t* link = aiopquic_wt_stream_link_create(s, stream_id);
    if (!link) {
        *out_first_touch = 0;
        return NULL;
    }
    stream_ctx->path_callback_ctx = link;
    *out_first_touch = 1;
    return link;
}

/*
 * WT push: notification event carrying a BORROWED pointer to
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
    /* RX data-event coalescing — see the raw-QUIC twin in callback.h.
     * At most one outstanding WT_STREAM_DATA notification per
     * stream; losers skip (the in-flight notification's drain pops all
     * available sc->rx bytes). FIN always pushes. */
    if (event_type == SPSC_EVT_WT_STREAM_DATA) {
        if (!aiopquic_stream_ctx_rx_event_pending_arm(sc)) {
            s->bridge->cnt_rx_data_event_coalesced++;
            return;
        }
    }
    spsc_entry_t entry = {0};
    entry.event_type = event_type;
    entry.stream_id = stream_id;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;     /* session ptr for asyncio routing */
    entry.data_buf = sc;      /* borrowed; freed by link_destroy */
    entry.data_length = 0;    /* sentinel: do not free on pop */
    entry.is_fin = is_fin;
    int ret = spsc_ring_push_borrowed(s->bridge->rx_event_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        s->bridge->worker_rx_event_drops++;
        if (event_type == SPSC_EVT_WT_STREAM_DATA) {
            s->bridge->worker_rx_event_drops_stream_data++;
            /* Clear rx_event_pending so the next arrival retries. */
            aiopquic_stream_ctx_rx_event_pending_clear(sc);
        }
        if (aiopquic_rx_log_enabled()
                && s->bridge->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] WT EVENT RING FULL (sc): drop "
                "stream=%llu evt=%u (rx_event_ring entries=%u)\n",
                (unsigned long long)stream_id, event_type,
                spsc_ring_count(s->bridge->rx_event_ring));
        }
    }
}

/*
 * Push a WT event to the bridge's rx_event_ring. data may be NULL/0 for
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
    int ret = spsc_ring_push(s->bridge->rx_event_ring, &entry, data, data_len);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        s->bridge->worker_rx_event_drops++;
        if (event_type == SPSC_EVT_WT_STREAM_DATA) {
            s->bridge->worker_rx_event_drops_stream_data++;
        }
        if (aiopquic_rx_log_enabled()
                && s->bridge->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] WT EVENT RING FULL: drop "
                "stream=%llu evt=%u (rx_event_ring entries=%u)\n",
                (unsigned long long)stream_id, event_type,
                spsc_ring_count(s->bridge->rx_event_ring));
        }
    }
}

/*
 * Push NEW_STREAM with a BORROWED sc pointer so Python can stash it
 * in its _stream_tx_ctxs dict for subsequent push_stream_data calls
 * without a round-trip lookup.
 */
static inline void aiopquic_wt_push_new_stream(
        aiopquic_wt_session_t* s,
        uint64_t stream_id,
        aiopquic_stream_ctx_t* sc) {
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_NEW_STREAM;
    entry.stream_id = stream_id;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;
    entry.data_buf = sc;     /* BORROWED */
    entry.data_length = 0;   /* sentinel: do not free on pop */
    int ret = spsc_ring_push_borrowed(s->bridge->rx_event_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        s->bridge->worker_rx_event_drops++;
    }
}

/*
 * Push STREAM_LINK_RELEASE — the last event for a stream. Carries the
 * link pointer (in data_buf, data_length=0). drain_rx pops this in
 * FIFO order with all preceding stream events, so by the time it
 * runs Python has already drained sc->rx for every STREAM_DATA/FIN
 * for this stream. drain_rx then calls aiopquic_wt_stream_link_destroy
 * and does NOT emit the event to user code.
 *
 * Ordering is the load-bearing property: this is the mechanism that
 * prevents the BORROWED-sc UAF that motivated the LINK_RELEASE pattern.
 */
/*
 * Push WT_STREAM_DESTROY — surfaces "stream is fully retired" to the
 * WebTransportSession dispatcher so it can pop _stream_tx_ctxs[sid]
 * etc. Must be pushed BEFORE link_release so the FIFO order on the
 * SPSC ring guarantees Python sees the destroy event while the link
 * (and its sc) are still alive. drain_rx surfaces this and does NOT
 * call any destroy — LINK_RELEASE owns the sc ref drop.
 */
static inline int aiopquic_wt_push_stream_destroy(
        aiopquic_wt_session_t* s,
        uint64_t stream_id) {
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_STREAM_DESTROY;
    entry.stream_id = stream_id;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;  /* session ptr for asyncio routing */
    int ret = spsc_ring_push_borrowed(s->bridge->rx_event_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        /* RX ring full. The Python dict retains a stale entry for
         * this stream until session close clears it. The CALLER
         * must skip the paired link_release when this fails —
         * freeing the sc while Python's dict still maps the sid
         * would leave a dangling pointer for any sid-keyed access.
         * Bounded by per-cnx stream count; surfaced via the drop
         * counter so sustained drops are visible. */
        s->bridge->worker_rx_event_drops++;
    }
    return ret;
}

static inline void aiopquic_wt_push_link_release(
        aiopquic_wt_session_t* s,
        uint64_t stream_id,
        aiopquic_wt_stream_link_t* link) {
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_STREAM_LINK_RELEASE;
    entry.stream_id = stream_id;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;
    entry.data_buf = link;
    entry.data_length = 0;
    int ret = spsc_ring_push_borrowed(s->bridge->rx_event_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        /* RX ring full when worker tries to push LINK_RELEASE. We
         * cannot retry from here (picoquic owns the calling thread)
         * and we cannot synchronously destroy (Python may have
         * STREAM_DATA/FIN for this stream still in the ring → UAF).
         * Best path: leak the link. Surface via the existing drop
         * counter so sustained drops are visible. */
        s->bridge->worker_rx_event_drops++;
        if (aiopquic_rx_log_enabled()
                && s->bridge->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] LINK_RELEASE drop on full rx_event_ring: "
                "leaking link for stream=%llu\n",
                (unsigned long long)stream_id);
        }
    }
}

/*
 * Push STREAM_CREATED for an outbound stream we just opened. Carries
 * the BORROWED sc pointer (in data_buf, data_length=0) so Python can
 * register sc in _stream_tx_ctxs[sid] before the first push.
 */
static inline void aiopquic_wt_push_stream_created(
        aiopquic_wt_session_t* s,
        uint64_t stream_id,
        aiopquic_stream_ctx_t* sc,
        uint64_t error_code) {
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_STREAM_CREATED;
    entry.stream_id = stream_id;
    entry.error_code = error_code;
    entry.cnx = s->cnx;
    entry.stream_ctx = s;
    entry.data_buf = sc;     /* BORROWED, NULL on error */
    entry.data_length = 0;
    int ret = spsc_ring_push_borrowed(s->bridge->rx_event_ring, &entry);
    if (ret == 0) {
        aiopquic_notify_rx(s->bridge);
    } else {
        s->bridge->worker_rx_event_drops++;
    }
}

/*
 * The picohttp_post_data_cb_fn registered via picowt_connect.
 * Runs in the picoquic network thread.
 *
 * Translates picohttp_call_back_event_t → SPSC_EVT_WT_* and pushes
 * to rx_event_ring. Special-cases the control stream: data on the control
 * stream is capsule bytes; we feed it to picowt_receive_capsule and
 * surface CLOSE/DRAIN as session events instead of stream events.
 *
 * path_app_ctx is polymorphic:
 *   - For control-stream events (and the very first event seen on a
 *     prefix-routed data stream before we install a link), it is the
 *     session pointer.
 *   - For all subsequent data-stream events, it is the link pointer
 *     we installed on first touch.
 * aiopquic_wt_ctx_resolve discriminates by the kind tag at offset 0
 * and rejects wild / freed contexts.
 */
static int aiopquic_wt_path_callback(
        picoquic_cnx_t* cnx,
        uint8_t* bytes, size_t length,
        picohttp_call_back_event_t event,
        h3zero_stream_ctx_t* stream_ctx,
        void* path_app_ctx) {
    aiopquic_wt_session_t* s = NULL;
    aiopquic_wt_stream_link_t* link = NULL;
    if (aiopquic_wt_ctx_resolve(path_app_ctx, &s, &link) != 0 || !s) {
        return 0;
    }

    int is_control = (stream_ctx != NULL &&
                       stream_ctx->stream_id == s->control_stream_id);
    uint64_t sid = stream_ctx ? stream_ctx->stream_id : 0;

    switch (event) {
    case picohttp_callback_connecting:
        break;

    case picohttp_callback_connect_accepted:
        s->session_ready = 1;
        /* Capture the server's selected WT-Protocol from the parsed
         * CONNECT response header. h3zero populates header.wt_protocol
         * (NUL-terminated) before firing this event; the buffer is
         * h3zero-owned and lives until stream cleanup, so we copy it
         * onto the session now. NULL when the server sent no WT-Protocol. */
        if (stream_ctx != NULL &&
            stream_ctx->ps.stream_state.header.wt_protocol != NULL) {
            s->wt_protocol = aiopquic_wt_strdup(
                (const char*)stream_ctx->ps.stream_state.header.wt_protocol);
        }
        if (s->bridge != NULL && s->bridge->keep_alive_us > 0) {
            /* PING keep-alive on the WT cnx — same rationale as the
             * raw-QUIC path: hold a quiet (e.g. FC-stalled) connection
             * open past the idle timeout. Worker thread owns cnx here. */
            picoquic_enable_keep_alive(cnx, s->bridge->keep_alive_us);
        }
        aiopquic_wt_log_cnxid("client-cnx-ready", cnx, UINT64_MAX);
        aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_READY,
                                s->control_stream_id, 0, NULL, 0);
        break;

    case picohttp_callback_connect_refused:
        aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_REFUSED,
                                s->control_stream_id, 0, NULL, 0);
        break;

    case picohttp_callback_post_data:
        if (is_control) {
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
                picowt_release_capsule(&s->capsule);
                memset(&s->capsule, 0, sizeof(s->capsule));
            }
        } else {
            /* Data stream: ensure a link is attached. First touch
             * triggers NEW_STREAM (which carries the sc as BORROWED
             * for the Python-side _stream_tx_ctxs dict). */
            int first_touch = 0;
            link = aiopquic_wt_ensure_link(s, link, stream_ctx, sid,
                                            &first_touch);
            if (!link) return -1;
            aiopquic_stream_ctx_t* sc = link->sc;

            uint32_t advertise_cap =
                s->bridge->rx_data_ring_cap > 0
                    ? s->bridge->rx_data_ring_cap
                    : AIOPQUIC_RX_DATA_RING_CAP_DEFAULT;
            int rx_first = (sc->rx == NULL);
            if (rx_first) {
                /* Switch picoquic to app-controlled FC on this stream.
                 * The actual MAX_STREAM_DATA grant is issued via
                 * picoquic_open_flow_control AFTER we push the
                 * just-delivered bytes — see the FC block below. */
                (void)picoquic_set_app_flow_control(cnx, sid, 1);
            }
            if (aiopquic_stream_ctx_ensure_rx(sc, advertise_cap) != 0) {
                return -1;
            }
            uint32_t pushed = aiopquic_stream_buf_push(
                sc->rx, bytes, (uint32_t)length);
            if (pushed > 0) {
                aiopquic_sc_rx_bytes_pushed_add(pushed);
            }
            if (pushed != length) {
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
            /* FC management: at first-touch we grant the initial buffer
             * capacity to peer (buf_free post-push = capacity - length).
             * From then on, peer's credit is REPLENISHED by Python's
             * drain side via SPSC_EVT_TX_OPEN_FLOW_CONTROL — see
             * _transport.pyx drain_rx. The worker doing extension here
             * inside post_data can't unblock a peer that's FC-stalled
             * (no inbound bytes → no callback → no extension), so we
             * rely on the consumer-driven model from picoquic's own
             * flow_control_test pattern. */
            if (rx_first) {
                uint32_t buf_free = aiopquic_stream_buf_free(sc->rx);
                (void)picoquic_open_flow_control(cnx, sid, buf_free);
                aiopquic_stream_ctx_rx_credit_store(sc, advertise_cap);
            }
            if (first_touch) {
                aiopquic_wt_push_new_stream(s, sid, sc);
            }
            aiopquic_wt_push_event_with_sc(
                s, SPSC_EVT_WT_STREAM_DATA, sid, sc, 0);
        }
        break;

    case picohttp_callback_post_fin:
        if (is_control) {
            if (length > 0) {
                int rc = picowt_receive_capsule(cnx, bytes,
                                                  bytes + length, &s->capsule);
                (void)rc;
            }
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                                    s->control_stream_id, 0, NULL, 0);
            s->session_closing = 1;
        } else {
            int first_touch = 0;
            link = aiopquic_wt_ensure_link(s, link, stream_ctx, sid,
                                            &first_touch);
            if (!link) return -1;
            aiopquic_stream_ctx_t* sc = link->sc;

            uint32_t advertise_cap =
                s->bridge->rx_data_ring_cap > 0
                    ? s->bridge->rx_data_ring_cap
                    : AIOPQUIC_RX_DATA_RING_CAP_DEFAULT;
            int rx_first = (sc->rx == NULL);
            if (rx_first) {
                (void)picoquic_set_app_flow_control(cnx, sid, 1);
            }
            if (aiopquic_stream_ctx_ensure_rx(sc, advertise_cap) != 0) {
                return -1;
            }
            if (length > 0) {
                uint32_t pushed = aiopquic_stream_buf_push(
                    sc->rx, bytes, (uint32_t)length);
                if (pushed > 0) {
                    aiopquic_sc_rx_bytes_pushed_add(pushed);
                }
                if (pushed != length) {
                    s->bridge->worker_rx_byte_ring_overflow++;
                    return -1;
                }
            }
            aiopquic_stream_buf_set_fin(sc->rx);

            /* On FIN we still need to issue a MAX_STREAM_DATA grant on
             * first-touch so picoquic's accounting matches our buffer
             * state; subsequent extensions are unnecessary since the
             * stream is over. See post_data above for the rationale on
             * passing buf_free. */
            if (rx_first) {
                uint32_t buf_free = aiopquic_stream_buf_free(sc->rx);
                (void)picoquic_open_flow_control(cnx, sid, buf_free);
                aiopquic_stream_ctx_rx_credit_store(
                    sc, aiopquic_stream_ctx_rx_consumed_load(sc) + advertise_cap);
            }

            if (first_touch) {
                aiopquic_wt_push_new_stream(s, sid, sc);
            }
            aiopquic_wt_push_event_with_sc(
                s, SPSC_EVT_WT_STREAM_FIN, sid, sc, 1);
        }
        break;

    case picohttp_callback_provide_data: {
        /* TX path. link MUST exist for an outbound stream — we set
         * it at create time. A provide_data on a peer-opened bidi
         * stream we haven't replied on yet → link exists from the
         * RX first-touch path but sc->tx is NULL. Either way: no
         * tx ring → provide 0. */
        if (!link || !link->sc || !link->sc->tx) {
            (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
            break;
        }
        aiopquic_stream_ctx_t* sc = link->sc;
        aiopquic_stream_buf_t* sb = sc->tx;
        uint32_t want = (uint32_t)length;
        uint32_t avail = aiopquic_stream_buf_used(sb);
        uint32_t to_send = (avail < want) ? avail : want;
        int fin_after = aiopquic_stream_buf_fin_pending(sb);
        int is_fin = (fin_after && to_send == avail) ? 1 : 0;
        int still_active = (avail > to_send) ? 1 : 0;
        uint8_t* buf = picoquic_provide_stream_data_buffer(
            bytes, to_send, is_fin, still_active);
        if (buf && to_send > 0) {
            aiopquic_stream_buf_pop(sb, buf, to_send);
            aiopquic_tx_data_bytes_pulled_add(to_send);
            if (getenv("AIOPQUIC_WT_DEBUG") != NULL) {
                fprintf(stderr, "[wt-debug] provide sid=%llu len=%u "
                        "is_fin=%d still_active=%d hex=",
                        (unsigned long long)sid, to_send,
                        is_fin, still_active);
                uint32_t dump = to_send < 32 ? to_send : 32;
                for (uint32_t i = 0; i < dump; i++) {
                    fprintf(stderr, "%02x", buf[i]);
                }
                fprintf(stderr, "\n");
                fflush(stderr);
            }
        }
        /* Edge-trigger TX backpressure: if a Python writer was
         * blocked (set tx_drain_pending=1 after seeing sc->tx full),
         * CAS-clear and emit one SPSC_EVT_STREAM_TX_DRAINED so it
         * wakes and retries. Hoisted OUTSIDE the to_send>0 guard so
         * a zero-byte provide_data still wakes the writer when the
         * ring genuinely drained via concurrent path (defensive vs
         * picoquic window-math edge cases). Single-shot per arm —
         * only the CAS winner pushes. */
        {
            uint32_t expected = 1;
            if (atomic_compare_exchange_strong_explicit(
                    &sc->tx_drain_pending, &expected, 0,
                    memory_order_acq_rel, memory_order_relaxed)) {
                spsc_entry_t drain_entry = {0};
                drain_entry.event_type = SPSC_EVT_STREAM_TX_DRAINED;
                drain_entry.stream_id = sid;
                drain_entry.cnx = s->cnx;
                drain_entry.stream_ctx = s;
                if (spsc_ring_push(s->bridge->rx_event_ring,
                                   &drain_entry, NULL, 0) == 0) {
                    sc->cnt_drain_fires++;
                    sc->last_drain_fire_ns = aiopquic_now_ns();
                    aiopquic_notify_rx(s->bridge);
                } else {
                    sc->cnt_drain_dropped++;
                    atomic_store_explicit(
                        &sc->tx_drain_pending, 1,
                        memory_order_release);
                }
            }
        }
        /* On FIN we don't free the link here — picoquic still owns
         * the stream until h3zero fires picohttp_callback_free. */
        break;
    }

    case picohttp_callback_post_datagram:
        aiopquic_wt_push_event(s, SPSC_EVT_WT_DATAGRAM,
                                s->control_stream_id, 0,
                                bytes, (uint32_t)length);
        break;

    case picohttp_callback_provide_datagram: {
        spsc_entry_t* tx = spsc_ring_peek(s->bridge->tx_event_ring);
        if (tx && tx->event_type == SPSC_EVT_TX_DATAGRAM) {
            uint32_t to_send = tx->data_length;
            if (to_send > length) to_send = (uint32_t)length;
            void* buf = h3zero_provide_datagram_buffer(stream_ctx, to_send, 0);
            if (buf && tx->data_buf) memcpy(buf, tx->data_buf, to_send);
            spsc_ring_pop(s->bridge->tx_event_ring);
        }
        break;
    }

    case picohttp_callback_reset:
        if (aiopquic_wt_diag_enabled()) {
            fprintf(stderr,
                "[wt-diag] picohttp_callback_reset sid=%llu "
                "remote_err=%llu control=%d\n",
                (unsigned long long)sid,
                (unsigned long long)picoquic_get_remote_stream_error(cnx, sid),
                is_control ? 1 : 0);
            fflush(stderr);
        }
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
            /* link is freed at picohttp_callback_free below. */
        }
        break;

    case picohttp_callback_stop_sending:
        if (aiopquic_wt_diag_enabled()) {
            fprintf(stderr,
                "[wt-diag] picohttp_callback_stop_sending sid=%llu "
                "remote_err=%llu\n",
                (unsigned long long)sid,
                (unsigned long long)picoquic_get_remote_stream_error(cnx, sid));
            fflush(stderr);
        }
        aiopquic_wt_push_event(s, SPSC_EVT_WT_STOP_SENDING,
                                sid,
                                picoquic_get_remote_stream_error(cnx, sid),
                                NULL, 0);
        /* link is freed at picohttp_callback_free below. */
        break;

    case picohttp_callback_deregister:
        if (!s->session_closing) {
            aiopquic_wt_push_event(s, SPSC_EVT_WT_SESSION_CLOSED,
                                    s->control_stream_id, 0, NULL, 0);
            s->session_closing = 1;
        }
        break;

    case picohttp_callback_free:
        /* h3zero is releasing this stream_ctx. By this point picoquic
         * has fully retired the stream — no further provide_data /
         * post_data / reset events will fire.
         *
         * Push STREAM_LINK_RELEASE so drain_rx tears down the link
         * AFTER it has popped every preceding STREAM_DATA / STREAM_FIN
         * / RESET / STOP_SENDING for this stream out of the SPSC ring.
         * The ring's FIFO order is what guarantees Python has drained
         * sc->rx before sc is freed — the lifetime invariant that
         * makes the BORROWED-sc convention safe. */
        if (aiopquic_wt_diag_enabled()) {
            fprintf(stderr,
                "[wt-diag] picohttp_callback_free sid=%llu "
                "link=%s control=%d session_closing=%d\n",
                (unsigned long long)sid,
                link ? "yes" : "no",
                is_control ? 1 : 0,
                s->session_closing);
            fflush(stderr);
        }
        if (link && stream_ctx
                && stream_ctx->path_callback_ctx == link) {
            stream_ctx->path_callback_ctx = NULL;
            /* Order: STREAM_DESTROY first (Python dict cleanup),
             * then LINK_RELEASE (link + sc ref drop). FIFO on the
             * SPSC ring preserves this ordering at the consumer.
             * PAIRED: if the destroy push fails (ring full), skip
             * the release too — otherwise drain_rx frees the sc
             * while Python's dict still maps the sid, leaving a
             * dangling pointer for any sid-keyed access. Leaking
             * the link+sc instead preserves "present in dict ⇒ sc
             * alive"; bounded by per-cnx stream count and swept at
             * session close. */
            if (aiopquic_wt_push_stream_destroy(s, sid) == 0) {
                aiopquic_wt_push_link_release(s, sid, link);
            }
        } else if (link) {
            /* Diagnostic: cleanup skipped despite a link being
             * provided via path_app_ctx. Tracks the leak signature
             * for the May-23 sub-side retention investigation. */
            s->bridge->cnt_wt_callback_free_skipped++;
            if (aiopquic_wt_diag_enabled()) {
                fprintf(stderr,
                    "[wt-diag] callback_free SKIP sid=%llu link=%p "
                    "stream_ctx=%p path_ctx=%p match=%d\n",
                    (unsigned long long)sid,
                    (void*)link,
                    (void*)stream_ctx,
                    stream_ctx ? stream_ctx->path_callback_ctx : NULL,
                    stream_ctx
                        ? (stream_ctx->path_callback_ctx == link) : 0);
                fflush(stderr);
            }
        }
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
 * landing at the registered path; we accept the WT session, allocate
 * a per-session aiopquic_wt_session_t, re-point the stream's callback
 * to aiopquic_wt_path_callback (the per-session handler used by the
 * client), declare the stream prefix so peer-opened WT streams within
 * this session route through us, and push SPSC_EVT_WT_NEW_SESSION so
 * the asyncio side can construct a Python WebTransportServerSession.
 *
 * Prefix-routed inbound streams arrive at aiopquic_wt_path_callback
 * with path_app_ctx == session (the declare_stream_prefix arg); the
 * per-session callback's first-touch path allocates a link and
 * rebinds stream_ctx->path_callback_ctx so subsequent dispatch is
 * O(0) via the link.
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
    if (bridge != NULL && bridge->keep_alive_us > 0) {
        /* PING keep-alive on the accepted WT cnx (worker thread). */
        picoquic_enable_keep_alive(cnx, bridge->keep_alive_us);
    }
    aiopquic_wt_log_cnxid("server-cnx-accept", cnx, stream_ctx->stream_id);

    /* WT subprotocol selection: pick the highest mutual from the
     * client's WT-Available-Protocols against our configured allowlist.
     * picowt_select_wt_protocol reads the client offer off stream_ctx
     * and, on a match, sets stream_state.wt_protocol — which h3zero then
     * emits as the WT-Protocol response header. We copy it onto the
     * session so Python can read the negotiated value. No allowlist or
     * no match → no WT-Protocol header sent (s->wt_protocol stays NULL).
     *
     * Deliberately PERMISSIVE: unlike raw-QUIC ALPN (mandatory in TLS, a
     * mismatch hard-fails the handshake), WT-Protocol is OPTIONAL, so the
     * CONNECT still succeeds with no subprotocol when there is no overlap
     * (or the client offered none — picowt can't tell those apart). The
     * version-mismatch policy belongs to the application layer (aiomoqt),
     * which sees negotiated_protocol == None and decides whether to close. */
    if (bridge->wt_supported_protocols != NULL &&
        picowt_select_wt_protocol(
            stream_ctx, bridge->wt_supported_protocols) == 0 &&
        stream_ctx->ps.stream_state.wt_protocol != NULL) {
        s->wt_protocol =
            aiopquic_wt_strdup(stream_ctx->ps.stream_state.wt_protocol);
    }

    stream_ctx->path_callback = aiopquic_wt_path_callback;
    stream_ctx->path_callback_ctx = s;

    (void)h3zero_declare_stream_prefix(
        h3_ctx, s->control_stream_id,
        aiopquic_wt_path_callback, s);

    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_WT_NEW_SESSION;
    entry.stream_id = s->control_stream_id;
    entry.cnx = cnx;
    entry.stream_ctx = s;
    int rc = spsc_ring_push(bridge->rx_event_ring, &entry,
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
        char protocols_buf[256];
        const char* protocols_arg = NULL;
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
        offset += p->path_len;

        if (p->protocols_len > 0
                && p->protocols_len < sizeof(protocols_buf)
                && offset + p->protocols_len <= entry->data_length) {
            memcpy(protocols_buf, raw + offset, p->protocols_len);
            protocols_buf[p->protocols_len] = '\0';
            protocols_arg = protocols_buf;
        }

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
        /* Suppress h3zero's stdout banner on client-side cnx close
         * ("Received a connection close request"). h3zero_common.c
         * gates on !ctx->no_print; raw-QUIC has no equivalent print
         * so this matches WT to raw-QUIC behavior. */
        h3_ctx->no_print = 1;

        rc = picowt_connect(cnx, h3_ctx, control_stream,
                              sni_buf, path_buf,
                              aiopquic_wt_path_callback,
                              (void*)s, protocols_arg);
        if (rc == 0) {
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
            aiopquic_wt_push_stream_created(s, 0, NULL, 1);
            return 1;
        }
        /* Allocate the link now so the first provide_data has sc->tx
         * ready and Python's _stream_tx_ctxs[sid] dict gets the same
         * sc pointer via the STREAM_CREATED event below. */
        aiopquic_wt_stream_link_t* link =
            aiopquic_wt_stream_link_create(s, new_stream->stream_id);
        if (!link) {
            aiopquic_wt_push_stream_created(s, new_stream->stream_id,
                                              NULL, 2);
            return 1;
        }
        new_stream->path_callback = aiopquic_wt_path_callback;
        new_stream->path_callback_ctx = link;
        aiopquic_wt_push_stream_created(s, new_stream->stream_id,
                                          link->sc, 0);
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

    case SPSC_EVT_TX_WT_SESSION_CLEANUP: {
        /* Bulk-free this session's per-stream wt_links without
         * tearing down the session itself. Mirrors step 1 of
         * TX_WT_DEREGISTER. Used by SESSION_CLOSED handler when the
         * cnx is stalled (cwin pinned, peer disconnected, BBR #2118
         * freeze) and per-sid RESETs can't be transmitted. The
         * session object survives so the Python wrapper's __dealloc__
         * can later push TX_WT_DEREGISTER for full teardown.
         *
         * Idempotent: subsequent calls find empty splay tree and
         * no-op. */
        if (s && s->cnx && s->h3_ctx) {
            picosplay_node_t* node =
                picosplay_first(&s->h3_ctx->h3_stream_tree);
            while (node != NULL) {
                h3zero_stream_ctx_t* st =
                    (h3zero_stream_ctx_t*)picohttp_stream_node_value(node);
                picosplay_node_t* next = picosplay_next(node);
                if (st != NULL && st->path_callback_ctx != NULL) {
                    uint32_t kind = *(uint32_t*)st->path_callback_ctx;
                    if (kind == AIOPQUIC_WT_CTX_LINK) {
                        aiopquic_wt_stream_link_t* lk =
                            (aiopquic_wt_stream_link_t*)
                                st->path_callback_ctx;
                        if (lk->session == s) {
                            st->path_callback = NULL;
                            st->path_callback_ctx = NULL;
                            if (s->bridge) {
                                s->bridge->cnt_sc_destroy_wt_link_close_walker++;
                            }
                            aiopquic_wt_stream_link_destroy(lk);
                        }
                    }
                }
                node = next;
            }
        }
        return 1;
    }

    case SPSC_EVT_TX_WT_DEREGISTER: {
        /* Python is releasing this session. Cleanup has three steps
         * that MUST happen in order, all on the worker thread:
         *
         * 1. Walk h3zero's splay tree and free any per-stream links
         *    that belong to THIS session. We must do this BEFORE
         *    picowt_deregister because picowt_deregister nulls each
         *    data stream's path_callback_ctx before deleting the
         *    h3zero stream_ctx — which means picohttp_callback_free
         *    is NOT dispatched for our streams, and our link+sc
         *    memory would be orphaned (leak). The kind discriminator
         *    + session pointer check ensures we only free our own.
         *
         * 2. Null the control stream's path_callback / _ctx. picowt_
         *    deregister does NOT touch the control stream's callback;
         *    h3zero will delete the control stream node later when
         *    the cnx closes and fire picohttp_callback_free on it.
         *    By then `s` is freed — that callback would UAF. Nulling
         *    path_callback makes h3zero skip dispatching to us.
         *
         * 3. Now picowt_deregister + session_destroy can run safely.
         *
         * Done at session-destroy time only (once per session). No
         * per-stream tracking overhead during active operation. */
        if (s) {
            if (s->cnx && s->h3_ctx && s->control_stream) {
                /* Step 1: free this session's per-stream links. */
                picosplay_node_t* node =
                    picosplay_first(&s->h3_ctx->h3_stream_tree);
                while (node != NULL) {
                    h3zero_stream_ctx_t* st =
                        (h3zero_stream_ctx_t*)picohttp_stream_node_value(node);
                    picosplay_node_t* next = picosplay_next(node);
                    if (st != NULL && st->path_callback_ctx != NULL) {
                        uint32_t kind = *(uint32_t*)st->path_callback_ctx;
                        if (kind == AIOPQUIC_WT_CTX_LINK) {
                            aiopquic_wt_stream_link_t* lk =
                                (aiopquic_wt_stream_link_t*)
                                    st->path_callback_ctx;
                            if (lk->session == s) {
                                st->path_callback = NULL;
                                st->path_callback_ctx = NULL;
                                if (s->bridge) {
                                    s->bridge->cnt_sc_destroy_wt_link_close_walker++;
                                }
                                aiopquic_wt_stream_link_destroy(lk);
                            }
                        }
                    }
                    node = next;
                }
                /* Step 2: detach the control stream's callback so the
                 * eventual picohttp_callback_free at cnx-close doesn't
                 * dispatch into a freed session pointer. */
                s->control_stream->path_callback = NULL;
                s->control_stream->path_callback_ctx = NULL;
                /* Step 3: now safe to deregister + free. */
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
