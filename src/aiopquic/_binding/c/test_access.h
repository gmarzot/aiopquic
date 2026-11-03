/*
 * Test-only reads of picoquic state that has no public getter. Read from
 * the asyncio thread while the worker owns the context: tests only.
 */
#ifndef AIOPQUIC_TEST_ACCESS_H
#define AIOPQUIC_TEST_ACCESS_H

#include "picoquic_internal.h"
#include "callback.h"

static inline int aiopquic_test_cnx_datagram_priority(picoquic_cnx_t* cnx) {
    return cnx == NULL ? -1 : (int)cnx->datagram_priority;
}

static inline int aiopquic_test_default_datagram_priority(picoquic_quic_t* quic) {
    return quic == NULL ? -1 : (int)quic->default_datagram_priority;
}

static inline const char* aiopquic_test_cnx_cc_options(picoquic_cnx_t* cnx) {
    return cnx == NULL ? NULL : cnx->congestion_alg_option_string;
}

static inline int aiopquic_test_cnx_packet_train_mode(picoquic_cnx_t* cnx) {
    return cnx == NULL ? -1 : (int)cnx->quic->packet_train_mode;
}

static int aiopquic_test_perflog_noop(picoquic_quic_t* quic, picoquic_cnx_t* cnx,
                                      int should_delete) {
    (void)quic; (void)cnx; (void)should_delete;
    return 0;
}

/* Takes quic->perflog_fn as another perflog would. */
static inline void aiopquic_test_take_perflog(picoquic_quic_t* quic) {
    if (quic != NULL) {
        quic->perflog_fn = aiopquic_test_perflog_noop;
        quic->v_perflog_ctx = NULL;
    }
}

/* Marks the worker running so producers skip its wake. Returns 1 if it was
 * idle: it is then blocked until its own timer and drains nothing. */
static inline int aiopquic_test_hold_tx_wake(aiopquic_ctx_t* ctx) {
    return __atomic_exchange_n(&ctx->tx_worker_idle, 0, __ATOMIC_ACQ_REL) != 0;
}

#endif /* AIOPQUIC_TEST_ACCESS_H */
