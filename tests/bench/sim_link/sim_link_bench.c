/*
 * sim_link_bench: protocol-only picoquic throughput bench.
 *
 * Drives two picoquic_quic_t instances over picoquictest_sim_link
 * (no kernel UDP, no sockets) using our own minimal callback that
 * just sends N bytes from server → client on a single bidi stream
 * and counts bytes at the receiver. Pure picoquic CPU cost.
 *
 * Output (stdout):
 *   sim_link_bench obj=<N>B sent=<bytes> recv=<bytes>
 *     mbps=<num> obj_per_s=<num> sim_time_s=<num>
 *     rtt_us=<N> rate_gbps=<N> rounds=<N>
 *
 * Built via build_picoquic.sh after the picoquic libs.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <inttypes.h>
#include <time.h>

#include "picoquic.h"
#include "picoquic_internal.h"
#include "picoquic_utils.h"
#include "picoquictest_internal.h"

#ifndef PICOQUIC_INTERNAL_TEST_VERSION_1
#define PICOQUIC_INTERNAL_TEST_VERSION_1 (PICOQUIC_TWELFTH_INTEROP_VERSION)
#endif

#ifndef PICOQUIC_TEST_SNI
#define PICOQUIC_TEST_SNI "test.example.com"
#endif

#define BENCH_ALPN "perf"

/* Per-side state. The two ctxs are independent so we can tell which
 * side a callback is running on without reading internal cnx flags. */
typedef struct bench_ctx {
    int is_client;
    int is_server;
    int64_t target_bytes;        /* server: cap on total bytes  */
    int64_t bytes_sent;
    int64_t bytes_recv;
    uint64_t open_stream_id;
    int stream_active;
    int stream_finished;
} bench_ctx_t;

static int bench_callback(picoquic_cnx_t* cnx,
    uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t fin_or_event,
    void* callback_ctx, void* v_stream_ctx)
{
    bench_ctx_t* bctx = (bench_ctx_t*)callback_ctx;
    int ret = 0;

    /* Server side: callback_ctx is initially NULL (set via
     * picoquic_set_default_callback). On first dispatch, set our
     * server bench_ctx and re-bind. */
    if (bctx == NULL) {
        bctx = (bench_ctx_t*)picoquic_get_default_callback_context(picoquic_get_quic_ctx(cnx));
        if (bctx) {
            picoquic_set_callback(cnx, bench_callback, bctx);
        } else {
            /* Should not happen — server default ctx should be set. */
            return -1;
        }
    }

    switch (fin_or_event) {
    case picoquic_callback_almost_ready:
    case picoquic_callback_ready:
        if (bctx->is_client && !bctx->stream_active) {
            /* Open a single bidi stream; the server will see it on
             * first incoming-frame callback and start sending bytes. */
            uint64_t sid = picoquic_get_next_local_stream_id(cnx, 0 /* bidi */);
            bctx->open_stream_id = sid;
            bctx->stream_active = 1;
            /* Send a tiny "ping" so the server stream gets created. */
            uint8_t ping[8] = {0};
            ret = picoquic_add_to_stream(cnx, sid, ping, sizeof(ping), 1);
        }
        break;

    case picoquic_callback_stream_data:
    case picoquic_callback_stream_fin:
        if (length > 0) {
            bctx->bytes_recv += (int64_t)length;
        }
        if (bctx->is_server && !bctx->stream_active) {
            /* Client opened the stream and sent its ping; the server
             * now uses the same stream to send target_bytes back. */
            bctx->open_stream_id = stream_id;
            bctx->stream_active = 1;
            ret = picoquic_mark_active_stream(cnx, stream_id, 1, NULL);
        }
        if (fin_or_event == picoquic_callback_stream_fin) {
            bctx->stream_finished = 1;
        }
        break;

    case picoquic_callback_prepare_to_send:
        if (bctx->is_server) {
            int64_t remaining = bctx->target_bytes - bctx->bytes_sent;
            if (remaining <= 0) {
                /* All sent — set fin. picoquic gives `bytes` as the
                 * out-buffer; we write zero bytes + fin. */
                uint8_t* buf = picoquic_provide_stream_data_buffer(
                    bytes, 0, /* fin */ 1, /* still_active */ 0);
                (void)buf;
                ret = picoquic_mark_active_stream(cnx, stream_id, 0, NULL);
            } else {
                size_t take = (length < (size_t)remaining) ? length : (size_t)remaining;
                uint8_t* buf = picoquic_provide_stream_data_buffer(
                    bytes, take,
                    /* fin */ (take == (size_t)remaining) ? 1 : 0,
                    /* still_active */ (take < (size_t)remaining) ? 1 : 0);
                if (buf) {
                    memset(buf, 0xab, take);
                }
                bctx->bytes_sent += (int64_t)take;
            }
        }
        break;

    case picoquic_callback_close:
    case picoquic_callback_application_close:
    case picoquic_callback_stateless_reset:
        picoquic_set_callback(cnx, NULL, NULL);
        break;

    default:
        break;
    }
    return ret;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "Usage: %s [--duration-s F] [--warmup-s F] [--obj-size N]\n"
        "          [--rate-gbps F] [--rtt-us N] [--quiet]\n"
        "  --duration-s F    SIMULATED steady-state window in seconds "
        "(default 30.0)\n"
        "  --warmup-s F      SIMULATED warmup before measurement "
        "(default 2.0)\n"
        "  --obj-size N      object size for obj/s reporting "
        "(default 16384)\n"
        "  --rate-gbps F     simulated link bandwidth in Gbps "
        "(default 0 = leave sim_link defaults: 0.01 Gbps)\n"
        "  --rtt-us N        simulated link RTT microseconds "
        "(default 100)\n"
        "  --quiet           suppress progress lines\n",
        argv0);
}

