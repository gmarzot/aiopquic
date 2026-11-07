/*
 * Client network loop with many UDP sockets on one thread.
 *
 * picoquic's packet loop holds at most PICOQUIC_PACKET_LOOP_SOCKETS_MAX
 * sockets. A load client needs a source port per connection, so a relay that
 * hashes 4-tuples to worker threads spreads them as it would real viewers.
 *
 * picoquic finds a received packet's connection by its connection ID, so any
 * socket can deliver to any connection. Each connection sends from one socket,
 * which is all the peer sees. Sockets open on first use.
 *
 * It enters through picoquic_start_custom_network_thread, so wake-up and
 * shutdown are picoquic's, and it drives aiopquic_loop_cb as the packet loop
 * does. Linux only; elsewhere the packet loop runs and connections share a socket.
 */
#ifndef AIOPQUIC_CLIENT_LOOP_H
#define AIOPQUIC_CLIENT_LOOP_H

#ifdef __linux__

#include <netinet/udp.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include "picosocks.h"

#define AIOPQUIC_CL_EVENTS 64
#define AIOPQUIC_CL_RECV_BATCH 8
#define AIOPQUIC_CL_PKT_MAX 1536
#define AIOPQUIC_CL_CMSG 256
#define AIOPQUIC_CL_IMMEDIATE_MAX 32
#define AIOPQUIC_CL_SEND_BUFFER 0xFFFF
#define AIOPQUIC_CL_DELAY_MAX_US 10000000

typedef struct {
    struct mmsghdr msgs[AIOPQUIC_CL_RECV_BATCH];
    struct iovec iov[AIOPQUIC_CL_RECV_BATCH];
    struct sockaddr_storage names[AIOPQUIC_CL_RECV_BATCH];
    char cmsg[AIOPQUIC_CL_RECV_BATCH][AIOPQUIC_CL_CMSG];
    uint8_t buf[AIOPQUIC_CL_RECV_BATCH][AIOPQUIC_CL_PKT_MAX];
} aiopquic_cl_recv_t;

/* Connection to socket slot. Entries are never removed: a freed connection's
 * address only ever comes back as a key, and it keeps a valid slot. */
typedef struct {
    const void* cnx;
    uint32_t slot;
} aiopquic_cl_entry_t;

typedef struct {
    picoquic_packet_loop_param_t* param;
    uint32_t nb_slots;
    uint8_t ecn_value;
    int epfd;
    int use_pwait2;
    /* Lazily allocated arrays of nb_slots, for AF_INET and AF_INET6. */
    picoquic_socket_ctx_t* sockets[2];
    aiopquic_cl_entry_t* entries;
    size_t entries_cap;
    size_t entries_count;
    uint32_t next_slot;
    const void* last_cnx;
    uint32_t last_slot;
} aiopquic_cl_t;

static size_t aiopquic_cl_hash(const void* cnx, size_t mask) {
    return (size_t)(((uint64_t)(uintptr_t)cnx * 0x9E3779B97F4A7C15ULL) >> 32) & mask;
}

static int aiopquic_cl_entries_grow(aiopquic_cl_t* cl) {
    size_t cap = cl->entries_cap ? cl->entries_cap * 2 : 256;
    aiopquic_cl_entry_t* grown = (aiopquic_cl_entry_t*)calloc(cap, sizeof(*grown));
    if (grown == NULL) {
        return -1;
    }
    for (size_t i = 0; i < cl->entries_cap; i++) {
        if (cl->entries[i].cnx != NULL) {
            size_t at = aiopquic_cl_hash(cl->entries[i].cnx, cap - 1);
            while (grown[at].cnx != NULL) {
                at = (at + 1) & (cap - 1);
            }
            grown[at] = cl->entries[i];
        }
    }
    free(cl->entries);
    cl->entries = grown;
    cl->entries_cap = cap;
    return 0;
}

/* The connection's slot, handed out in turn on first sight. */
static uint32_t aiopquic_cl_slot(aiopquic_cl_t* cl, const void* cnx) {
    if (cnx == NULL) {
        return 0;
    }
    if (cnx == cl->last_cnx) {
        return cl->last_slot;
    }
    if ((cl->entries_count + 1) * 2 > cl->entries_cap
            && aiopquic_cl_entries_grow(cl) != 0) {
        return 0;
    }
    size_t mask = cl->entries_cap - 1;
    size_t at = aiopquic_cl_hash(cnx, mask);
    while (cl->entries[at].cnx != NULL && cl->entries[at].cnx != cnx) {
        at = (at + 1) & mask;
    }
    if (cl->entries[at].cnx == NULL) {
        cl->entries[at].cnx = cnx;
        cl->entries[at].slot = cl->next_slot++ % cl->nb_slots;
        cl->entries_count++;
    }
    cl->last_cnx = cnx;
    cl->last_slot = cl->entries[at].slot;
    return cl->last_slot;
}

