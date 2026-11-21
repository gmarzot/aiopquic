/*
 * cc_floor.h — "bbr1-floor" and "bbr-floor": picoquic's bbr1 or bbr (v3)
 * with a media-rate floor, and the process-wide congestion-control table
 * that registers them.
 *
 * Every callback delegates to the inner algorithm on its own state. After
 * each notify, while the floor is engaged, pacing is raised to at least the
 * floor rate and cwin to twice that rate's BDP over the smoothed RTT, so
 * loss does not cut a live stream below its own rate.
 *
 * Media rate: app-limited bytes acked per 1 s bin, a bin counting when at
 * least 3/4 of its acked bytes were app-limited, so a backlog drained at
 * full rate does not inflate it. The estimate is the largest counted bin of
 * the last 10 s, held while no bin counts (the sender is below demand).
 *
 * Floor rate: media x (1 + headroom), headroom = loss / (1 - loss) + 5%
 * (at most 100%), loss being the lost share of the last 10 s; K<ratio>
 * fixes the ratio instead. See aiopquic_cc_floor_rate() for the cap.
 *
 * The floor releases (plain inner algorithm) while the windowed min RTT
 * has grown past baseline + max(baseline / 4, 10 ms), baseline being the
 * lowest windowed min RTT since the floor first engaged, until growth is
 * back under half that; or while more than half the bytes of the last
 * 1-2 RTTs were lost. With a latency budget L, queueing delay up to
 * L - 2 x baseline still arrives in time, so the guard allows that much
 * growth.
 *
 * Options: K<ratio> fixes the ratio; R<bits/s> pins the media rate;
 * L<ms> is the latency budget. Other characters are ignored, and reach the
 * inner algorithm with these three removed: bbr reads R and L as its own
 * experiment switches.
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

#define AIOPQUIC_CC_BBR1_FLOOR_ID "bbr1-floor"
#define AIOPQUIC_CC_BBR_FLOOR_ID "bbr-floor"
#define AIOPQUIC_CC_ALGO_NUMBER_BBR1_FLOOR 64
#define AIOPQUIC_CC_ALGO_NUMBER_BBR_FLOOR 65
#define AIOPQUIC_CC_FLOOR_K_MAX 16.0
#define AIOPQUIC_CC_FLOOR_R_MAX 1000000000000ull  /* bits/s */
#define AIOPQUIC_CC_FLOOR_MARGIN 0.05
#define AIOPQUIC_CC_FLOOR_HEADROOM_MAX 1.0
#define AIOPQUIC_CC_FLOOR_LOSS_MAX 0.5
#define AIOPQUIC_CC_FLOOR_RATE_WIN_MIN_US 25000ull
#define AIOPQUIC_CC_FLOOR_LOSS_MIN_BYTES 16384ull
#define AIOPQUIC_CC_FLOOR_BIN_US 1000000ull
#define AIOPQUIC_CC_FLOOR_WINDOW_US 10000000ull
#define AIOPQUIC_CC_FLOOR_NB_BINS 16
#define AIOPQUIC_CC_FLOOR_RTT_WIN_US 500000ull
#define AIOPQUIC_CC_FLOOR_GROWTH_DIV 4
#define AIOPQUIC_CC_FLOOR_GROWTH_MIN_US 10000ull
#define AIOPQUIC_CC_FLOOR_L_MAX_MS 600000ull

/* cc_state bits above the inner algorithm's state byte. */
#define AIOPQUIC_CC_FLOOR_ENGAGED 0x100u
#define AIOPQUIC_CC_FLOOR_QUEUE_GUARD 0x200u
#define AIOPQUIC_CC_FLOOR_LOSS_GUARD 0x400u

typedef struct {
    uint64_t end;                   /* 0 = empty */
    uint64_t media;                 /* bytes/s; 0 = bin not app-limited */
    uint64_t bw_max;                /* bytes/s */
    uint64_t lost;
    uint64_t delivered;
} aiopquic_cc_floor_bin_t;

