# cython: language_level=3
"""
_transport — Cython bridge between picoquic (C) and Python asyncio.

Manages the picoquic context lifecycle, SPSC ring buffers,
and the dedicated network thread.

Note: free-threaded Python (cp314t) is not yet supported. The TX-ring
producer side, TransportContext lifecycle fields, and the WebTransport
session/engine state currently rely on the GIL for serialization;
running under a no-GIL build risks ring corruption and use-after-free.
Re-enable freethreading_compatible once the per-context locking audit
covers TX entry points and the WT TX dispatch path.
"""

import os
import sys

from cpython.buffer cimport PyBuffer_FillInfo
from cpython.bytes cimport PyBytes_AsString, PyBytes_FromStringAndSize
from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t, int64_t, uintptr_t
from libc.stdlib cimport free, malloc
from libc.string cimport memcpy, memset

from aiopquic._binding.spsc_ring cimport (
    spsc_ring_t, spsc_ring_create, spsc_ring_destroy,
    spsc_ring_count, spsc_ring_bytes_pending,
    spsc_ring_empty, spsc_ring_peek,
    spsc_ring_entry_data, spsc_ring_pop, spsc_ring_push,
    spsc_ring_take_data,
    spsc_entry_t,
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
    SPSC_EVT_STREAM_TX_DRAINED,
    SPSC_EVT_STREAM_DESTROY,
    SPSC_EVT_STREAM_RESET, SPSC_EVT_STOP_SENDING,
    SPSC_EVT_CLOSE, SPSC_EVT_APP_CLOSE,
    SPSC_EVT_READY, SPSC_EVT_ALMOST_READY,
    SPSC_EVT_DATAGRAM,
    SPSC_EVT_WT_STREAM_DATA, SPSC_EVT_WT_STREAM_FIN,
    SPSC_EVT_WT_STREAM_LINK_RELEASE,
    SPSC_EVT_TX_RING_DRAINED,
    SPSC_EVT_TX_DATAGRAM, SPSC_EVT_TX_CLOSE,
    SPSC_EVT_TX_MARK_ACTIVE, SPSC_EVT_TX_CONNECT,
    SPSC_EVT_TX_WT_OPEN, SPSC_EVT_TX_WT_CREATE_STREAM,
    SPSC_EVT_TX_WT_CLOSE, SPSC_EVT_TX_WT_DRAIN,
    SPSC_EVT_TX_WT_RESET_STREAM, SPSC_EVT_TX_WT_DEREGISTER,
    SPSC_EVT_TX_WT_STOP_SENDING,
    SPSC_EVT_TX_OPEN_FLOW_CONTROL,
    SPSC_EVT_TX_SET_APP_FLOW_CONTROL,
)

# Socket address helpers (needed by picoquic declarations)
cdef extern from "<sys/socket.h>":
    enum: AF_INET
    cdef struct sockaddr:
        unsigned short sa_family

cdef extern from "<netinet/in.h>":
    cdef struct sockaddr_in:
        unsigned short sin_family
        unsigned short sin_port
        unsigned int sin_addr
    unsigned short htons(unsigned short hostshort)

cdef extern from "<arpa/inet.h>":
    int inet_pton(int af, const char* src, void* dst)

# picoquic declarations
cdef extern from "picoquic.h":
    ctypedef struct picoquic_quic_t:
        pass
    ctypedef struct picoquic_cnx_t:
        pass

    ctypedef int (*picoquic_stream_data_cb_fn)(
        picoquic_cnx_t* cnx, uint64_t stream_id,
        uint8_t* bytes, size_t length,
        int fin_or_event, void* callback_ctx, void* stream_ctx)

    ctypedef void (*picoquic_connection_id_cb_fn)(
        picoquic_quic_t* quic, void* cnx_id_local,
        void* cnx_id_remote, void* cnx_id_cb_data,
        void* cnx_id_returned)

    picoquic_quic_t* picoquic_create(
        uint32_t max_nb_connections,
        const char* cert_file_name, const char* key_file_name,
        const char* cert_root_file_name, const char* default_alpn,
        picoquic_stream_data_cb_fn default_callback_fn,
        void* default_callback_ctx,
        picoquic_connection_id_cb_fn cnx_id_callback,
        void* cnx_id_callback_data,
        uint8_t* reset_seed, uint64_t current_time,
        uint64_t* p_simulated_time,
        const char* ticket_file_name,
        const uint8_t* ticket_encryption_key,
        size_t ticket_encryption_key_length)

    void picoquic_set_default_congestion_algorithm_by_name(
        picoquic_quic_t* quic, const char* alg_name)

    void picoquic_register_all_congestion_control_algorithms()

    void picoquic_free(picoquic_quic_t* quic)
    uint64_t picoquic_current_time()
    void picoquic_set_null_verifier(picoquic_quic_t* quic)
    void picoquic_set_default_idle_timeout(picoquic_quic_t* quic, uint64_t idle_timeout_ms)
    void picoquic_set_log_level(picoquic_quic_t* quic, int log_level)
    void picoquic_enable_sslkeylog(picoquic_quic_t* quic, int enable)
    void picoquic_set_key_log_file(picoquic_quic_t* quic,
                                    const char* keylog_filename)
    void picoquic_set_callback(picoquic_cnx_t* cnx,
        picoquic_stream_data_cb_fn callback_fn, void* callback_ctx)

# picoquic_logger.h transitively pulls picoquic_unified_log.h which
# uses internal-only types. Declare the prototype directly so Cython
# emits a forward declaration rather than including the header.
cdef extern from *:
    """
    int picoquic_set_textlog(picoquic_quic_t* quic, const char* textlog_file);
    int picoquic_set_qlog(picoquic_quic_t* quic, const char* qlog_dir);
    """
    int picoquic_set_textlog(picoquic_quic_t* quic, const char* textlog_file)
    int picoquic_set_qlog(picoquic_quic_t* quic, const char* qlog_dir)

    ctypedef struct picoquic_tp_t:
        uint64_t initial_max_stream_data_bidi_local
        uint64_t initial_max_stream_data_bidi_remote
        uint64_t initial_max_stream_data_uni
        uint64_t initial_max_data
        uint64_t initial_max_stream_id_bidir
        uint64_t initial_max_stream_id_unidir
        uint64_t max_idle_timeout
        uint32_t max_packet_size
        uint32_t max_ack_delay
        uint32_t active_connection_id_limit
        uint8_t ack_delay_exponent
        unsigned int migration_disabled
        uint32_t max_datagram_frame_size
        int enable_loss_bit
        int enable_time_stamp
        uint64_t min_ack_delay
        int do_grease_quic_bit
        int enable_bdp_frame
        int is_multipath_enabled
        uint64_t initial_max_path_id

    int picoquic_set_default_tp(picoquic_quic_t* quic, picoquic_tp_t* tp)
    const picoquic_tp_t* picoquic_get_default_tp(picoquic_quic_t* quic)

    ctypedef struct picoquic_connection_id_t:
        uint8_t id[20]
        uint8_t id_len

    picoquic_cnx_t* picoquic_create_client_cnx(
        picoquic_quic_t* quic, sockaddr* addr,
        uint64_t start_time, uint32_t preferred_version,
        const char* sni, const char* alpn,
        picoquic_stream_data_cb_fn callback_fn,
        void* callback_ctx)
    int picoquic_start_client_cnx(picoquic_cnx_t* cnx)
    int picoquic_close(picoquic_cnx_t* cnx, uint64_t reason)
    int picoquic_get_cnx_state(picoquic_cnx_t* cnx)
    uint64_t picoquic_get_data_sent(picoquic_cnx_t* cnx)
    uint64_t picoquic_get_data_received(picoquic_cnx_t* cnx)

cdef extern from "picoquic_packet_loop.h":
    ctypedef struct picoquic_packet_loop_param_t:
        unsigned short local_port
        int local_af
        int dest_if
        unsigned short public_port
        int is_port_shared
        int socket_buffer_size
        int do_not_use_gso
        int extra_socket_required
        int prefer_extra_socket
        int simulate_eio
        size_t send_length_max

    ctypedef int (*picoquic_packet_loop_cb_fn)(
        picoquic_quic_t* quic, int cb_mode,
        void* callback_ctx, void* callback_argv)

    ctypedef struct picoquic_network_thread_ctx_t:
        picoquic_quic_t* quic
        int thread_is_ready
        int thread_should_close
        int thread_is_closed
        int return_code

    picoquic_network_thread_ctx_t* picoquic_start_network_thread(
        picoquic_quic_t* quic,
        picoquic_packet_loop_param_t* param,
        picoquic_packet_loop_cb_fn loop_callback,
        void* loop_callback_ctx,
        int* ret)
    int picoquic_wake_up_network_thread(picoquic_network_thread_ctx_t* thread_ctx)
    void picoquic_delete_network_thread(picoquic_network_thread_ctx_t* thread_ctx)

# C callback declarations
cdef extern from "c/callback.h":
    # Resource defaults — single source of truth in callback.h.
    enum:
        AIOPQUIC_RX_STREAM_RING_CAP_DEFAULT
        AIOPQUIC_TX_STREAM_RING_CAP_DEFAULT
        AIOPQUIC_SPSC_RING_CAPACITY_DEFAULT
        AIOPQUIC_TX_RING_CAP_DEFAULT
        AIOPQUIC_RX_RING_CAP_DEFAULT
        AIOPQUIC_TX_RING_WAKE_PCT_DEFAULT

    ctypedef struct aiopquic_ctx_t:
        spsc_ring_t* rx_ring
        spsc_ring_t* tx_ring
        int eventfd
        picoquic_quic_t* quic
        picoquic_network_thread_ctx_t* thread_ctx
        uint32_t rx_ring_cap
        uint64_t worker_mark_active_processed
        uint64_t worker_prepare_to_send_calls
        uint64_t worker_prepare_to_send_pulled_bytes
        uint64_t worker_rx_event_drops
        uint64_t worker_rx_event_drops_stream_data
        uint64_t worker_rx_byte_ring_overflow
        uint32_t rx_notify_pending
        uint32_t tx_wake_pending
        uint32_t tx_ring_drain_pending
        uint32_t tx_ring_low_water
        # Observability counters (added without behavior change).
        uint64_t cnt_tx_ring_pushes
        uint64_t cnt_tx_ring_pops
        uint64_t cnt_tx_ring_arms
        uint64_t cnt_tx_ring_fires
        uint64_t cnt_tx_ring_fire_dropped
        uint64_t cnt_wake_calls
        uint64_t cnt_wake_skipped_coalesced
        uint64_t cnt_prepare_to_send_empty
        uint64_t last_tx_ring_arm_ns
        uint64_t last_tx_ring_fire_ns
        uint64_t cnt_fc_credit_pushed
        uint64_t cnt_fc_credit_handled
        uint64_t cnt_fc_credit_dropped

    aiopquic_ctx_t* aiopquic_ctx_create(uint32_t tx_cap,
                                          uint32_t rx_cap,
                                          uint32_t low_water_pct)
    void aiopquic_ctx_destroy(aiopquic_ctx_t* ctx)
    void aiopquic_clear_rx(aiopquic_ctx_t* ctx)
    void aiopquic_notify_rx(aiopquic_ctx_t* ctx)
    int aiopquic_tx_wake_set_pending(aiopquic_ctx_t* ctx)
    void aiopquic_arm_tx_ring_drain_pending(aiopquic_ctx_t* ctx)
    void aiopquic_clear_tx_ring_drain_pending(aiopquic_ctx_t* ctx)
    void aiopquic_push_fc_credit(aiopquic_ctx_t* ctx, void* cnx,
                                  uint64_t stream_id,
                                  void* sc)

    int aiopquic_stream_cb(picoquic_cnx_t* cnx, uint64_t stream_id,
                            uint8_t* bytes, size_t length,
                            int fin_or_event,
                            void* callback_ctx, void* stream_ctx)
    int aiopquic_loop_cb(picoquic_quic_t* quic, int cb_mode,
                          void* callback_ctx, void* callback_argv)


# Per-stream byte ring (PULL-model send path). Allocated by Python,
# owned by Python (refcount-style); picoquic-pthread reads from it via
# stream_ctx in aiopquic_stream_cb.
cdef extern from "c/stream_buf.h":
    ctypedef struct aiopquic_stream_buf_t:
        pass

    aiopquic_stream_buf_t* aiopquic_stream_buf_create(uint32_t capacity)
    void aiopquic_stream_buf_destroy(aiopquic_stream_buf_t* sb)
    uint32_t aiopquic_ceil_pow2_u32(uint32_t n)
    uint32_t aiopquic_stream_buf_push(
        aiopquic_stream_buf_t* sb,
        const uint8_t* data, uint32_t length)
    uint32_t aiopquic_stream_buf_pop(
        aiopquic_stream_buf_t* sb,
        uint8_t* out, uint32_t max_bytes)
    uint32_t aiopquic_stream_buf_used(aiopquic_stream_buf_t* sb)
    uint32_t aiopquic_stream_buf_free(aiopquic_stream_buf_t* sb)
    void aiopquic_stream_buf_set_fin(aiopquic_stream_buf_t* sb)
    int aiopquic_stream_buf_fin_pending(aiopquic_stream_buf_t* sb)
    uint64_t aiopquic_stream_buf_pushed(aiopquic_stream_buf_t* sb)
    uint64_t aiopquic_stream_buf_popped(aiopquic_stream_buf_t* sb)
    uint32_t aiopquic_stream_buf_push_hash(aiopquic_stream_buf_t* sb)
    uint32_t aiopquic_stream_buf_pop_hash(aiopquic_stream_buf_t* sb)


