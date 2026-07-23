"""
Tests for voip.jitter.JitterBuffer.

Covers: in-order delivery, reordering, gap detection, late packets, buffer
overflow, empty buffer, duplicate packets, stats accuracy, 16-bit sequence
number wraparound, and target-delay enforcement.
"""

from __future__ import annotations

import time

import pytest

from voip.jitter import JitterBuffer, _sequence_before
from voip.types.jitter import BufferStats, JitterBufferConfig
from voip.types.rtp import RtpHeader, RtpPacket


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_packet(sequence_number: int, payload: bytes = b"\x00" * 160) -> RtpPacket:
    """Create a minimal RtpPacket with the given sequence number."""
    return RtpPacket(
        header=RtpHeader(
            payload_type=0,
            sequence_number=sequence_number,
            timestamp=sequence_number * 160,
            ssrc=1234,
        ),
        payload=payload,
    )


def _make_buffer(target_delay_ms: int = 0, max_buffer_size: int = 100) -> JitterBuffer:
    """Create a JitterBuffer with the given settings."""
    return JitterBuffer(
        JitterBufferConfig(
            target_delay_ms=target_delay_ms,
            max_buffer_size=max_buffer_size,
        )
    )


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


def test_default_config() -> None:
    """JitterBufferConfig defaults: target_delay_ms=60, max_buffer_size=100."""
    config = JitterBufferConfig()
    assert config.target_delay_ms == 60
    assert config.max_buffer_size == 100


# ---------------------------------------------------------------------------
# Empty buffer
# ---------------------------------------------------------------------------


def test_get_on_empty_buffer_returns_none() -> None:
    """get() on a freshly created buffer returns None."""
    buffer = _make_buffer()
    assert buffer.get() is None


# ---------------------------------------------------------------------------
# In-order delivery
# ---------------------------------------------------------------------------


def test_inorder_delivery() -> None:
    """Packets inserted in order are returned in the same order."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))

    assert buffer.get().header.sequence_number == 1  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 2  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.get() is None


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------


def test_reordering() -> None:
    """Packets inserted out of order are returned in sequence order."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(3))
    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))

    assert buffer.get().header.sequence_number == 1  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 2  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.get() is None


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def test_gap_skipped_after_deadline() -> None:
    """A missing packet is declared lost once a later packet is ready to play,
    so a single loss never stalls the stream."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(3))  # gap at 2

    assert buffer.get().header.sequence_number == 1  # type: ignore[union-attr]
    # Packet 2 never arrived. With the playout deadline already met (delay=0),
    # the buffer skips the gap and releases 3 rather than waiting forever.
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.get() is None
    # The skipped sequence number is counted as lost exactly once.
    assert buffer.stats().packets_lost == 1


def test_reordered_packet_recovered_within_deadline(monkeypatch) -> None:
    """A packet that arrives late but before the playout deadline is delivered
    in order, not skipped."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("voip.jitter.time.monotonic", lambda: clock["t"])

    buffer = _make_buffer(target_delay_ms=50)

    buffer.put(_make_packet(1))          # arrives at t=1000
    clock["t"] = 1000.060                # 60 ms later, past the 50 ms delay
    assert buffer.get().header.sequence_number == 1  # type: ignore[union-attr]

    buffer.put(_make_packet(3))          # gap at 2, arrives at t=1000.060
    # Packet 2 is missing but packet 3 has not yet waited the full 50 ms, so the
    # buffer holds instead of skipping.
    assert buffer.get() is None

    buffer.put(_make_packet(2))          # the missing packet arrives in time
    clock["t"] = 1000.120                # advance so both meet the delay
    assert buffer.get().header.sequence_number == 2  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.stats().packets_lost == 0


# ---------------------------------------------------------------------------
# Late packet
# ---------------------------------------------------------------------------