typedef struct {
    picoquic_congestion_algorithm_t const* inner;
    void* inner_state;
    double k;                       /* 0 = headroom from loss */
    uint64_t pinned_rate;           /* bytes/s; 0 = estimate */
    uint64_t budget_us;             /* latency budget; 0 = none */
    uint64_t estimate;              /* bytes/s */
    uint64_t floor_rate;            /* bytes/s */

    uint64_t bin_start;
    uint64_t bin_bytes;
    uint64_t bin_app_bytes;
    uint64_t bin_bw_max;
    uint64_t rate_win_start;
    uint64_t rate_win_mark;
    uint64_t bin_lost_mark;
    uint64_t bin_delivered_mark;
    aiopquic_cc_floor_bin_t bins[AIOPQUIC_CC_FLOOR_NB_BINS];
    unsigned int bin_next;
    double loss;
    double loss_min;
    uint64_t bw_max;

    uint64_t rtt_win_start;
    uint64_t rtt_min_cur;
    uint64_t rtt_min_prev;
    uint64_t rtt_base;              /* 0 = not engaged yet */

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

/* K<ratio> (0 < K <= 16), R<bits/s> (up to 1 Tb/s) and L<ms> (1 ms to
 * 10 min); each output keeps its value when absent or out of range. */
static void aiopquic_cc_floor_parse_options(const char* x, double* k,
                                            uint64_t* rate_bps,
                                            uint64_t* budget_ms) {
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
            while (*x >= '0' && *x <= '9') {
                if (u <= AIOPQUIC_CC_FLOOR_R_MAX) {
                    u = u * 10 + (uint64_t)(*x - '0');
                }
                digits++;
                x++;
            }
            if (digits > 0 && u <= AIOPQUIC_CC_FLOOR_R_MAX) {
                *rate_bps = u;
            }
        } else if (c == 'L') {
            uint64_t u = 0;
            int digits = 0;
            while (*x >= '0' && *x <= '9') {
                if (u <= AIOPQUIC_CC_FLOOR_L_MAX_MS) {
                    u = u * 10 + (uint64_t)(*x - '0');
                }
                digits++;
                x++;
            }
            if (digits > 0 && u > 0 && u <= AIOPQUIC_CC_FLOOR_L_MAX_MS) {
                *budget_ms = u;
            }
        }
    }
}

/* option_string without the floor's K, R and L fields, for the inner
 * algorithm; out holds at most n - 1 characters. */
static void aiopquic_cc_floor_inner_options(const char* x, char* out, size_t n) {
    size_t i = 0;
    if (n == 0) {
        return;
    }
    while (x != NULL && *x != 0) {
        char c = *x++;
        if (c == 'K' || c == 'R' || c == 'L') {
            while ((*x >= '0' && *x <= '9') || (c == 'K' && *x == '.')) {
                x++;
            }
            continue;
        }
        if (i + 1 < n) {
            out[i++] = c;
        }
    }
    out[i] = 0;
}

/* Floor rate in bytes/s. Above the media rate it is capped at the
 * bottleneck estimate: the 10 s max delivery rate over (1 - the 10 s min
 * per-second loss, the path's non-congestive loss). No margin below it:
 * under random loss the samples are the floor's own rate, so a margin would
 * ratchet the floor down. The cap never takes the floor below the media
 * rate (K x media rate for K < 1). */
static uint64_t aiopquic_cc_floor_rate(uint64_t media, double k, double loss,
                                       double loss_min, uint64_t bw_max) {
    if (media == 0) {
        return 0;
    }
    double ratio = k;
    if (ratio <= 0.0) {
        double l = loss < AIOPQUIC_CC_FLOOR_LOSS_MAX ? loss : AIOPQUIC_CC_FLOOR_LOSS_MAX;
        double h = (l > 0.0 ? l / (1.0 - l) : 0.0) + AIOPQUIC_CC_FLOOR_MARGIN;
        ratio = 1.0 + (h < AIOPQUIC_CC_FLOOR_HEADROOM_MAX ? h : AIOPQUIC_CC_FLOOR_HEADROOM_MAX);
    }
    double rate = ratio * (double)media;
    double low = ratio < 1.0 ? rate : (double)media;
    double lm = loss_min > 0.0 ? loss_min : 0.0;
    if (lm > AIOPQUIC_CC_FLOOR_LOSS_MAX) {
        lm = AIOPQUIC_CC_FLOOR_LOSS_MAX;
    }
    double cap = (double)bw_max / (1.0 - lm);
    if (rate > cap) {
        rate = cap > low ? cap : low;
    }
    return (uint64_t)rate;
}