# Per-stream wrapper holding both TX and RX byte rings + flow-control
# state. Set as picoquic's app_stream_ctx so the same slot serves both
# directions for bidi streams.
cdef extern from "c/stream_ctx.h":
    uint64_t aiopquic_now_ns()
    ctypedef struct aiopquic_stream_ctx_t:
        aiopquic_stream_buf_t* tx
        aiopquic_stream_buf_t* rx
        # Atomic on the C side; Cython sees plain uint64_t and we touch
        # them via aiopquic_stream_ctx_rx_* accessors.
        uint64_t rx_consumed
        uint64_t rx_credit_limit
        # Bytes wrapped in StreamChunk for the Python consumer but
        # whose StreamChunk has not yet been released (last memoryview
        # ref dropped). Atomic on the C side; the worker reads via
        # pending_load() inside the SPSC_EVT_TX_OPEN_FLOW_CONTROL
        # handler to compute the effective MAX_STREAM_DATA grant.
        uint64_t bytes_pending_release
        uint32_t tx_drain_pending
        uint8_t  pending_destroy
        # Observability counters (single-writer; relaxed semantics).
        uint64_t cnt_drain_arms
        uint64_t cnt_drain_fires
        uint64_t cnt_drain_dropped
        uint64_t last_drain_arm_ns
        uint64_t last_drain_fire_ns

    aiopquic_stream_ctx_t* aiopquic_stream_ctx_create()
    int aiopquic_stream_ctx_ensure_tx(aiopquic_stream_ctx_t* sc,
                                      uint32_t capacity)
    int aiopquic_stream_ctx_ensure_rx(aiopquic_stream_ctx_t* sc,
                                      uint32_t capacity)
    void aiopquic_stream_ctx_ref(aiopquic_stream_ctx_t* sc)
    void aiopquic_stream_ctx_destroy(aiopquic_stream_ctx_t* sc)
    uint64_t aiopquic_stream_ctx_rx_consumed_load(aiopquic_stream_ctx_t* sc)
    void aiopquic_stream_ctx_rx_consumed_add(
        aiopquic_stream_ctx_t* sc, uint64_t delta)
    void aiopquic_stream_ctx_pending_add(
        aiopquic_stream_ctx_t* sc, uint64_t n)
    void aiopquic_stream_ctx_pending_sub(
        aiopquic_stream_ctx_t* sc, uint64_t n)
    uint64_t aiopquic_stream_ctx_pending_load(aiopquic_stream_ctx_t* sc)
    uint64_t aiopquic_stream_ctx_last_fc_push_consumed_load(
        aiopquic_stream_ctx_t* sc)
    void aiopquic_stream_ctx_last_fc_push_consumed_store(
        aiopquic_stream_ctx_t* sc, uint64_t v)
    int aiopquic_stream_ctx_send_data(
        aiopquic_stream_ctx_t* sc,
        const uint8_t* data, uint32_t length,
        uint32_t capacity, uint8_t set_fin)
    void aiopquic_arm_stream_tx_drain_pending(
        aiopquic_stream_ctx_t* sc)
    void aiopquic_clear_stream_tx_drain_pending(
        aiopquic_stream_ctx_t* sc)
    void aiopquic_chunks_alive_inc()
    void aiopquic_chunks_alive_dec()
    uint64_t aiopquic_cnt_sc_created_load()
    uint64_t aiopquic_cnt_sc_destroyed_load()
    int64_t  aiopquic_cnt_chunks_alive_load()
    void aiopquic_sc_rx_bytes_pushed_add(uint64_t n)
    void aiopquic_sc_rx_bytes_popped_add(uint64_t n)
    uint64_t aiopquic_cnt_sc_rx_bytes_pushed_load()
    uint64_t aiopquic_cnt_sc_rx_bytes_popped_load()


# picoquic flow-control APIs are thread-bound to the picoquic worker
# (PICOQUIC_THREAD_CHECK in sender.c). The asyncio thread cannot call
# them directly. Instead it queues SPSC events
# (SPSC_EVT_TX_OPEN_FLOW_CONTROL, SPSC_EVT_TX_SET_APP_FLOW_CONTROL)
# which the picoquic worker drains and dispatches to the actual
# picoquic_open_flow_control / picoquic_set_app_flow_control calls.
# See aiopquic_loop_cb's TX-ring-drain switch in callback.h.


# H3+WebTransport: opaque types from picoquic; we hold pointers only.
cdef extern from "h3zero_common.h":
    ctypedef struct h3zero_callback_ctx_t:
        pass
    ctypedef struct h3zero_stream_ctx_t:
        uint64_t stream_id
    ctypedef int (*picohttp_post_data_cb_fn)(
        picoquic_cnx_t* cnx, uint8_t* bytes, size_t length,
        int fin_or_event,
        h3zero_stream_ctx_t* stream_ctx, void* path_app_ctx)

    ctypedef struct picohttp_server_path_item_t:
        const char* path
        size_t path_length
        picohttp_post_data_cb_fn path_callback
        void* path_app_ctx

    ctypedef struct picohttp_server_parameters_t:
        const char* web_folder
        picohttp_server_path_item_t* path_table
        size_t path_table_nb

    int h3zero_callback(picoquic_cnx_t* cnx, uint64_t stream_id,
                         uint8_t* bytes, size_t length,
                         int fin_or_event, void* callback_ctx,
                         void* stream_ctx)


cdef extern from "pico_webtransport.h":
    int picowt_prepare_client_cnx(
        picoquic_quic_t* quic, sockaddr* server_address,
        picoquic_cnx_t** p_cnx, h3zero_callback_ctx_t** p_h3_ctx,
        h3zero_stream_ctx_t** p_stream_ctx,
        uint64_t current_time, const char* sni)

    int picowt_connect(
        picoquic_cnx_t* cnx, h3zero_callback_ctx_t* h3_ctx,
        h3zero_stream_ctx_t* stream_ctx,
        const char* authority, const char* path,
        picohttp_post_data_cb_fn wt_callback, void* wt_ctx,
        const char* wt_available_protocols)

    int picowt_send_close_session_message(
        picoquic_cnx_t* cnx, h3zero_stream_ctx_t* control_stream_ctx,
        uint32_t err, const char* err_msg)

    int picowt_send_drain_session_message(
        picoquic_cnx_t* cnx, h3zero_stream_ctx_t* control_stream_ctx)

    h3zero_stream_ctx_t* picowt_create_local_stream(
        picoquic_cnx_t* cnx, int is_bidir,
        h3zero_callback_ctx_t* h3_ctx, uint64_t control_stream_id)

    int picowt_reset_stream(picoquic_cnx_t* cnx,
                              h3zero_stream_ctx_t* stream_ctx,
                              uint64_t local_stream_error)

    void picowt_deregister(picoquic_cnx_t* cnx,
                            h3zero_callback_ctx_t* h3_ctx,
                            h3zero_stream_ctx_t* control_stream_ctx)

    void picowt_set_transport_parameters(picoquic_cnx_t* cnx)
    void picowt_set_default_transport_parameters(picoquic_quic_t* quic)


cdef extern from "c/h3wt_callback.h":
    ctypedef struct aiopquic_wt_session_t:
        aiopquic_ctx_t* bridge
        picoquic_cnx_t* cnx
        h3zero_callback_ctx_t* h3_ctx
        h3zero_stream_ctx_t* control_stream
        uint64_t control_stream_id
        int session_ready
        int session_closing

    aiopquic_wt_session_t* aiopquic_wt_session_create(aiopquic_ctx_t* bridge)
    void aiopquic_wt_session_destroy(aiopquic_wt_session_t* s)
    int aiopquic_wt_path_callback(
        picoquic_cnx_t* cnx, uint8_t* bytes, size_t length,
        int event,
        h3zero_stream_ctx_t* stream_ctx, void* path_app_ctx)
    int aiopquic_wt_server_path_callback(
        picoquic_cnx_t* cnx, uint8_t* bytes, size_t length,
        int event,
        h3zero_stream_ctx_t* stream_ctx, void* path_app_ctx)
    # Per-WT-stream link, owned by h3zero's stream_ctx->path_callback_ctx.
    # We only ever destroy these from drain_rx on a LINK_RELEASE event;
    # the worker thread allocates them in h3wt_callback.h.
    ctypedef struct aiopquic_wt_stream_link_t:
        aiopquic_stream_ctx_t* sc
        uint64_t stream_id

    void aiopquic_wt_stream_link_destroy(aiopquic_wt_stream_link_t* link)


# Default ring sizing
DEF DEFAULT_RING_CAPACITY = 262144


cdef class StreamChunk:
    """
    Owns a malloc'd byte buffer and exposes it via the Python buffer
    protocol. Built by drain_rx; the underlying buffer is the same
    memory written by the picoquic callback's mandatory copy-out.

    Ownership transfers from spsc_ring entry → StreamChunk via
    spsc_ring_take_data(). The buffer is freed in __dealloc__ when
    the last reference (memoryview or otherwise) is dropped.

    Per-stream flow-control feedback (chunk-lifetime model):
      - On _wrap, chunk length is added to sc->bytes_pending_release
        AND a ref is taken on sc (so sc outlives the stream FIN if
        chunks are still alive in the consumer's pipeline).
      - On __dealloc__ (last memoryview ref dropped, chunk truly done),
        length is subtracted, SPSC_EVT_TX_OPEN_FLOW_CONTROL is pushed
        so the worker re-evaluates the effective grant, and the sc
        ref is released. The worker's effective MAX_STREAM_DATA grant
        is buf_free - bytes_pending_release, so chunks alive in the
        pipeline (or held by an app archiving the memoryview) reduce
        peer's window — that IS the backpressure mechanism.
      - __getbuffer__ intentionally does NOT release FC: memoryview
        construction fires __getbuffer__ immediately, which would
        decrement before the consumer has actually finished, defeating
        the backpressure entirely (peer floods, chunks accumulate,
        original bloat returns).
      - Apps that want to archive memoryviews without slowing peer
        should COPY (bytes(memoryview)) to release the chunk
        immediately and decouple their archive from FC.

    Back-pointers (_sc, _cnx, _stream_id, _ctx) are required for the
    release-side accounting. They are BORROWED pointers; the chunk
    does NOT hold strong refs to picoquic state. NULL _sc skips
    accounting (used by the RingBuffer.pop() test path where the
    chunk is not tied to a real stream).

    Internal type. Public surface is memoryview(chunk).
    """
    cdef void* _buf
    cdef Py_ssize_t _len
    cdef aiopquic_ctx_t* _ctx
    cdef aiopquic_stream_ctx_t* _sc
    cdef void* _cnx
    cdef uint64_t _stream_id
    cdef bint _delivered

    def __cinit__(self):
        self._buf = NULL
        self._len = 0
        self._ctx = NULL
        self._sc = NULL
        self._cnx = NULL
        self._stream_id = 0
        self._delivered = False

    @staticmethod
    cdef StreamChunk _wrap(void* buf, Py_ssize_t length,
                            aiopquic_ctx_t* ctx,
                            aiopquic_stream_ctx_t* sc,
                            void* cnx,
                            uint64_t stream_id):
        cdef StreamChunk c = StreamChunk.__new__(StreamChunk)
        c._buf = buf
        c._len = length
        c._ctx = ctx
        c._sc = sc
        c._cnx = cnx
        c._stream_id = stream_id
        aiopquic_chunks_alive_inc()
        if sc is not NULL:
            # Take a ref on sc so it stays alive until this chunk
            # is dealloc'd, even if the stream FINs (SPSC_EVT_STREAM_DESTROY
            # drops the stream-lifetime ref) before the consumer
            # releases the memoryview. Released in __dealloc__ via
            # the matching aiopquic_stream_ctx_destroy (unref).
            aiopquic_stream_ctx_ref(sc)
            if length > 0:
                aiopquic_stream_ctx_pending_add(sc, <uint64_t>length)
        return c

    cdef inline void _release_fc(self):
        """Idempotent pending_release accounting. Fired by __getbuffer__
        (consumer accessed the bytes — committed to the consumer's
        pipeline) and as a fallback by __dealloc__ when the chunk was
        never accessed. _delivered flag makes both paths converge to a
        single pending_sub regardless of how many memoryviews are
        created or in what order dealloc fires.

        Does NOT push fc_credit on the tx_event_ring. Per-chunk push
        was the May-25 storm (~280K events/sec at 2 Gbps × 64KB) that
        saturated the asyncio thread and triggered rx_event_ring
        overflow → cliff. The credit push happens in drain_rx after
        popping bytes from sc->rx, with hysteresis (see
        _push_fc_credit). The worker's grant computation reads
        bytes_pending_release at push time, so this pending_sub
        update lands in the next push's grant automatically."""
        if (not self._delivered and self._sc is not NULL
                and self._len > 0):
            self._delivered = True
            aiopquic_stream_ctx_pending_sub(
                self._sc, <uint64_t>self._len)

    def __dealloc__(self):
        if self._buf is not NULL:
            # Fallback release: chunk dropped without consumer read.
            # No-op when _delivered=True (FC already released at
            # first __getbuffer__). Order matters: push fc_credit
            # BEFORE dropping the chunk's sc ref so the pending
            # SPSC_EVT_TX_OPEN_FLOW_CONTROL event has a valid sc
            # to dereference when the worker handler runs (the
            # worker handler is responsible for its own sc unref).
            self._release_fc()
            free(self._buf)
            self._buf = NULL
        # Drop the chunk's ref on sc. If this is the LAST ref
        # (stream already FIN'd AND all pending fc events handled),
        # sc is freed here. Otherwise the worker handler or another
        # outstanding chunk holds it alive.
        if self._sc is not NULL:
            aiopquic_stream_ctx_destroy(self._sc)
            self._sc = NULL
        aiopquic_chunks_alive_dec()

    def __len__(self):
        return self._len

    def __getbuffer__(self, Py_buffer* buffer, int flags):
        # Release FC on first consumer access. Semantic: pending_release
        # bounds bytes peer sent that we have NOT yet handed to the
        # consumer. Once a memoryview exists, the bytes are committed
        # to the consumer's pipeline — bounding memory beyond this
        # point is the consumer's responsibility (parser queue caps,
        # track-level limits, etc.), not QUIC FC.
        #
        # Releasing here breaks the parser-hoarding deadlock that
        # __dealloc__-only release caused: parser holds chunks while
        # accumulating bytes for an object boundary, pending stays at
        # full ring capacity, grant = buf_free - pending = 0, peer
        # FC-stalls, the chunk that would unblock things never arrives.
        # _release_fc is idempotent via _delivered, so multiple
        # getbuffer calls (e.g. slicing) and the dealloc fallback all
        # converge to a single release.
        self._release_fc()
        PyBuffer_FillInfo(buffer, self, self._buf, self._len, 1, flags)

    def __releasebuffer__(self, Py_buffer* buffer):
        pass


cdef class RingBuffer:
    """Python wrapper around the SPSC ring buffer for testing/inspection."""
    cdef spsc_ring_t* _ring
    cdef bint _owned

    def __cinit__(self, uint32_t capacity=DEFAULT_RING_CAPACITY):
        self._ring = spsc_ring_create(capacity)
        self._owned = True
        if self._ring is NULL:
            raise MemoryError("Failed to create SPSC ring buffer")

    def __dealloc__(self):
        if self._owned and self._ring is not NULL:
            spsc_ring_destroy(self._ring)
            self._ring = NULL

    @property
    def capacity(self):
        return self._ring.capacity

    @property
    def count(self):
        return spsc_ring_count(self._ring)

    @property
    def empty(self):
        return spsc_ring_empty(self._ring) != 0

    def push(self, uint32_t event_type, uint64_t stream_id,
             bytes data=None, uint8_t is_fin=0, uint64_t error_code=0):
        """Push an entry into the ring (producer side)."""
        cdef spsc_entry_t entry
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0

        entry.event_type = event_type
        entry.stream_id = stream_id
        entry.is_fin = is_fin
        entry.cnx = NULL
        entry.stream_ctx = NULL
        entry.error_code = error_code

        if data is not None:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        cdef int ret = spsc_ring_push(self._ring, &entry, data_ptr, data_len)
        if ret != 0:
            raise BufferError("Ring buffer is full")

    def pop(self):
        """
        Pop the next entry (consumer side).
        Returns (event_type, stream_id, data, is_fin, error_code) or None.
        `data` is a memoryview backed by an internal StreamChunk, or None.
        """
        cdef spsc_entry_t* entry = spsc_ring_peek(self._ring)
        if entry is NULL:
            return None

        data = None
        cdef void* buf
        cdef Py_ssize_t length
        if entry.data_length > 0 and entry.data_buf is not NULL:
            length = entry.data_length
            buf = spsc_ring_take_data(entry)
            data = memoryview(StreamChunk._wrap(
                buf, length, NULL, NULL, NULL, 0))

        result = (entry.event_type, entry.stream_id, data,
                  entry.is_fin, entry.error_code)
        spsc_ring_pop(self._ring)
        return result


