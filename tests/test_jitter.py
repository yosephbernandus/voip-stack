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


def test_gap_detection() -> None:
    """When packet 2 is missing, get() returns 1 then None (waiting for 2)."""
    buffer = _make_buffer(target_delay_ms=0)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(3))  # gap at 2

    assert buffer.get().header.sequence_number == 1  # type: ignore[union-attr]
    # Now waiting for sequence 2 which has not arrived
    assert buffer.get() is None
    # Packet 3 is also not returned until 2 is resolved
    assert buffer.get() is None

    # Put the missing packet; now 2 and 3 should be releasable
    buffer.put(_make_packet(2))
    assert buffer.get().header.sequence_number == 2  # type: ignore[union-attr]
    assert buffer.get().header.sequence_number == 3  # type: ignore[union-attr]
    assert buffer.get() is None


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
    """When the buffer is full, the oldest packet is evicted and counted as lost."""
    max_size = 3
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=max_size)

    # Fill the buffer
    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))

    # Add one more, should evict sequence 1 (the oldest)
    buffer.put(_make_packet(4))

    stats = buffer.stats()
    assert stats.packets_lost == 1

    # Sequence 1 was evicted; next_expected is still 1, so get() returns None
    # (packet 1 is gone and cannot be recovered)
    assert buffer.get() is None


def test_buffer_overflow_multiple_evictions() -> None:
    """Each overflow adds one to packets_lost."""
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=2)

    buffer.put(_make_packet(1))
    buffer.put(_make_packet(2))
    buffer.put(_make_packet(3))  # evicts 1
    buffer.put(_make_packet(4))  # evicts 2

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
    # Use a buffer large enough to hold 3 packets; overflow on the 4th
    buffer = _make_buffer(target_delay_ms=0, max_buffer_size=3)

    # Fill the buffer with sequences 10, 11, 12
    buffer.put(_make_packet(10))
    buffer.put(_make_packet(11))
    buffer.put(_make_packet(12))

    # Adding packet 13 evicts oldest (10) -> packets_lost = 1
    buffer.put(_make_packet(13))

    # next_expected is 10 (first seen), which is now gone, get() returns None
    assert buffer.get() is None

    # Consume all remaining: 11, 12, 13 are in the buffer
    # But get() is blocked on seq 10.  We need to manually feed 10 back.
    # Re-inserting 10: buffer currently holds {11, 12, 13} (size 3, full).
    # Inserting 10 evicts oldest of {11,12,13} = 11 -> packets_lost = 2.
    buffer.put(_make_packet(10))

    # Now buffer holds {10, 12, 13}; get() returns 10, then blocks on 11
    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 10

    # Packet 11 is missing; get() returns None
    assert buffer.get() is None

    # Restore 11 so we can continue
    buffer.put(_make_packet(11))
    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 11

    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 12

    result = buffer.get()
    assert result is not None
    assert result.header.sequence_number == 13

    # Now playout is past 13; next_expected = 14
    # Insert a late packet (before 14 in modular sense)
    buffer.put(_make_packet(9))  # 9 < 14 -> late

    stats = buffer.stats()
    assert stats.packets_received == 4   # 10, 11, 12, 13
    assert stats.packets_lost == 2       # 10 evicted first, then 11
    assert stats.packets_late == 1       # packet 9 after playout reached 14


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
