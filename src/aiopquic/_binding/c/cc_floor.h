/*
 * cc_floor.h — "bbr1-floor": picoquic's bbr1 with a media-rate floor, and
 * the process-wide congestion-control table that registers it.
 *
 * Every callback delegates to bbr1 on bbr1's own state. After each notify,
 * while the floor is engaged, pacing is raised to at least K x the media
 * rate and cwin to twice that rate's BDP over the smoothed RTT, so loss
 * does not cut a live stream below its own rate.
 *
 * Media rate: app-limited bytes acked per 1 s bin, a bin counting when at
 * least 3/4 of its acked bytes were app-limited, so a backlog drained at
 * full rate does not inflate it. The estimate is the largest counted bin of
 * the last 10 s, held while no bin counts (the sender is below demand).
 *
 * The floor releases (plain bbr1) while a standing queue builds, the
 * minimum RTT of the last 0.5-1 s above min_rtt + max(min_rtt / 2, 50 ms),
 * or while more than half the bytes of the last 1-2 RTTs were lost.
 *
 * Options: K<ratio> sets K (default 1.25); R<bits/s> pins the media rate.
 * Other characters are ignored; the string also reaches bbr1 (T, Q).
 *
 * Network thread only: state is per path and picoquic calls a connection's
 * controller from its context's one thread.
 *
 * Copyright (c) 2026, aiopquic contributors. BSD-3-Clause license.
 */

#ifndef AIOPQUIC_CC_FLOOR_H
#define AIOPQUIC_CC_FLOOR_H

#include <stdint.h>
#include <stdlib.h>
#include <picoquic.h>
#include "picoquic_internal.h"
#include "picoquic_newreno.h"
#include "picoquic_cubic.h"
#include "picoquic_bbr.h"
#include "picoquic_bbr1.h"
#include "picoquic_fastcc.h"
#include "picoquic_prague.h"
#include "c4.h"

#define AIOPQUIC_CC_FLOOR_ID "bbr1-floor"
#define AIOPQUIC_CC_ALGO_NUMBER_BBR1_FLOOR 64
#define AIOPQUIC_CC_FLOOR_K_DEFAULT 1.25
#define AIOPQUIC_CC_FLOOR_K_MAX 16.0
#define AIOPQUIC_CC_FLOOR_BIN_US 1000000ull
#define AIOPQUIC_CC_FLOOR_WINDOW_US 10000000ull
#define AIOPQUIC_CC_FLOOR_NB_BINS 16
#define AIOPQUIC_CC_FLOOR_RTT_WIN_US 500000ull
#define AIOPQUIC_CC_FLOOR_QUEUE_MIN_US 50000ull

/* cc_state bits above bbr1's state byte. */
#define AIOPQUIC_CC_FLOOR_ENGAGED 0x100u
#define AIOPQUIC_CC_FLOOR_QUEUE_GUARD 0x200u
#define AIOPQUIC_CC_FLOOR_LOSS_GUARD 0x400u

typedef struct {
    void* bbr1_state;
    double k;
    uint64_t pinned_rate;           /* bytes/s; 0 = estimate */
    uint64_t estimate;              /* bytes/s */

    uint64_t bin_start;
    uint64_t bin_bytes;
    uint64_t bin_app_bytes;
    uint64_t bin_end[AIOPQUIC_CC_FLOOR_NB_BINS];
    uint64_t bin_rate[AIOPQUIC_CC_FLOOR_NB_BINS];
    unsigned int bin_next;

    uint64_t rtt_win_start;
    uint64_t rtt_min_cur;
    uint64_t rtt_min_prev;

    uint64_t loss_win_start;
    uint64_t lost_mark;
    uint64_t delivered_mark;
    uint64_t lost_prev;
    uint64_t delivered_prev;

    unsigned int bin_started : 1;
    unsigned int queue_guard : 1;
    unsigned int loss_guard : 1;
    unsigned int engaged : 1;
} aiopquic_cc_floor_t;

/* K<ratio> and R<bits/s>; *k and *rate_bps keep their values when absent
 * or out of range. */