# Module-level registry of live TransportContext instances. Walked by
# dump_all_counters() (driven from aiomoqt's SIGUSR2 taskdump handler)
# to print forensic counters from every active context. WeakSet so
# contexts aren't kept alive by the registry.
import weakref as _weakref
_TRANSPORT_REGISTRY = _weakref.WeakSet()


def dump_all_counters(file=None):
    """Print counters from every live TransportContext to `file`
    (default stderr). Used by the SIGUSR2 handler in
    aiomoqt.utils.taskdump for hang-localization.

    For each TransportContext, also enumerates per-stream counters
    by walking gc-visible objects to find QuicConnection /
    WebTransportSession instances bound to that transport — those
    hold the `_stream_ctxs` / `_stream_tx_ctxs` maps that record
    each stream's `aiopquic_stream_ctx_t*` pointer. Per-stream
    counters are the load-bearing signal for diagnosing
    per-stream sc->tx_drain_pending wake stalls: an outstanding
    arm without matching fire on a specific stream is exactly the
    pattern that hangs a producer in stream_write_drain.
    """
    import os, sys, gc
    if file is None:
        file = sys.stderr
    # Per-stream walk dereferences sc_ptr from the QuicConnection /
    # WebTransportSession _stream_ctxs / _stream_tx_ctxs dicts. Those
    # dicts are not cleaned up when sc is destroyed by drain_rx's
    # internal STREAM_DESTROY handler — stale pointers accumulate
    # and dereferencing them in dump_counters causes UAF / SEGV.
    # Default OFF; opt in with AIOPQUIC_DUMP_PER_STREAM=1 for cases
    # where you know the dicts are clean (small synthetic tests).
    # Proper fix: surface STREAM_DESTROY to QuicConnection so it can
    # del the dict entry. Tracked separately.
    per_stream_enabled = (os.environ.get("AIOPQUIC_DUMP_PER_STREAM")
                          == "1")
    n = 0
    for ctx in list(_TRANSPORT_REGISTRY):
        sc_map = {}
        if per_stream_enabled:
            for obj in gc.get_objects():
                try:
                    if getattr(obj, '_transport', None) is ctx:
                        inner = getattr(obj, '_stream_ctxs', None)
                        if isinstance(inner, dict):
                            sc_map.update(inner)
                        inner = getattr(obj, '_stream_tx_ctxs', None)
                        if isinstance(inner, dict):
                            sc_map.update(inner)
                except Exception:
                    continue
        ctx.dump_counters(file=file, label=f"ctx#{n}",
                           stream_ctxs=sc_map if per_stream_enabled else None)
        n += 1
    if n == 0:
        print("=== no live TransportContext instances ===",
              file=file, flush=True)


