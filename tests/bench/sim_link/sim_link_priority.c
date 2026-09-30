/* Does picoquic's stream priority actually change delivery?
 *
 * Opens N streams the server sends on concurrently over a link too slow to
 * carry them all, assigns each a different priority, and reports bytes
 * received per stream. Loopback cannot answer this: with no congestion
 * there is nothing to schedule, so every stream is served and priority is
 * invisible. picoquictest_sim_link gives a rate we choose, in simulated
 * time, so the result is deterministic — no host jitter, unlike the
 * wall-clock loopback benches.
 *
 * Priorities are set on the SENDING side (the server here). They are the
 * transport byte, not a MoQT priority: lower is more urgent, and the low
 * bit selects picoquic's discipline among equals rather than adding
 * resolution, so the values below stay on one parity.
 *
 * Usage:
 *   PICOQUIC_SOLUTION_DIR=third_party/picoquic/ \
 *       tests/bench/sim_link/sim_link_priority \
 *           --streams 4 --duration-s 2 --rate-mbps 10
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "picoquic.h"
#include "picoquic_internal.h"
#include "picoquic_utils.h"
#include "picoquictest_internal.h"

#define PRIO_ALPN "sim-prio"
#define MAX_STREAMS 8

typedef struct stream_slot {
    uint64_t stream_id;
    uint8_t priority;
    int64_t bytes_recv;
    int64_t bytes_sent;
    int active;
} stream_slot_t;

typedef struct prio_ctx {
    int is_client;
    int n_streams;
    stream_slot_t slots[MAX_STREAMS];
} prio_ctx_t;

static stream_slot_t* slot_of(prio_ctx_t* ctx, uint64_t sid)
{
    for (int i = 0; i < ctx->n_streams; i++) {
        if (ctx->slots[i].active && ctx->slots[i].stream_id == sid) {
            return &ctx->slots[i];
        }
    }
    return NULL;
}

static int prio_callback(picoquic_cnx_t* cnx,
    uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t fin_or_event,
    void* callback_ctx, void* v_stream_ctx)
{
    prio_ctx_t* ctx = (prio_ctx_t*)callback_ctx;
    int ret = 0;
    (void)v_stream_ctx;

    if (ctx == NULL) {
        ctx = (prio_ctx_t*)picoquic_get_default_callback_context(
            picoquic_get_quic_ctx(cnx));
        if (ctx == NULL) return -1;
        picoquic_set_callback(cnx, prio_callback, ctx);
    }

    switch (fin_or_event) {
    case picoquic_callback_almost_ready:
    case picoquic_callback_ready:
        if (ctx->is_client && !ctx->slots[0].active) {
            /* Open every stream and ping each, so the server sees them
             * all before it starts sending and schedules across the set
             * rather than draining them in arrival order. */
            uint8_t ping[8] = {0};
            for (int i = 0; i < ctx->n_streams; i++) {
                uint64_t sid = picoquic_get_next_local_stream_id(cnx, 0);
                ctx->slots[i].stream_id = sid;
                ctx->slots[i].active = 1;
                ret = picoquic_add_to_stream(cnx, sid, ping, sizeof(ping), 0);
                if (ret != 0) break;
            }
        }
        break;

    case picoquic_callback_stream_data:
    case picoquic_callback_stream_fin: {
        stream_slot_t* s = slot_of(ctx, stream_id);
        if (ctx->is_client) {
            if (s && length > 0) s->bytes_recv += (int64_t)length;
            break;
        }
        /* Server: first sight of a stream. Claim the next free slot, set
         * its priority, and mark it active so prepare_to_send starts
         * asking us for bytes. */
        if (s == NULL) {
            for (int i = 0; i < ctx->n_streams; i++) {
                if (!ctx->slots[i].active) {
                    ctx->slots[i].stream_id = stream_id;
                    ctx->slots[i].active = 1;
                    picoquic_set_stream_priority(cnx, stream_id,
                                                 ctx->slots[i].priority);
                    ret = picoquic_mark_active_stream(cnx, stream_id, 1, NULL);
                    break;
                }
            }
        }
        break;
    }

    case picoquic_callback_prepare_to_send: {
        stream_slot_t* s = slot_of(ctx, stream_id);
        if (s == NULL || ctx->is_client) break;
        /* Always have more to send: the point is contention, so no
         * stream may run dry and free the link for the others. */
        uint8_t* buf = picoquic_provide_stream_data_buffer(
            bytes, length, /* fin */ 0, /* still_active */ 1);
        if (buf != NULL) {
            memset(buf, 0xA5, length);
            s->bytes_sent += (int64_t)length;
        }
        break;
    }

    case picoquic_callback_stop_sending:
    case picoquic_callback_stream_reset:
        ret = picoquic_mark_active_stream(cnx, stream_id, 0, NULL);
        break;

    case picoquic_callback_close:
    case picoquic_callback_application_close:
        picoquic_set_callback(cnx, NULL, NULL);
        break;

    default:
        break;
    }

    return ret;
}

static void usage(const char* argv0)
{
    fprintf(stderr,
        "usage: %s [--streams N] [--duration-s S] [--rate-mbps R]\n"
        "          [--rtt-us U] [--base-priority P] [--step D]\n"
        "\n"
        "  --streams        concurrent streams, 2-%d (default 4)\n"
        "  --duration-s     simulated seconds after handshake (default 2)\n"
        "  --rate-mbps      link rate; low enough to force contention\n"
        "                   (default 10)\n"
        "  --rtt-us         round-trip latency (default 1000)\n"
        "  --base-priority  priority of stream 0 (default 1)\n"
        "  --step           added per stream; keep even to hold one\n"
        "                   discipline (default 2)\n",
        argv0, MAX_STREAMS);
}

