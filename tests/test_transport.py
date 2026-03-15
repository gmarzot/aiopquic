"""Tests for TransportContext lifecycle (picoquic + network thread)."""

import time
import pytest
from aiopquic._binding._transport import TransportContext


class TestTransportLifecycle:
    """Test start/stop of the picoquic network thread."""

    def test_start_stop_client(self):
        """Start as a client (no certs) and cleanly stop."""
        ctx = TransportContext()
        assert not ctx.started

        ctx.start(alpn="h3")
        assert ctx.started

        # Wait briefly for the network thread to become ready
        for _ in range(100):
            if ctx.thread_ready:
                break
            time.sleep(0.01)
        assert ctx.thread_ready

        ctx.stop()
        assert not ctx.started

    def test_double_start_raises(self):
        """Starting twice should raise RuntimeError."""
        ctx = TransportContext()
        ctx.start(alpn="h3")
        with pytest.raises(RuntimeError, match="already started"):
            ctx.start(alpn="h3")
        ctx.stop()

    def test_stop_before_start(self):
        """Stopping before starting should be a no-op."""
        ctx = TransportContext()
        ctx.stop()  # Should not raise
        assert not ctx.started

    def test_dealloc_stops_thread(self):
        """Garbage collecting a started context should not crash."""
        ctx = TransportContext()
        ctx.start(alpn="h3")
        # Wait for thread to be ready
        for _ in range(100):
            if ctx.thread_ready:
                break
            time.sleep(0.01)
        # Drop reference — __dealloc__ should clean up
        del ctx

    def test_eventfd_valid(self):
        """eventfd should be a valid file descriptor."""
        ctx = TransportContext()
        assert ctx.eventfd >= 0
        ctx.start(alpn="h3")
        # eventfd should still be valid after start
        assert ctx.eventfd >= 0
        ctx.stop()

    def test_wake_up_without_start_raises(self):
        """wake_up before start should raise RuntimeError."""
        ctx = TransportContext()
        with pytest.raises(RuntimeError, match="not started"):
            ctx.wake_up()

    def test_wake_up_after_start(self):
        """wake_up should succeed when the thread is running."""
        ctx = TransportContext()
        ctx.start(alpn="h3")
        for _ in range(100):
            if ctx.thread_ready:
                break
            time.sleep(0.01)
        ctx.wake_up()  # Should not raise
        ctx.stop()

    def test_rx_ring_receives_ready_event(self):
        """The network thread should push a READY event to RX ring."""
        ctx = TransportContext()
        ctx.start(alpn="h3")

        # Wait for thread ready
        for _ in range(100):
            if ctx.thread_ready:
                break
            time.sleep(0.01)
        assert ctx.thread_ready

        # Give a moment for the READY event to propagate
        time.sleep(0.05)

        events = ctx.drain_rx()
        # Should have at least one event (READY=6)
        ready_events = [e for e in events if e[0] == 6]  # SPSC_EVT_READY
        assert len(ready_events) >= 1, f"Expected READY event, got: {events}"

        ctx.stop()

    def test_create_connection_before_start_raises(self):
        """Creating a connection before start should raise."""
        ctx = TransportContext()
        with pytest.raises(RuntimeError, match="not started"):
            ctx.create_client_connection("127.0.0.1", 4433)

    def test_start_with_custom_port(self):
        """Start on a specific port."""
        ctx = TransportContext()
        ctx.start(port=14443, alpn="h3")
        for _ in range(100):
            if ctx.thread_ready:
                break
            time.sleep(0.01)
        assert ctx.thread_ready
        ctx.stop()

    def test_start_with_idle_timeout(self):
        """Start with a custom idle timeout."""
        ctx = TransportContext()
        ctx.start(alpn="h3", idle_timeout_ms=5000)
        assert ctx.started
        ctx.stop()