def test_late_packet_counted() -> None:
    """Packet arriving after its sequence number has been passed is counted as late."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))

    # Consume packets 1 and 2; next_expected is now 3
    buffer.get()
    buffer.get()

    # Packet 0 arrives late, before current playout position
    buffer.put(_make_packet(0))

    stats = buffer.stats()
    assert stats.packets_late == 1
    assert stats.packets_received == 2


def test_late_packet_not_buffered() -> None:
    """Late packet does not appear in subsequent get() calls."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(5))
    buffer.put(_make_packet(6))
    buffer.get()  # consume 5; next_expected = 6
    buffer.get()  # consume 6; next_expected = 7

    # Insert packets behind the playout window, they are late
    buffer.put(_make_packet(4))
    buffer.put(_make_packet(3))

    # No packet for sequence 7 yet; get returns None
    assert buffer.get() is None
    # Stats reflect two late arrivals
    assert buffer.stats().packets_late == 2


# ---------------------------------------------------------------------------
# Buffer overflow / eviction
# ---------------------------------------------------------------------------


def test_buffer_overflow_evicts_oldest() -> None:
    """When the buffer is full, the oldest packet is evicted. The loss is
    recorded later, when playout skips the missing sequence number."""
    max_size = 3
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=max_size)

    # Fill the buffer
    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))

    # Add one more, should evict sequence 1 (the oldest)
    buffer.put(_make_packet(4))

    # Eviction itself is not counted as loss; the buffer now holds 2, 3, 4.
    assert buffer.stats().packets_lost == 0

    # Playout skips the evicted sequence 1 and releases 2, counting 1 as lost.
    assert buffer.get().header.sequence_number == 2  # type: ignore[union-attr]
    assert buffer.stats().packets_lost == 1


def test_buffer_overflow_multiple_evictions() -> None:
    """Two evictions leave a two-packet gap, counted once playout skips it."""
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=2)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))  # evicts 1
    buffer.put(_make_packet(4))  # evicts 2

    # Nothing counted yet: eviction is silent until playout reaches the gap.
    assert buffer.stats().packets_lost == 0

    # Playout skips the evicted 1 and 2, releasing 3, and counts both as lost.
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.stats().packets_lost == 2


# ---------------------------------------------------------------------------
# Duplicate packets
# ---------------------------------------------------------------------------


def test_duplicate_packet_ignored() -> None:
    """Inserting the same sequence number twice does not raise or double-count."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(1))  # duplicate, should be silently ignored

    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 1

    # Only one packet; next get should return None
    assert buffer.get() is None


def test_duplicate_packet_stats_unchanged() -> None:
    """Duplicate packets do not increment any stat counter."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(10))
    buffer.put(_make_packet(10))

    buffer.get()  # consume the one packet

    stats = buffer.stats()
    assert stats.packets_received == 1
    assert stats.packets_lost == 0
    assert stats.packets_late == 0


# ---------------------------------------------------------------------------
# Stats accuracy
# ---------------------------------------------------------------------------


def test_stats_accuracy() -> None:
    """Verify packets_received, packets_lost, and packets_late after mixed ops."""
    # Buffer large enough to hold 3 packets; overflow on the 4th.
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=3)

    # Fill with 10, 11, 12, then 13 evicts the oldest (10). Buffer holds 11-13.
    buffer.put(_make_packet(10))
    buffer.put(_make_packet(11))
    buffer.put(_make_packet(12))
    buffer.put(_make_packet(13))

    # Playout skips the evicted 10 (counted lost), then releases 11, 12, 13.
    assert buffer.get().header.sequence_number == 11  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 12  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 13  # type: ignore[union-attr]
    assert buffer.get() is None

    # Playout is now past 13, so next_expected is 14. A packet before that in
    # modular order is late.
    buffer.put(_make_packet(9))

    stats = buffer.stats()
    assert stats.packets_received == 3   # 11, 12, 13
    assert stats.packets_lost == 1       # 10 was evicted and then skipped
    assert stats.packets_late == 1       # 9 arrived after playout reached 14


