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

/* =====================================================================
 * Resource defaults — single source of truth for aiopquic tunables.
 *
 * Per-stream byte rings double as peer's max in-flight window (RX) and
 * Python's local TX staging buffer. Buffer size has two effects:
 *   - max throughput (FC-limited) = buffer / RTT
 *   - max queueing latency under drain stall = buffer / drain_rate
 * For live-media workloads (MoQT) prefer buffer ≈ BDP at target RTT;
 * going larger doesn't gain throughput (network-limited) but adds
 * latency-spike potential. Override per-cnx via QuicConfiguration.
 * ===================================================================== */

/* Per-stream RX byte ring fallback. 4 MB covers 1 Gbps × 32 ms or
 * 2.5 Gbps × 13 ms cleanly. For 2.5 Gbps × 100 ms WAN set ~32 MB via
 * QuicConfiguration.rx_data_ring_cap; for 10 Gbps × 100 ms set ~128 MB.
 * Memory cost scales with concurrent stream count. Power of two. */
#define AIOPQUIC_RX_DATA_RING_CAP_DEFAULT (1u << 22)

/* Per-stream TX byte ring fallback. Holds bytes between Python's
 * send_stream_data and picoquic's worker drain. Doesn't need to
 * be BDP-sized (picoquic drains continuously); 4 MB matches RX
 * for symmetry and gives ample staging headroom. */
#define AIOPQUIC_TX_DATA_RING_CAP_DEFAULT (1u << 22)

/* Legacy shared SPSC ring default — preserved for backward-compat
 * callers (Python QuicConfiguration.event_ring_capacity fallback).
 * New code should size tx and rx independently via the two macros
 * below. */
#define AIOPQUIC_SPSC_RING_CAPACITY_DEFAULT 262144

/* TX SPSC event ring default. Carries producer→worker notifications:
 * one MARK_ACTIVE per send_stream_data call in pull-model (bytes
 * live in sc->tx, not here), plus control msgs / WT commands / push-
 * model byte-bearing entries. 2048 entries = ~20 ms of producer
 * headroom at 100k events/s — small enough to backpressure early,
 * large enough to absorb command bursts without starving the data
 * path. Drainage threshold controlled by AIOPQUIC_TX_RING_WAKE_PCT.
 * Power of two. */
#define AIOPQUIC_TX_EVENT_RING_CAP_DEFAULT 2048

/* RX SPSC event ring default. Carries worker→asyncio notifications:
 * stream_data / stream_fin / stream_open / connection events + the
 * STREAM_TX_DRAINED and TX_RING_DRAINED wake events. Worker push
 * rate is peer-paced, not producer-paced — undersizing causes
 * silent event drops on multi-Gbps stream-churn workloads (see
 * worker_rx_event_drops counter; aiopquic_rx_log_enabled stderr).
 * 16384 entries chosen as the floor at which sustained MoQT
 * subgroup churn at multi-Gbps stops triggering drops in lab.
 * Power of two. */
#define AIOPQUIC_RX_EVENT_RING_CAP_DEFAULT 16384

/* TX SPSC ring drain-wake low-water mark, as percent of ring
 * capacity. Worker fires SPSC_EVT_TX_EVENT_RING_DRAINED when count
 * drops to/below this fraction while a Python writer has armed
 * tx_event_ring_drain_pending. 50% leaves the producer half-empty
 * headroom on wake. Overridable via AIOPQUIC_TX_RING_WAKE_PCT
 * env var at ctx_create time. (Temporarily reverted from 75%
 * while investigating control-stream delivery regression.) */
#define AIOPQUIC_TX_RING_WAKE_PCT_DEFAULT 50

#include <fcntl.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* O(N_connections) walk over picoquic's live-cnx list. Used as the
 * guard before dispatching any TX_* SPSC event into a `cnx*` from
 * the entry — the cnx may have been freed between the Python push
 * and our pop (peer CLOSE_CONNECTION, app-side close, timeout). Any
 * deref into a freed cnx is a UAF, surfacing as a NULL-page fault
 * inside picoquic when a struct field is loaded.
 *
 * TODO: replace with a generation-counter lookup if aiomoqt
 * grows into a relay role with many concurrent cnxs. For the
 * current publisher/client shape (1–2 cnxs) the walk is one or two
 * pointer compares per event — effectively free. Cost scales O(N)
 * and starts to matter past ~50 cnxs at megapacket-per-second
 * event rates. */
static inline int aiopquic_cnx_is_alive(picoquic_quic_t* quic,
                                         picoquic_cnx_t* cnx) {
    picoquic_cnx_t* cur = picoquic_get_first_cnx(quic);
    while (cur) {
        if (cur == cnx) {
            return 1;
        }
        cur = picoquic_get_next_cnx(cur);
    }
    return 0;
}
#include <netinet/in.h>
#include <arpa/inet.h>

/* Cached AIOPQUIC_RX_LOG env flag — resolved once at first use so the
 * overflow-diagnostic fprintf paths don't repeatedly hit getenv()
 * under sustained overflow (libc env lookups are not cheap when fired
 * from inside the picoquic worker callback). 0=unset, 1=set, -1=not
 * yet probed. Atomic only to ensure the first-probe write is visible
 * across threads — there is no correctness issue if two threads race
 * the probe; both write the same value. */
static _Atomic(int) _aiopquic_rx_log_cached = -1;

static inline int aiopquic_rx_log_enabled(void) {
    int v = atomic_load_explicit(&_aiopquic_rx_log_cached,
                                  memory_order_relaxed);
    if (v < 0) {
        const char* s = getenv("AIOPQUIC_RX_LOG");
        v = (s && *s && *s != '0') ? 1 : 0;
        atomic_store_explicit(&_aiopquic_rx_log_cached, v,
                              memory_order_relaxed);
    }
    return v;
}

/* AIOPQUIC_FC_RAW=1 forces the FC handler to grant raw buf_free
 * (ignoring bytes_pending_release). Diagnostic only — disables the
 * Python-pipeline backpressure that bounds RSS. Default OFF. */