/* Queue guard with hysteresis: set past base + max(base / 4, 10 ms,
 * budget - 2 x base) of windowed min RTT, cleared at or under half that
 * growth. */
static int aiopquic_cc_floor_queue_guard(int held, uint64_t base, uint64_t wmin,
                                         uint64_t budget) {
    if (base == 0 || wmin == UINT64_MAX) {
        return held;
    }
    uint64_t limit = base / AIOPQUIC_CC_FLOOR_GROWTH_DIV;
    if (limit < AIOPQUIC_CC_FLOOR_GROWTH_MIN_US) {
        limit = AIOPQUIC_CC_FLOOR_GROWTH_MIN_US;
    }
    if (budget > 2 * base && budget - 2 * base > limit) {
        limit = budget - 2 * base;
    }
    uint64_t growth = wmin > base ? wmin - base : 0;
    return held ? growth > limit / 2 : growth > limit;
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
    f->rtt_base = 0;
    f->loss_win_start = now;
    f->lost_mark = path_x->total_bytes_lost;
    f->delivered_mark = path_x->delivered;
    f->lost_prev = 0;
    f->delivered_prev = 0;
    f->queue_guard = 0;
    f->loss_guard = 0;
}

/* Refreshes the windowed loss, min loss and max delivery rate, the current
 * bin's delivery rate included. */
static void aiopquic_cc_floor_window(aiopquic_cc_floor_t* f, uint64_t now) {
    uint64_t lost = 0;
    uint64_t total = 0;
    double loss_min = -1.0;
    uint64_t bw = f->bin_bw_max;
    for (unsigned int i = 0; i < AIOPQUIC_CC_FLOOR_NB_BINS; i++) {
        aiopquic_cc_floor_bin_t* b = &f->bins[i];
        if (b->end == 0 || b->end + AIOPQUIC_CC_FLOOR_WINDOW_US <= now) {
            continue;
        }
        uint64_t t = b->lost + b->delivered;
        lost += b->lost;
        total += t;
        if (t >= AIOPQUIC_CC_FLOOR_LOSS_MIN_BYTES) {
            double l = (double)b->lost / (double)t;
            if (loss_min < 0.0 || l < loss_min) {
                loss_min = l;
            }
        }
        if (b->bw_max > bw) {
            bw = b->bw_max;
        }
    }
    f->loss = total > 0 ? (double)lost / (double)total : 0.0;
    f->loss_min = loss_min > 0.0 ? loss_min : 0.0;
    f->bw_max = bw;
}

static void aiopquic_cc_floor_close_bin(aiopquic_cc_floor_t* f, picoquic_path_t* path_x,
                                        uint64_t now) {
    /* Spurious-loss corrections can lower total_bytes_lost. */
    if (path_x->total_bytes_lost < f->bin_lost_mark) {
        f->bin_lost_mark = path_x->total_bytes_lost;
    }
    aiopquic_cc_floor_bin_t* b = &f->bins[f->bin_next];
    b->end = now;
    b->media = 0;
    if (f->bin_bytes > 0 && 4 * f->bin_app_bytes >= 3 * f->bin_bytes) {
        b->media = (uint64_t)((double)f->bin_app_bytes * 1000000.0
                              / (double)(now - f->bin_start));
    }
    b->bw_max = f->bin_bw_max;
    b->lost = path_x->total_bytes_lost - f->bin_lost_mark;
    b->delivered = path_x->delivered - f->bin_delivered_mark;
    f->bin_next = (f->bin_next + 1) % AIOPQUIC_CC_FLOOR_NB_BINS;

    if (f->pinned_rate == 0) {
        uint64_t best = 0;
        int found = 0;
        for (unsigned int i = 0; i < AIOPQUIC_CC_FLOOR_NB_BINS; i++) {
            aiopquic_cc_floor_bin_t* c = &f->bins[i];
            if (c->end != 0 && c->media != 0 && c->end + AIOPQUIC_CC_FLOOR_WINDOW_US > now) {
                found = 1;
                if (c->media > best) {
                    best = c->media;
                }
            }
        }
        if (found) {
            f->estimate = best;
        }
    }
    f->bin_start = now;
    f->bin_bytes = 0;
    f->bin_app_bytes = 0;
    f->bin_bw_max = 0;
    f->bin_lost_mark = path_x->total_bytes_lost;
    f->bin_delivered_mark = path_x->delivered;
}