cdef class TransportContext:
    """
    Manages the picoquic context and network thread.

    This is the core bridge between the C picoquic library and Python.
    It owns the SPSC rings and the eventfd used for async notification.
    """
    # Make cdef class weakref-able so the SIGUSR2 counter-dump registry
    # (_TRANSPORT_REGISTRY at module level above) can hold WeakRef
    # references without keeping ctx objects alive past their natural
    # lifetime.
    cdef object __weakref__
    cdef aiopquic_ctx_t* _ctx
    cdef picoquic_quic_t* _quic
    cdef picoquic_network_thread_ctx_t* _thread_ctx
    cdef picoquic_packet_loop_param_t _param
    cdef bint _started
    # WT server-mode storage; must persist for picoquic's lifetime.
    cdef bytes _wt_path_bytes
    cdef picohttp_server_path_item_t _wt_path_item
    cdef picohttp_server_parameters_t _wt_params
    # Forensic counters for the atomic send_stream_data path.
    # All updated from the asyncio thread (single producer); reads
    # from Python don't need atomicity beyond the GIL.
    cdef uint64_t _send_calls
    cdef uint64_t _send_busy_event_ring
    cdef uint64_t _send_busy_stream_ring
    cdef uint64_t _send_alloc_fail
    # Connection-global TX-ring drain wakeup. Lazy-allocated on first
    # access (needs a running event loop); see tx_ring_drain_event.
    cdef object _tx_ring_drain_event
    # Test-friendly low-level pull primitive: per-(cnx, sid) sc_ptr
    # tracker for tx_send_stream(). Production code uses
    # QuicConnection.send_stream_data which manages its own per-cnx
    # _stream_ctxs dict; this dict is for the bare-TransportContext
    # test path where there's no QuicConnection wrapper.
    cdef object _test_stream_ctxs

    def __cinit__(self,
                  uint32_t ring_capacity=0,
                  uint32_t tx_ring_cap=0,
                  uint32_t rx_ring_cap=0,
                  uint32_t tx_ring_low_water_pct=0):
        # Resolution order for each SPSC cap:
        #   1. Explicit per-direction kwarg (tx_ring_cap / rx_ring_cap)
        #   2. Legacy shared ring_capacity (positional or kwarg)
        #   3. 0 → C-side picks AIOPQUIC_{TX,RX}_RING_CAP_DEFAULT
        # ring_capacity preserves the pre-0.3.5 positional signature
        # (TransportContext(262144) still works).
        cdef uint32_t tx_cap = tx_ring_cap if tx_ring_cap > 0 else ring_capacity
        cdef uint32_t rx_cap = rx_ring_cap if rx_ring_cap > 0 else ring_capacity
        if tx_cap > 0:
            tx_cap = aiopquic_ceil_pow2_u32(tx_cap)
        if rx_cap > 0:
            rx_cap = aiopquic_ceil_pow2_u32(rx_cap)
        self._ctx = aiopquic_ctx_create(tx_cap, rx_cap, tx_ring_low_water_pct)
        self._quic = NULL
        self._thread_ctx = NULL
        self._started = False
        self._send_calls = 0
        self._send_busy_event_ring = 0
        self._send_busy_stream_ring = 0
        self._send_alloc_fail = 0
        self._tx_ring_drain_event = None
        self._test_stream_ctxs = {}
        if self._ctx is NULL:
            raise MemoryError("Failed to create transport context")
        # Register for module-level SIGUSR2 counter dump.
        _TRANSPORT_REGISTRY.add(self)

    def __dealloc__(self):
        self._shutdown()
        if self._ctx is not NULL:
            aiopquic_ctx_destroy(self._ctx)
            self._ctx = NULL
        # WeakSet auto-removes; no explicit discard needed.

    cdef void _shutdown(self):
        """Stop the network thread and free picoquic context."""
        if self._thread_ctx is not NULL:
            # Clear the C-side mirror BEFORE freeing so any in-flight
            # aiopquic_push_fc_credit calls see NULL and skip wake.
            if self._ctx is not NULL:
                self._ctx.thread_ctx = NULL
            picoquic_delete_network_thread(self._thread_ctx)
            self._thread_ctx = NULL
        if self._quic is not NULL:
            picoquic_free(self._quic)
            self._quic = NULL
        self._started = False

    @property
    def eventfd(self):
        """File descriptor for asyncio add_reader() registration."""
        return self._ctx.eventfd

    @property
    def rx_count(self):
        """Number of events pending in the RX ring."""
        return spsc_ring_count(self._ctx.rx_ring)

    @property
    def tx_count(self):
        """Number of events pending in the TX ring."""
        return spsc_ring_count(self._ctx.tx_ring)

    @property
    def tx_bytes_pending(self):
        """Aggregate bytes across all in-flight TX-ring entries (the
        sum of `data_length` over events currently queued). For latency-
        targeted backpressure: callers can cap publish rate against a
        bytes budget independent of ring entry capacity or object size.

        Pull-model note: TX-ring entries are mostly MARK_ACTIVE with
        `data_length=0`; the actual bytes live in per-stream sc->tx
        rings. So this aggregate is ~0 for pull-model streams. Use
        `stream_tx_buf_used(sc_ptr)` for per-stream sc->tx accounting."""
        return spsc_ring_bytes_pending(self._ctx.tx_ring)

    @property
    def counters(self):
        """Snapshot of forensic counters from the C-side context.

        Counter names mirror callback.h fields. Use for hang-diagnosis
        via SIGUSR2 dump (aiomoqt taskdump wires this up). The dict
        also includes live ring counts for quick comparison against
        cumulative push/pop totals.

        Key counters and what mismatches reveal:
          tx_ring_pushes vs tx_ring_pops: drain lag
          tx_ring_arms vs tx_ring_fires: missed ring-drain wakes
          tx_ring_fire_dropped > 0: rx_ring full at fire time (re-arm path)
          wake_calls vs wake_skipped_coalesced: wake-coalescing efficiency
          prepare_to_send_calls vs prepare_to_send_pulled_bytes: worker
            actually drained sc->tx vs ran with nothing to send
          last_tx_ring_arm_ns > last_tx_ring_fire_ns: an arm without
            matching fire is currently pending (still in flight or lost)
        """
        if self._ctx is NULL:
            return {}
        return {
            'tx_ring_pushes': self._ctx.cnt_tx_ring_pushes,
            'tx_ring_pops': self._ctx.cnt_tx_ring_pops,
            'tx_ring_arms': self._ctx.cnt_tx_ring_arms,
            'tx_ring_fires': self._ctx.cnt_tx_ring_fires,
            'tx_ring_fire_dropped': self._ctx.cnt_tx_ring_fire_dropped,
            'wake_calls': self._ctx.cnt_wake_calls,
            'wake_skipped_coalesced': self._ctx.cnt_wake_skipped_coalesced,
            'prepare_to_send_calls': self._ctx.worker_prepare_to_send_calls,
            'prepare_to_send_pulled_bytes': self._ctx.worker_prepare_to_send_pulled_bytes,
            'prepare_to_send_empty': self._ctx.cnt_prepare_to_send_empty,
            'mark_active_processed': self._ctx.worker_mark_active_processed,
            'rx_event_drops': self._ctx.worker_rx_event_drops,
            'rx_event_drops_stream_data': self._ctx.worker_rx_event_drops_stream_data,
            'rx_byte_ring_overflow': self._ctx.worker_rx_byte_ring_overflow,
            'last_tx_ring_arm_ns': self._ctx.last_tx_ring_arm_ns,
            'last_tx_ring_fire_ns': self._ctx.last_tx_ring_fire_ns,
            'tx_ring_count_now': spsc_ring_count(self._ctx.tx_ring),
            'rx_ring_count_now': spsc_ring_count(self._ctx.rx_ring),
            'tx_ring_drain_pending_now': self._ctx.tx_ring_drain_pending,
            'tx_wake_pending_now': self._ctx.tx_wake_pending,
            # RX flow-control observability (added 2026-05-25)
            'fc_credit_pushed': self._ctx.cnt_fc_credit_pushed,
            'fc_credit_handled': self._ctx.cnt_fc_credit_handled,
            'fc_credit_dropped': self._ctx.cnt_fc_credit_dropped,
            # Process-wide stream / chunk lifecycle (leak detection)
            'sc_created_total': aiopquic_cnt_sc_created_load(),
            'sc_destroyed_total': aiopquic_cnt_sc_destroyed_load(),
            'sc_alive_total': (aiopquic_cnt_sc_created_load()
                               - aiopquic_cnt_sc_destroyed_load()),
            'chunks_alive_total': aiopquic_cnt_chunks_alive_load(),
            'sc_rx_bytes_pushed_total':
                aiopquic_cnt_sc_rx_bytes_pushed_load(),
            'sc_rx_bytes_popped_total':
                aiopquic_cnt_sc_rx_bytes_popped_load(),
            'sc_rx_bytes_in_flight': (
                aiopquic_cnt_sc_rx_bytes_pushed_load()
                - aiopquic_cnt_sc_rx_bytes_popped_load()),
        }


    def stream_counters(self, uintptr_t sc_ptr):
        """Snapshot of per-stream sc->tx drain counters for the
        stream_ctx at sc_ptr. Returns {} for NULL pointer."""
        if sc_ptr == 0:
            return {}
        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*><void*>sc_ptr
        return {
            'drain_arms': sc.cnt_drain_arms,
            'drain_fires': sc.cnt_drain_fires,
            'drain_dropped': sc.cnt_drain_dropped,
            'last_drain_arm_ns': sc.last_drain_arm_ns,
            'last_drain_fire_ns': sc.last_drain_fire_ns,
            'tx_used_now': aiopquic_stream_buf_used(sc.tx) if sc.tx is not NULL else 0,
            'tx_drain_pending_now': sc.tx_drain_pending,
        }

    def dump_counters(self, file=None, label=None, stream_ctxs=None):
        """Print counters to stderr (or given file). Used by SIGUSR2.

        Includes the connection-global counters, plus per-stream
        counters when `stream_ctxs` (a dict mapping stream_id → sc_ptr)
        is supplied. dump_all_counters() harvests this from Python
        QuicConnection / WebTransportSession instances and passes it
        in automatically; manual callers can pass an explicit dict.
        """
        import sys, time
        if file is None:
            file = sys.stderr
        prefix = f"[{label}] " if label else ""
        now_ns = aiopquic_now_ns()
        print(f"=== {prefix}aiopquic counters (now_ns={now_ns}) ===",
              file=file, flush=True)
        cdef dict c = self.counters
        for k in sorted(c.keys()):
            print(f"  {k:32s} = {c[k]}", file=file, flush=True)
        # Derived: arm/fire delta
        arms = c.get('tx_ring_arms', 0)
        fires = c.get('tx_ring_fires', 0)
        if arms != fires:
            print(f"  ** tx_ring arm/fire delta: arms={arms} fires={fires} "
                  f"diff={arms - fires} **", file=file, flush=True)
        arm_ns = c.get('last_tx_ring_arm_ns', 0)
        fire_ns = c.get('last_tx_ring_fire_ns', 0)
        if arm_ns > fire_ns and arm_ns > 0:
            print(f"  ** outstanding tx_ring arm: armed {(now_ns - arm_ns) / 1e6:.1f}ms ago, "
                  f"no fire since **", file=file, flush=True)

        # Per-stream counters, if a {stream_id: sc_ptr} map was passed.
        # Each row: stream_id, drain arms/fires/dropped, current
        # sc->tx bytes pending, pending flag, and a "STUCK" warning
        # when the most-recent arm has no matching fire.
        if stream_ctxs:
            stuck = []
            print(f"  --- per-stream ({len(stream_ctxs)} streams) ---",
                  file=file, flush=True)
            for sid in sorted(stream_ctxs.keys()):
                sc_ptr = stream_ctxs[sid]
                if not sc_ptr:
                    continue
                sc = self.stream_counters(sc_ptr)
                if not sc:
                    continue
                arms = sc['drain_arms']
                fires = sc['drain_fires']
                dropped = sc['drain_dropped']
                arm_ns = sc['last_drain_arm_ns']
                fire_ns = sc['last_drain_fire_ns']
                tx_used = sc['tx_used_now']
                pending = sc['tx_drain_pending_now']
                # Status flag — STUCK if there's an outstanding arm
                # OR pending=1 with sc->tx still holding bytes.
                status = ""
                if arm_ns > fire_ns and arm_ns > 0:
                    delta_ms = (now_ns - arm_ns) / 1e6
                    status = f"  ** STUCK arm {delta_ms:.1f}ms ago **"
                    stuck.append((sid, delta_ms, tx_used))
                print(f"  sid={sid:<5} sc=0x{sc_ptr:x} "
                      f"arms={arms} fires={fires} dropped={dropped} "
                      f"tx_used={tx_used} pending={pending}{status}",
                      file=file, flush=True)
            if stuck:
                print(f"  ** {len(stuck)} stream(s) with outstanding arm — "
                      f"oldest {max(d for _, d, _ in stuck):.1f}ms **",
                      file=file, flush=True)

        print(f"=== end counters ===", file=file, flush=True)

    def enable_sigusr2_dump(self, stream_ctxs_callable=None):
        """Install a SIGUSR2 handler that calls dump_counters().

        Must be called from the main thread (signal.signal limitation).
        If a handler is already installed for SIGUSR2, it is replaced.
        Returns the previous handler so the caller can chain if desired.

        `stream_ctxs_callable`, if provided, is invoked at signal time
        to gather a {stream_id: sc_ptr} dict (usually `lambda:
        my_conn._stream_ctxs.copy()`). Without it the dump shows only
        connection-global counters."""
        import signal
        def _handler(*_args):
            stream_ctxs = stream_ctxs_callable() if stream_ctxs_callable else None
            self.dump_counters(stream_ctxs=stream_ctxs)
        prev = signal.signal(signal.SIGUSR2, _handler)
        return prev

    def stream_tx_buf_used(self, uintptr_t sc_ptr):
        """Bytes currently sitting in a given stream's sc->tx ring,
        not yet consumed by the picoquic worker's prepare_to_send.

        For the pull model this is the load-bearing back-pressure
        signal — `tx_bytes_pending` is ~0 because MARK_ACTIVE entries
        carry no payload. Callers can compare this against an absolute
        byte budget (latency-bound queue depth) on a per-stream basis.

        sc_ptr is the opaque pointer kept in the Python connection's
        per-stream wrapper map (`_stream_ctxs[stream_id]`). Returns 0
        for NULL or a stream that has not yet allocated sc->tx.
        """
        if sc_ptr == 0:
            return 0
        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*><void*>sc_ptr
        if sc.tx is NULL:
            return 0
        return aiopquic_stream_buf_used(sc.tx)

    def arm_stream_tx_drain_pending(self, uintptr_t sc_ptr):
        """Arm the per-stream sc->tx drain signal so the next worker
        drain of sc->tx (in prepare_to_send) fires SPSC_EVT_STREAM_TX_DRAINED
        and wakes the producer waiting on the per-stream asyncio.Event.

        Use with the clear-arm-recheck-wait pattern on the per-stream
        event when waiting on a soft byte threshold below sc->tx full —
        e.g. a tx_max_inflight_bytes cap whose wakeup must align with
        sc->tx drain, NOT the connection-global SPSC ring drain. Pairs
        with `stream_tx_buf_used(sc_ptr)` for the byte measurement.
        """
        if sc_ptr == 0:
            return
        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*><void*>sc_ptr
        aiopquic_arm_stream_tx_drain_pending(sc)

    def clear_stream_tx_drain_pending(self, uintptr_t sc_ptr):
        """Clear the per-stream sc->tx drain signal. Called by the
        producer when a recheck shows pressure dropped between arm and
        wait, to avoid spurious wakes."""
        if sc_ptr == 0:
            return
        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*><void*>sc_ptr
        aiopquic_clear_stream_tx_drain_pending(sc)

    @property
    def tx_capacity(self):
        """TX ring entry capacity (constant; for free-space pre-checks)."""
        return self._ctx.tx_ring.capacity

    @property
    def tx_ring_drain_event(self):
        """Connection-global asyncio.Event signaled when the worker
        observes the TX SPSC ring fill drop to/below the low-water
        mark while a Python writer has armed tx_ring_drain_pending.

        Used by stream_write_drain / send_stream_data_drained to
        avoid the per-stream-event hard-wait deadlock at the
        connection-global pressure threshold (the per-stream
        STREAM_TX_DRAINED only fires when sc->tx is full, NOT when
        the SPSC ring is full — so awaiting it under a
        ring-pressure check could hang forever).

        Lazy-allocated on first access so __cinit__ doesn't require
        a running event loop. """
        import asyncio
        if self._tx_ring_drain_event is None:
            self._tx_ring_drain_event = asyncio.Event()
        return self._tx_ring_drain_event

    def arm_tx_ring_drain_pending(self):
        """Atomically set tx_ring_drain_pending = 1. The worker's
        post-pop check (aiopquic_maybe_fire_tx_ring_drained in
        callback.h) CAS-clears this and fires SPSC_EVT_TX_RING_DRAINED
        when ring count drops to/below tx_ring_low_water.

        Use pattern (caller side):
            event = tx.tx_ring_drain_event
            event.clear()
            tx.arm_tx_ring_drain_pending()
            if tx_pressure > wait_threshold:
                await event.wait()       # safe: worker will fire
            else:
                tx.clear_tx_ring_drain_pending()  # raced, clear arm
        """
        aiopquic_arm_tx_ring_drain_pending(self._ctx)

    def clear_tx_ring_drain_pending(self):
        """Clear the arm without waiting. Use when the producer
        re-checks pressure after arming and finds it has already
        dropped below the wait threshold (raced with worker drain)."""
        aiopquic_clear_tx_ring_drain_pending(self._ctx)

    def drain_rx(self, int max_events=256):
        """
        Drain events from the RX ring.

        Returns a list of
            (event_type, stream_id, data, is_fin, error_code,
             cnx_ptr, stream_ctx_ptr, sc_ptr).

        sc_ptr is the BORROWED per-stream aiopquic_stream_ctx_t pointer
        (as uintptr_t) for events that carry one — STREAM_DATA,
        STREAM_FIN, STREAM_CREATED, NEW_STREAM. 0 otherwise. WT callers
        stash this in their _stream_tx_ctxs[sid] dict to feed back into
        push_stream_data; raw-QUIC callers can ignore it (their sc is
        already on entry.stream_ctx).

        `data` is a memoryview over an internal StreamChunk, or None for
        events without payload. The chunk owns its buffer; the memoryview
        keeps it alive via PEP 3118 buffer protocol refcount. No copy
        after picoquic.

        SPSC_EVT_WT_STREAM_LINK_RELEASE is handled internally and never
        emitted: drain_rx calls aiopquic_wt_stream_link_destroy on the
        link pointer carried in data_buf. The SPSC ring's FIFO order
        guarantees every preceding STREAM_DATA/FIN/RESET event for this
        stream has been popped (and sc->rx drained) before the link
        is freed — that's the lifetime invariant for BORROWED-sc.

        Order matters: drain entries FIRST, then call aiopquic_clear_rx
        which atomically (a) clears the producer-skip flag, (b) drains
        the wake-fd counter, (c) re-arms via a fresh wake-fd write if
        the ring is still non-empty (race recovery for producers that
        observed pending=1 and skipped their own wake while we were
        partway through draining).
        """
        events = []
        cdef int i
        cdef spsc_entry_t* entry
        cdef void* buf
        cdef Py_ssize_t length
        cdef uintptr_t sc_ptr

        cdef aiopquic_stream_ctx_t* sc
        cdef aiopquic_stream_buf_t* rx_sb
        cdef uint32_t avail
        # Per-cycle FC dedupe: collect (sc_ptr → (cnx_ptr, stream_id))
        # while draining; emit at most one OPEN_FLOW_CONTROL push per
        # stream per cycle, after the loop. Combined with the
        # hysteresis check inside _push_fc_credit, this keeps
        # tx_event_ring traffic at ~max(streams_per_cycle, advance_rate
        # / hysteresis_bytes) instead of per-chunk.
        cdef dict pending_fc = {}
        for i in range(max_events):
            entry = spsc_ring_peek(self._ctx.rx_ring)
            if entry is NULL:
                break

            # LINK_RELEASE is internal: free the link, never emit.
            if entry.event_type == SPSC_EVT_WT_STREAM_LINK_RELEASE:
                if entry.data_buf is not NULL:
                    aiopquic_wt_stream_link_destroy(
                        <aiopquic_wt_stream_link_t*>entry.data_buf)
                spsc_ring_pop(self._ctx.rx_ring)
                continue

            # STREAM_DESTROY is internal: raw-QUIC pure-receiver sc
            # teardown. SPSC FIFO order guarantees all prior STREAM_DATA
            # / STREAM_FIN / STREAM_RESET for this stream have been
            # popped and sc->rx drained. Never emitted to user code.
            if entry.event_type == SPSC_EVT_STREAM_DESTROY:
                if entry.stream_ctx is not NULL:
                    aiopquic_stream_ctx_destroy(
                        <aiopquic_stream_ctx_t*>entry.stream_ctx)
                spsc_ring_pop(self._ctx.rx_ring)
                continue

            # TX_RING_DRAINED is internal: set the connection-global
            # asyncio.Event so any awaiting writer wakes. Worker fired
            # this because tx_ring_drain_pending was armed and the
            # ring count just dropped below tx_ring_low_water.
            if entry.event_type == SPSC_EVT_TX_RING_DRAINED:
                if self._tx_ring_drain_event is not None:
                    self._tx_ring_drain_event.set()
                spsc_ring_pop(self._ctx.rx_ring)
                continue

            data = None
            sc_ptr = 0
            if entry.data_length > 0 and entry.data_buf is not NULL:
                # Inline-payload path (datagrams, NEW_SESSION path bytes,
                # etc.) — not tied to a per-stream sc, no FC accounting.
                length = entry.data_length
                buf = spsc_ring_take_data(entry)
                data = memoryview(StreamChunk._wrap(
                    buf, length, NULL, NULL, NULL, 0))
            elif (entry.event_type == SPSC_EVT_STREAM_DATA or
                  entry.event_type == SPSC_EVT_STREAM_FIN) and \
                  entry.stream_ctx is not NULL:
                # Canonical raw-QUIC RX path: bytes live on the per-stream
                # ring. Pop into a StreamChunk wired with sc back-pointer
                # so __dealloc__ can re-open peer FC when the consumer
                # releases. Per-stream backpressure: peer's effective
                # window = sc->rx_buf_free - sc->bytes_pending_release.
                sc = <aiopquic_stream_ctx_t*>entry.stream_ctx
                rx_sb = sc.rx
                if rx_sb is not NULL:
                    avail = aiopquic_stream_buf_used(rx_sb)
                    if avail > 0:
                        length = avail
                        buf = malloc(<size_t>length)
                        if buf is not NULL:
                            aiopquic_stream_buf_pop(
                                rx_sb, <uint8_t*>buf, <uint32_t>length)
                            aiopquic_sc_rx_bytes_popped_add(<uint64_t>length)
                            data = memoryview(StreamChunk._wrap(
                                buf, length, self._ctx, sc,
                                <void*>entry.cnx, entry.stream_id))
                            aiopquic_stream_ctx_rx_consumed_add(
                                sc, <uint64_t>length)
                            pending_fc[<uintptr_t>sc] = (
                                <uintptr_t>entry.cnx, entry.stream_id)
            elif entry.data_buf is not NULL and entry.data_length == 0:
                # BORROWED-sc convention: data_buf is an aiopquic_stream_ctx_t*
                # we don't own. Used by WT STREAM_DATA / STREAM_FIN (bytes
                # in sc->rx) and by NEW_STREAM / STREAM_CREATED (sc passed
                # up so Python can stash it in _stream_tx_ctxs[sid]).
                # Expose sc_ptr in the tuple for all of them.
                sc = <aiopquic_stream_ctx_t*>entry.data_buf
                sc_ptr = <uintptr_t>sc
                if (entry.event_type == SPSC_EVT_WT_STREAM_DATA or
                        entry.event_type == SPSC_EVT_WT_STREAM_FIN):
                    rx_sb = sc.rx
                    if rx_sb is not NULL:
                        avail = aiopquic_stream_buf_used(rx_sb)
                        if avail > 0:
                            length = avail
                            buf = malloc(<size_t>length)
                            if buf is not NULL:
                                aiopquic_stream_buf_pop(
                                    rx_sb, <uint8_t*>buf, <uint32_t>length)
                                aiopquic_sc_rx_bytes_popped_add(<uint64_t>length)
                                data = memoryview(StreamChunk._wrap(
                                    buf, length, self._ctx, sc,
                                    <void*>entry.cnx, entry.stream_id))
                                aiopquic_stream_ctx_rx_consumed_add(
                                    sc, <uint64_t>length)
                                pending_fc[<uintptr_t>sc] = (
                                    <uintptr_t>entry.cnx, entry.stream_id)

            # Cython-side coalescing: when the picoquic worker fires
            # multiple stream_data callbacks for the same stream
            # rapidly, each pushes an SPSC event but the FIRST drain
            # pops all of sc->rx. Subsequent events for that stream
            # carry data=None — pure dispatch waste. Skip them. FIN
            # events still emit since they signal end-of-stream
            # regardless of data presence.
            if (data is None and
                (entry.event_type == SPSC_EVT_STREAM_DATA or
                 entry.event_type == SPSC_EVT_WT_STREAM_DATA)):
                spsc_ring_pop(self._ctx.rx_ring)
                continue

            events.append((
                entry.event_type,
                entry.stream_id,
                data,
                entry.is_fin,
                entry.error_code,
                <uintptr_t>entry.cnx,
                <uintptr_t>entry.stream_ctx,
                sc_ptr,
            ))
            spsc_ring_pop(self._ctx.rx_ring)

        # Per-cycle FC dedupe flush: emit at most one push per stream
        # for the work just performed. Hysteresis inside
        # _push_fc_credit filters further.
        cdef uintptr_t sc_uint
        cdef uintptr_t cnx_uint
        cdef uint64_t sid_uint
        for sc_uint, (cnx_uint, sid_uint) in pending_fc.items():
            self._push_fc_credit(
                <void*><uintptr_t>cnx_uint, sid_uint,
                <aiopquic_stream_ctx_t*><void*><uintptr_t>sc_uint)
        aiopquic_clear_rx(self._ctx)
        return events

    cdef inline void _push_fc_credit(self, void* cnx,
                                       uint64_t stream_id,
                                       aiopquic_stream_ctx_t* sc):
        """Push SPSC_EVT_TX_OPEN_FLOW_CONTROL onto tx_event_ring so
        the picoquic worker re-evaluates peer's MAX_STREAM_DATA for
        this stream.

        Hysteresis gate: skip the push if the stream's rx_consumed has
        not advanced by AIOPQUIC_RX_FC_HYSTERESIS_BYTES since the last
        push (default = sc->rx ring capacity / 4). Prevents the
        per-chunk FC storm — at 2 Gbps × 64KB objects we were emitting
        ~280K OPEN_FLOW_CONTROL events/sec, saturating the asyncio
        thread which then could not drain the rx_event_ring, causing
        STREAM_DATA event drops (the cliff).

        Hysteresis cost: peer's MAX_STREAM_DATA advances in chunks of
        HYSTERESIS_BYTES instead of continuously. Peer has full
        ring_cap of headroom at all times; advancing every quarter-ring
        means peer never actually waits at our typical rates.

        The worker (in the SPSC handler) computes effective_free
        = sc->rx_buf_free - sc->bytes_pending_release and calls
        picoquic_open_flow_control(grant). picoquic itself is
        monotonic — passing a value that doesn't advance is a no-op.

        Called only from drain_rx data paths (regular credit
        replenishment as peer data lands). StreamChunk.__getbuffer__
        used to also push, but per-chunk pushes were the storm root
        cause; getbuffer now only accounts pending_sub. """
        # Hysteresis: require rx_consumed to advance by at least
        # HYSTERESIS_BYTES (1/16 of advertise_cap, ~256KB at default
        # 4MB cap) before pushing. Tighter than ring_cap/4 so latency
        # stays low under continuous load (peer's window advances in
        # ~256KB steps = ~0.8ms at 2.5Gbps, well under typical p99
        # targets). Still ~256× lower event rate than per-chunk push.
        cdef uint64_t consumed = aiopquic_stream_ctx_rx_consumed_load(sc)
        cdef uint64_t last_push = aiopquic_stream_ctx_last_fc_push_consumed_load(sc)
        cdef uint64_t hysteresis = self._ctx.rx_ring_cap
        if hysteresis > 0:
            hysteresis >>= 4  # 1/16 of advertise_cap (~256KB default)
        else:
            hysteresis = 1 << 18  # 256 KB fallback
        if consumed - last_push < hysteresis:
            return
        aiopquic_stream_ctx_last_fc_push_consumed_store(sc, consumed)
        aiopquic_push_fc_credit(self._ctx, cnx, stream_id, <void*>sc)

    def drain_rx_callback(self, handler, int max_events=256):
        """Drain events and call handler() per entry — zero list/tuple
        allocation compared with drain_rx.

        handler is a callable invoked per ring entry as:
            handler(evt_type, stream_id, data, is_fin, error_code,
                    cnx_ptr, stream_ctx_ptr, sc_ptr)

        sc_ptr is the BORROWED per-stream aiopquic_stream_ctx_t pointer
        (uintptr_t) for events that carry one (WT STREAM_DATA/FIN,
        NEW_STREAM, STREAM_CREATED); 0 otherwise.

        SPSC_EVT_WT_STREAM_LINK_RELEASE is handled internally: drain_rx
        calls aiopquic_wt_stream_link_destroy and never invokes the
        handler. SPSC FIFO ordering guarantees every preceding
        STREAM_DATA/FIN/RESET for that stream has been processed first.

        Returns the number of entries drained (LINK_RELEASE counted).
        Same per-entry semantics as drain_rx (StreamChunk memoryview
        construction, byte-ring pop, rx_consumed advance, no-data
        coalescing).
        """
        cdef int i
        cdef int count = 0
        cdef spsc_entry_t* entry
        cdef void* buf
        cdef Py_ssize_t length
        cdef uintptr_t sc_ptr
        cdef aiopquic_stream_ctx_t* sc
        cdef aiopquic_stream_buf_t* rx_sb
        cdef uint32_t avail
        cdef dict pending_fc = {}
        for i in range(max_events):
            entry = spsc_ring_peek(self._ctx.rx_ring)
            if entry is NULL:
                break

            if entry.event_type == SPSC_EVT_WT_STREAM_LINK_RELEASE:
                if entry.data_buf is not NULL:
                    aiopquic_wt_stream_link_destroy(
                        <aiopquic_wt_stream_link_t*>entry.data_buf)
                spsc_ring_pop(self._ctx.rx_ring)
                count += 1
                continue

            if entry.event_type == SPSC_EVT_STREAM_DESTROY:
                if entry.stream_ctx is not NULL:
                    aiopquic_stream_ctx_destroy(
                        <aiopquic_stream_ctx_t*>entry.stream_ctx)
                spsc_ring_pop(self._ctx.rx_ring)
                count += 1
                continue

            if entry.event_type == SPSC_EVT_TX_RING_DRAINED:
                if self._tx_ring_drain_event is not None:
                    self._tx_ring_drain_event.set()
                spsc_ring_pop(self._ctx.rx_ring)
                count += 1
                continue

            data = None
            sc_ptr = 0
            if entry.data_length > 0 and entry.data_buf is not NULL:
                length = entry.data_length
                buf = spsc_ring_take_data(entry)
                data = memoryview(StreamChunk._wrap(
                    buf, length, NULL, NULL, NULL, 0))
            elif (entry.event_type == SPSC_EVT_STREAM_DATA or
                  entry.event_type == SPSC_EVT_STREAM_FIN) and \
                  entry.stream_ctx is not NULL:
                sc = <aiopquic_stream_ctx_t*>entry.stream_ctx
                rx_sb = sc.rx
                if rx_sb is not NULL:
                    avail = aiopquic_stream_buf_used(rx_sb)
                    if avail > 0:
                        length = avail
                        buf = malloc(<size_t>length)
                        if buf is not NULL:
                            aiopquic_stream_buf_pop(
                                rx_sb, <uint8_t*>buf, <uint32_t>length)
                            aiopquic_sc_rx_bytes_popped_add(<uint64_t>length)
                            data = memoryview(StreamChunk._wrap(
                                buf, length, self._ctx, sc,
                                <void*>entry.cnx, entry.stream_id))
                            aiopquic_stream_ctx_rx_consumed_add(
                                sc, <uint64_t>length)
                            pending_fc[<uintptr_t>sc] = (
                                <uintptr_t>entry.cnx, entry.stream_id)
            elif entry.data_buf is not NULL and entry.data_length == 0:
                sc = <aiopquic_stream_ctx_t*>entry.data_buf
                sc_ptr = <uintptr_t>sc
                if (entry.event_type == SPSC_EVT_WT_STREAM_DATA or
                        entry.event_type == SPSC_EVT_WT_STREAM_FIN):
                    rx_sb = sc.rx
                    if rx_sb is not NULL:
                        avail = aiopquic_stream_buf_used(rx_sb)
                        if avail > 0:
                            length = avail
                            buf = malloc(<size_t>length)
                            if buf is not NULL:
                                aiopquic_stream_buf_pop(
                                    rx_sb, <uint8_t*>buf, <uint32_t>length)
                                aiopquic_sc_rx_bytes_popped_add(<uint64_t>length)
                                data = memoryview(StreamChunk._wrap(
                                    buf, length, self._ctx, sc,
                                    <void*>entry.cnx, entry.stream_id))
                                aiopquic_stream_ctx_rx_consumed_add(
                                    sc, <uint64_t>length)
                                pending_fc[<uintptr_t>sc] = (
                                    <uintptr_t>entry.cnx, entry.stream_id)

            if (data is None and
                (entry.event_type == SPSC_EVT_STREAM_DATA or
                 entry.event_type == SPSC_EVT_WT_STREAM_DATA)):
                spsc_ring_pop(self._ctx.rx_ring)
                continue

            handler(
                entry.event_type,
                entry.stream_id,
                data,
                entry.is_fin,
                entry.error_code,
                <uintptr_t>entry.cnx,
                <uintptr_t>entry.stream_ctx,
                sc_ptr,
            )
            spsc_ring_pop(self._ctx.rx_ring)
            count += 1

        # Per-cycle FC dedupe flush (same pattern as drain_rx).
        cdef uintptr_t sc_uint
        cdef uintptr_t cnx_uint
        cdef uint64_t sid_uint
        for sc_uint, (cnx_uint, sid_uint) in pending_fc.items():
            self._push_fc_credit(
                <void*><uintptr_t>cnx_uint, sid_uint,
                <aiopquic_stream_ctx_t*><void*><uintptr_t>sc_uint)
        aiopquic_clear_rx(self._ctx)
        return count

    def push_tx(self, uint32_t event_type, uint64_t stream_id,
                bytes data=None, uint64_t error_code=0,
                uintptr_t cnx_ptr=0, uintptr_t stream_ctx=0,
                uint8_t is_fin=0):
        """
        Push a command into the TX ring (asyncio → picoquic thread).

        stream_ctx is forwarded as the v_stream_ctx pointer for events
        that consume it (SPSC_EVT_TX_MARK_ACTIVE → picoquic_mark_active_stream).
        For the PULL-model send path it carries an aiopquic_stream_buf_t*.

        is_fin is also reused as a small auxiliary flag: e.g.,
        SPSC_EVT_TX_SET_APP_FLOW_CONTROL uses it as the
        `use_app_flow_control` argument to picoquic_set_app_flow_control.

        After pushing, caller should call wake_up() to signal the network thread.
        """
        cdef spsc_entry_t entry
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0

        entry.event_type = event_type
        entry.stream_id = stream_id
        entry.is_fin = is_fin
        entry.cnx = <void*>cnx_ptr
        entry.stream_ctx = <void*>stream_ctx
        entry.error_code = error_code

        if data is not None:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        cdef int ret = spsc_ring_push(self._ctx.tx_ring, &entry, data_ptr, data_len)
        if ret == 0:
            self._ctx.cnt_tx_ring_pushes += 1
        else:
            raise BufferError("TX ring buffer is full")

    def tx_send_atomic(self, uint64_t stream_id, bytes data,
                        bint end_stream, uintptr_t cnx_ptr,
                        uintptr_t stream_ctx,
                        uint32_t stream_ring_cap):
        """Atomic send_stream_data primitive — all-or-nothing.

        Composes ensure-tx-ring + tx-event-ring capacity check + per-
        stream byte-ring push + TX_MARK_ACTIVE event push + worker
        wake-up into one Cython call. Holds the GIL through all four
        steps so no other Python coroutine can interleave between the
        bytes-commit and the event-push that the previous Python-level
        composition exposed.

        On a retryable failure (return 1 or 2), no bytes are committed
        to sc->tx — caller may safely re-call with the SAME data buffer
        without risk of duplicating bytes on the wire.

        Returns:
            0   success — bytes pushed to sc->tx, MARK_ACTIVE event
                queued, worker notified
            1   TX event ring full (caller retries)
            2   per-stream send ring full (caller retries)
           -1   allocation failure (sc->tx couldn't be created)
        """
        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*>stream_ctx
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0
        cdef spsc_entry_t entry
        cdef int rc

        self._send_calls += 1

        if data is not None:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        # PRE-CHECK 1: TX event ring must have room for the
        # MARK_ACTIVE event we'll push after committing bytes.
        # Without this, retry semantics duplicate bytes on the wire.
        # Single-producer (asyncio thread) ⇒ no TOCTOU window.
        if spsc_ring_count(self._ctx.tx_ring) >= self._ctx.tx_ring.capacity:
            # Arm the connection-global drain wakeup BEFORE returning
            # rc=1 so the caller can await tx_ring_drain_event without
            # losing the wakeup. Worker fires SPSC_EVT_TX_RING_DRAINED
            # when the ring drops to/below low_water.
            aiopquic_arm_tx_ring_drain_pending(self._ctx)
            self._send_busy_event_ring += 1
            return 1

        # COMMIT: push bytes to per-stream ring (atomic, all-or-nothing
        # internally; rc=0 means ring full, rc<0 alloc fail).
        rc = aiopquic_stream_ctx_send_data(
            sc, data_ptr, data_len, stream_ring_cap, end_stream)
        if rc == 0:
            self._send_busy_stream_ring += 1
            return 2
        if rc < 0:
            self._send_alloc_fail += 1
            return -1

        # Push MARK_ACTIVE event. Pre-check above guarantees room
        # under the single-producer asyncio invariant. The post-check
        # below catches invariant violations rather than masking them.
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_MARK_ACTIVE
        entry.stream_id = stream_id
        entry.cnx = <void*>cnx_ptr
        entry.stream_ctx = <void*>sc
        rc = spsc_ring_push(self._ctx.tx_ring, &entry, NULL, 0)
        if rc == 0:
            self._ctx.cnt_tx_ring_pushes += 1
        else:
            # Single-producer invariant violated — bytes are committed
            # to sc->tx but MARK_ACTIVE didn't post. Caller's retry
            # would re-commit and duplicate bytes. Arm the drain
            # signal so the caller wakes; the rc=1 path will trigger
            # again until the producer invariant is restored.
            aiopquic_arm_tx_ring_drain_pending(self._ctx)
            self._send_busy_event_ring += 1
            return 1

        # Coalesce the wake_up syscall: only invoke
        # picoquic_wake_up_network_thread on the 0→1 transition of
        # tx_wake_pending. The worker clears the flag back to 0 on
        # entry to its wake handler (BEFORE draining the tx ring), so
        # any push that races against the worker drain is guaranteed
        # to either land in the ring before the drain loop's next peek
        # OR cause a fresh wake.
        if self._thread_ctx is not NULL:
            if aiopquic_tx_wake_set_pending(self._ctx) == 0:
                picoquic_wake_up_network_thread(self._thread_ctx)

        return 0

    def tx_send_stream(self, uintptr_t cnx_ptr, uint64_t stream_id,
                        bytes data, bint end_stream=False,
                        uint32_t stream_ring_cap=AIOPQUIC_TX_STREAM_RING_CAP_DEFAULT):
        """Test-friendly low-level pull-model send primitive.

        Wraps tx_send_atomic with per-(cnx, sid) stream-context lifecycle
        management. The first call for a given (cnx_ptr, stream_id)
        allocates an aiopquic_stream_ctx_t via the C helper; subsequent
        calls reuse it. The dict is process-local to this TransportContext.

        Production code should use QuicConnection.send_stream_data, which
        manages its own per-cnx _stream_ctxs dict and exposes a richer
        BufferError-based backpressure API. This method exists for the
        bare-TransportContext test path (tests/test_loopback.py and
        derived integration / bench tests) so tests don't need to wrap
        every TransportContext in a QuicConnection.

        Raises:
            BufferError: TX event ring or per-stream send ring is full
                — caller may retry the SAME data without risk of
                duplicating bytes on the wire (tx_send_atomic guarantees
                all-or-nothing on retryable failures).
            MemoryError: per-stream send ring allocation failed, or
                aiopquic_stream_ctx_create returned NULL on first use.
        """
        cdef aiopquic_stream_ctx_t* sc
        cdef uintptr_t sc_ptr
        cdef int rc
        key = (cnx_ptr, stream_id)
        cached = self._test_stream_ctxs.get(key)
        if cached is None:
            sc = aiopquic_stream_ctx_create()
            if sc is NULL:
                raise MemoryError("aiopquic_stream_ctx_create returned NULL")
            sc_ptr = <uintptr_t>sc
            self._test_stream_ctxs[key] = sc_ptr
        else:
            sc_ptr = <uintptr_t>cached
        rc = self.tx_send_atomic(stream_id, data, end_stream,
                                  cnx_ptr, sc_ptr, stream_ring_cap)
        if rc == 1:
            raise BufferError(
                f"TX event ring full (stream={stream_id})"
            )
        if rc == 2:
            raise BufferError(
                f"per-stream send ring full "
                f"(stream={stream_id}, need={len(data) if data else 0})"
            )
        if rc < 0:
            raise MemoryError(
                f"tx_send_stream alloc failed (stream={stream_id})"
            )

    @property
    def send_calls(self):
        """Total tx_send_atomic invocations."""
        return self._send_calls

    @property
    def send_busy_event_ring(self):
        """Times tx_send_atomic returned 1 (TX event ring full)."""
        return self._send_busy_event_ring

    @property
    def send_busy_stream_ring(self):
        """Times tx_send_atomic returned 2 (per-stream byte ring full)."""
        return self._send_busy_stream_ring

    @property
    def send_alloc_fail(self):
        """Times tx_send_atomic returned -1 (allocation failure)."""
        return self._send_alloc_fail

    @property
    def worker_mark_active_processed(self):
        """Worker thread: count of TX_MARK_ACTIVE events drained from
        tx_ring and dispatched to picoquic_mark_active_stream. If this
        is < send_calls, MARK_ACTIVE events are being lost in the ring
        between Python push and worker dequeue (which would be a real
        SPSC bug)."""
        return self._ctx.worker_mark_active_processed

    @property
    def worker_prepare_to_send_calls(self):
        """Worker thread: count of picoquic_callback_prepare_to_send
        invocations on this context. Compare against
        worker_mark_active_processed to see whether picoquic ever
        actually polled the streams it was asked to mark active."""
        return self._ctx.worker_prepare_to_send_calls

    @property
    def worker_prepare_to_send_pulled_bytes(self):
        """Worker thread: cumulative bytes pulled from sc->tx rings via
        prepare_to_send. Should equal total bytes Python pushed across
        all streams once everything is drained."""
        return self._ctx.worker_prepare_to_send_pulled_bytes

    @property
    def worker_rx_event_drops(self):
        """Worker thread: count of events dropped because the RX
        event ring (rx_ring) was full at push time. For stream_data
        events the bytes ARE in sc->rx but Python is never notified —
        Python only pops sc->rx if a LATER event for the same stream
        pushes successfully. This is the stream-loss bug root cause."""
        return self._ctx.worker_rx_event_drops

    @property
    def worker_rx_event_drops_stream_data(self):
        """Subset of worker_rx_event_drops that were stream_data /
        stream_fin events (vs control events like datagram/close)."""
        return self._ctx.worker_rx_event_drops_stream_data

    @property
    def worker_rx_byte_ring_overflow(self):
        """Worker thread: count of per-stream RX byte-ring overflow
        events. Indicates the peer sent more bytes than its
        flow-control window allowed (spec violation). Previously
        printed to stderr per occurrence; now silent unless
        AIOPQUIC_RX_LOG=1 is set in the env."""
        return self._ctx.worker_rx_byte_ring_overflow

    def start(self, int port=0, cert_file=None, key_file=None,
              alpn=None, bint is_client=True, uint64_t idle_timeout_ms=30000,
              uint32_t max_datagram_frame_size=0,
              wt_path=None, debug_log=None, keylog_filename=None,
              uint32_t rx_ring_cap=0, congestion_control_algorithm=None,
              uint64_t initial_max_data=0, qlog_dir=None):
        """
        Create the picoquic context and start the network thread.

        Args:
            port: Local UDP port (0 = ephemeral for clients).
            cert_file: Path to TLS certificate (server mode).
            key_file: Path to TLS private key (server mode).
            alpn: Default ALPN string (e.g. "h3", "moq-chat").
            is_client: If True, skip cert verification.
            idle_timeout_ms: Idle timeout in milliseconds.
            max_datagram_frame_size: Max DATAGRAM frame size (0 = disabled).
            wt_path: Server-mode WebTransport path (e.g. "/moq").
                When set, uses h3zero_callback as picoquic's default
                callback and routes CONNECT-on-path to the WT bridge.
            keylog_filename: Path to write TLS secrets in NSS Key Log
                Format (Wireshark-compatible). When set, picoquic emits
                client/server randoms + master secrets per connection so
                packet captures can be decrypted offline. Requires
                picoquic built without PICOQUIC_WITHOUT_SSLKEYLOG.
        """
        if self._started:
            raise RuntimeError("Transport already started")

        # Stash per-stream RX ring capacity on the C ctx so the
        # stream_data callback sizes per-stream byte rings to match the
        # configured max_stream_data window. The C-side ring allocator
        # handles power-of-two rounding internally; we just pass the
        # configured window verbatim. 0 leaves the C default in place.
        if rx_ring_cap > 0:
            self._ctx.rx_ring_cap = aiopquic_ceil_pow2_u32(rx_ring_cap)

        cdef const char* c_cert = NULL
        cdef const char* c_key = NULL
        cdef const char* c_alpn = NULL
        cdef bytes b_cert, b_key, b_alpn

        if cert_file is not None:
            b_cert = cert_file.encode() if isinstance(cert_file, str) else cert_file
            c_cert = b_cert
        if key_file is not None:
            b_key = key_file.encode() if isinstance(key_file, str) else key_file
            c_key = b_key
        if alpn is not None:
            b_alpn = alpn.encode() if isinstance(alpn, str) else alpn
            c_alpn = b_alpn

        cdef picoquic_stream_data_cb_fn default_cb_fn = aiopquic_stream_cb
        cdef void* default_cb_ctx = <void*>self._ctx
        cdef bytes wt_path_b
        if wt_path is not None and not is_client:
            wt_path_b = wt_path.encode() if isinstance(wt_path, str) else wt_path
            # Empty server path → picoquic's "*" wildcard (PR #2085).
            if wt_path_b == b"":
                wt_path_b = b"*"
            self._wt_path_bytes = wt_path_b
            self._wt_path_item.path = self._wt_path_bytes
            self._wt_path_item.path_length = len(self._wt_path_bytes)
            self._wt_path_item.path_callback = aiopquic_wt_server_path_callback
            self._wt_path_item.path_app_ctx = <void*>self._ctx
            self._wt_params.web_folder = NULL
            self._wt_params.path_table = &self._wt_path_item
            self._wt_params.path_table_nb = 1
            default_cb_fn = h3zero_callback
            default_cb_ctx = <void*>&self._wt_params

        # Register picoquic's full CC algorithm catalog. Without this,
        # picoquic_get_congestion_algorithm() returns NULL for ANY name
        # (the algorithm registry is empty by default), so
        # picoquic_set_default_congestion_algorithm_by_name() silently
        # stores NULL → new cnxs have cnx->congestion_alg == NULL →
        # alg_init is never called → cwin stays pinned at
        # PICOQUIC_CWIN_INITIAL (10 × MSS = 15360) for the connection's
        # entire lifetime. picoquicdemo, pico_sim, pqbench, and every
        # picoquic test driver all call this at startup; aiopquic
        # didn't, which is what capped raw-QUIC loopback throughput.
        # Idempotent — repeats just overwrite the same static array.
        picoquic_register_all_congestion_control_algorithms()

        # Create picoquic context with the chosen default callback.
        self._quic = picoquic_create(
            256,            # max connections
            c_cert, c_key,
            NULL,           # cert root (use default)
            c_alpn,
            default_cb_fn,
            default_cb_ctx,
            NULL, NULL,     # no cnx_id callback
            NULL,           # no reset seed
            picoquic_current_time(),
            NULL,           # not simulated time
            NULL, NULL, 0)  # no tickets

        if self._quic is NULL:
            raise RuntimeError("Failed to create picoquic context")

        self._ctx.quic = self._quic

        # Optional congestion-control selection. None defers to picoquic's
        # compile-time default (newreno). Unknown names fall back the
        # same way (picoquic logs a warning and keeps the default).
        cdef bytes _b_cc
        cdef const char* _c_cc
        if congestion_control_algorithm is not None:
            _b_cc = (congestion_control_algorithm.encode()
                     if isinstance(congestion_control_algorithm, str)
                     else congestion_control_algorithm)
            _c_cc = _b_cc
            picoquic_set_default_congestion_algorithm_by_name(
                self._quic, _c_cc)

        cdef bytes _b_log
        # Textlog: per-cnx human-readable log of every packet, ACK, FC
        # frame, CC state. Tiny vs qlog, perfect for "what was the
        # worker last doing." Explicit debug_log arg wins; otherwise
        # AIOPQUIC_TEXTLOG_FILE env var.
        _textlog = debug_log
        if _textlog is None:
            import os
            _env_log = os.environ.get("AIOPQUIC_TEXTLOG_FILE")
            if _env_log:
                _textlog = _env_log
        if _textlog is not None:
            picoquic_set_log_level(self._quic, 1)
            _b_log = _textlog.encode() if isinstance(_textlog, str) else _textlog
            picoquic_set_textlog(self._quic, _b_log)

        cdef bytes _b_keylog
        if keylog_filename is not None:
            _b_keylog = (keylog_filename.encode()
                         if isinstance(keylog_filename, str)
                         else keylog_filename)
            picoquic_enable_sslkeylog(self._quic, 1)
            picoquic_set_key_log_file(self._quic, _b_keylog)

        # qlog: per-cnx JSON traces of CC state, RTT, FC frames, etc.
        # picoquic writes one .qlog file per connection (named by initial
        # CID) into the given directory. Explicit qlog_dir arg wins; if
        # not provided, fall back to AIOPQUIC_QLOG_DIR env var so qlog
        # can be enabled from the shell without code changes. Directory
        # must already exist; picoquic doesn't create it.
        cdef bytes _b_qlog
        _qlog_dir = qlog_dir
        if _qlog_dir is None:
            import os
            _env_qlog = os.environ.get("AIOPQUIC_QLOG_DIR")
            if _env_qlog:
                _qlog_dir = _env_qlog
        if _qlog_dir is not None:
            _b_qlog = (_qlog_dir.encode()
                       if isinstance(_qlog_dir, str)
                       else _qlog_dir)
            picoquic_set_qlog(self._quic, _b_qlog)

        if is_client:
            picoquic_set_null_verifier(self._quic)

        if wt_path is not None and not is_client:
            picowt_set_default_transport_parameters(self._quic)

        if idle_timeout_ms > 0:
            picoquic_set_default_idle_timeout(self._quic, idle_timeout_ms)

        # Default transport-parameter overrides. Both the per-stream
        # initial_max_stream_data window AND optional max_datagram_frame
        # are merged into the existing TP defaults. Setting the per-
        # stream window to match rx_ring_cap ensures the peer is told
        # at handshake time exactly how many bytes it may keep
        # unconsumed before MAX_STREAM_DATA must be extended — keeping
        # peer-allowed in-flight ≤ our RX ring capacity.
        cdef picoquic_tp_t tp
        cdef const picoquic_tp_t* cur_tp
        if (max_datagram_frame_size > 0 or rx_ring_cap > 0
                or initial_max_data > 0):
            cur_tp = picoquic_get_default_tp(self._quic)
            if cur_tp != NULL:
                tp = cur_tp[0]
                if max_datagram_frame_size > 0:
                    tp.max_datagram_frame_size = max_datagram_frame_size
                if rx_ring_cap > 0:
                    tp.initial_max_stream_data_bidi_local = rx_ring_cap
                    tp.initial_max_stream_data_bidi_remote = rx_ring_cap
                    tp.initial_max_stream_data_uni = rx_ring_cap
                if initial_max_data > 0:
                    # Connection-level flow control. picoquic's default
                    # is 1 MiB which falls over hard on MP-loopback at
                    # multi-stream workloads — every 1 MiB of TX data
                    # forces a MAX_DATA roundtrip, capping throughput
                    # at ~1 MiB / RTT. Pass cfg.max_data here to size
                    # the connection window for sustained workloads.
                    tp.initial_max_data = initial_max_data
                picoquic_set_default_tp(self._quic, &tp)

        # Configure packet loop parameters (must persist — picoquic stores a pointer)
        self._param.local_port = <unsigned short>port
        self._param.local_af = AF_INET
        self._param.dest_if = 0
        # SO_RCVBUF/SO_SNDBUF size. Default 0 lets picoquic use kernel
        # default (~200 KB on Linux). At line-rate UDP loopback (~3 Gbps
        # at QUIC MTU), 200 KB drains in 0.5 ms — bursty publishers
        # overrun the receive buffer, kernel drops packets, entire
        # streams disappear. 16 MiB matches the typical TCP autotune
        # ceiling and is plenty for sustained loopback.
        self._param.socket_buffer_size = 64 * 1024 * 1024
        self._param.extra_socket_required = 0
        self._param.prefer_extra_socket = 0
        # GSO + max-send-length defaults are platform-specific. The
        # send_length_max field changes meaning depending on
        # do_not_use_gso:
        #   GSO on (Linux):  send_length_max caps the kernel-coalesced
        #     buffer size for UDP segmentation offload. 65535 = max
        #     stride; kernel splits into individual QUIC packets. Big
        #     win at high pps — fewer sendmmsg syscalls.
        #   GSO off (macOS, FreeBSD): send_length_max becomes the
        #     max size of a single UDP datagram passed to sendmsg.
        #     macOS net.inet.udp.maxdgram defaults to 9216 so anything
        #     larger hits EMSGSIZE. lo0 MTU is 16384.
        # Defaults: Linux gets GSO + 65535; Darwin gets GSO off +
        # picoquic's default (0). Env vars AIOPQUIC_GSO and
        # AIOPQUIC_SEND_LENGTH_MAX override either side.
        cdef bint _darwin = sys.platform == "darwin"
        cdef int _gso_default = 0 if _darwin else 1
        cdef size_t _slm_default = 0 if _darwin else 65535
        _gso_env = os.environ.get("AIOPQUIC_GSO")
        if _gso_env is not None:
            _gso_default = 0 if _gso_env in ("0", "false", "False") else 1
        _slm_env = os.environ.get("AIOPQUIC_SEND_LENGTH_MAX")
        if _slm_env is not None:
            try:
                _slm_default = <size_t>int(_slm_env)
            except (TypeError, ValueError):
                pass
        self._param.do_not_use_gso = 0 if _gso_default else 1
        self._param.send_length_max = _slm_default

        # Start the network thread
        cdef int ret = 0
        self._thread_ctx = picoquic_start_network_thread(
            self._quic, &self._param,
            aiopquic_loop_cb, <void*>self._ctx,
            &ret)

        if self._thread_ctx is NULL or ret != 0:
            picoquic_free(self._quic)
            self._quic = NULL
            raise RuntimeError(f"Failed to start network thread (ret={ret})")

        # Mirror the thread_ctx into the C-level ctx so C-side helpers
        # (aiopquic_push_fc_credit etc.) can wake the worker without
        # round-tripping through Python. picoquic_wake_up_network_thread
        # requires picoquic_network_thread_ctx_t*, NOT picoquic_quic_t*
        # — a type mismatch here is a silent UAF-class corruption.
        self._ctx.thread_ctx = self._thread_ctx

        self._started = True

    def stop(self):
        """Stop the network thread and free the picoquic context."""
        self._shutdown()

    @property
    def started(self):
        """Whether the network thread is running."""
        return self._started

    @property
    def thread_ready(self):
        """Whether the network thread has completed initialization."""
        if self._thread_ctx is NULL:
            return False
        return self._thread_ctx.thread_is_ready != 0

    def wake_up(self):
        """Signal the network thread to process TX ring entries.

        No-op when the network thread is stopped — teardown is a normal
        state for unsynchronized push_* callers (e.g. subgroup-task
        cancel handlers firing after the transport has shut down).
        """
        if self._thread_ctx is NULL:
            return
        cdef int ret = picoquic_wake_up_network_thread(self._thread_ctx)
        if ret != 0:
            raise RuntimeError(f"Failed to wake network thread (ret={ret})")

    def create_client_connection(self, str host, int port,
                                  str sni=None, str alpn=None):
        """
        Create a client QUIC connection (thread-safe).

        Pushes a CONNECT command to the TX ring; the network thread
        creates the connection and sends back an ALMOST_READY event
        with the cnx pointer via the RX ring.

        Args:
            host: Remote IP address (IPv4).
            port: Remote port.
            sni: Server Name Indication (defaults to host).
            alpn: ALPN to negotiate (uses context default if None).
        """
        if not self._started:
            raise RuntimeError("Transport not started")

        # Build sockaddr_in
        cdef sockaddr_in addr
        addr.sin_family = AF_INET
        addr.sin_port = htons(<unsigned short>port)

        cdef bytes b_host = host.encode()
        if inet_pton(AF_INET, b_host, &addr.sin_addr) != 1:
            raise ValueError(f"Invalid IPv4 address: {host}")

        # Pack connect params into ring data payload
        # Layout: sockaddr_in | sni_len(2) | alpn_len(2) | sni | alpn
        cdef bytes b_sni = (sni or host).encode()
        cdef bytes b_alpn = alpn.encode() if alpn else b""

        cdef uint32_t sni_len = <uint32_t>len(b_sni)
        cdef uint32_t alpn_len = <uint32_t>len(b_alpn)
        cdef uint32_t hdr_size = sizeof(sockaddr_in) + 4
        cdef uint32_t total = hdr_size + sni_len + alpn_len

        cdef bytearray buf = bytearray(total)
        cdef uint8_t* p = <uint8_t*><char*>buf

        memcpy(p, &addr, sizeof(sockaddr_in))
        p += sizeof(sockaddr_in)
        # sni_len as little-endian uint16
        p[0] = <uint8_t>(sni_len & 0xFF)
        p[1] = <uint8_t>((sni_len >> 8) & 0xFF)
        # alpn_len as little-endian uint16
        p[2] = <uint8_t>(alpn_len & 0xFF)
        p[3] = <uint8_t>((alpn_len >> 8) & 0xFF)
        p += 4

        if sni_len > 0:
            memcpy(p, <const uint8_t*>b_sni, sni_len)
            p += sni_len
        if alpn_len > 0:
            memcpy(p, <const uint8_t*>b_alpn, alpn_len)

        # Push CONNECT command to TX ring
        cdef spsc_entry_t entry
        entry.event_type = SPSC_EVT_TX_CONNECT
        entry.stream_id = 0
        entry.is_fin = 0
        entry.cnx = NULL
        entry.stream_ctx = NULL
        entry.error_code = 0

        cdef bytes payload = bytes(buf)
        cdef int ret = spsc_ring_push(
            self._ctx.tx_ring, &entry,
            <const uint8_t*>payload, <uint32_t>len(payload))
        if ret != 0:
            raise BufferError("TX ring buffer is full")

        self.wake_up()