static _Atomic(int) _aiopquic_fc_raw_cached = -1;

static inline int aiopquic_fc_raw_enabled(void) {
    int v = atomic_load_explicit(&_aiopquic_fc_raw_cached,
                                  memory_order_relaxed);
    if (v < 0) {
        const char* s = getenv("AIOPQUIC_FC_RAW");
        v = (s && *s && *s != '0') ? 1 : 0;
        atomic_store_explicit(&_aiopquic_fc_raw_cached, v,
                              memory_order_relaxed);
    }
    return v;
}

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
    spsc_ring_t*    rx_event_ring;
    spsc_ring_t*    tx_event_ring;
    int             eventfd;        /* readable fd asyncio watches (eventfd
                                       on Linux, pipe read end elsewhere) */
    int             wake_write_fd;  /* fd the network thread writes to
                                       (== eventfd on Linux; pipe write end
                                       elsewhere) */
    picoquic_quic_t* quic;
    /* Network-thread handle returned by picoquic_start_network_thread.
     * Set by Python's start() after the thread is up; used by C-side
     * helpers (aiopquic_push_fc_credit, etc.) that need to wake the
     * worker without round-tripping through Python. NULL during early
     * init and after thread teardown.
     *
     * IMPORTANT: picoquic_wake_up_network_thread takes this struct
     * pointer, NOT the picoquic_quic_t*. Passing the wrong type is
     * a UAF-class bug — the function dereferences fields that don't
     * exist at the same offsets. */
    picoquic_network_thread_ctx_t* thread_ctx;
    /* Per-stream RX byte ring capacity. Set at start() time from the
     * QuicConfiguration's max_stream_data so the ring can hold the
     * full peer-allowed in-flight window (the spec promises peer
     * never sends more before MAX_STREAM_DATA is extended). Rounded
     * up to a power of two by the Cython binding before being stored
     * here. */
    uint32_t        rx_data_ring_cap;
    /* Forensic counters — incremented from the picoquic worker thread
     * as it processes TX events. The asyncio thread reads them via
     * Cython properties to verify per-event accounting. Plain ints,
     * read with relaxed semantics (only one writer). */
    uint64_t        worker_mark_active_processed;
    uint64_t        worker_prepare_to_send_calls;
    uint64_t        worker_prepare_to_send_pulled_bytes;
    /* RX-side: count of spsc_ring_push failures on rx_event_ring (event
     * ring full). On stream_data callbacks the BYTES were already
     * pushed to sc->rx; the dropped EVENT means asyncio is not told
     * those bytes arrived → small streams whose only events drop
     * are silently lost. THIS WAS THE STREAM-LOSS BUG ROOT CAUSE. */
    uint64_t        worker_rx_event_drops;
    uint64_t        worker_rx_event_drops_stream_data;
    uint64_t        cnt_rx_data_event_coalesced;     /* data events skipped: notification already in flight */
    /* Notify-coalescing state. eventfd is level-triggered; multiple
     * write()s coalesce to one wake-up at the consumer, but each
     * write is still a syscall. At 250K events/sec on a stream-churn
     * workload that's a quarter-million unnecessary syscalls/sec on
     * the picoquic worker thread. Track whether the consumer has
     * acknowledged the last notify (= drained) and skip the syscall
     * when there's already a pending notification.
     *
     * Set to 1 by aiopquic_notify_rx (worker) on first push since
     * last drain. Cleared to 0 by aiopquic_clear_rx (consumer) when
     * it reads the eventfd. Producer-only-sets, single-writer
     * semantics — atomic exchange used to detect the 0→1 edge. */
    uint32_t        rx_notify_pending;
    /* Same coalescing on TX side: skip picoquic_wake_up_network_thread
     * unless the worker has drained since our last wake. The worker
     * sets this back to 0 whenever it observes the tx_event_ring drained
     * to empty (after popping all entries) so the next producer push
     * triggers a wake. */
    uint32_t        tx_wake_pending;
    /* Per-stream RX byte ring overflow counter. Replaces the
     * fprintf(stderr,...) on full from the previous diagnostic —
     * libc stdio holds a global lock and stalls the picoquic worker
     * under load. Set AIOPQUIC_RX_LOG=1 in the env to re-enable a
     * one-shot stderr line per overflow event. */
    uint64_t        worker_rx_byte_ring_overflow;
    /* TX ring drain wakeup. Python writers blocked on a full SPSC TX
     * event ring arm tx_event_ring_drain_pending=1 (clear-arm-recheck-wait
     * pattern); worker fires SPSC_EVT_TX_EVENT_RING_DRAINED via CAS-clear
     * once spsc_ring_count(tx_event_ring) <= tx_event_ring_low_water. Default
     * low_water = 50% of ring capacity, overridable via
     * AIOPQUIC_TX_RING_WAKE_PCT env var at ctx_create time. */
    uint32_t        tx_event_ring_drain_pending;
    uint32_t        tx_event_ring_low_water;

    /* ============================================================
     * Observability counters (added without behavior change).
     * All single-writer per cell; reads from asyncio thread via
     * Cython properties. Plain uint64_t with relaxed semantics
     * because correctness doesn't depend on these — they exist to
     * answer "which side of the wake chain stalled" by inspection.
     *
     * Timestamps use CLOCK_MONOTONIC ns to align worker- and
     * asyncio-thread events on a single comparable time-base.
     * ============================================================ */
    uint64_t        cnt_tx_event_ring_pushes;            /* Python push into tx_event_ring */
    uint64_t        cnt_tx_event_ring_pops;              /* worker pop from tx_event_ring */
    uint64_t        cnt_tx_event_ring_arms;              /* Python armed tx_event_ring_drain_pending */
    uint64_t        cnt_tx_event_ring_fires;             /* worker fired SPSC_EVT_TX_EVENT_RING_DRAINED */
    uint64_t        cnt_tx_event_ring_fire_dropped;      /* fire CAS won but rx_event_ring push failed → re-armed */
    uint64_t        cnt_wake_calls;                /* picoquic_wake_up_network_thread invoked */
    uint64_t        cnt_wake_skipped_coalesced;    /* wake skipped (tx_wake_pending already 1) */
    uint64_t        cnt_prepare_to_send_empty;     /* prepare_to_send invoked with avail==0 */
    uint64_t        last_tx_event_ring_arm_ns;           /* CLOCK_MONOTONIC of last arm */
    uint64_t        last_tx_event_ring_fire_ns;          /* CLOCK_MONOTONIC of last fire */
    /* RX flow-control observability (added 2026-05-25). Drain side
     * (asyncio) pushes OPEN_FLOW_CONTROL events; worker handles them.
     * cnt_fc_credit_pushed must equal cnt_fc_credit_handled + cnt_fc_credit_dropped
     * in steady state — otherwise events are leaking on the worker side. */
    uint64_t        cnt_fc_credit_pushed;          /* asyncio push of SPSC_EVT_TX_OPEN_FLOW_CONTROL */
    uint64_t        cnt_fc_credit_handled;         /* worker processed SPSC_EVT_TX_OPEN_FLOW_CONTROL */
    uint64_t        cnt_fc_credit_dropped;         /* push failed (tx_event_ring full) */
    /* WT-side sc/link leak diagnostic. Incremented when
     * picohttp_callback_free fires with a non-NULL link but
     * stream_ctx->path_callback_ctx no longer matches — the
     * "skipped cleanup" path that may correlate with the May-23
     * sub-side sc retention. Zero in healthy steady-state; growth
     * rate = sc leak rate per cnx. */
    uint64_t        cnt_wt_callback_free_skipped;
    /* Per-site sc create/ref/destroy counters (added 2026-06-06).
     * Invariant: Σ create + Σ ref == Σ destroy at process end.
     * Per-site imbalance localizes the May-23 sub-side retention. */
    uint64_t        cnt_sc_create_raw_quic;         /* callback.h ~803 first-touch */
    uint64_t        cnt_sc_create_wt_link;          /* h3wt_callback.h ~163 link create */
    uint64_t        cnt_sc_ref_fc_credit;           /* callback.h ~440 FC credit push ref */
    /* No total "cnt_sc_destroy_wt_link" — accessing link->session inside
     * aiopquic_wt_stream_link_destroy is a UAF on shutdown (session freed
     * before all LINK_RELEASE events drain). The split counters below are
     * incremented at call sites where session validity is guaranteed. */
    uint64_t        cnt_sc_destroy_wt_link_callback_free; /* via picohttp_callback_free cleanup */
    uint64_t        cnt_sc_destroy_wt_link_close_walker;  /* via session-close walker sweep */
    uint64_t        cnt_sc_destroy_fc_credit_pushfail; /* callback.h ~464 push-fail unref */
    uint64_t        cnt_sc_destroy_fc_credit_worker;   /* callback.h ~1114 worker unref */
    /* QUIC keep-alive interval (microseconds). 0 = disabled. Applied
     * per-cnx by the worker thread at the ready callback via
     * picoquic_enable_keep_alive — PING frames hold an otherwise-quiet
     * connection open past the idle timeout (e.g. a consumer-stalled
     * subscriber whose flow control has back-pressured the sender to
     * silence). Set once at start(); read by the worker. */
    uint64_t        keep_alive_us;
} aiopquic_ctx_t;