int main(int argc, char **argv)
{
    double duration_s = 30.0;
    double warmup_s = 2.0;
    int64_t obj_size = 16384;
    double rate_gbps = 0.0;
    int64_t rtt_us = 100;
    int quiet = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--duration-s") == 0 && i + 1 < argc) {
            duration_s = strtod(argv[++i], NULL);
        } else if (strcmp(argv[i], "--warmup-s") == 0 && i + 1 < argc) {
            warmup_s = strtod(argv[++i], NULL);
        } else if (strcmp(argv[i], "--obj-size") == 0 && i + 1 < argc) {
            obj_size = strtoll(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--rate-gbps") == 0 && i + 1 < argc) {
            rate_gbps = strtod(argv[++i], NULL);
        } else if (strcmp(argv[i], "--rtt-us") == 0 && i + 1 < argc) {
            rtt_us = strtoll(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--quiet") == 0) {
            quiet = 1;
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    /* Effectively unbounded server response: target_bytes large enough
     * the test can run for `duration_s + warmup_s + slack` seconds at
     * any reasonable rate. We stop based on simulated time elapsed,
     * not bytes. */
    int64_t total_bytes = (int64_t)(50000.0 * (duration_s + warmup_s + 5.0))
                            * 1000000LL;

    /* picoquic test scaffolding loads certs/cert.pem etc. via
     * picoquic_get_input_path(picoquic_solution_dir, ...). Caller sets
     * PICOQUIC_SOLUTION_DIR to the picoquic submodule root. */
    {
        const char *dir = getenv("PICOQUIC_SOLUTION_DIR");
        if (dir) {
            picoquic_solution_dir = dir;
        }
    }

    bench_ctx_t client_bctx = {0};
    bench_ctx_t server_bctx = {0};
    client_bctx.is_client = 1;
    server_bctx.is_server = 1;
    server_bctx.target_bytes = total_bytes;

    uint64_t simulated_time = 0;
    uint64_t loss_mask = 0;
    picoquic_test_tls_api_ctx_t *tctx = NULL;
    picoquic_connection_id_t initial_cid = {{0xa1, 0x0a, 0xb0, 0xb1,
                                              0xb2, 0xb3, 0xb4, 0xb5}, 8};

    int ret = tls_api_init_ctx_ex(&tctx,
        PICOQUIC_INTERNAL_TEST_VERSION_1,
        PICOQUIC_TEST_SNI, BENCH_ALPN, &simulated_time,
        NULL, NULL, 0, 1, 0, &initial_cid);
    if (ret != 0 || !tctx || !tctx->cnx_client || !tctx->qserver) {
        fprintf(stderr, "tls_api_init_ctx_ex failed (ret=%d)\n", ret);
        return 1;
    }

    /* Override sim_link rate/RTT if user requested. Default
     * tls_api_init_ctx_ex setup is 0.01 Gbps × 10ms RTT — too slow
     * for protocol throughput tests (the picoquic CC will limit us
     * to that link rate). Setting rate_gbps=0 keeps the defaults. */
    if (rate_gbps > 0.0 && tctx->c_to_s_link && tctx->s_to_c_link) {
        tctx->c_to_s_link->picosec_per_byte = (uint64_t)(8000.0 / rate_gbps);
        tctx->c_to_s_link->microsec_latency = (uint64_t)(rtt_us / 2);
        tctx->s_to_c_link->picosec_per_byte = (uint64_t)(8000.0 / rate_gbps);
        tctx->s_to_c_link->microsec_latency = (uint64_t)(rtt_us / 2);
    }

    /* Wire our minimal callback. */
    picoquic_set_default_callback(tctx->qserver, bench_callback, &server_bctx);
    picoquic_set_callback(tctx->cnx_client, bench_callback, &client_bctx);

    ret = picoquic_start_client_cnx(tctx->cnx_client);
    if (ret != 0) {
        fprintf(stderr, "picoquic_start_client_cnx failed (ret=%d)\n", ret);
        return 1;
    }

    ret = tls_api_connection_loop(tctx, &loss_mask, 0, &simulated_time);
    if (ret != 0) {
        fprintf(stderr, "connection handshake failed (ret=%d)\n", ret);
        return 1;
    }

    uint64_t handshake_done_us = simulated_time;
    if (!quiet) {
        fprintf(stderr, "handshake done at sim_t=%" PRIu64 "us\n",
                handshake_done_us);
    }

    int was_active = 0;
    int nb_trials = 0;
    /* Sim deadlines, all relative to handshake_done_us. */
    uint64_t warmup_deadline = handshake_done_us + (uint64_t)(warmup_s * 1e6);
    uint64_t ss_end_deadline = warmup_deadline + (uint64_t)(duration_s * 1e6);
    uint64_t sim_time_out = ss_end_deadline + 60000000ULL;  /* +60s slack */

    /* Steady-state samples: bytes received at warmup_deadline and at
     * ss_end_deadline. Difference / duration_s = real throughput.
     *
     * sim_throughput is platform-independent (it's the picoquic CC
     * equilibrium at this link config). The real cross-platform metric
     * is wall_throughput — bytes / wall-clock seconds spent processing
     * the simulator. We track wall timestamps at each window boundary. */
    int64_t warmup_recv = -1;
    int64_t ss_end_recv = -1;
    int64_t warmup_sent = -1;
    int64_t ss_end_sent = -1;

    struct timespec wall_handshake, wall_warmup, wall_ss_end;
    clock_gettime(CLOCK_MONOTONIC, &wall_handshake);

    while (ret == 0 &&
           picoquic_get_cnx_state(tctx->cnx_client) != picoquic_state_disconnected) {
        ret = tls_api_one_sim_round(tctx, &simulated_time, sim_time_out, &was_active);
        if (ret < 0) break;

        /* Sample at warmup boundary. */
        if (warmup_recv < 0 && simulated_time >= warmup_deadline) {
            warmup_recv = client_bctx.bytes_recv;
            warmup_sent = server_bctx.bytes_sent;
            clock_gettime(CLOCK_MONOTONIC, &wall_warmup);
            if (!quiet) {
                fprintf(stderr, "[warmup] sim_t=%.3fs recv=%" PRId64
                        " (%.1f sim_Mbps over %.2fs)\n",
                        (simulated_time - handshake_done_us) / 1e6,
                        warmup_recv,
                        (warmup_recv * 8.0) /
                        (double)(simulated_time - handshake_done_us),
                        (simulated_time - handshake_done_us) / 1e6);
            }
        }

        /* Stop after the steady-state window. */
        if (simulated_time >= ss_end_deadline) {
            ss_end_recv = client_bctx.bytes_recv;
            ss_end_sent = server_bctx.bytes_sent;
            clock_gettime(CLOCK_MONOTONIC, &wall_ss_end);
            break;
        }

        if (++nb_trials > 1000000000) {
            fprintf(stderr, "abort: round count exceeded\n");
            ret = -1;
            break;
        }
    }

    if (ret < 0) return 1;

    if (warmup_recv < 0) {
        warmup_recv = client_bctx.bytes_recv;
        warmup_sent = server_bctx.bytes_sent;
        clock_gettime(CLOCK_MONOTONIC, &wall_warmup);
    }
    if (ss_end_recv < 0) {
        ss_end_recv = client_bctx.bytes_recv;
        ss_end_sent = server_bctx.bytes_sent;
        clock_gettime(CLOCK_MONOTONIC, &wall_ss_end);
    }

    /* Sim-time mbps: picoquic CC equilibrium at the simulated link
     * config. Same number across machines — useful as a determinism
     * check, NOT as a hardware comparison. */
    double sim_warmup_mbps = (warmup_recv * 8.0) /
        (double)(warmup_deadline - handshake_done_us);
    double sim_ss_mbps = ((ss_end_recv - warmup_recv) * 8.0) /
        (double)(ss_end_deadline - warmup_deadline);

    /* Wall-time mbps: bytes processed per second of host CPU time.
     * THIS is the cross-platform aiopquic CPU performance number.
     * Lower means the host is slower at running the simulator. */
    double wall_warmup_s =
        (wall_warmup.tv_sec - wall_handshake.tv_sec) +
        (wall_warmup.tv_nsec - wall_handshake.tv_nsec) / 1e9;
    double wall_ss_s =
        (wall_ss_end.tv_sec - wall_warmup.tv_sec) +
        (wall_ss_end.tv_nsec - wall_warmup.tv_nsec) / 1e9;
    double wall_total_s =
        (wall_ss_end.tv_sec - wall_handshake.tv_sec) +
        (wall_ss_end.tv_nsec - wall_handshake.tv_nsec) / 1e9;
    double wall_ss_mbps = (wall_ss_s > 0)
        ? ((ss_end_recv - warmup_recv) * 8.0) / (wall_ss_s * 1e6)
        : 0.0;
    double obj_per_wall_s = (wall_ss_s > 0)
        ? (double)(ss_end_recv - warmup_recv) /
          (double)obj_size / wall_ss_s
        : 0.0;

    printf("sim_link_bench obj=%" PRId64 "B"
           " | sim_ss_mbps=%.1f sim_total_s=%.3f"
           " | wall_ss_mbps=%.1f wall_ss_s=%.2f wall_total_s=%.2f"
           " obj_per_wall_s=%.0f"
           " | rtt_us=%" PRId64 " rate_gbps=%.2f rounds=%d\n",
           obj_size,
           sim_ss_mbps, (ss_end_deadline - handshake_done_us) / 1e6,
           wall_ss_mbps, wall_ss_s, wall_total_s, obj_per_wall_s,
           rtt_us, rate_gbps, nb_trials);
    (void)sim_warmup_mbps;
    (void)wall_warmup_s;

    /* Cleanly close so picoquic doesn't complain at exit. */
    (void)picoquic_close(tctx->cnx_client, 0);
    for (int i = 0; i < 100 &&
         tctx->cnx_client->cnx_state != picoquic_state_disconnected; i++) {
        (void)tls_api_one_sim_round(tctx, &simulated_time, sim_time_out, &was_active);
    }

    (void)warmup_sent;
    (void)ss_end_sent;
    return 0;
}