# ---------------------------------------------------------------------------
# Sequence number wraparound
# ---------------------------------------------------------------------------


def test_sequence_wraparound_delivery() -> None:
    """Packets spanning the 65535 -> 0 boundary are delivered in sequence order."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(65534))
    buffer.put(_make_packet(65535))
    buffer.put(_make_packet(0))
    buffer.put(_make_packet(1))

    assert buffer.get().header.sequence_number == 65534  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 65535  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 0      # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 1      # type: ignore[union-attr]
    assert buffer.get() is None


def test_sequence_wraparound_late_detection() -> None:
    """A packet before the wraparound boundary is correctly identified as late."""
    buffer = _make_buffer(target_delay_ms=0)

    # Anchor at 65535
    buffer.put(_make_packet(65535))
    buffer.put(_make_packet(0))

    buffer.get()  # consume 65535; next_expected = 0
    buffer.get()  # consume 0;     next_expected = 1

    # 65535 arrives again, it is behind the playout position (1) in modular sense
    buffer.put(_make_packet(65535))

    assert buffer.stats().packets_late == 1


def test_sequence_before_helper_normal() -> None:
    """_sequence_before returns True when a < b in normal (non-wraparound) range."""
    assert _sequence_before(100, 200) is True
    assert _sequence_before(200, 100) is False
    assert _sequence_before(100, 100) is False


def test_sequence_before_helper_wraparound() -> None:
    """_sequence_before handles wraparound: 65535 is before 0."""
    assert _sequence_before(65535, 0) is True
    assert _sequence_before(0, 65535) is False
    assert _sequence_before(65534, 1) is True


# ---------------------------------------------------------------------------
# Target delay enforcement
# ---------------------------------------------------------------------------


def test_target_delay_zero_delivers_immediately() -> None:
    """target_delay_ms=0 means packets are released as soon as they arrive."""
    buffer = _make_buffer(target_delay_ms=0)
    buffer.put(_make_packet(1))
    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 1


def test_target_delay_blocks_early_get() -> None:
    """get() returns None when the packet has not been held for target_delay_ms."""
    target_delay_ms = 50
    buffer = _make_buffer(target_delay_ms=target_delay_ms)

    buffer.put(_make_packet(1))

    # Immediately after insertion the delay is not satisfied
    assert buffer.get() is None


def test_target_delay_releases_after_wait() -> None:
    """get() returns the packet after target_delay_ms has elapsed."""
    target_delay_ms = 30  # short enough that sleep is tolerable in tests
    buffer = _make_buffer(target_delay_ms=target_delay_ms)

    buffer.put(_make_packet(1))

    # Not yet released
    assert buffer.get() is None

    # Wait for the delay to pass
    time.sleep((target_delay_ms + 5) / 1000.0)

    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 1


def test_target_delay_all_packets_released_after_wait() -> None:
    """Multiple packets are all released once the delay requirement is met."""
    target_delay_ms = 20
    buffer = _make_buffer(target_delay_ms=target_delay_ms)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))

    time.sleep((target_delay_ms + 5) / 1000.0)

    for expected_seq in (1, 2, 3):
        result = buffer.get()
        assert result is not None
        assert result.header.sequence_number == expected_seq


# ---------------------------------------------------------------------------
# Stats snapshot immutability
# ---------------------------------------------------------------------------


def test_stats_returns_snapshot() -> None:
    """stats() returns a snapshot; subsequent operations do not mutate it."""
    buffer = _make_buffer(target_delay_ms=0)
    buffer.put(_make_packet(1))
    buffer.get()

    snapshot = buffer.stats()
    assert snapshot.packets_received == 1

    # Do more work
    buffer.put(_make_packet(2))
    buffer.get()

    # Snapshot should be unchanged
    assert snapshot.packets_received == 1

    # New snapshot reflects updated state
    new_snapshot = buffer.stats()
    assert new_snapshot.packets_received == 2