static void aiopquic_cc_floor_parse_options(const char* x, double* k,
                                            uint64_t* rate_bps) {
    if (x == NULL) {
        return;
    }
    while (*x != 0) {
        char c = *x++;
        if (c == 'K') {
            double d = 0.0;
            double div = 1.0;
            int dotted = 0;
            int digits = 0;
            while (*x != 0) {
                if (*x >= '0' && *x <= '9') {
                    if (dotted) {
                        div /= 10.0;
                        d += div * (*x - '0');
                    } else if (d < 1e6) {
                        d = d * 10.0 + (*x - '0');
                    }
                    digits++;
                } else if (*x == '.' && !dotted) {
                    dotted = 1;
                } else {
                    break;
                }
                x++;
            }
            if (digits > 0 && d > 0.0 && d <= AIOPQUIC_CC_FLOOR_K_MAX) {
                *k = d;
            }
        } else if (c == 'R') {
            uint64_t u = 0;
            int digits = 0;
            int overflow = 0;
            while (*x >= '0' && *x <= '9') {
                unsigned int v = (unsigned int)(*x - '0');
                if (u > (UINT64_MAX - v) / 10) {
                    overflow = 1;
                } else {
                    u = u * 10 + v;
                }
                digits++;
                x++;
            }
            if (digits > 0 && !overflow) {
                *rate_bps = u;
            }
        }
    }
}

/* bbr1's send quantum for that pacing rate (default quantum ratio). */
static inline uint64_t aiopquic_cc_floor_quantum(double rate, size_t mtu) {
    if (rate < 150000.0) {
        return (uint64_t)mtu;
    }
    if (rate < 3000000.0) {
        return 2ull * mtu;
    }
    uint64_t q = (uint64_t)(rate * 0.001);
    return q > 0x10000 ? 0x10000 : q;
}

static void aiopquic_cc_floor_reset_guards(aiopquic_cc_floor_t* f,
                                           picoquic_path_t* path_x,
                                           uint64_t now) {
    f->rtt_win_start = now;
    f->rtt_min_cur = UINT64_MAX;
    f->rtt_min_prev = UINT64_MAX;
    f->loss_win_start = now;
    f->lost_mark = path_x->total_bytes_lost;
    f->delivered_mark = path_x->delivered;
    f->lost_prev = 0;
    f->delivered_prev = 0;
    f->queue_guard = 0;
    f->loss_guard = 0;
}

static void aiopquic_cc_floor_close_bin(aiopquic_cc_floor_t* f, uint64_t now) {
    uint64_t elapsed = now - f->bin_start;
    if (f->bin_bytes > 0 && 4 * f->bin_app_bytes >= 3 * f->bin_bytes) {
        unsigned int i = f->bin_next;
        f->bin_end[i] = now;
        f->bin_rate[i] = (uint64_t)((double)f->bin_app_bytes * 1000000.0 / (double)elapsed);
        f->bin_next = (i + 1) % AIOPQUIC_CC_FLOOR_NB_BINS;
    }
    uint64_t best = 0;
    int found = 0;
    for (unsigned int i = 0; i < AIOPQUIC_CC_FLOOR_NB_BINS; i++) {
        if (f->bin_end[i] != 0 && f->bin_end[i] + AIOPQUIC_CC_FLOOR_WINDOW_US > now) {
            found = 1;
            if (f->bin_rate[i] > best) {
                best = f->bin_rate[i];
            }
        }
    }
    if (found) {
        f->estimate = best;
    }
    f->bin_start = now;
    f->bin_bytes = 0;
    f->bin_app_bytes = 0;
}

static void aiopquic_cc_floor_on_ack(aiopquic_cc_floor_t* f,
                                     picoquic_per_ack_state_t* ack,
                                     uint64_t now) {
    if (f->pinned_rate == 0) {
        if (!f->bin_started) {
            f->bin_started = 1;
            f->bin_start = now;
        } else if (now >= f->bin_start + AIOPQUIC_CC_FLOOR_BIN_US) {
            aiopquic_cc_floor_close_bin(f, now);
        }
        f->bin_bytes += ack->nb_bytes_acknowledged;
        if (ack->is_app_limited) {
            f->bin_app_bytes += ack->nb_bytes_acknowledged;
        }
    }
}

