# Issue: First connection created during `picoquic_packet_loop_wake_up` callback never completes handshake

## Setup

We use `picoquic_start_network_thread()` with a custom `picoquic_packet_loop_cb_fn`. When the application thread wants to create a new client connection, it signals the network thread via `picoquic_wake_up_network_thread()`. In our `picoquic_packet_loop_wake_up` callback, we call `picoquic_create_client_cnx()` (which internally calls `picoquic_start_client_cnx()`). This is analogous to what `thread_tester` does.

## Symptom

The very first client connection created this way never completes its TLS handshake. The server never receives the Initial packet. Subsequent connections created the same way work fine.

## Suspected root cause

In `sockloop.c` (~line 869), the `wake_up` branch and the send branch are mutually exclusive:

```c
else if (bytes_recv == 0 && is_wake_up_event) {
    ret = loop_callback(quic, picoquic_packet_loop_wake_up, ...);
}
else {
    // ... send path (picoquic_prepare_next_packet_ex) is here
}
```

When `picoquic_create_client_cnx()` is called inside the wake_up callback, picoquic internally queues the Initial packet. But because the send path is in the `else` branch, `picoquic_prepare_next_packet_ex()` is never called in that iteration — the Initial is never sent.

On the **next** iteration, `picoquic_get_next_wake_delay()` should return 0 (connection has pending data), select should return immediately, and the send path should run. This does appear to work for the 2nd+ connections, but the first connection in the process consistently stalls.

## Reproducer

Similar to `thread_tester` (pure C, no Python needed):

1. Create a picoquic server context, start its network thread
2. Create a picoquic client context, start its network thread
3. In the client's wake_up callback, call `picoquic_create_client_cnx()`
4. Signal with `picoquic_wake_up_network_thread()`
5. The handshake never completes

## Possible fix

After the wake_up callback returns, fall through to the send path instead of skipping it:

```c
if (bytes_recv == 0 && is_wake_up_event) {
    ret = loop_callback(quic, picoquic_packet_loop_wake_up, ...);
}
if (ret == 0 && bytes_recv >= 0) {
    // send path (picoquic_prepare_next_packet_ex)
}
```

Or more simply: remove the `else` so both wake_up processing and send always run in the same iteration.

## Environment

- picoquic: current master (vendored copy)
- Linux 6.6.87 (WSL2)
- Observed via aiopquic (Cython/picoquic binding), but issue is in sockloop.c