# =====================================================================
# WebTransport client session — Cython-side state holder.
#
# Owns one aiopquic_wt_session_t (allocated in C). Pushes WT TX commands
# into the TransportContext's tx_ring, then wakes the picoquic thread.
# Sync-only methods; the Python-level async wrapper in
# aiopquic.asyncio.webtransport handles futures + event routing.
# =====================================================================

cdef extern from "c/h3wt_callback.h":
    ctypedef struct aiopquic_wt_open_params_t:
        sockaddr_in addr
        uint16_t sni_len
        uint16_t path_len
        uint16_t protocols_len


cdef class WebTransportSessionState:
    """C-side state for one WebTransport client session.

    Holds the aiopquic_wt_session_t pointer, which the picoquic-thread
    uses as the path_app_ctx in our WT path callback. Events for this
    session show up in drain_rx with stream_ctx_ptr == this pointer.
    """
    cdef aiopquic_wt_session_t* _wt
    cdef TransportContext _transport
    cdef bint _opened     # set after push_open; picoquic owns the
                           # session as path_app_ctx — must deregister
                           # before destroy.

    def __cinit__(self, TransportContext transport,
                  uintptr_t session_ptr=0):
        self._transport = transport
        if session_ptr != 0:
            self._wt = <aiopquic_wt_session_t*><void*>session_ptr
            self._opened = True
        else:
            self._wt = aiopquic_wt_session_create(transport._ctx)
            self._opened = False
            if self._wt is NULL:
                raise MemoryError("Failed to create WT session")

    def __dealloc__(self):
        """If we ever pushed TX_WT_OPEN, picoquic is holding our
        session as path_app_ctx. Push TX_WT_DEREGISTER and let the
        picoquic thread call picowt_deregister + free. Don't free
        here; doing so would race with any in-flight path callback.

        If never opened, free directly (picoquic has no reference)."""
        cdef spsc_entry_t entry
        if self._wt is NULL:
            return
        if self._opened:
            # Push deregister; the picoquic thread will free wt.
            memset(&entry, 0, sizeof(entry))
            entry.event_type = SPSC_EVT_TX_WT_DEREGISTER
            entry.cnx = <void*>self._wt
            entry.stream_ctx = <void*>self._wt
            spsc_ring_push(self._transport._ctx.tx_ring, &entry,
                           NULL, 0)
            try:
                self._transport.wake_up()
            except Exception:
                pass
            self._wt = NULL
        else:
            aiopquic_wt_session_destroy(self._wt)
            self._wt = NULL

    @property
    def session_ptr(self):
        """Pointer to the aiopquic_wt_session_t struct (uintptr_t).

        Used by the asyncio dispatcher to route incoming events to
        the correct session: the picoquic-thread side stores this
        pointer in entry.stream_ctx for every WT event."""
        return <uintptr_t>self._wt

    @property
    def cnx_ptr(self):
        """Pointer to the picoquic_cnx_t (uintptr_t), valid after
        SESSION_READY. Used for TX_STREAM_DATA pushes that need cnx."""
        if self._wt is NULL or self._wt.cnx is NULL:
            return 0
        return <uintptr_t>self._wt.cnx

    @property
    def control_stream_id(self):
        if self._wt is NULL:
            return 0
        return self._wt.control_stream_id

    def push_open(self, str host, int port, str path, str sni,
                  str wt_protocols=""):
        """Push TX_WT_OPEN to the picoquic thread. Build the params
        payload (addr + sni + path + protocols), then push entry.

        wt_protocols is a comma-separated string of WT-Available-Protocols
        subprotocol identifiers (empty string = none, send NULL on wire).
        aiopquic does no interpretation; verbatim handed to picoquic.
        """
        cdef sockaddr_in addr
        memset(&addr, 0, sizeof(addr))
        addr.sin_family = AF_INET
        addr.sin_port = htons(<unsigned short>port)
        cdef bytes b_host = host.encode()
        if inet_pton(AF_INET, b_host, &addr.sin_addr) != 1:
            raise ValueError(f"Invalid IPv4 address: {host}")

        cdef bytes b_sni = sni.encode()
        # Empty client path → "/" (HTTP/3 root, RFC 9114 §4.3.1).
        cdef bytes b_path = b"/" if path == "" else path.encode()
        cdef bytes b_protocols = wt_protocols.encode()
        cdef uint32_t sni_len = <uint32_t>len(b_sni)
        cdef uint32_t path_len = <uint32_t>len(b_path)
        cdef uint32_t protocols_len = <uint32_t>len(b_protocols)
        cdef uint32_t hdr_size = sizeof(aiopquic_wt_open_params_t)
        cdef uint32_t total = hdr_size + sni_len + path_len + protocols_len

        cdef bytearray buf = bytearray(total)
        cdef uint8_t* p = <uint8_t*><char*>buf
        # Layout: addr | sni_len(2) | path_len(2) | protocols_len(2)
        #       | sni | path | protocols
        memcpy(p, &addr, sizeof(sockaddr_in))
        cdef uint16_t* hdr_lens = <uint16_t*>(p + sizeof(sockaddr_in))
        hdr_lens[0] = <uint16_t>sni_len
        hdr_lens[1] = <uint16_t>path_len
        hdr_lens[2] = <uint16_t>protocols_len
        memcpy(p + hdr_size, <const uint8_t*>b_sni, sni_len)
        memcpy(p + hdr_size + sni_len, <const uint8_t*>b_path, path_len)
        if protocols_len > 0:
            memcpy(p + hdr_size + sni_len + path_len,
                   <const uint8_t*>b_protocols, protocols_len)

        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_OPEN
        entry.cnx = <void*>self._wt   # session ptr — C side downcasts
        entry.stream_ctx = <void*>self._wt
        cdef bytes payload = bytes(buf)
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry,
            <const uint8_t*>payload, total)
        if ret != 0:
            raise BufferError("TX ring full (WT_OPEN)")
        self._opened = True
        self._transport.wake_up()

    def push_create_stream(self, bint bidir):
        """Push TX_WT_CREATE_STREAM. Reply event WT_STREAM_CREATED
        carries the assigned stream_id in stream_id field."""
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_CREATE_STREAM
        entry.cnx = <void*>self._wt
        entry.stream_ctx = <void*>self._wt
        entry.is_fin = 1 if bidir else 0
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, NULL, 0)
        if ret != 0:
            raise BufferError("TX ring full (WT_CREATE_STREAM)")
        self._transport.wake_up()

    def push_close(self, uint32_t error_code, bytes reason=b""):
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_CLOSE
        entry.cnx = <void*>self._wt
        entry.stream_ctx = <void*>self._wt
        entry.error_code = error_code
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0
        if reason:
            data_ptr = <const uint8_t*>reason
            data_len = <uint32_t>len(reason)
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, data_ptr, data_len)
        if ret != 0:
            raise BufferError("TX ring full (WT_CLOSE)")
        self._transport.wake_up()

    def push_drain(self):
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_DRAIN
        entry.cnx = <void*>self._wt
        entry.stream_ctx = <void*>self._wt
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, NULL, 0)
        if ret != 0:
            raise BufferError("TX ring full (WT_DRAIN)")
        self._transport.wake_up()

    def push_stream_data(self, uint64_t stream_id, uintptr_t sc_ptr,
                          bytes data,
                          bint end_stream=False,
                          uint32_t stream_ring_cap=AIOPQUIC_TX_STREAM_RING_CAP_DEFAULT):
        """Send bytes on a WT data stream — pull model.

        sc_ptr is the per-stream aiopquic_stream_ctx_t pointer the
        caller stashed from a STREAM_CREATED (we initiated) or
        NEW_STREAM (peer initiated) event. The worker owns the link
        + sc lifetime via h3zero's stream_ctx; Python only borrows
        the pointer for writes and must drop its reference on
        FIN/RESET/STOP_SENDING.

        Atomic from the asyncio thread's perspective:
          1. PRE-CHECK the SPSC TX-event ring has room for the
             MARK_ACTIVE event we'll need to push.
          2. Atomically commit bytes into sc->tx (lazy-allocates the
             ring at stream_ring_cap on first push). All-or-nothing —
             on ring-full no bytes are committed; tx_drain_pending is
             armed so the picoquic worker emits one
             SPSC_EVT_STREAM_TX_DRAINED when it next drains.
          3. Push TX_MARK_ACTIVE (guaranteed room from step 1 under
             the single-producer asyncio model). Worker pops it,
             calls picoquic_mark_active_stream(cnx, sid, 1, NULL) —
             passing NULL so picoquic's app_stream_ctx doesn't clobber
             h3zero's stream lookup. picoquic then asks h3zero for
             bytes via prepare_to_send; h3zero invokes our
             aiopquic_wt_path_callback(provide_data) which drains
             sc->tx and emits the drain event via the same edge-
             triggered pattern raw-QUIC uses.

        Raises:
          BufferError: TX-event ring or sc->tx is full. Bytes from
              the failed call were NOT committed; caller can retry
              the SAME data buffer without risk of duplicating bytes.
          RuntimeError: WT session not yet open, or sc_ptr is 0.
          MemoryError: sc->tx ring allocation failed.
        """
        cdef picoquic_cnx_t* cnx = NULL
        if self._wt is not NULL:
            cnx = <picoquic_cnx_t*>self._wt.cnx
        if cnx is NULL:
            raise RuntimeError("WT session not yet open")

        cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*>sc_ptr
        if sc is NULL:
            raise RuntimeError(
                f"WT stream {stream_id} has no sc (closed or never created)"
            )

        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0
        if data:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)

        # PRE-CHECK 1: TX event ring must have room for the
        # MARK_ACTIVE event we'll push after committing bytes.
        # Without this, retry semantics could duplicate bytes.
        # Single-producer (asyncio thread) ⇒ no TOCTOU window.
        if (spsc_ring_count(self._transport._ctx.tx_ring) >=
                self._transport._ctx.tx_ring.capacity):
            # Arm the connection-global drain wakeup BEFORE raising so
            # the caller can await tx_ring_drain_event without losing
            # the wakeup. Worker fires SPSC_EVT_TX_RING_DRAINED when
            # the ring drops to/below low_water.
            aiopquic_arm_tx_ring_drain_pending(self._transport._ctx)
            raise BufferError(
                f"TX event ring full (WT stream={stream_id})"
            )

        # COMMIT: push bytes to per-stream sc->tx (lazy-allocates
        # ring at stream_ring_cap if absent; atomic / all-or-nothing).
        cdef int rc = aiopquic_stream_ctx_send_data(
            sc, data_ptr, data_len, stream_ring_cap,
            <uint8_t>(1 if end_stream else 0))
        if rc == 0:
            # sc->tx full; tx_drain_pending armed inside helper.
            raise BufferError(
                f"WT TX ring full (stream={stream_id}, "
                f"need={data_len})"
            )
        if rc < 0:
            raise MemoryError(
                f"WT sc->tx alloc failed (stream={stream_id})"
            )

        # MARK_ACTIVE: stream_ctx=NULL preserves h3zero's lookup-by-
        # sid behavior. h3zero handles NULL app_stream_ctx safely on
        # both prepare_to_send (h3zero_common.c:1640) and stream_data
        # (h3zero_common.c:1492) paths.
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_MARK_ACTIVE
        entry.stream_id = stream_id
        entry.cnx = <void*>cnx
        entry.stream_ctx = NULL
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, NULL, 0)
        if ret != 0:
            # Single-producer invariant violated. Arm the connection-
            # global drain signal so the caller's BufferError handler
            # waking on tx_ring_drain_event will fire when the ring
            # drains. Bytes are already in sc->tx; caller's retry must
            # check rc semantics so as not to duplicate. send_data is
            # all-or-nothing per call so the SAME buffer can be retried
            # without doubling, but pre-check + caller-retry should
            # converge to MARK_ACTIVE landing.
            aiopquic_arm_tx_ring_drain_pending(self._transport._ctx)
            raise BufferError(
                f"TX event ring full (WT MARK_ACTIVE stream={stream_id})"
            )
        self._transport.wake_up()

    def push_reset_stream(self, uint64_t stream_id, uint64_t error_code):
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_RESET_STREAM
        entry.cnx = <void*>self._wt
        entry.stream_ctx = <void*>self._wt
        entry.stream_id = stream_id
        entry.error_code = error_code
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, NULL, 0)
        if ret != 0:
            raise BufferError("TX ring full (WT_RESET_STREAM)")
        self._transport.wake_up()

    def push_stop_sending(self, uint64_t stream_id, uint64_t error_code):
        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = SPSC_EVT_TX_WT_STOP_SENDING
        entry.cnx = <void*>self._wt
        entry.stream_ctx = <void*>self._wt
        entry.stream_id = stream_id
        entry.error_code = error_code
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, NULL, 0)
        if ret != 0:
            raise BufferError("TX ring full (WT_STOP_SENDING)")
        self._transport.wake_up()


