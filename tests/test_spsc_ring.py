"""Tests for the SPSC ring buffer via the Cython RingBuffer wrapper."""

import threading
import pytest


def test_import():
    """Verify the extension module can be imported."""
    from aiopquic._binding._transport import RingBuffer
    assert RingBuffer is not None


def test_create_ring():
    """Create a ring buffer with default settings."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer()
    assert ring.capacity == 4096
    assert ring.count == 0
    assert ring.empty is True


def test_create_ring_custom_size():
    """Create a ring with custom capacity (must be power of 2)."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=256)
    assert ring.capacity == 256
    assert ring.empty is True


def test_push_pop_no_data():
    """Push and pop an event with no payload."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    ring.push(event_type=6, stream_id=0)  # SPSC_EVT_READY
    assert ring.count == 1
    assert ring.empty is False

    result = ring.pop()
    assert result is not None
    event_type, stream_id, data, is_fin, error_code = result
    assert event_type == 6
    assert stream_id == 0
    assert data is None
    assert is_fin == 0

    assert ring.empty is True
    assert ring.pop() is None


def test_push_pop_with_data():
    """Push and pop an event with payload data."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    payload = b"Hello, QUIC!"
    ring.push(event_type=0, stream_id=42, data=payload)

    result = ring.pop()
    assert result is not None
    event_type, stream_id, data, is_fin, error_code = result
    assert event_type == 0  # SPSC_EVT_STREAM_DATA
    assert stream_id == 42
    assert data == payload


def test_push_pop_with_fin():
    """Push stream data with FIN flag."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    ring.push(event_type=1, stream_id=4, data=b"end", is_fin=1)

    result = ring.pop()
    event_type, stream_id, data, is_fin, error_code = result
    assert event_type == 1  # SPSC_EVT_STREAM_FIN
    assert is_fin == 1
    assert data == b"end"


def test_push_pop_with_error_code():
    """Push a close event with error code."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    ring.push(event_type=4, stream_id=0, error_code=0x100)

    result = ring.pop()
    event_type, stream_id, data, is_fin, error_code = result
    assert event_type == 4  # SPSC_EVT_CLOSE
    assert error_code == 0x100


def test_multiple_push_pop():
    """Push multiple entries and pop them in FIFO order."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    for i in range(10):
        ring.push(event_type=0, stream_id=i, data=f"msg-{i}".encode())

    assert ring.count == 10

    for i in range(10):
        result = ring.pop()
        assert result is not None
        _, stream_id, data, _, _ = result
        assert stream_id == i
        assert data == f"msg-{i}".encode()

    assert ring.empty is True


def test_ring_full():
    """Ring should raise BufferError when full."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=4)

    for i in range(4):
        ring.push(event_type=0, stream_id=i)

    with pytest.raises(BufferError):
        ring.push(event_type=0, stream_id=99)


def test_ring_wrap_around():
    """Fill, drain, refill — ensure wrap-around works."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=4)

    # Fill
    for i in range(4):
        ring.push(event_type=0, stream_id=i, data=b"x" * 100)

    # Drain
    for i in range(4):
        result = ring.pop()
        assert result[1] == i

    # Refill (head/tail have wrapped)
    for i in range(4):
        ring.push(event_type=0, stream_id=100 + i, data=b"y" * 100)

    for i in range(4):
        result = ring.pop()
        assert result[1] == 100 + i


def test_large_data():
    """Push entries with large payloads."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=16)

    big_data = b"\xab" * 65536
    ring.push(event_type=0, stream_id=0, data=big_data)

    result = ring.pop()
    assert result[2] == big_data


def test_threaded_producer_consumer():
    """Basic thread safety: one producer thread, one consumer thread."""
    from aiopquic._binding._transport import RingBuffer
    ring = RingBuffer(capacity=1024)

    num_messages = 10000
    received = []
    errors = []

    def producer():
        try:
            for i in range(num_messages):
                data = f"msg-{i}".encode()
                # Spin until we can push (ring might be full)
                while True:
                    try:
                        ring.push(event_type=0, stream_id=i, data=data)
                        break
                    except BufferError:
                        pass  # retry
        except Exception as e:
            errors.append(e)

    def consumer():
        try:
            count = 0
            while count < num_messages:
                result = ring.pop()
                if result is not None:
                    received.append(result)
                    count += 1
        except Exception as e:
            errors.append(e)

    t_prod = threading.Thread(target=producer)
    t_cons = threading.Thread(target=consumer)

    t_cons.start()
    t_prod.start()

    t_prod.join(timeout=10)
    t_cons.join(timeout=10)

    assert not errors, f"Errors in threads: {errors}"
    assert len(received) == num_messages

    # Verify FIFO order
    for i, result in enumerate(received):
        _, stream_id, data, _, _ = result
        assert stream_id == i
        assert data == f"msg-{i}".encode()


def test_transport_context_create():
    """Create and destroy a TransportContext."""
    from aiopquic._binding._transport import TransportContext
    ctx = TransportContext()
    assert ctx.eventfd >= 0  # valid fd on Linux
    assert ctx.rx_count == 0
    del ctx  # should not crash
