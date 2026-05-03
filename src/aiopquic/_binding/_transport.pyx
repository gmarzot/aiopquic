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

from cpython.buffer cimport PyBuffer_FillInfo
from cpython.bytes cimport PyBytes_AsString, PyBytes_FromStringAndSize
from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t, uintptr_t
from libc.stdlib cimport free, malloc
from libc.string cimport memcpy, memset

from aiopquic._binding.spsc_ring cimport (
    spsc_ring_t, spsc_ring_create, spsc_ring_destroy,
    spsc_ring_count, spsc_ring_empty, spsc_ring_peek,
    spsc_ring_entry_data, spsc_ring_pop, spsc_ring_push,
    spsc_ring_take_data,
    spsc_entry_t,
    SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN,
    SPSC_EVT_STREAM_RESET, SPSC_EVT_STOP_SENDING,
    SPSC_EVT_CLOSE, SPSC_EVT_APP_CLOSE,
    SPSC_EVT_READY, SPSC_EVT_ALMOST_READY,
    SPSC_EVT_DATAGRAM,
    SPSC_EVT_TX_STREAM_DATA, SPSC_EVT_TX_STREAM_FIN,
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
    """
    int picoquic_set_textlog(picoquic_quic_t* quic, const char* textlog_file)

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
    int picoquic_add_to_stream(picoquic_cnx_t* cnx, uint64_t stream_id,
                                const uint8_t* data, size_t length, int set_fin)

cdef extern from "picoquic_packet_loop.h":
    ctypedef struct picoquic_packet_loop_param_t:
        unsigned short local_port
        int local_af
        int dest_if
        int socket_buffer_size
        int do_not_use_gso
        int extra_socket_required
        int prefer_extra_socket

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
    ctypedef struct aiopquic_ctx_t:
        spsc_ring_t* rx_ring
        spsc_ring_t* tx_ring
        int eventfd
        picoquic_quic_t* quic
        uint32_t rx_ring_cap

    aiopquic_ctx_t* aiopquic_ctx_create(uint32_t ring_capacity)
    void aiopquic_ctx_destroy(aiopquic_ctx_t* ctx)
    void aiopquic_clear_rx(aiopquic_ctx_t* ctx)
    void aiopquic_notify_rx(aiopquic_ctx_t* ctx)

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
    ctypedef struct aiopquic_stream_ctx_t:
        aiopquic_stream_buf_t* tx
        aiopquic_stream_buf_t* rx
        # Atomic on the C side; Cython sees plain uint64_t and we touch
        # them via aiopquic_stream_ctx_rx_* accessors.
        uint64_t rx_consumed
        uint64_t rx_credit_limit
        uint8_t  pending_destroy

    aiopquic_stream_ctx_t* aiopquic_stream_ctx_create()
    int aiopquic_stream_ctx_ensure_tx(aiopquic_stream_ctx_t* sc,
                                      uint32_t capacity)
    int aiopquic_stream_ctx_ensure_rx(aiopquic_stream_ctx_t* sc,
                                      uint32_t capacity)
    void aiopquic_stream_ctx_destroy(aiopquic_stream_ctx_t* sc)
    uint64_t aiopquic_stream_ctx_rx_consumed_load(aiopquic_stream_ctx_t* sc)
    void aiopquic_stream_ctx_rx_consumed_add(
        aiopquic_stream_ctx_t* sc, uint64_t delta)


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


# Default ring sizing
DEF DEFAULT_RING_CAPACITY = 4096


cdef class StreamChunk:
    """
    Owns a malloc'd byte buffer and exposes it via the Python buffer
    protocol. Built by drain_rx; the underlying buffer is the same
    memory written by the picoquic callback's mandatory copy-out.

    Ownership transfers from spsc_ring entry → StreamChunk via
    spsc_ring_take_data(). The buffer is freed in __dealloc__ when
    the last reference (memoryview or otherwise) is dropped.

    Internal type. Public surface is memoryview(chunk).
    """
    cdef void* _buf
    cdef Py_ssize_t _len

    def __cinit__(self):
        self._buf = NULL
        self._len = 0

    @staticmethod
    cdef StreamChunk _wrap(void* buf, Py_ssize_t length):
        cdef StreamChunk c = StreamChunk.__new__(StreamChunk)
        c._buf = buf
        c._len = length
        return c

    def __dealloc__(self):
        if self._buf is not NULL:
            free(self._buf)
            self._buf = NULL

    def __len__(self):
        return self._len

    def __getbuffer__(self, Py_buffer* buffer, int flags):
        # Read-only 1-D byte view; refcount on self keeps the buffer
        # alive for the consumer's memoryview lifetime.
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
            data = memoryview(StreamChunk._wrap(buf, length))

        result = (entry.event_type, entry.stream_id, data,
                  entry.is_fin, entry.error_code)
        spsc_ring_pop(self._ring)
        return result


cdef class TransportContext:
    """
    Manages the picoquic context and network thread.

    This is the core bridge between the C picoquic library and Python.
    It owns the SPSC rings and the eventfd used for async notification.
    """
    cdef aiopquic_ctx_t* _ctx
    cdef picoquic_quic_t* _quic
    cdef picoquic_network_thread_ctx_t* _thread_ctx
    cdef picoquic_packet_loop_param_t _param
    cdef bint _started
    # WT server-mode storage; must persist for picoquic's lifetime.
    cdef bytes _wt_path_bytes
    cdef picohttp_server_path_item_t _wt_path_item
    cdef picohttp_server_parameters_t _wt_params

    def __cinit__(self, uint32_t ring_capacity=DEFAULT_RING_CAPACITY):
        self._ctx = aiopquic_ctx_create(ring_capacity)
        self._quic = NULL
        self._thread_ctx = NULL
        self._started = False
        if self._ctx is NULL:
            raise MemoryError("Failed to create transport context")

    def __dealloc__(self):
        self._shutdown()
        if self._ctx is not NULL:
            aiopquic_ctx_destroy(self._ctx)
            self._ctx = NULL

    cdef void _shutdown(self):
        """Stop the network thread and free picoquic context."""
        if self._thread_ctx is not NULL:
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

    def drain_rx(self, int max_events=256):
        """
        Drain events from the RX ring.

        Returns a list of
            (event_type, stream_id, data, is_fin, error_code,
             cnx_ptr, stream_ctx_ptr).
        `data` is a memoryview over an internal StreamChunk, or None for
        events without payload. The chunk owns its buffer; the memoryview
        keeps it alive via PEP 3118 buffer protocol refcount. No copy
        after picoquic.

        For raw-QUIC events: cnx_ptr is the picoquic_cnx_t*, stream_ctx
        is whatever picoquic set on the stream (often NULL).
        For WebTransport events: cnx_ptr is the picoquic_cnx_t*,
        stream_ctx_ptr is the aiopquic_wt_session_t* — used for routing
        events to the right WT session in the asyncio side.
        """
        aiopquic_clear_rx(self._ctx)

        events = []
        cdef int i
        cdef spsc_entry_t* entry
        cdef void* buf
        cdef Py_ssize_t length

        cdef aiopquic_stream_ctx_t* sc
        cdef aiopquic_stream_buf_t* rx_sb
        cdef uint32_t avail
        for i in range(max_events):
            entry = spsc_ring_peek(self._ctx.rx_ring)
            if entry is NULL:
                break

            data = None
            if entry.data_length > 0 and entry.data_buf is not NULL:
                length = entry.data_length
                buf = spsc_ring_take_data(entry)
                data = memoryview(StreamChunk._wrap(buf, length))
            elif (entry.event_type == SPSC_EVT_STREAM_DATA or
                  entry.event_type == SPSC_EVT_STREAM_FIN) and \
                  entry.stream_ctx is not NULL:
                # Canonical RX path: bytes live on the per-stream ring
                # attached to the wrapper. Pop them here so callers see
                # the same `data` shape as before. The wrapper pointer
                # is also returned so the caller can advance MAX_STREAM_DATA
                # via picoquic_open_flow_control after consuming.
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
                            data = memoryview(
                                StreamChunk._wrap(buf, length))
                            # Atomic advance — picoquic worker reads this
                            # in its next stream_data callback to decide
                            # when to extend MAX_STREAM_DATA. No SPSC
                            # dispatch, no wake_up, no thread crossing.
                            aiopquic_stream_ctx_rx_consumed_add(
                                sc, <uint64_t>length)

            events.append((
                entry.event_type,
                entry.stream_id,
                data,
                entry.is_fin,
                entry.error_code,
                <uintptr_t>entry.cnx,
                <uintptr_t>entry.stream_ctx,
            ))
            spsc_ring_pop(self._ctx.rx_ring)

        return events

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
        if ret != 0:
            raise BufferError("TX ring buffer is full")

    def start(self, int port=0, cert_file=None, key_file=None,
              alpn=None, bint is_client=True, uint64_t idle_timeout_ms=30000,
              uint32_t max_datagram_frame_size=0,
              wt_path=None, debug_log=None, keylog_filename=None,
              uint32_t rx_ring_cap=0, congestion_control_algorithm=None):
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
        if debug_log is not None:
            picoquic_set_log_level(self._quic, 1)
            _b_log = debug_log.encode() if isinstance(debug_log, str) else debug_log
            picoquic_set_textlog(self._quic, _b_log)

        cdef bytes _b_keylog
        if keylog_filename is not None:
            _b_keylog = (keylog_filename.encode()
                         if isinstance(keylog_filename, str)
                         else keylog_filename)
            picoquic_enable_sslkeylog(self._quic, 1)
            picoquic_set_key_log_file(self._quic, _b_keylog)

        if is_client:
            picoquic_set_null_verifier(self._quic)

        if wt_path is not None and not is_client:
            picowt_set_default_transport_parameters(self._quic)

        if idle_timeout_ms > 0:
            picoquic_set_default_idle_timeout(self._quic, idle_timeout_ms)

        # Set datagram transport parameter if requested
        cdef picoquic_tp_t tp
        cdef const picoquic_tp_t* cur_tp
        if max_datagram_frame_size > 0:
            cur_tp = picoquic_get_default_tp(self._quic)
            if cur_tp != NULL:
                tp = cur_tp[0]
                tp.max_datagram_frame_size = max_datagram_frame_size
                picoquic_set_default_tp(self._quic, &tp)

        # Configure packet loop parameters (must persist — picoquic stores a pointer)
        self._param.local_port = <unsigned short>port
        self._param.local_af = AF_INET
        self._param.dest_if = 0
        self._param.socket_buffer_size = 0
        self._param.do_not_use_gso = 0
        self._param.extra_socket_required = 0
        self._param.prefer_extra_socket = 0

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
        """Signal the network thread to process TX ring entries."""
        if self._thread_ctx is NULL:
            raise RuntimeError("Network thread not started")
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

    def push_open(self, str host, int port, str path, str sni):
        """Push TX_WT_OPEN to the picoquic thread. Build the params
        payload (addr + sni + path), then push entry."""
        cdef sockaddr_in addr
        memset(&addr, 0, sizeof(addr))
        addr.sin_family = AF_INET
        addr.sin_port = htons(<unsigned short>port)
        cdef bytes b_host = host.encode()
        if inet_pton(AF_INET, b_host, &addr.sin_addr) != 1:
            raise ValueError(f"Invalid IPv4 address: {host}")

        cdef bytes b_sni = sni.encode()
        cdef bytes b_path = path.encode()
        cdef uint32_t sni_len = <uint32_t>len(b_sni)
        cdef uint32_t path_len = <uint32_t>len(b_path)
        cdef uint32_t hdr_size = sizeof(aiopquic_wt_open_params_t)
        cdef uint32_t total = hdr_size + sni_len + path_len

        cdef bytearray buf = bytearray(total)
        cdef uint8_t* p = <uint8_t*><char*>buf
        # Layout: addr | sni_len(2) | path_len(2) | sni | path
        memcpy(p, &addr, sizeof(sockaddr_in))
        # sni_len + path_len native-uint16; aiopquic_wt_open_params_t
        # struct on the C side uses host byte order for these fields.
        cdef uint16_t* hdr_lens = <uint16_t*>(p + sizeof(sockaddr_in))
        hdr_lens[0] = <uint16_t>sni_len
        hdr_lens[1] = <uint16_t>path_len
        memcpy(p + hdr_size, <const uint8_t*>b_sni, sni_len)
        memcpy(p + hdr_size + sni_len, <const uint8_t*>b_path, path_len)

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

    def push_stream_data(self, uint64_t stream_id, bytes data,
                          bint end_stream=False):
        """Send bytes on a WT data stream.

        WT data streams are real QUIC streams once created, so we
        reuse the raw-QUIC TX_STREAM_DATA / TX_STREAM_FIN path with
        the session's underlying cnx pointer. This keeps cnx_ptr
        encapsulated inside the cdef class instead of leaking to
        the Python WebTransportClient."""
        cdef picoquic_cnx_t* cnx = NULL
        if self._wt is not NULL:
            cnx = <picoquic_cnx_t*>self._wt.cnx
        if cnx is NULL:
            raise RuntimeError("WT session not yet open")

        cdef spsc_entry_t entry
        memset(&entry, 0, sizeof(entry))
        entry.event_type = (SPSC_EVT_TX_STREAM_FIN if end_stream
                             else SPSC_EVT_TX_STREAM_DATA)
        entry.stream_id = stream_id
        entry.cnx = <void*>cnx
        cdef const uint8_t* data_ptr = NULL
        cdef uint32_t data_len = 0
        if data:
            data_ptr = <const uint8_t*>data
            data_len = <uint32_t>len(data)
        cdef int ret = spsc_ring_push(
            self._transport._ctx.tx_ring, &entry, data_ptr, data_len)
        if ret != 0:
            raise BufferError("TX ring full (WT stream data)")
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


# Note: set_max_stream_data and enable_app_flow_control are issued from
# the connection.py drain path via push_tx(SPSC_EVT_TX_OPEN_FLOW_CONTROL,
# error_code=new_max) and push_tx(SPSC_EVT_TX_SET_APP_FLOW_CONTROL,
# is_fin=1). No direct Python wrappers needed — the existing push_tx
# API already delivers events to the picoquic worker thread.