/* aiopquic_now_ns() is defined in stream_ctx.h (included above). */

/* Create a TransportContext with independent TX and RX SPSC ring
 * sizes and an explicit drain-wake low-water percent. Pass 0 for
 * any of the three to take the compile-time default
 * (AIOPQUIC_TX_EVENT_RING_CAP_DEFAULT / AIOPQUIC_RX_EVENT_RING_CAP_DEFAULT /
 * AIOPQUIC_TX_RING_WAKE_PCT_DEFAULT). Ring caps must be powers of
 * two — the caller (Cython binding) is responsible for rounding. */
static inline aiopquic_ctx_t* aiopquic_ctx_create(uint32_t tx_cap,
                                                   uint32_t rx_cap,
                                                   uint32_t low_water_pct) {
    if (tx_cap == 0) tx_cap = AIOPQUIC_TX_EVENT_RING_CAP_DEFAULT;
    if (rx_cap == 0) rx_cap = AIOPQUIC_RX_EVENT_RING_CAP_DEFAULT;
    if (low_water_pct == 0 || low_water_pct >= 100) {
        low_water_pct = AIOPQUIC_TX_RING_WAKE_PCT_DEFAULT;
    }

    aiopquic_ctx_t* ctx = (aiopquic_ctx_t*)calloc(1, sizeof(aiopquic_ctx_t));
    if (!ctx) return NULL;

    ctx->rx_event_ring = spsc_ring_create(rx_cap);
    ctx->tx_event_ring = spsc_ring_create(tx_cap);
    if (!ctx->rx_event_ring || !ctx->tx_event_ring) {
        spsc_ring_destroy(ctx->rx_event_ring);
        spsc_ring_destroy(ctx->tx_event_ring);
        free(ctx);
        return NULL;
    }

    /* TX ring drain wake threshold: explicit param wins, then env
     * override (AIOPQUIC_TX_RING_WAKE_PCT), then the macro default.
     * Computed against tx_cap, not the legacy single-cap. */
    {
        uint32_t lw_pct = low_water_pct;
        const char* env_lw = getenv("AIOPQUIC_TX_RING_WAKE_PCT");
        if (env_lw && *env_lw) {
            int v = atoi(env_lw);
            if (v > 0 && v < 100) lw_pct = (uint32_t)v;
        }
        ctx->tx_event_ring_low_water = (tx_cap * lw_pct) / 100;
    }

#ifdef __linux__
    ctx->eventfd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (ctx->eventfd < 0) {
        spsc_ring_destroy(ctx->rx_event_ring);
        spsc_ring_destroy(ctx->tx_event_ring);
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
        spsc_ring_destroy(ctx->rx_event_ring);
        spsc_ring_destroy(ctx->tx_event_ring);
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
        spsc_ring_destroy(ctx->rx_event_ring);
        spsc_ring_destroy(ctx->tx_event_ring);
        free(ctx);
    }
}