/* The slot's socket for af, opened if needed. NULL if it cannot be opened. */
static picoquic_socket_ctx_t* aiopquic_cl_socket(aiopquic_cl_t* cl, int af, uint32_t slot) {
    int family = (af == AF_INET6) ? 1 : 0;
    if (cl->sockets[family] == NULL) {
        cl->sockets[family] = (picoquic_socket_ctx_t*)calloc(
            cl->nb_slots, sizeof(picoquic_socket_ctx_t));
        if (cl->sockets[family] == NULL) {
            return NULL;
        }
    }
    picoquic_socket_ctx_t* s = &cl->sockets[family][slot];
    if (s->port == 0) {
        /* One ephemeral socket of this family. */
        picoquic_packet_loop_param_t param = *cl->param;
        struct epoll_event ev;
        param.local_af = af;
        param.local_port = 0;
        param.public_port = 0;
        param.extra_socket_required = 0;
        param.prefer_extra_socket = 0;
        if (picoquic_packet_loop_open_sockets(&param, s, cl->ecn_value) != 1) {
            return NULL;
        }
        memset(&ev, 0, sizeof(ev));
        ev.events = EPOLLIN;
        ev.data.ptr = s;
        if (epoll_ctl(cl->epfd, EPOLL_CTL_ADD, s->fd, &ev) != 0) {
            picoquic_packet_loop_close_socket(s);
            memset(s, 0, sizeof(*s));
            return NULL;
        }
    }
    return s;
}

/* Waits up to delta_us, in microseconds as the patched packet loop does. A
 * delay of zero or less only reads what is ready. */
static int aiopquic_cl_wait(aiopquic_cl_t* cl, struct epoll_event* events, int64_t delta_us) {
    struct timespec ts = { 0, 0 };
    if (delta_us > 0) {
        ts.tv_sec = (time_t)(delta_us / 1000000);
        ts.tv_nsec = (long)(delta_us % 1000000) * 1000;
    }
#ifdef SYS_epoll_pwait2
    if (cl->use_pwait2) {
        int n = (int)syscall(SYS_epoll_pwait2, cl->epfd, events, AIOPQUIC_CL_EVENTS, &ts, NULL, 0);
        if (n >= 0 || errno == EINTR) {
            return n;
        }
        /* Older kernel, or a filter refusing it. */
        cl->use_pwait2 = 0;
    }
#endif
    {
        struct pollfd pfd = { cl->epfd, POLLIN, 0 };
        int ready = ppoll(&pfd, 1, &ts, NULL);
        if (ready <= 0) {
            return ready;
        }
    }
    return epoll_wait(cl->epfd, events, AIOPQUIC_CL_EVENTS, 0);
}

/* Reads one batch from a socket; a level-triggered wait reports any remainder. */
static int aiopquic_cl_receive(picoquic_quic_t* quic, picoquic_socket_ctx_t* s,
    aiopquic_cl_recv_t* r, picoquic_cnx_t** last_cnx, uint64_t current_time)
{
    int n;
    for (int i = 0; i < AIOPQUIC_CL_RECV_BATCH; i++) {
        r->msgs[i].msg_hdr.msg_namelen = sizeof(r->names[i]);
        r->msgs[i].msg_hdr.msg_controllen = sizeof(r->cmsg[i]);
        r->msgs[i].msg_hdr.msg_flags = 0;
    }
    n = recvmmsg(s->fd, r->msgs, AIOPQUIC_CL_RECV_BATCH, MSG_DONTWAIT, NULL);
    for (int i = 0; i < n; i++) {
        struct sockaddr_storage addr_to;
        int if_index = 0;
        unsigned char ecn = 0;
        addr_to.ss_family = AF_UNSPEC;
        picoquic_socks_cmsg_parse(&r->msgs[i].msg_hdr, &addr_to, &if_index, &ecn, NULL);
        if (addr_to.ss_family == AF_INET6) {
            ((struct sockaddr_in6*)&addr_to)->sin6_port = s->n_port;
        } else if (addr_to.ss_family == AF_INET) {
            ((struct sockaddr_in*)&addr_to)->sin_port = s->n_port;
        }
        /* As in the packet loop, a packet picoquic rejects does not end the loop. */
        (void)picoquic_incoming_packet_ex(quic, r->buf[i], r->msgs[i].msg_len,
            (struct sockaddr*)&r->names[i], (struct sockaddr*)&addr_to, if_index, ecn,
            last_cnx, current_time);
    }
    return (n > 0) ? n : 0;
}