# ---------------------------------------------------------------------------
# Wire-level cnx counters — picoquic-side accounting of bytes that actually
# crossed the UDP socket. send_stream_data only queues into picoquic's
# per-stream send buffer; cwnd/pacing/pacing-fairness then governs when
# bytes leave the wire. data_sent / data_received expose that ground truth.
# ---------------------------------------------------------------------------

def cnx_data_sent(uintptr_t cnx_ptr):
    """Cumulative bytes the cnx has placed on the wire."""
    if cnx_ptr == 0:
        return 0
    return picoquic_get_data_sent(<picoquic_cnx_t*>cnx_ptr)


def cnx_data_received(uintptr_t cnx_ptr):
    """Cumulative bytes the cnx has received from the wire."""
    if cnx_ptr == 0:
        return 0
    return picoquic_get_data_received(<picoquic_cnx_t*>cnx_ptr)


# ---------------------------------------------------------------------------
# Per-stream send buffer (PULL-model send path).
#
# Real backpressure: producer push returns # bytes accepted; 0 means full.
# picoquic pulls from the ring at wire rate via the prepare_to_send callback
# in aiopquic_stream_cb (when stream_ctx == buffer pointer).
#
# Lifecycle:
#   sb = stream_buf_create(capacity)         # capacity must be power of 2
#   transport.push_tx(SPSC_EVT_TX_MARK_ACTIVE,
#                     stream_id, cnx_ptr=cnx, stream_ctx=sb)
#   transport.wake_up()
#   stream_buf_push(sb, data)                # producer; returns bytes accepted
#   ...
#   stream_buf_set_fin(sb)                   # mark FIN follows on drain
#   transport.push_tx(SPSC_EVT_TX_MARK_ACTIVE,
#                     stream_id, cnx_ptr=cnx, stream_ctx=sb)  # ensure active
#   transport.wake_up()
#   ... wait for ring to drain ...
#   stream_buf_destroy(sb)
# ---------------------------------------------------------------------------

