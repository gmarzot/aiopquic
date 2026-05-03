/*
 * stream_ctx.h — per-stream wrapper holding both TX and RX byte rings.
 *
 * picoquic gives applications a single stream_ctx slot per stream
 * (settable via picoquic_set_app_stream_ctx() or as v_stream_ctx in
 * picoquic_mark_active_stream()). For bidirectional streams we need both
 * TX and RX rings, so the slot points at this wrapper rather than at a
 * raw aiopquic_stream_buf_t. Each direction's ring is allocated lazily —
 * unidirectional streams only ever populate one side.
 *
 * Lifecycle:
 *   - First contact (RX path's first stream_data callback OR TX path's
 *     send_stream_data): aiopquic_stream_ctx_get_or_create binds a fresh
 *     wrapper to the stream slot.
 *   - aiopquic_stream_ctx_ensure_tx / _ensure_rx allocate the side ring
 *     on demand (idempotent).
 *   - On stream_reset / stop_sending: mark pending_destroy; the TX ring
 *     can be freed immediately (sender abandoned), RX waits for Python
 *     to drain.
 *   - On stream_fin: same; mark pending_destroy and let drain complete.
 *   - aiopquic_stream_ctx_destroy frees both rings + wrapper.
 *
 * All ring access remains single-producer/single-consumer per direction:
 *   TX: Python pushes, picoquic worker pops in prepare_to_send.
 *   RX: picoquic worker pushes in stream_data callback, Python pops.
 * Memory ordering is handled inside aiopquic_stream_buf_t.
 */
#pragma once

#include "stream_buf.h"
#include <stdint.h>
#include <stdlib.h>

typedef struct {
    aiopquic_stream_buf_t* tx;
    aiopquic_stream_buf_t* rx;
    /* Cumulative bytes Python has drained from the RX ring; used to
     * compute the next MAX_STREAM_DATA advertisement. Single writer
     * (Python via stream_buf_pop_to_bytes wrapper); single reader
     * (Python's flow-control advancer). No atomics needed. */
    uint64_t rx_consumed;
    uint64_t rx_max_data_advertised;
    /* fin/reset arrived; free wrapper after final drain. */
    uint8_t  pending_destroy;
} aiopquic_stream_ctx_t;

static inline aiopquic_stream_ctx_t* aiopquic_stream_ctx_create(void) {
    return (aiopquic_stream_ctx_t*)calloc(1, sizeof(aiopquic_stream_ctx_t));
}

static inline int aiopquic_stream_ctx_ensure_tx(aiopquic_stream_ctx_t* sc,
                                                 uint32_t capacity) {
    if (!sc) return -1;
    if (sc->tx) return 0;
    sc->tx = aiopquic_stream_buf_create(capacity);
    return sc->tx ? 0 : -1;
}

static inline int aiopquic_stream_ctx_ensure_rx(aiopquic_stream_ctx_t* sc,
                                                 uint32_t capacity) {
    if (!sc) return -1;
    if (sc->rx) return 0;
    sc->rx = aiopquic_stream_buf_create(capacity);
    return sc->rx ? 0 : -1;
}

static inline void aiopquic_stream_ctx_destroy(aiopquic_stream_ctx_t* sc) {
    if (!sc) return;
    if (sc->tx) aiopquic_stream_buf_destroy(sc->tx);
    if (sc->rx) aiopquic_stream_buf_destroy(sc->rx);
    free(sc);
}