static void aiopquic_cc_floor_on_ack(aiopquic_cc_floor_t* f, picoquic_path_t* path_x,
                                     picoquic_per_ack_state_t* ack, uint64_t now) {
    if (!f->bin_started) {
        f->bin_started = 1;
        f->bin_start = now;
        f->bin_lost_mark = path_x->total_bytes_lost;
        f->bin_delivered_mark = path_x->delivered;
    } else if (now >= f->bin_start + AIOPQUIC_CC_FLOOR_BIN_US) {
        aiopquic_cc_floor_close_bin(f, path_x, now);
    }
    f->bin_bytes += ack->nb_bytes_acknowledged;
    if (ack->is_app_limited) {
        f->bin_app_bytes += ack->nb_bytes_acknowledged;
    }
    /* Delivery rate over windows of max(smoothed RTT, 25 ms). */
    uint64_t win = path_x->smoothed_rtt > AIOPQUIC_CC_FLOOR_RATE_WIN_MIN_US
        ? path_x->smoothed_rtt : AIOPQUIC_CC_FLOOR_RATE_WIN_MIN_US;
    if (f->rate_win_start == 0) {
        f->rate_win_start = now;
        f->rate_win_mark = path_x->delivered;
    } else if (now >= f->rate_win_start + win) {
        uint64_t bw = (uint64_t)((double)(path_x->delivered - f->rate_win_mark) * 1000000.0
                                 / (double)(now - f->rate_win_start));
        if (bw > f->bin_bw_max) {
            f->bin_bw_max = bw;
        }
        f->rate_win_start = now;
        f->rate_win_mark = path_x->delivered;
    }
    aiopquic_cc_floor_window(f, now);
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
    uint64_t wmin = f->rtt_min_cur < f->rtt_min_prev ? f->rtt_min_cur : f->rtt_min_prev;
    if (f->rtt_base != 0 && wmin < f->rtt_base) {
        f->rtt_base = wmin;
    }
    f->queue_guard = aiopquic_cc_floor_queue_guard(f->queue_guard, f->rtt_base, wmin,
                                                   f->budget_us);

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
    f->floor_rate = aiopquic_cc_floor_rate(media, f->k, f->loss, f->loss_min, f->bw_max);
    if (media > 0 && f->rtt_base == 0) {
        uint64_t wmin = f->rtt_min_cur < f->rtt_min_prev ? f->rtt_min_cur : f->rtt_min_prev;
        if (wmin != UINT64_MAX) {
            f->rtt_base = wmin;
        }
    }
    f->engaged = media > 0 && !f->queue_guard && !f->loss_guard;
    if (!f->engaged) {
        return;
    }
    double rate = (double)f->floor_rate;
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
                                   uint64_t current_time,
                                   picoquic_congestion_algorithm_t const* inner) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)calloc(1, sizeof(*f));
    path_x->congestion_alg_state = NULL;
    if (f == NULL) {
        return;
    }
    char inner_options[256];
    aiopquic_cc_floor_inner_options(option_string, inner_options, sizeof(inner_options));
    inner->alg_init(path_x, inner_options, current_time);
    if (path_x->congestion_alg_state == NULL) {
        free(f);
        return;
    }
    f->inner = inner;
    f->inner_state = path_x->congestion_alg_state;
    uint64_t rate_bps = 0;
    uint64_t budget_ms = 0;
    aiopquic_cc_floor_parse_options(option_string, &f->k, &rate_bps, &budget_ms);
    f->pinned_rate = rate_bps / 8;
    f->budget_us = budget_ms * 1000;
    aiopquic_cc_floor_reset_guards(f, path_x, current_time);
    path_x->congestion_alg_state = f;
}

static void aiopquic_cc_bbr1_floor_init(picoquic_path_t* path_x, char const* option_string,
                                        uint64_t current_time) {
    aiopquic_cc_floor_init(path_x, option_string, current_time, picoquic_bbr1_algorithm);
}

static void aiopquic_cc_bbr_floor_init(picoquic_path_t* path_x, char const* option_string,
                                       uint64_t current_time) {
    aiopquic_cc_floor_init(path_x, option_string, current_time, picoquic_bbr_algorithm);
}