def stream_buf_create(uint32_t capacity):
    """Allocate a per-stream send buffer of `capacity` bytes (power of 2).

    Returns an integer pointer (uintptr_t) that should be passed as
    stream_ctx to push_tx(SPSC_EVT_TX_MARK_ACTIVE, ...) and freed via
    stream_buf_destroy() when the stream is done.
    """
    cdef aiopquic_stream_buf_t* sb = aiopquic_stream_buf_create(capacity)
    if sb is NULL:
        raise ValueError(
            f"stream_buf_create({capacity}): bad capacity "
            "(must be power of 2 and non-zero) or out of memory")
    return <uintptr_t>sb


def stream_buf_destroy(uintptr_t sb_ptr):
    """Free a stream buffer. Caller must ensure picoquic has stopped
    referencing it (stream closed / reset / connection ended)."""
    if sb_ptr == 0:
        return
    aiopquic_stream_buf_destroy(<aiopquic_stream_buf_t*>sb_ptr)


def stream_buf_push(uintptr_t sb_ptr, bytes data):
    """Append bytes to the stream's send ring. Returns # bytes accepted.

    A return < len(data) indicates partial acceptance — the ring is at
    capacity. Caller should retry the unaccepted tail later (after the
    consumer has drained more).
    """
    if sb_ptr == 0 or not data:
        return 0
    cdef const uint8_t* p = <const uint8_t*>data
    return aiopquic_stream_buf_push(
        <aiopquic_stream_buf_t*>sb_ptr, p, <uint32_t>len(data))