static void* aiopquic_client_loop(void* v_ctx) {
    picoquic_network_thread_ctx_t* thread_ctx = (picoquic_network_thread_ctx_t*)v_ctx;
    picoquic_quic_t* quic = thread_ctx->quic;
    picoquic_packet_loop_param_t* param = thread_ctx->param;
    picoquic_packet_loop_cb_fn loop_callback = thread_ctx->loop_callback;
    void* loop_ctx = thread_ctx->loop_callback_ctx;
    aiopquic_cl_t cl;
    aiopquic_cl_recv_t* recv_batch = NULL;
    uint8_t* send_buffer = NULL;
    size_t send_msg_size = 0;
    size_t* send_msg_ptr = NULL;
    size_t send_batch_max = (param->send_batch_max == 0) ?
        PICOQUIC_PACKET_LOOP_SEND_MAX : param->send_batch_max;
    picoquic_packet_loop_options_t options;
    picoquic_cnx_t* last_cnx = NULL;
    picoquic_connection_id_t log_cid;
    struct epoll_event events[AIOPQUIC_CL_EVENTS];
    int immediate = 0;
    int nb_immediate = 0;
    int ret = 0;

    PICOQUIC_THREAD_SET_CHECK(quic);
    memset(&cl, 0, sizeof(cl));
    memset(&options, 0, sizeof(options));
    cl.param = param;
    cl.nb_slots = ((aiopquic_ctx_t*)loop_ctx)->client_sockets;
    cl.ecn_value = (quic->default_congestion_alg == NULL) ? 0 : quic->default_congestion_alg->ecn_mark;
    cl.use_pwait2 = 1;
    cl.epfd = epoll_create1(EPOLL_CLOEXEC);
    recv_batch = (aiopquic_cl_recv_t*)calloc(1, sizeof(*recv_batch));
    send_buffer = (uint8_t*)malloc(AIOPQUIC_CL_SEND_BUFFER);
    if (cl.epfd < 0 || recv_batch == NULL || send_buffer == NULL) {
        ret = -1;
    }
    if (ret == 0) {
        struct epoll_event ev;
        memset(&ev, 0, sizeof(ev));
        ev.events = EPOLLIN;
        ev.data.ptr = NULL;
        ret = epoll_ctl(cl.epfd, EPOLL_CTL_ADD, thread_ctx->wake_up_pipe_fd[0], &ev);
    }
    if (ret == 0) {
        for (int i = 0; i < AIOPQUIC_CL_RECV_BATCH; i++) {
            recv_batch->iov[i].iov_base = recv_batch->buf[i];
            recv_batch->iov[i].iov_len = AIOPQUIC_CL_PKT_MAX;
            recv_batch->msgs[i].msg_hdr.msg_name = &recv_batch->names[i];
            recv_batch->msgs[i].msg_hdr.msg_iov = &recv_batch->iov[i];
            recv_batch->msgs[i].msg_hdr.msg_iovlen = 1;
            recv_batch->msgs[i].msg_hdr.msg_control = recv_batch->cmsg[i];
        }
#ifdef UDP_SEGMENT
        if (!param->do_not_use_gso) {
            send_msg_ptr = &send_msg_size;
        }
#endif
        ret = loop_callback(quic, picoquic_packet_loop_ready, loop_ctx, &options);
    }
    if (ret == 0) {
        thread_ctx->thread_is_ready = 1;
    }

    while (ret == 0 && !thread_ctx->thread_should_close) {
        uint64_t current_time = picoquic_current_time();
        int64_t delta_t = 0;
        int nb_events;
        int woken = 0;
        int nb_received = 0;

        if (!immediate) {
            nb_immediate = 0;
            delta_t = picoquic_get_next_wake_delay(quic, current_time, AIOPQUIC_CL_DELAY_MAX_US);
            if (options.do_time_check) {
                packet_loop_time_check_arg_t time_check;
                time_check.current_time = current_time;
                time_check.delta_t = delta_t;
                ret = loop_callback(quic, picoquic_packet_loop_time_check, loop_ctx, &time_check);
                if (ret != 0) {
                    break;
                }
                if (time_check.delta_t < delta_t) {
                    delta_t = time_check.delta_t;
                }
            }
        }
        immediate = 0;

        nb_events = aiopquic_cl_wait(&cl, events, delta_t);
        current_time = picoquic_current_time();
        if (nb_events < 0) {
            if (errno == EINTR) {
                continue;
            }
            ret = thread_ctx->thread_should_close ? PICOQUIC_NO_ERROR_TERMINATE_PACKET_LOOP : -1;
            break;
        }
        for (int i = 0; i < nb_events; i++) {
            if (events[i].data.ptr == NULL) {
                uint8_t drain[8];
                if (read(thread_ctx->wake_up_pipe_fd[0], drain, sizeof(drain)) > 0) {
                    woken = 1;
                }
            } else {
                nb_received += aiopquic_cl_receive(quic,
                    (picoquic_socket_ctx_t*)events[i].data.ptr, recv_batch, &last_cnx, current_time);
            }
        }
        if (woken) {
            ret = loop_callback(quic, picoquic_packet_loop_wake_up, loop_ctx, NULL);
        }
        if (ret == 0 && nb_received > 0) {
            size_t received = (size_t)nb_received;
            ret = loop_callback(quic, picoquic_packet_loop_after_receive, loop_ctx, &received);
        }
        /* More sockets may be ready: read them before sending, within a limit. */
        if (ret == 0 && nb_events == AIOPQUIC_CL_EVENTS
                && ++nb_immediate < AIOPQUIC_CL_IMMEDIATE_MAX) {
            immediate = 1;
            continue;
        }

        {
            size_t bytes_sent = 0;
            size_t nb_packets_sent = 0;
            while (ret == 0 && nb_packets_sent < send_batch_max) {
                struct sockaddr_storage peer_addr;
                struct sockaddr_storage local_addr;
                int if_index = 0;
                size_t send_length = 0;
                picoquic_socket_ctx_t* s;

                memset(&local_addr, 0, sizeof(local_addr));
                ret = picoquic_prepare_next_packet_ex(quic, current_time,
                    send_buffer, AIOPQUIC_CL_SEND_BUFFER, &send_length,
                    &peer_addr, &local_addr, &if_index, &log_cid, &last_cnx, send_msg_ptr);
                if (ret != 0 || send_length == 0) {
                    break;
                }
                nb_packets_sent += (send_msg_size == 0) ? 1 :
                    (send_length + send_msg_size - 1) / send_msg_size;
                bytes_sent += send_length;
                if (send_length > param->send_length_max) {
                    param->send_length_max = send_length;
                }
                s = aiopquic_cl_socket(&cl, peer_addr.ss_family, aiopquic_cl_slot(&cl, last_cnx));
                if (s == NULL) {
                    /* Out of descriptors: share the first socket. */
                    s = aiopquic_cl_socket(&cl, peer_addr.ss_family, 0);
                }
                if (s == NULL) {
                    ret = -1;
                    break;
                }
                picoquic_packet_loop_set_send_source(s, &local_addr);
                ret = picoquic_packet_loop_do_udp_send(quic, last_cnx, s->fd, param,
                    send_buffer, send_length, &peer_addr, &local_addr, if_index,
                    send_msg_size, &send_msg_ptr, &log_cid, current_time, NULL);
            }
            if (ret == 0) {
                ret = loop_callback(quic, picoquic_packet_loop_after_send, loop_ctx, &bytes_sent);
            }
        }
    }

    thread_ctx->thread_is_ready = 0;
    if (ret == PICOQUIC_NO_ERROR_TERMINATE_PACKET_LOOP) {
        ret = 0;
    }
    for (int family = 0; family < 2; family++) {
        if (cl.sockets[family] != NULL) {
            for (uint32_t i = 0; i < cl.nb_slots; i++) {
                if (cl.sockets[family][i].port != 0) {
                    picoquic_packet_loop_close_socket(&cl.sockets[family][i]);
                }
            }
            free(cl.sockets[family]);
        }
    }
    if (cl.epfd >= 0) {
        close(cl.epfd);
    }
    free(cl.entries);
    free(recv_batch);
    free(send_buffer);
    thread_ctx->return_code = ret;
    if (thread_ctx->is_threaded) {
        pthread_exit((void*)&thread_ctx->return_code);
    }
    return NULL;
}

/* picoquic passes its own loop as thread_fn; run this one instead. */
static int aiopquic_client_loop_thread_create(void** thread_id, picoquic_thread_fn thread_fn, void* arg) {
    (void)thread_fn;
    return picoquic_internal_thread_create(thread_id, aiopquic_client_loop, arg);
}

#endif /* __linux__ */

static picoquic_network_thread_ctx_t* aiopquic_start_client_loop(picoquic_quic_t* quic,
    picoquic_packet_loop_param_t* param, aiopquic_ctx_t* ctx, uint32_t nb_sockets, int* ret)
{
#ifdef __linux__
    ctx->client_sockets = nb_sockets;
    return picoquic_start_custom_network_thread(quic, param, aiopquic_client_loop_thread_create,
        NULL, NULL, NULL, aiopquic_loop_cb, ctx, ret);
#else
    (void)nb_sockets;
    return picoquic_start_network_thread(quic, param, aiopquic_loop_cb, ctx, ret);
#endif
}

#endif /* AIOPQUIC_CLIENT_LOOP_H */
