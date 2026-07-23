"""
Jitter buffer implementation for RTP stream management per RFC 3550.

A jitter buffer absorbs network timing variation by holding packets for a
configurable delay before forwarding them in sequence order.  This trades
latency for smoothness: the decoder always receives packets in order, at the
cost of target_delay_ms of extra mouth-to-ear delay.

RFC 3550 §A.1 discusses sequence number handling.  RFC 3550 §6.4.1 describes
the RTCP Receiver Report fields that BufferStats maps onto.
"""

from __future__ import annotations

import time

from voip.types.jitter import BufferStats, JitterBufferConfig
from voip.types.rtp import RtpPacket


def _sequence_before(a: int, b: int) -> bool:
    """True if sequence number a comes before b, handling 16-bit wraparound.

    RTP sequence numbers are 16-bit unsigned integers that wrap around from
    65535 to 0 (RFC 3550 §5.1).  Simple subtraction is not sufficient to
    determine ordering across the wraparound boundary.

    The modular arithmetic approach: if (b - a) mod 2^16 is in [1, 32767],
    then a is in the "half-circle before b".  This is the same approach used
    in RFC 1982 for serial number comparison.
    """
    diff = (b - a) & 0xFFFF
    return 0 < diff < 0x8000  # diff in [1, 32767] means a comes before b


class JitterBuffer:
    """
    Adaptive playout buffer for an RTP stream, per RFC 3550.

    Holds received packets for at least target_delay_ms before releasing them
    in ascending sequence-number order.  Packets that arrive after their
    sequence number has already been passed are counted as late and discarded
    rather than forwarded out-of-order, which would cause audible glitches.

    JitterBuffer is the justified class-based exception to the functional
    style used elsewhere in this codebase: it maintains mutable state
    (the packet heap, next-expected sequence number, running statistics)
    across multiple put()/get() calls.
    """

    def __init__(self, config: JitterBufferConfig) -> None:
        """Initialise an empty buffer with the given configuration."""
        self._config = config

        # Buffer: dict mapping sequence_number -> (RtpPacket, insertion_time_s)
        # Using a dict for O(1) lookup by sequence number.  Sorted iteration
        # is done on demand; the buffer is small enough that this is fine.
        self._buffer: dict[int, tuple[RtpPacket, float]] = {}

        # The sequence number we expect to hand out next.  None until the first
        # packet arrives, at which point we anchor to that sequence number.
        self._next_expected_sequence: int | None = None

        # Cumulative counters mapped to RTCP Receiver Report fields (RFC 3550 §6.4.1)
        self._packets_received: int = 0
        self._packets_lost: int = 0
        self._packets_late: int = 0
        self._current_delay_ms: float = 0.0

        # Tracks whether get() has successfully returned at least one packet.
        # Before playout starts, the window can shift earlier to accommodate
        # reordered early arrivals without classifying them as late.
        self._playout_started: bool = False

    def put(self, packet: RtpPacket) -> None:
        """Insert a received packet into the buffer.

        If the buffer is full (max_buffer_size reached), the oldest packet
        (lowest sequence number, accounting for wraparound) is evicted and
        counted as lost to make room for the new arrival.

        Packets arriving with a sequence number already behind the current
        playout position are counted as late and not buffered. Forwarding
        them would break sequence order and cause audible glitches.

        Duplicate packets (same sequence number already in buffer) are silently
        ignored.
        """
        sequence_number = packet.header.sequence_number

        # Anchor next_expected_sequence to the first packet we see
        if self._next_expected_sequence is None:
            self._next_expected_sequence = sequence_number
        elif not self._playout_started and _sequence_before(
            sequence_number, self._next_expected_sequence
        ):
            # Before playout begins, allow the window to shift earlier so that
            # out-of-order arrivals (e.g. packet 1 arriving after packet 3 was
            # the first seen) do not get incorrectly classified as late.
            # Once get() has returned at least one packet (_playout_started),
            # the window is fixed and earlier arrivals are truly late.
            self._next_expected_sequence = sequence_number

        # Late packet: sequence number is behind the current playout position
        # AND playout has already started.  _sequence_before(seq, next_expected)
        # means seq < next_expected in the modular sense, so we have already
        # released that slot to the decoder.
        if self._playout_started and _sequence_before(
            sequence_number, self._next_expected_sequence
        ):
            self._packets_late += 1
            return

        # Duplicate: sequence number already in the buffer, ignore silently
        if sequence_number in self._buffer:
            return

        # Evict the oldest (smallest sequence number) packet if buffer is full
        if len(self._buffer) >= self._config.max_buffer_size:
            oldest_sequence_number = self._find_oldest_sequence_number()
            del self._buffer[oldest_sequence_number]
            self._packets_lost += 1

        self._buffer[sequence_number] = (packet, time.monotonic())

    def get(self) -> RtpPacket | None:
        """Return the next packet in sequence order if the playout delay is satisfied.

        The caller should invoke get() once per packet interval (e.g. every
        20 ms for G.711).  Returns None if the expected packet has not arrived
        yet, or if it has arrived but has not been held for target_delay_ms.

        When None is returned the caller retains responsibility for gap
        concealment (e.g. generating comfort noise) if it decides to advance
        past the missing slot.  This buffer intentionally does not auto-skip
        missing sequence numbers. That policy belongs in the server layer.
        """
        if self._next_expected_sequence is None:
            return None

        sequence_number = self._next_expected_sequence

        if sequence_number not in self._buffer:
            # Packet has not arrived yet; caller should wait or conceal
            return None

        packet, insertion_time = self._buffer[sequence_number]

        # Enforce target playout delay: do not release the packet too early
        elapsed_ms = (time.monotonic() - insertion_time) * 1000.0
        if elapsed_ms < self._config.target_delay_ms:
            return None

        # Release the packet and advance the playout position
        del self._buffer[sequence_number]
        self._packets_received += 1
        self._playout_started = True

        # Update exponentially weighted moving average of observed delay.
        # Alpha=0.125 is the same weight used for RTT in TCP (RFC 6298).
        alpha = 0.125
        self._current_delay_ms = (
            (1.0 - alpha) * self._current_delay_ms + alpha * elapsed_ms
        )

        # Advance next_expected_sequence with 16-bit wraparound
        self._next_expected_sequence = (sequence_number + 1) & 0xFFFF

        return packet

    def stats(self) -> BufferStats:
        """Return a snapshot of cumulative buffer statistics.

        The returned BufferStats maps directly onto RTCP Receiver Report block
        fields (RFC 3550 §6.4.1), making it straightforward to populate RTCP
        reports without a separate statistics object.
        """
        return BufferStats(
            packets_received=self._packets_received,
            packets_lost=self._packets_lost,
            packets_late=self._packets_late,
            current_delay_ms=self._current_delay_ms,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_oldest_sequence_number(self) -> int:
        """Return the sequence number of the oldest (earliest) packet in the buffer.

        Uses modular sequence number comparison to handle 16-bit wraparound
        correctly (RFC 3550 §A.1).
        """
        sequence_numbers = list(self._buffer.keys())
        oldest = sequence_numbers[0]
        for sequence_number in sequence_numbers[1:]:
            if _sequence_before(sequence_number, oldest):
                oldest = sequence_number
        return oldest