def stream_buf_used(uintptr_t sb_ptr):
    """Bytes currently buffered (waiting for picoquic to pull)."""
    if sb_ptr == 0:
        return 0
    return aiopquic_stream_buf_used(<aiopquic_stream_buf_t*>sb_ptr)


def stream_buf_free(uintptr_t sb_ptr):
    """Bytes of capacity currently free (max producer can push next)."""
    if sb_ptr == 0:
        return 0
    return aiopquic_stream_buf_free(<aiopquic_stream_buf_t*>sb_ptr)


def stream_buf_set_fin(uintptr_t sb_ptr):
    """Mark FIN to be sent once the ring has been fully drained by picoquic."""
    if sb_ptr == 0:
        return
    aiopquic_stream_buf_set_fin(<aiopquic_stream_buf_t*>sb_ptr)


def stream_buf_stats(uintptr_t sb_ptr):
    """Return (pushed, popped, push_hash, pop_hash) for byte-conservation
    diagnostics. push_hash and pop_hash are FNV-1a accumulators that are
    updated when AIOPQUIC_TX_HASH=1 was set at stream_buf_create() time.
    Returns (0, 0, 0, 0) for a NULL pointer.
    """
    if sb_ptr == 0:
        return (0, 0, 0, 0)
    cdef aiopquic_stream_buf_t* sb = <aiopquic_stream_buf_t*>sb_ptr
    return (
        aiopquic_stream_buf_pushed(sb),
        aiopquic_stream_buf_popped(sb),
        aiopquic_stream_buf_push_hash(sb),
        aiopquic_stream_buf_pop_hash(sb),
    )


def stream_buf_pop_to_bytes(uintptr_t sb_ptr, uint32_t max_bytes):
    """Pop up to max_bytes from the per-stream byte ring as a fresh
    `bytes` object. Returns b'' when the ring is empty or sb_ptr is NULL.

    Used on the RX path: picoquic worker thread pushed bytes into this
    ring synchronously inside the stream_data callback; this drains
    them on the asyncio thread for delivery as StreamDataReceived.
    """
    if sb_ptr == 0 or max_bytes == 0:
        return b''
    cdef aiopquic_stream_buf_t* sb = <aiopquic_stream_buf_t*>sb_ptr
    cdef uint32_t avail = aiopquic_stream_buf_used(sb)
    if avail == 0:
        return b''
    cdef uint32_t to_read = max_bytes if max_bytes < avail else avail
    cdef bytes out = PyBytes_FromStringAndSize(NULL, to_read)
    cdef char* buf = PyBytes_AsString(out)
    cdef uint32_t actual = aiopquic_stream_buf_pop(
        sb, <uint8_t*>buf, to_read)
    if actual != to_read:
        # Shouldn't happen — head only advances on the consumer thread.
        return out[:actual]
    return out


# ---------------------------------------------------------------------------
# stream_ctx_t — per-stream wrapper. Allocated on first contact (TX or RX),
# bound to picoquic's app_stream_ctx slot so both prepare_to_send and
# stream_data callbacks find the same struct. Holds the per-direction
# byte rings + RX flow-control state.
# ---------------------------------------------------------------------------
def stream_ctx_create():
    """Allocate a fresh wrapper. Returns its address as uintptr_t.
    Both rings start NULL — call stream_ctx_ensure_tx / _ensure_rx to
    populate. Caller must eventually stream_ctx_destroy() to free."""
    cdef aiopquic_stream_ctx_t* sc = aiopquic_stream_ctx_create()
    if not sc:
        raise MemoryError("aiopquic_stream_ctx_create returned NULL")
    return <uintptr_t>sc


def stream_ctx_destroy(uintptr_t sc_ptr):
    """Free the wrapper + both rings. Call only after the picoquic worker
    has stopped using this stream_ctx (i.e., after stream_reset/_fin and
    the next picoquic_packet_loop_wake-up cycle has completed). For now
    callers defer destroy until process exit to avoid use-after-free with
    the picoquic worker thread."""
    if sc_ptr == 0:
        return
    aiopquic_stream_ctx_destroy(<aiopquic_stream_ctx_t*>sc_ptr)


def stream_ctx_ensure_tx(uintptr_t sc_ptr, uint32_t capacity):
    """Lazily allocate the TX ring on the wrapper. Idempotent — repeated
    calls with the ring already present are a no-op. Capacity must be
    a power of two. Returns 0 on success, raises on alloc failure."""
    if sc_ptr == 0:
        raise ValueError("stream_ctx_ensure_tx called with NULL pointer")
    cdef int ret = aiopquic_stream_ctx_ensure_tx(
        <aiopquic_stream_ctx_t*>sc_ptr, capacity)
    if ret != 0:
        raise MemoryError(f"aiopquic_stream_ctx_ensure_tx failed (ret={ret})")
    return 0


def stream_ctx_ensure_rx(uintptr_t sc_ptr, uint32_t capacity):
    """Lazily allocate the RX ring on the wrapper. Idempotent."""
    if sc_ptr == 0:
        raise ValueError("stream_ctx_ensure_rx called with NULL pointer")
    cdef int ret = aiopquic_stream_ctx_ensure_rx(
        <aiopquic_stream_ctx_t*>sc_ptr, capacity)
    if ret != 0:
        raise MemoryError(f"aiopquic_stream_ctx_ensure_rx failed (ret={ret})")
    return 0


def stream_ctx_get_tx(uintptr_t sc_ptr):
    """Return the TX ring pointer (or 0 if not yet ensured)."""
    if sc_ptr == 0:
        return 0
    cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*>sc_ptr
    return <uintptr_t>sc.tx


def stream_ctx_get_rx(uintptr_t sc_ptr):
    """Return the RX ring pointer (or 0 if not yet ensured)."""
    if sc_ptr == 0:
        return 0
    cdef aiopquic_stream_ctx_t* sc = <aiopquic_stream_ctx_t*>sc_ptr
    return <uintptr_t>sc.rx


def stream_ctx_rx_consumed(uintptr_t sc_ptr):
    """Cumulative RX bytes drained from the per-stream ring (atomic
    load). Useful for diagnostics; flow-control advancement is now a
    side-effect of the picoquic worker thread reading this counter
    inside its stream_data callback — no Python-side dispatch."""
    if sc_ptr == 0:
        return 0
    return aiopquic_stream_ctx_rx_consumed_load(
        <aiopquic_stream_ctx_t*>sc_ptr)


def stream_ctx_send_data(uintptr_t sc_ptr, bytes data,
                          uint32_t capacity, bint fin):
    """Combined send-data fast path — collapses ensure_tx + free-check
    + push + (optional) set_fin into one Cython call.

    Returns:
       1  pushed all bytes (and set FIN if requested)
       0  ring full — caller waits + retries the SAME data buffer
          (push is all-or-nothing; no partial commit happens).
      -1  allocation failure (caller raises MemoryError).

    Pull model unchanged: bytes go into the SPSC TX ring; picoquic
    pulls at wire rate via prepare_to_send. The MARK_ACTIVE event +
    wake_up still fire from QuicConnection.send_stream_data after a
    successful return.
    """
    if sc_ptr == 0:
        raise ValueError("stream_ctx_send_data called with NULL sc")
    cdef const uint8_t* p = <const uint8_t*>data if data else NULL
    cdef uint32_t length = <uint32_t>len(data) if data else 0
    return aiopquic_stream_ctx_send_data(
        <aiopquic_stream_ctx_t*>sc_ptr,
        p, length, capacity, <uint8_t>(1 if fin else 0),
    )


# Note: set_max_stream_data and enable_app_flow_control are issued from
# the connection.py drain path via push_tx(SPSC_EVT_TX_OPEN_FLOW_CONTROL,
# error_code=new_max) and push_tx(SPSC_EVT_TX_SET_APP_FLOW_CONTROL,
# is_fin=1). No direct Python wrappers needed — the existing push_tx
# API already delivers events to the picoquic worker thread.