static void aiopquic_cc_floor_notify(picoquic_cnx_t* cnx, picoquic_path_t* path_x,
                                     picoquic_congestion_notification_t notification,
                                     picoquic_per_ack_state_t* ack_state,
                                     uint64_t current_time) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)path_x->congestion_alg_state;
    if (f == NULL) {
        return;
    }
    path_x->congestion_alg_state = f->inner_state;
    f->inner->alg_notify(cnx, path_x, notification, ack_state, current_time);
    f->inner_state = path_x->congestion_alg_state;
    path_x->congestion_alg_state = f;

    switch (notification) {
    case picoquic_congestion_notification_acknowledgement:
        if (ack_state != NULL) {
            aiopquic_cc_floor_on_ack(f, path_x, ack_state, current_time);
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
        path_x->congestion_alg_state = f->inner_state;
        f->inner->alg_delete(path_x);
        free(f);
    }
    path_x->congestion_alg_state = NULL;
}

static inline uint64_t aiopquic_cc_floor_sat32(uint64_t v) {
    return v > 0xffffffffull ? 0xffffffffull : v;
}

/* cc_state: the inner algorithm's state (bits 0-7), ENGAGED / QUEUE_GUARD
 * / LOSS_GUARD (bits 8-10), the media rate in bytes/s (bits 32-63).
 * cc_param: the inner algorithm's bandwidth (bits 0-31) and the floor rate
 * (bits 32-63), bytes/s. Rates saturate at 2^32 - 1. */
static void aiopquic_cc_floor_observe(picoquic_path_t* path_x, uint64_t* cc_state,
                                      uint64_t* cc_param) {
    aiopquic_cc_floor_t* f = (aiopquic_cc_floor_t*)path_x->congestion_alg_state;
    *cc_state = 0;
    *cc_param = 0;
    if (f == NULL || f->inner_state == NULL) {
        return;
    }
    uint64_t s = 0;
    uint64_t p = 0;
    path_x->congestion_alg_state = f->inner_state;
    f->inner->alg_observe(path_x, &s, &p);
    path_x->congestion_alg_state = f;

    uint64_t media = f->pinned_rate != 0 ? f->pinned_rate : f->estimate;
    uint64_t flags = (f->engaged ? AIOPQUIC_CC_FLOOR_ENGAGED : 0)
        | (f->queue_guard ? AIOPQUIC_CC_FLOOR_QUEUE_GUARD : 0)
        | (f->loss_guard ? AIOPQUIC_CC_FLOOR_LOSS_GUARD : 0);
    *cc_state = (s & 0xff) | flags | (aiopquic_cc_floor_sat32(media) << 32);
    *cc_param = aiopquic_cc_floor_sat32(p)
        | (aiopquic_cc_floor_sat32(f->floor_rate) << 32);
}

static picoquic_congestion_algorithm_t aiopquic_cc_bbr1_floor_algorithm_struct = {
    AIOPQUIC_CC_BBR1_FLOOR_ID, AIOPQUIC_CC_ALGO_NUMBER_BBR1_FLOOR, PICOQUIC_ECN_ECT_0,
    aiopquic_cc_bbr1_floor_init,
    aiopquic_cc_floor_notify,
    aiopquic_cc_floor_delete,
    aiopquic_cc_floor_observe
};

static picoquic_congestion_algorithm_t aiopquic_cc_bbr_floor_algorithm_struct = {
    AIOPQUIC_CC_BBR_FLOOR_ID, AIOPQUIC_CC_ALGO_NUMBER_BBR_FLOOR, PICOQUIC_ECN_ECT_0,
    aiopquic_cc_bbr_floor_init,
    aiopquic_cc_floor_notify,
    aiopquic_cc_floor_delete,
    aiopquic_cc_floor_observe
};

/* picoquic keeps a pointer to this table: it must outlive every context. */
static picoquic_congestion_algorithm_t const* aiopquic_cc_algorithms[10];

/* picoquic's eight algorithms plus bbr1-floor and bbr-floor. Call once per
 * process, before any network thread starts: those threads read the table. */
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
    aiopquic_cc_algorithms[n++] = &aiopquic_cc_bbr1_floor_algorithm_struct;
    aiopquic_cc_algorithms[n++] = &aiopquic_cc_bbr_floor_algorithm_struct;
    picoquic_register_congestion_control_algorithms(aiopquic_cc_algorithms, n);
}

#endif /* AIOPQUIC_CC_FLOOR_H */