int main(int argc, char** argv)
{
    int n_streams = 4;
    double duration_s = 2.0;
    double rate_mbps = 10.0;
    int64_t rtt_us = 1000;
    int base_priority = 1;
    int step = 2;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--streams") && i + 1 < argc) {
            n_streams = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--duration-s") && i + 1 < argc) {
            duration_s = strtod(argv[++i], NULL);
        } else if (!strcmp(argv[i], "--rate-mbps") && i + 1 < argc) {
            rate_mbps = strtod(argv[++i], NULL);
        } else if (!strcmp(argv[i], "--rtt-us") && i + 1 < argc) {
            rtt_us = strtoll(argv[++i], NULL, 10);
        } else if (!strcmp(argv[i], "--base-priority") && i + 1 < argc) {
            base_priority = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--step") && i + 1 < argc) {
            step = atoi(argv[++i]);
        } else {
            usage(argv[0]);
            return 1;
        }
    }
    if (n_streams < 2 || n_streams > MAX_STREAMS) {
        usage(argv[0]);
        return 1;
    }

    {
        const char* dir = getenv("PICOQUIC_SOLUTION_DIR");
        if (dir) picoquic_solution_dir = dir;
    }

    prio_ctx_t client_ctx = {0};
    prio_ctx_t server_ctx = {0};
    client_ctx.is_client = 1;
    client_ctx.n_streams = n_streams;
    server_ctx.n_streams = n_streams;
    for (int i = 0; i < n_streams; i++) {
        int p = base_priority + i * step;
        if (p > 255) p = 255;
        server_ctx.slots[i].priority = (uint8_t)p;
    }

    uint64_t simulated_time = 0;
    uint64_t loss_mask = 0;
    picoquic_test_tls_api_ctx_t* tctx = NULL;
    picoquic_connection_id_t initial_cid = {{0xa1, 0x0a, 0xb0, 0xb1,
                                              0xb2, 0xb3, 0xb4, 0xb6}, 8};

    int ret = tls_api_init_ctx_ex(&tctx,
        PICOQUIC_INTERNAL_TEST_VERSION_1,
        PICOQUIC_TEST_SNI, PRIO_ALPN, &simulated_time,
        NULL, NULL, 0, 1, 0, &initial_cid);
    if (ret != 0 || !tctx || !tctx->cnx_client || !tctx->qserver) {
        fprintf(stderr, "tls_api_init_ctx_ex failed (ret=%d)\n", ret);
        return 1;
    }

    if (rate_mbps > 0.0 && tctx->c_to_s_link && tctx->s_to_c_link) {
        uint64_t psec = (uint64_t)(8000000.0 / rate_mbps);
        tctx->c_to_s_link->picosec_per_byte = psec;
        tctx->c_to_s_link->microsec_latency = (uint64_t)(rtt_us / 2);
        tctx->s_to_c_link->picosec_per_byte = psec;
        tctx->s_to_c_link->microsec_latency = (uint64_t)(rtt_us / 2);
    }

    picoquic_set_default_callback(tctx->qserver, prio_callback, &server_ctx);
    picoquic_set_callback(tctx->cnx_client, prio_callback, &client_ctx);

    ret = picoquic_start_client_cnx(tctx->cnx_client);
    if (ret != 0) {
        fprintf(stderr, "picoquic_start_client_cnx failed (ret=%d)\n", ret);
        return 1;
    }
    ret = tls_api_connection_loop(tctx, &loss_mask, 0, &simulated_time);
    if (ret != 0) {
        fprintf(stderr, "handshake failed (ret=%d)\n", ret);
        return 1;
    }

    uint64_t deadline = simulated_time + (uint64_t)(duration_s * 1e6);
    uint64_t sim_time_out = deadline + 60000000ULL;
    int was_active = 0;
    int64_t rounds = 0;

    while (ret == 0 && simulated_time < deadline &&
           picoquic_get_cnx_state(tctx->cnx_client) !=
               picoquic_state_disconnected) {
        ret = tls_api_one_sim_round(tctx, &simulated_time, sim_time_out,
                                    &was_active);
        if (ret < 0) break;
        if (++rounds > 1000000000) {
            fprintf(stderr, "abort: round count exceeded\n");
            return 1;
        }
    }
    if (ret < 0) return 1;

    int64_t total = 0;
    for (int i = 0; i < n_streams; i++) total += client_ctx.slots[i].bytes_recv;

    printf("sim_link_priority streams=%d rate_mbps=%.1f rtt_us=%" PRId64
           " dur_s=%.2f total_recv=%" PRId64 "\n",
           n_streams, rate_mbps, rtt_us, duration_s, total);
    for (int i = 0; i < n_streams; i++) {
        int64_t got = client_ctx.slots[i].bytes_recv;
        printf("  stream %d priority=%3u recv=%12" PRId64 "  %5.1f%%\n",
               i, server_ctx.slots[i].priority, got,
               total ? (100.0 * (double)got / (double)total) : 0.0);
    }

    /* A strict scheduler serves the most urgent stream first, so its share
     * must exceed the least urgent. Equal shares mean priority changed
     * nothing — either the link was not actually contended, or the values
     * landed in one band. */
    int64_t first = client_ctx.slots[0].bytes_recv;
    int64_t last = client_ctx.slots[n_streams - 1].bytes_recv;
    if (total == 0) {
        printf("VERDICT: no data — nothing was measured\n");
        return 1;
    }
    printf("VERDICT: %s (most urgent %" PRId64 " vs least %" PRId64 ")\n",
           first > last ? "priority honoured" : "PRIORITY HAD NO EFFECT",
           first, last);
    return first > last ? 0 : 2;
}