/* TX wake-coalescing: producer-side helper. Returns 1 if a wake is
 * already pending (caller should skip the picoquic_wake_up syscall),
 * 0 if this caller should write the wake. The worker clears the flag
 * back to 0 when it finishes draining the TX ring. Single-producer
 * (asyncio thread) so atomic exchange is sufficient. */
static inline int aiopquic_tx_wake_set_pending(aiopquic_ctx_t* ctx) {
    uint32_t prev = __atomic_exchange_n(&ctx->tx_wake_pending, 1,
                                         __ATOMIC_RELEASE);
    if (prev == 0) {
        ctx->cnt_wake_calls++;  /* this caller WILL invoke wake_up */
    } else {
        ctx->cnt_wake_skipped_coalesced++;  /* coalesced under prior wake */
    }
    return (int)prev;
}

/* Forward decl: aiopquic_maybe_fire_tx_event_ring_drained pushes
 * SPSC_EVT_TX_EVENT_RING_DRAINED into rx_event_ring and calls aiopquic_notify_rx
 * which is defined further down in this header. */
static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx);

/* Python-side helpers for the TX ring drain wakeup protocol. The
 * producer (asyncio thread) calls arm() before awaiting the event,
 * and clear() if it raced with a worker drain. Worker side does
 * the CAS-clear+fire inside aiopquic_maybe_fire_tx_event_ring_drained. */
static inline void aiopquic_arm_tx_event_ring_drain_pending(aiopquic_ctx_t* ctx) {
    ctx->cnt_tx_event_ring_arms++;
    ctx->last_tx_event_ring_arm_ns = aiopquic_now_ns();
    __atomic_store_n(&ctx->tx_event_ring_drain_pending, 1,
                      __ATOMIC_RELEASE);
}

static inline void aiopquic_clear_tx_event_ring_drain_pending(aiopquic_ctx_t* ctx) {
    __atomic_store_n(&ctx->tx_event_ring_drain_pending, 0,
                      __ATOMIC_RELEASE);
}

/* Push an SPSC_EVT_TX_OPEN_FLOW_CONTROL event onto the TX ring so
 * the picoquic worker re-evaluates the per-stream MAX_STREAM_DATA
 * grant. Called from two sites:
 *   - drain_rx data paths after popping bytes from sc->rx (normal
 *     credit replenishment as peer data is consumed).
 *   - StreamChunk.__dealloc__ when a chunk's bytes are released
 *     back to aiopquic, so peer credit reopens when the Python
 *     pipeline drains. This is the closed-loop signal that breaks
 *     the FC-stall deadlock: peer doesn't need to send anything
 *     for credit to resume; releasing a chunk is sufficient.
 *
 * entry.stream_ctx carries the BORROWED aiopquic_stream_ctx_t* so
 * the worker can read both sc->rx_buf_free and sc->bytes_pending_release
 * when computing the effective grant. Wake is coalesced via
 * tx_wake_pending — many back-to-back pushes collapse to one wake. */
static inline void aiopquic_push_fc_credit(aiopquic_ctx_t* ctx,
                                            void* cnx,
                                            uint64_t stream_id,
                                            aiopquic_stream_ctx_t* sc) {
    if (!ctx || !cnx || !sc) return;
    /* Take a ref on sc so the worker handler (which runs later,
     * possibly after the caller's chunk has been dealloc'd) can
     * safely deref sc->rx and sc->bytes_pending_release. The
     * handler is responsible for the matching unref via
     * aiopquic_stream_ctx_destroy. */
    aiopquic_stream_ctx_ref(sc);
    ctx->cnt_sc_ref_fc_credit++;
    spsc_entry_t entry;
    memset(&entry, 0, sizeof(entry));
    entry.event_type = SPSC_EVT_TX_OPEN_FLOW_CONTROL;
    entry.stream_id = stream_id;
    entry.cnx = (picoquic_cnx_t*)cnx;
    entry.stream_ctx = sc;
    if (spsc_ring_push(ctx->tx_event_ring, &entry, NULL, 0) == 0) {
        ctx->cnt_tx_event_ring_pushes++;
        ctx->cnt_fc_credit_pushed++;
        /* Wake only if the thread is running. Coalesced via
         * tx_wake_pending so back-to-back pushes collapse to one
         * syscall. CRITICAL: picoquic_wake_up_network_thread takes
         * picoquic_network_thread_ctx_t*, NOT picoquic_quic_t* —
         * passing the wrong type is a UAF-class bug that silently
         * corrupts picoquic state. */
        if (ctx->thread_ctx != NULL &&
            aiopquic_tx_wake_set_pending(ctx) == 0) {
            picoquic_wake_up_network_thread(ctx->thread_ctx);
        }
    } else {
        /* tx_event_ring push failed (ring full). Drop the ref we just
         * took since no worker handler will run for this push. */
        ctx->cnt_fc_credit_dropped++;
        ctx->cnt_sc_destroy_fc_credit_pushfail++;
        aiopquic_stream_ctx_destroy(sc);
    }
}

/* Worker-side: if a Python writer armed tx_event_ring_drain_pending and the
 * TX SPSC event ring has now drained to or below the low-water mark,
 * fire SPSC_EVT_TX_EVENT_RING_DRAINED to wake the writer. CAS-clear ensures
 * exactly one event per arm.
 *
 * Called after every spsc_ring_pop(tx_event_ring) in the worker's drain
 * loop so the wake fires at the moment the count crosses below
 * low_water — gives the producer concurrent runway rather than
 * waiting for full drain.
 *
 * The arm itself is the producer-side "clear-arm-recheck-wait"
 * pattern: producer clears event, arms pending, re-checks pressure
 * (if dropped below high_water during the arm window, producer
 * proceeds without waiting; otherwise awaits the event).
 *
 * No lost wakeup: if producer armed and pressure is still high,
 * worker WILL fire at the next crossing of low_water (CAS-clear is
 * single-shot per arm; pending=1 persists until worker observes it
 * AND ring is below low_water). */
