# cython: language_level=3
# Cython declarations for picoquic public API (subset needed by aiopquic)

from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t, int64_t
from libc.string cimport memcpy
from posix.types cimport socklen_t

cdef extern from "<sys/socket.h>":
    ctypedef struct sockaddr:
        pass
    ctypedef struct sockaddr_storage:
        pass

cdef extern from "picoquic.h":
    # Opaque types
    ctypedef struct picoquic_quic_t:
        pass
    ctypedef struct picoquic_cnx_t:
        pass
    ctypedef struct picoquic_path_t:
        pass

    # Connection states
    ctypedef enum picoquic_state_enum:
        picoquic_state_client_init
        picoquic_state_ready
        picoquic_state_disconnecting
        picoquic_state_closing
        picoquic_state_draining
        picoquic_state_disconnected

    # Callback events
    ctypedef enum picoquic_call_back_event_t:
        picoquic_callback_stream_data
        picoquic_callback_stream_fin
        picoquic_callback_stream_reset
        picoquic_callback_stop_sending
        picoquic_callback_stateless_reset
        picoquic_callback_close
        picoquic_callback_application_close
        picoquic_callback_stream_gap
        picoquic_callback_prepare_to_send
        picoquic_callback_almost_ready
        picoquic_callback_ready
        picoquic_callback_datagram
        picoquic_callback_version_negotiation
        picoquic_callback_request_alpn_list
        picoquic_callback_set_alpn
        picoquic_callback_pacing_changed
        picoquic_callback_prepare_datagram
        picoquic_callback_datagram_acked
        picoquic_callback_datagram_lost
        picoquic_callback_datagram_spurious
        picoquic_callback_path_available
        picoquic_callback_path_suspended
        picoquic_callback_path_deleted
        picoquic_callback_path_quality_changed
        picoquic_callback_path_address_observed
        picoquic_callback_app_wakeup

    # Connection ID
    ctypedef struct picoquic_connection_id_t:
        uint8_t id[20]
        uint8_t id_len

    # Transport parameters
    ctypedef struct picoquic_tp_t:
        uint64_t initial_max_stream_data_bidi_local
        uint64_t initial_max_stream_data_bidi_remote
        uint64_t initial_max_stream_data_uni
        uint64_t initial_max_data
        uint64_t initial_max_stream_id_bidir
        uint64_t initial_max_stream_id_unidir
        uint64_t max_idle_timeout
        uint32_t max_packet_size
        uint32_t max_datagram_frame_size

    # Callback function type
    ctypedef int (*picoquic_stream_data_cb_fn)(
        picoquic_cnx_t* cnx,
        uint64_t stream_id,
        uint8_t* bytes,
        size_t length,
        picoquic_call_back_event_t fin_or_event,
        void* callback_ctx,
        void* stream_ctx)

    ctypedef void (*picoquic_connection_id_cb_fn)(
        picoquic_quic_t* quic,
        picoquic_connection_id_t cnx_id_local,
        picoquic_connection_id_t cnx_id_remote,
        void* cnx_id_cb_data,
        picoquic_connection_id_t* cnx_id_returned)

    # Context lifecycle
    picoquic_quic_t* picoquic_create(
        uint32_t max_nb_connections,
        const char* cert_file_name,
        const char* key_file_name,
        const char* cert_root_file_name,
        const char* default_alpn,
        picoquic_stream_data_cb_fn default_callback_fn,
        void* default_callback_ctx,
        picoquic_connection_id_cb_fn cnx_id_callback,
        void* cnx_id_callback_data,
        uint8_t* reset_seed,
        uint64_t current_time,
        uint64_t* p_simulated_time,
        const char* ticket_file_name,
        const uint8_t* ticket_encryption_key,
        size_t ticket_encryption_key_length)

    void picoquic_free(picoquic_quic_t* quic)

    # Connection lifecycle
    picoquic_cnx_t* picoquic_create_cnx(
        picoquic_quic_t* quic,
        picoquic_connection_id_t initial_cnx_id,
        picoquic_connection_id_t remote_cnx_id,
        const sockaddr* addr_to,
        uint64_t start_time,
        uint32_t preferred_version,
        const char* sni,
        const char* alpn,
        char client_mode)

    picoquic_cnx_t* picoquic_create_client_cnx(
        picoquic_quic_t* quic,
        sockaddr* addr,
        uint64_t start_time,
        uint32_t preferred_version,
        const char* sni,
        const char* alpn,
        picoquic_stream_data_cb_fn callback_fn,
        void* callback_ctx)

    int picoquic_start_client_cnx(picoquic_cnx_t* cnx)
    int picoquic_close(picoquic_cnx_t* cnx, uint64_t application_reason_code)
    void picoquic_close_immediate(picoquic_cnx_t* cnx)
    void picoquic_delete_cnx(picoquic_cnx_t* cnx)

    # State queries
    picoquic_state_enum picoquic_get_cnx_state(picoquic_cnx_t* cnx)
    picoquic_quic_t* picoquic_get_quic_ctx(picoquic_cnx_t* cnx)
    uint64_t picoquic_current_time()
    uint64_t picoquic_get_quic_time(picoquic_quic_t* quic)

    # Stream operations
    int picoquic_add_to_stream(picoquic_cnx_t* cnx, uint64_t stream_id,
                                const uint8_t* data, size_t length, int set_fin)
    int picoquic_mark_active_stream(picoquic_cnx_t* cnx, uint64_t stream_id,
                                     int is_active, void* v_stream_ctx)
    uint8_t* picoquic_provide_stream_data_buffer(void* context, size_t nb_bytes,
                                                   int is_fin, int is_still_active)
    int picoquic_set_app_stream_ctx(picoquic_cnx_t* cnx, uint64_t stream_id,
                                     void* app_stream_ctx)
    void picoquic_unlink_app_stream_ctx(picoquic_cnx_t* cnx, uint64_t stream_id)
    int picoquic_reset_stream(picoquic_cnx_t* cnx, uint64_t stream_id,
                               uint64_t local_stream_error)
    int picoquic_stop_sending(picoquic_cnx_t* cnx, uint64_t stream_id,
                               uint64_t local_stream_error)
    int picoquic_discard_stream(picoquic_cnx_t* cnx, uint64_t stream_id,
                                 uint16_t local_stream_error)
    uint64_t picoquic_get_next_local_stream_id(picoquic_cnx_t* cnx, int is_unidir)

    # Datagram operations
    int picoquic_queue_datagram_frame(picoquic_cnx_t* cnx, size_t length,
                                       const uint8_t* bytes)
    int picoquic_mark_datagram_ready(picoquic_cnx_t* cnx, int is_ready)

    # Callback management
    void picoquic_set_callback(picoquic_cnx_t* cnx,
                                picoquic_stream_data_cb_fn callback_fn,
                                void* callback_ctx)

    # Configuration
    int picoquic_set_default_tp(picoquic_quic_t* quic, picoquic_tp_t* tp)
    void picoquic_set_default_idle_timeout(picoquic_quic_t* quic,
                                            uint64_t idle_timeout_ms)
    void picoquic_set_null_verifier(picoquic_quic_t* quic)
    void picoquic_set_log_level(picoquic_quic_t* quic, int log_level)


cdef extern from "picoquic_packet_loop.h":
    ctypedef struct picoquic_packet_loop_param_t:
        uint16_t local_port
        int local_af
        int dest_if
        int socket_buffer_size
        int do_not_use_gso
        int extra_socket_required
        int prefer_extra_socket

    ctypedef enum picoquic_packet_loop_cb_enum:
        picoquic_packet_loop_ready
        picoquic_packet_loop_after_receive
        picoquic_packet_loop_after_send
        picoquic_packet_loop_port_update
        picoquic_packet_loop_time_check
        picoquic_packet_loop_system_call_duration
        picoquic_packet_loop_wake_up

    ctypedef int (*picoquic_packet_loop_cb_fn)(
        picoquic_quic_t* quic,
        picoquic_packet_loop_cb_enum cb_mode,
        void* callback_ctx,
        void* callback_argv)

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