static void aiopquic_cc_floor_on_rtt(aiopquic_cc_floor_t* f, uint64_t rtt,
                                     uint64_t now) {
    if (rtt == 0) {
        return;
    }
    if (now >= f->rtt_win_start + AIOPQUIC_CC_FLOOR_RTT_WIN_US) {
        f->rtt_min_prev = f->rtt_min_cur;
        f->rtt_min_cur = UINT64_MAX;
        f->rtt_win_start = now;
    }
    if (rtt < f->rtt_min_cur) {
        f->rtt_min_cur = rtt;
    }
}

static void aiopquic_cc_floor_update_guards(aiopquic_cc_floor_t* f,
                                            picoquic_path_t* path_x,
                                            uint64_t now) {
    uint64_t base = path_x->rtt_min;
    uint64_t wmin = f->rtt_min_cur < f->rtt_min_prev ? f->rtt_min_cur : f->rtt_min_prev;
    if (base > 0 && wmin != UINT64_MAX) {
        uint64_t margin = base / 2;
        if (margin < AIOPQUIC_CC_FLOOR_QUEUE_MIN_US) {
            margin = AIOPQUIC_CC_FLOOR_QUEUE_MIN_US;
        }
        f->queue_guard = wmin > base + margin;
    }

    /* Spurious-loss corrections can lower total_bytes_lost. */
    if (path_x->total_bytes_lost < f->lost_mark) {
        f->lost_mark = path_x->total_bytes_lost;
    }
    uint64_t lost = path_x->total_bytes_lost - f->lost_mark;
    uint64_t delivered = path_x->delivered - f->delivered_mark;
    uint64_t win = path_x->smoothed_rtt > 1000 ? path_x->smoothed_rtt : 1000;
    if (now >= f->loss_win_start + win) {
        f->lost_prev = lost;
        f->delivered_prev = delivered;
        f->lost_mark = path_x->total_bytes_lost;
        f->delivered_mark = path_x->delivered;
        f->loss_win_start = now;
        lost = 0;
        delivered = 0;
    }
    lost += f->lost_prev;
    delivered += f->delivered_prev;
    if (lost > 0 || delivered > 0) {
        f->loss_guard = lost > delivered;
    }
}

static void aiopquic_cc_floor_apply(aiopquic_cc_floor_t* f, picoquic_path_t* path_x) {
    uint64_t media = f->pinned_rate != 0 ? f->pinned_rate : f->estimate;
    f->engaged = media > 0 && !f->queue_guard && !f->loss_guard;
    if (!f->engaged) {
        return;
    }
    double rate = f->k * (double)media;
    if ((double)path_x->pacing.rate < rate) {
        picoquic_update_pacing_rate(path_x, rate,
                                    aiopquic_cc_floor_quantum(rate, path_x->send_mtu));
    }
    uint64_t cwin = (uint64_t)(2.0 * rate * (double)path_x->smoothed_rtt / 1000000.0);
    if (path_x->cwin < cwin) {
        path_x->cwin = cwin;
    }
}

static void aiopquic_cc_floor_init(picoquic_path_t* path_x, char const* option_string,
                                   uint64_t current_time) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)calloc(1, sizeof(*f));
    path_x->congestion_alg_state = NULL;
    if (f == NULL) {
        return;
    }
    picoquic_bbr1_algorithm->alg_init(path_x, option_string, current_time);
    if (path_x->congestion_alg_state == NULL) {
        free(f);
        return;
    }
    f->bbr1_state = path_x->congestion_alg_state;
    f->k = AIOPQUIC_CC_FLOOR_K_DEFAULT;
    uint64_t rate_bps = 0;
    aiopquic_cc_floor_parse_options(option_string, &f->k, &rate_bps);
    f->pinned_rate = rate_bps / 8;
    aiopquic_cc_floor_reset_guards(f, path_x, current_time);
    path_x->congestion_alg_state = f;
}