static inline void aiopquic_maybe_fire_tx_event_ring_drained(aiopquic_ctx_t* ctx) {
    if (__atomic_load_n(&ctx->tx_event_ring_drain_pending,
                         __ATOMIC_ACQUIRE) != 1) {
        return;
    }
    if (spsc_ring_count(ctx->tx_event_ring) > ctx->tx_event_ring_low_water) {
        return;
    }
    uint32_t expected = 1;
    if (!__atomic_compare_exchange_n(
            &ctx->tx_event_ring_drain_pending, &expected, 0,
            0, __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
        return;  /* another caller beat us; their fire is sufficient */
    }
    spsc_entry_t entry = {0};
    entry.event_type = SPSC_EVT_TX_EVENT_RING_DRAINED;
    if (spsc_ring_push(ctx->rx_event_ring, &entry, NULL, 0) == 0) {
        ctx->cnt_tx_event_ring_fires++;
        ctx->last_tx_event_ring_fire_ns = aiopquic_now_ns();
        aiopquic_notify_rx(ctx);
    } else {
        /* rx_event_ring full — re-arm tx_event_ring_drain_pending so the next pop
         * (or post-drain re-check in the wake_up handler) retries
         * once Python catches up. Mirrors the STREAM_TX_DRAINED
         * handling at callback.h:496-507 and the WT analog at
         * h3wt_callback.h:649-664. Without this re-arm the wake is
         * lost (CAS-clear is already committed) and the producer
         * awaiting ring_event deadlocks. */
        ctx->cnt_tx_event_ring_fire_dropped++;
        __atomic_store_n(&ctx->tx_event_ring_drain_pending, 1,
                          __ATOMIC_RELEASE);
    }
}

/* Signal asyncio that there are RX events.
 * Coalesced: write the wake fd only on the 0→1 transition of
 * rx_notify_pending. The consumer (aiopquic_clear_rx) restores the
 * flag to 0 after it has drained its ring snapshot AND re-armed if
 * the ring is still non-empty, so any push that races against the
 * consumer's drain is guaranteed to wake asyncio at least once.
 * ACQ_REL pairs with the consumer's RELEASE store on the same flag
 * and ACQUIRE on the ring entries. */
static inline void aiopquic_notify_rx(aiopquic_ctx_t* ctx) {
    if (__atomic_exchange_n(&ctx->rx_notify_pending, 1,
                              __ATOMIC_ACQ_REL) != 0) {
        return;
    }
#ifdef __linux__
    uint64_t val = 1;
    (void)write(ctx->wake_write_fd, &val, sizeof(val));
#else
    uint8_t b = 1;
    (void)write(ctx->wake_write_fd, &b, 1);
#endif
}

/* Consumer-side wake-fd clear + re-arm. Caller (drain_rx) MUST have
 * already drained as many ring entries as it intends to consume in
 * this cycle BEFORE calling this. We then:
 *   1. RELEASE-store rx_notify_pending=0 so the NEXT producer push
 *      will write the wake fd (the producer's exchange sees 0→1).
 *   2. Drain the wake-fd counter (Linux: one read; other: read until
 *      EAGAIN).
 *   3. Re-arm by writing the wake fd if the ring is still non-empty.
 *      Covers the race where a producer pushed an entry between the
 *      consumer's peek-empty and pending=0, observing pending=1 and
 *      thus skipping its own wake write — without re-arm those
 *      stranded entries would never wake asyncio again. */
static inline void aiopquic_clear_rx(aiopquic_ctx_t* ctx) {
    __atomic_store_n(&ctx->rx_notify_pending, 0, __ATOMIC_RELEASE);
#ifdef __linux__
    uint64_t val;
    (void)read(ctx->eventfd, &val, sizeof(val));
#else
    uint8_t buf[64];
    while (read(ctx->eventfd, buf, sizeof(buf)) > 0) {
        /* drain */
    }
#endif
    if (!spsc_ring_empty(ctx->rx_event_ring)) {
#ifdef __linux__
        uint64_t one = 1;
        (void)write(ctx->wake_write_fd, &one, sizeof(one));
#else
        uint8_t b = 1;
        (void)write(ctx->wake_write_fd, &b, 1);
#endif
    }
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

    /* TX-side callback. PULL model only: stream_ctx is an
     * aiopquic_stream_ctx_t* set by picoquic_mark_active_stream
     * (queued via SPSC_EVT_TX_MARK_ACTIVE). Drain bytes from the
     * stream's sc->tx ring into picoquic's frame buffer up to the
     * caller's budget. If sc->tx drains to empty AND fin_pending
     * is set, signal FIN; else keep the stream active. The legacy
     * push-model fallback (stream_ctx==NULL → drain a single
     * SPSC_EVT_TX_STREAM_DATA/FIN entry from the shared TX ring)
     * was removed in 0.3.5; mark_active never passes NULL.
     */
    if (fin_or_event == picoquic_callback_prepare_to_send) {
        ctx->worker_prepare_to_send_calls++;
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
                ctx->worker_prepare_to_send_pulled_bytes += to_send;
                aiopquic_tx_data_bytes_pulled_add(to_send);
            } else {
                /* prepare_to_send invoked with no bytes to pull —
                 * either sc->tx empty (worker faster than producer)
                 * or picoquic offered length==0 (out of packet space).
                 * Tracked separately from the bytes-pulled case so the
                 * counter dump can tell idle from productive cycles. */
                ctx->cnt_prepare_to_send_empty++;
            }
            /* No sender-side STREAM_DESTROY here: picoquic may still
             * callback with stream_ctx=sc after our FIN (retransmit
             * / ACK), so early destroy is a UAF. Deferred to 0.3.6. */
            /* Edge-trigger TX backpressure: if a Python writer was
             * blocked (set tx_drain_pending=1 after seeing sc->tx
             * full OR via the soft byte-budget arm in path B),
             * CAS-clear and emit one SPSC event so it can wake and
             * retry. Single-shot per cycle — only the CAS winner
             * pushes the event.
             *
             * Hoisted OUTSIDE the to_send > 0 guard: prepare_to_send
             * may legitimately arrive with to_send == 0 (sc->tx drained
             * by a prior call, picoquic still calling us to ask) yet
             * the writer's "wait for sc->tx room" precondition is in
             * fact satisfied (avail == 0 means maximum room). Keeping
             * the fire inside the guard caused a deterministic miss
             * after the LAST drain emptied sc->tx — pending stayed
             * at 1 forever, sc_event never fired. Mirrors the WT
             * fix at h3wt_callback.h. */
            uint32_t expected = 1;
            if (atomic_compare_exchange_strong_explicit(
                    &sc->tx_drain_pending, &expected, 0,
                    memory_order_acq_rel, memory_order_relaxed)) {
                spsc_entry_t drain_entry = {0};
                drain_entry.event_type = SPSC_EVT_STREAM_TX_DRAINED;
                drain_entry.stream_id = stream_id;
                drain_entry.cnx = cnx;
                drain_entry.stream_ctx = sc;
                if (spsc_ring_push(ctx->rx_event_ring,
                                   &drain_entry, NULL, 0) == 0) {
                    sc->cnt_drain_fires++;
                    sc->last_drain_fire_ns = aiopquic_now_ns();
                    aiopquic_notify_rx(ctx);
                } else {
                    /* RX ring full — re-arm so Python re-attempts
                     * once it drains; the worker will retry next
                     * prepare_to_send. Better than losing the
                     * wakeup. */
                    sc->cnt_drain_dropped++;
                    atomic_store_explicit(
                        &sc->tx_drain_pending, 1,
                        memory_order_release);
                }
            }
            return 0;
        }

        /* stream_ctx==NULL on prepare_to_send is unexpected in pull-model
         * 0.3.5+: mark_active is always called with a stream_ctx, so
         * picoquic should never invoke this callback with a null ctx.
         * Defensive: signal "no data" and return without consuming
         * anything from the shared TX ring. */
        (void)picoquic_provide_stream_data_buffer(bytes, 0, 0, 0);
        return 0;
    }

    /* Local picoquic patch (patches/0003-...): fires once per stream
     * when picoquic considers it fully retired (pure receivers on
     * FIN/RESET; senders/bidi when all sent data ACKed). Universal
     * sc-destroy signal; covers cases the old pure-receiver-only
     * STREAM_DESTROY emit (further down) didn't. picoquic won't call
     * back with this stream_ctx again, so dropping the stream-
     * lifetime ref is safe. */
    if (fin_or_event == picoquic_callback_stream_released) {
        if (stream_ctx != NULL) {
            spsc_entry_t destroy_entry = {0};
            destroy_entry.event_type = SPSC_EVT_STREAM_DESTROY;
            destroy_entry.stream_id = stream_id;
            destroy_entry.cnx = cnx;
            destroy_entry.stream_ctx = stream_ctx;
            if (spsc_ring_push(ctx->rx_event_ring, &destroy_entry,
                               NULL, 0) == 0) {
                aiopquic_notify_rx(ctx);
            } else {
                ctx->worker_rx_event_drops++;
            }
        }
        return 0;
    }

    if (fin_or_event == picoquic_callback_ready
            && ctx->keep_alive_us > 0) {
        /* Enable keep-alive once the cnx is ready (worker thread owns
         * the cnx here). PING frames keep a quiet connection alive past
         * the idle timeout — important for a flow-controlled subscriber
         * whose consumer stalled and back-pressured the sender silent. */
        picoquic_enable_keep_alive(cnx, ctx->keep_alive_us);
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
    aiopquic_stream_ctx_t* coalesce_sc = NULL;
    if (has_payload) {
        /* Physical RX ring sized at exactly the advertised window
         * (1x). picoquic's auto-FC extension is gated by
         * !stream->use_app_flow_control (frames.c:4638), and we set
         * use_app_flow_control=1 SYNCHRONOUSLY inside this first
         * stream_data callback before returning to picoquic's send
         * framer — so no auto-extend frame can be emitted after our
         * opt-in. Before our opt-in, the peer's max in-flight bytes
         * is bounded by the initial_max_stream_data transport
         * parameter; after, by advertise_cap. Neither produces 2x
         * overshoot, so the previous 2x headroom was unnecessary
         * memory traffic + cache footprint per stream. RX ring
         * overflow remains a hard error: a peer that overruns is a
         * spec-correct FLOW_CONTROL_ERROR connection close. */
        uint32_t advertise_cap = ctx->rx_data_ring_cap > 0
            ? ctx->rx_data_ring_cap
            : AIOPQUIC_RX_DATA_RING_CAP_DEFAULT;
        uint32_t physical_cap = advertise_cap;
        aiopquic_stream_ctx_t* sc = (aiopquic_stream_ctx_t*)stream_ctx;
        int rx_first_touch = 0;
        if (!sc) {
            sc = aiopquic_stream_ctx_create();
            if (!sc) {
                return -1;
            }
            ctx->cnt_sc_create_raw_quic++;
            picoquic_set_app_stream_ctx(cnx, stream_id, sc);
            entry.stream_ctx = sc;
            /* Opt into application-driven flow control on this stream.
             * The MAX_STREAM_DATA grant itself is issued via
             * picoquic_open_flow_control AFTER we push the just-
             * delivered bytes so the grant reflects actual free space —
             * see the FC block below. */
            (void)picoquic_set_app_flow_control(cnx, stream_id, 1);
            rx_first_touch = 1;
        }
        if (aiopquic_stream_ctx_ensure_rx(sc, physical_cap) != 0) {
            return -1;
        }
        uint32_t pushed = aiopquic_stream_buf_push(
            sc->rx, bytes, (uint32_t)length);
        if (pushed > 0) {
            aiopquic_sc_rx_bytes_pushed_add(pushed);
        }
        if (pushed != length) {
            /* Per-stream RX byte-ring overflow. Means the peer sent
             * more than its advertised flow-control window allowed —
             * a spec-correct FLOW_CONTROL_ERROR connection close.
             * Counter incremented from worker thread; opt-in stderr
             * via env AIOPQUIC_RX_LOG=1 (libc stdio holds a global
             * lock and stalls the worker thread under load, so the
             * default is silent). */
            ctx->worker_rx_byte_ring_overflow++;
            if (aiopquic_rx_log_enabled()) {
                fprintf(stderr,
                    "[aiopquic_rx] stream=%llu RX ring overflow: "
                    "pushed %u of %zu (free=%u, physical_cap=%u, "
                    "advertised=%u). Peer flow-control violation.\n",
                    (unsigned long long)stream_id, pushed, length,
                    aiopquic_stream_buf_free(sc->rx),
                    physical_cap, advertise_cap);
            }
            return -1;
        }
        if (fin_or_event == picoquic_callback_stream_fin) {
            aiopquic_stream_buf_set_fin(sc->rx);
        }
        /* FC management: first-touch grants initial buffer capacity to
         * peer via open_flow_control(buf_free post-push). Subsequent
         * replenishment is consumer-driven — Python's drain pushes
         * SPSC_EVT_TX_OPEN_FLOW_CONTROL with the popped byte count as
         * an incremental credit, mirroring picoquic's own
         * flow_control_test pattern. Worker-side extension inside
         * post_data can't unblock an FC-stalled peer. */
        if (rx_first_touch) {
            uint32_t buf_free = aiopquic_stream_buf_free(sc->rx);
            (void)picoquic_open_flow_control(cnx, stream_id, buf_free);
            aiopquic_stream_ctx_rx_credit_store(sc, advertise_cap);
        }
        /* RX data-event coalescing: at most ONE outstanding STREAM_DATA
         * notification per stream. The event carries no payload —
         * drain pops ALL available sc->rx bytes per notification — so
         * extra notifications are pure ring traffic that, under flood,
         * overflow the ring and get DROPPED, stranding bytes already
         * sitting in sc->rx. Only the CAS winner pushes; losers
         * return knowing the in-flight notification's drain picks up
         * their bytes. FIN always pushes — end-of-stream is a
         * distinct signal the drain must see. */
        if (entry.event_type == SPSC_EVT_STREAM_DATA) {
            if (!aiopquic_stream_ctx_rx_event_pending_arm(sc)) {
                ctx->cnt_rx_data_event_coalesced++;
                return 0;
            }
            coalesce_sc = sc;
        }
        ret = spsc_ring_push(ctx->rx_event_ring, &entry, NULL, 0);
    } else {
        ret = spsc_ring_push(ctx->rx_event_ring, &entry, bytes, (uint32_t)length);
    }
    if (ret == 0) {
        aiopquic_notify_rx(ctx);
        /* Note: the pure-receiver STREAM_DESTROY emit that used to
         * live here is removed in 0.3.6. The new
         * picoquic_callback_stream_released path (handled near the
         * top of this function) is universal — it covers pure
         * receivers AND senders AND bidi at the right moment
         * (immediately for pure receivers, after FIN+ACK for
         * senders). */
    } else {
        /* RX EVENT RING FULL — the data is already in sc->rx (for
         * stream_data) but the notification event is gone. asyncio
         * will only learn about the bytes if a LATER event for the
         * same stream pushes successfully (drain_rx pops avail bytes
         * from sc->rx then). Short streams whose only events drop
         * are silently lost. Counter is exposed via Cython so callers
         * can detect the condition; opt-in stderr via AIOPQUIC_RX_LOG=1. */
        ctx->worker_rx_event_drops++;
        if (has_payload) {
            ctx->worker_rx_event_drops_stream_data++;
        }
        if (coalesce_sc != NULL) {
            /* Notification push failed after arming: clear so the next
             * arrival re-arms and retries; bytes wait in sc->rx
             * meanwhile (same exposure as the pre-coalescing drop
             * path, now vastly rarer since data events can no
             * longer flood the ring). */
            aiopquic_stream_ctx_rx_event_pending_clear(coalesce_sc);
        }
        if (aiopquic_rx_log_enabled()
                && ctx->worker_rx_event_drops <= 100) {
            fprintf(stderr,
                "[aiopquic_rx] EVENT RING FULL: drop "
                "stream=%llu evt=%u (rx_event_ring entries=%u)\n",
                (unsigned long long)stream_id, entry.event_type,
                spsc_ring_count(ctx->rx_event_ring));
        }
    }

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
            spsc_ring_push_event(ctx->rx_event_ring, SPSC_EVT_READY, 0, NULL, 0);
            aiopquic_notify_rx(ctx);
            break;

        case picoquic_packet_loop_wake_up:
            /* Clear pending BEFORE draining. Race-safe ordering:
             * - If producer pushes BEFORE we clear: their exchange
             *   sees 1, skips wake — but their entry is in the ring,
             *   we'll see it on the next peek (still in the loop).
             * - If producer pushes AFTER we clear: their exchange
             *   sees 0, fires wake — picoquic re-enters this case.
             * - If producer pushes between our peek-empty and clear:
             *   their exchange could see 1 (stale) and skip — that's
             *   why we clear FIRST. Producer always sees 0 when the
             *   ring was actually drained. */
            __atomic_store_n(&ctx->tx_wake_pending, 0,
                              __ATOMIC_RELEASE);
            while (1) {
                spsc_entry_t* entry = spsc_ring_peek(ctx->tx_event_ring);
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
                            spsc_ring_push(ctx->rx_event_ring, &resp, NULL, 0);
                            aiopquic_notify_rx(ctx);
                        }
                    }
                    ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                    aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                    continue;
                }

                /* WT events route through aiopquic_wt_handle_tx, which
                 * interprets entry->cnx as aiopquic_wt_session_t* — NOT a
                 * picoquic_cnx_t*. TX_WT_OPEN in particular CREATES the
                 * picoquic cnx (via picowt_prepare_client_cnx) and stashes
                 * it into wt_session->cnx. The raw-QUIC liveness guard
                 * below must not be applied to these: walking picoquic's
                 * live-cnx list with a wt_session pointer always misses
                 * and drops the event. WT sessions track their own
                 * lifecycle inside aiopquic_wt_handle_tx. */
                if (entry->event_type >= SPSC_EVT_TX_WT_OPEN &&
                    entry->event_type <= SPSC_EVT_TX_WT_STOP_SENDING) {
                    (void)aiopquic_wt_handle_tx(quic, ctx, entry);
                    ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                    aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                    continue;
                }

                /* Stale-cnx guard. Drops events whose cnx was freed
                 * between the Python push and this pop. Without this,
                 * any picoquic_* call below UAFs. See
                 * aiopquic_cnx_is_alive() comment for cost notes. */
                if (!cnx || !aiopquic_cnx_is_alive(quic, cnx)) {
                    ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                    aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                    continue;
                }

                switch (entry->event_type) {
                    case SPSC_EVT_TX_MARK_ACTIVE: {
                        /* Raw QUIC pushes stream_ctx = sc so picoquic's
                         * app_stream_ctx routes sc into subsequent
                         * stream callbacks. WT pushes stream_ctx = NULL
                         * (h3zero owns app_stream_ctx) — use v2 so the
                         * h3zero pointer isn't clobbered. */
                        if (entry->stream_ctx != NULL) {
                            picoquic_mark_active_stream(cnx, entry->stream_id,
                                                         1, entry->stream_ctx);
                        } else {
                            picoquic_mark_active_stream_v2(cnx, entry->stream_id,
                                                            1);
                        }
                        ctx->worker_mark_active_processed++;
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    /* SPSC_EVT_TX_STREAM_DATA / SPSC_EVT_TX_STREAM_FIN
                     * (push-model byte-bearing events) were removed in
                     * 0.3.5. All stream writes now flow through the
                     * pull-model path: producer commits to sc->tx and
                     * pushes a MARK_ACTIVE event; picoquic later drains
                     * via the prepare_to_send callback. */
                    case SPSC_EVT_TX_DATAGRAM: {
                        const uint8_t* data = (const uint8_t*)entry->data_buf;
                        picoquic_queue_datagram_frame(cnx, entry->data_length, data);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    case SPSC_EVT_TX_CLOSE: {
                        /* cnx liveness already verified by the outer
                         * aiopquic_cnx_is_alive() guard above. */
                        picoquic_close(cnx, entry->error_code);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    case SPSC_EVT_TX_STREAM_RESET: {
                        picoquic_reset_stream(cnx, entry->stream_id, entry->error_code);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    case SPSC_EVT_TX_STOP_SENDING: {
                        picoquic_stop_sending(cnx, entry->stream_id, entry->error_code);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    case SPSC_EVT_TX_OPEN_FLOW_CONTROL: {
                        /* Asyncio thread asks us to advance peer's
                         * MAX_STREAM_DATA. entry->stream_ctx carries a
                         * borrowed pointer to the per-stream wrapper
                         * (aiopquic_stream_ctx_t*); we compute the
                         * effective grant HERE rather than relying on
                         * a value Python snapshotted at push time.
                         *
                         * Effective grant accounts for BOTH sc->rx
                         * ring fullness AND bytes that drain_rx has
                         * dispatched to Python (StreamChunks) but the
                         * consumer hasn't released yet (sc->bytes_pending_release).
                         * Over-granting ignores the Python pipeline
                         * backlog and lets peer flood beyond what the
                         * end-to-end consumer can absorb. The min/sub
                         * here is the application-aware backpressure
                         * point: peer's window grows only as Python
                         * actually releases chunks.
                         *
                         * effective_free = max(0, sc->rx_buf_free
                         *                          - bytes_pending_release)
                         *
                         * StreamChunk.__dealloc__ pushes a fresh event
                         * here on every chunk release, so when peer
                         * stalls on FC, the next release reopens
                         * credit — no asyncio polling required. */
                        ctx->cnt_fc_credit_handled++;
                        aiopquic_stream_ctx_t* sc =
                            (aiopquic_stream_ctx_t*)entry->stream_ctx;
                        if (sc != NULL && sc->rx != NULL) {
                            uint32_t buf_free =
                                aiopquic_stream_buf_free(sc->rx);
                            uint32_t grant;
                            if (aiopquic_fc_raw_enabled()) {
                                grant = buf_free;
                            } else {
                                uint64_t pending =
                                    aiopquic_stream_ctx_pending_load(sc);
                                grant = (pending >= buf_free)
                                    ? 0
                                    : (uint32_t)(buf_free - pending);
                            }
                            if (grant > 0) {
                                (void)picoquic_open_flow_control(
                                    cnx, entry->stream_id, grant);
                            }
                        }
                        /* Drop the ref aiopquic_push_fc_credit took
                         * on our behalf. sc may be freed here if this
                         * was the last reference (chunks all dealloc'd
                         * AND stream FIN'd AND no other pending fc
                         * events). Safe — we accessed sc above; this
                         * is the very last touch. */
                        if (sc != NULL) {
                            ctx->cnt_sc_destroy_fc_credit_worker++;
                            aiopquic_stream_ctx_destroy(sc);
                        }
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    case SPSC_EVT_TX_SET_APP_FLOW_CONTROL: {
                        (void)picoquic_set_app_flow_control(
                            cnx, entry->stream_id, (int)entry->is_fin);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
                        break;
                    }
                    default:
                        /* Unknown for raw-QUIC; route to WT dispatch
                         * which handles WT-specific TX commands. */
                        (void)aiopquic_wt_handle_tx(quic, ctx, entry);
                        ctx->cnt_tx_event_ring_pops++; spsc_ring_pop(ctx->tx_event_ring);
                        aiopquic_maybe_fire_tx_event_ring_drained(ctx);
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