static void aiopquic_cc_floor_notify(picoquic_cnx_t* cnx, picoquic_path_t* path_x,
                                     picoquic_congestion_notification_t notification,
                                     picoquic_per_ack_state_t* ack_state,
                                     uint64_t current_time) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)path_x->congestion_alg_state;
    if (f == NULL) {
        return;
    }
    path_x->congestion_alg_state = f->bbr1_state;
    picoquic_bbr1_algorithm->alg_notify(cnx, path_x, notification, ack_state, current_time);
    f->bbr1_state = path_x->congestion_alg_state;
    path_x->congestion_alg_state = f;

    switch (notification) {
    case picoquic_congestion_notification_acknowledgement:
        if (ack_state != NULL) {
            aiopquic_cc_floor_on_ack(f, ack_state, current_time);
            aiopquic_cc_floor_on_rtt(f, ack_state->rtt_measurement, current_time);
        }
        break;
    case picoquic_congestion_notification_rtt_measurement:
        if (ack_state != NULL) {
            aiopquic_cc_floor_on_rtt(f, ack_state->rtt_measurement, current_time);
        }
        break;
    case picoquic_congestion_notification_reset:
        aiopquic_cc_floor_reset_guards(f, path_x, current_time);
        break;
    default:
        break;
    }
    aiopquic_cc_floor_update_guards(f, path_x, current_time);
    aiopquic_cc_floor_apply(f, path_x);
}

static void aiopquic_cc_floor_delete(picoquic_path_t* path_x) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)path_x->congestion_alg_state;
    if (f != NULL) {
        path_x->congestion_alg_state = f->bbr1_state;
        picoquic_bbr1_algorithm->alg_delete(path_x);
        free(f);
    }
    path_x->congestion_alg_state = NULL;
}

static inline uint64_t aiopquic_cc_floor_sat32(uint64_t v) {
    return v > 0xffffffffull ? 0xffffffffull : v;
}

/* cc_state: bbr1's state (bits 0-7), ENGAGED / QUEUE_GUARD / LOSS_GUARD
 * (bits 8-10), the media rate in bytes/s (bits 32-63). cc_param: bbr1's
 * bottleneck bandwidth (bits 0-31) and the floor rate K x media rate
 * (bits 32-63), bytes/s. Rates saturate at 2^32 - 1. */
static void aiopquic_cc_floor_observe(picoquic_path_t* path_x, uint64_t* cc_state,
                                      uint64_t* cc_param) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)path_x->congestion_alg_state;
    *cc_state = 0;
    *cc_param = 0;
    if (f == NULL || f->bbr1_state == NULL) {
        return;
    }
    uint64_t s = 0;
    uint64_t p = 0;
    path_x->congestion_alg_state = f->bbr1_state;
    picoquic_bbr1_algorithm->alg_observe(path_x, &s, &p);
    path_x->congestion_alg_state = f;

    uint64_t media = f->pinned_rate != 0 ? f->pinned_rate : f->estimate;
    uint64_t flags = (f->engaged ? AIOPQUIC_CC_FLOOR_ENGAGED : 0)
        | (f->queue_guard ? AIOPQUIC_CC_FLOOR_QUEUE_GUARD : 0)
        | (f->loss_guard ? AIOPQUIC_CC_FLOOR_LOSS_GUARD : 0);
    *cc_state = (s & 0xff) | flags | (aiopquic_cc_floor_sat32(media) << 32);
    *cc_param = aiopquic_cc_floor_sat32(p)
        | (aiopquic_cc_floor_sat32((uint64_t)(f->k * (double)media)) << 32);
}

static picoquic_congestion_algorithm_t aiopquic_cc_floor_algorithm_struct = {
    AIOPQUIC_CC_FLOOR_ID, AIOPQUIC_CC_ALGO_NUMBER_BBR1_FLOOR, PICOQUIC_ECN_ECT_0,
    aiopquic_cc_floor_init,
    aiopquic_cc_floor_notify,
    aiopquic_cc_floor_delete,
    aiopquic_cc_floor_observe
};

/* picoquic keeps a pointer to this table: it must outlive every context. */
static picoquic_congestion_algorithm_t const* aiopquic_cc_algorithms[9];

/* picoquic's eight algorithms plus bbr1-floor. Call once per process,
 * before any network thread starts: those threads read the table. */
static void aiopquic_register_cc_algorithms(void) {
    size_t n = 0;
    aiopquic_cc_algorithms[n++] = picoquic_newreno_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_cubic_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_dcubic_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_fastcc_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_bbr_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_prague_algorithm;
    aiopquic_cc_algorithms[n++] = picoquic_bbr1_algorithm;
    aiopquic_cc_algorithms[n++] = c4_algorithm;
    aiopquic_cc_algorithms[n++] = &aiopquic_cc_floor_algorithm_struct;
    picoquic_register_congestion_control_algorithms(aiopquic_cc_algorithms, n);
}

#endif /* AIOPQUIC_CC_FLOOR_H */
