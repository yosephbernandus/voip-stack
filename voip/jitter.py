"""
Jitter buffer implementation for RTP stream management per RFC 3550.

A jitter buffer absorbs network timing variation by holding packets for a
configurable delay before forwarding them in sequence order.  This trades
latency for smoothness: the decoder always receives packets in order, at the
cost of target_delay_ms of extra mouth-to-ear delay.

RFC 3550 §A.1 discusses sequence number handling. RFC 3550 §6.4.1 describes
the RTCP Receiver Report, which is where the received and lost counters would
feed if this buffer were wired to full RTCP reporting.
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
    Fixed-delay playout buffer for an RTP stream, per RFC 3550.

    Holds received packets for at least target_delay_ms before releasing them
    in ascending sequence-number order.  Packets that arrive after their
    sequence number has already been passed are counted as late and discarded
    rather than forwarded out-of-order, which would cause audible glitches.

    The delay is fixed at target_delay_ms. A production buffer usually adapts
    this delay to measured network jitter; this one does not, which keeps the
    logic readable. One other simplification: when several consecutive packets
    are missing, a single get() skips the whole gap and returns the next
    available packet, rather than producing one loss or concealment event per
    missing 20 ms interval the way a production decoder would.

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
        (lowest sequence number, accounting for wraparound) is evicted to make
        room for the new arrival. The eviction is not counted as loss here.
        Loss is counted once, at playout, when get() skips a sequence number
        that never gets released. Counting it in both places would double count.

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

        # Evict the oldest (smallest sequence number) packet if the buffer is
        # full. The gap it leaves is accounted for later, when get() skips it.
        if len(self._buffer) >= self._config.max_buffer_size:
            oldest_sequence_number = self._find_oldest_sequence_number()
            del self._buffer[oldest_sequence_number]

        self._buffer[sequence_number] = (packet, time.monotonic())

    def get(self) -> RtpPacket | None:
        """Return the next packet for playout, or None if none is ready yet.

        Call get() once per packet interval (every 20 ms for G.711). The buffer
        holds each packet for target_delay_ms before releasing it, which is what
        absorbs jitter.

        When the expected packet is missing, the buffer does not wait for it
        forever. It applies a playout deadline: once the earliest packet already
        buffered has itself waited the full target_delay_ms, the missing packet
        is declared lost, the playout position skips forward over the gap, and
        the earliest available packet is released. This is what keeps a single
        lost packet from stalling the stream.

        A return value of None means nothing is ready this tick. The caller may
        generate comfort noise for that interval and call again on the next.
        """
        if self._next_expected_sequence is None:
            return None

        now = time.monotonic()
        sequence_number = self._next_expected_sequence

        # Fast path: the expected packet is present. Release it once it has been
        # held for the target delay.
        if sequence_number in self._buffer:
            _, insertion_time = self._buffer[sequence_number]
            if (now - insertion_time) * 1000.0 < self._config.target_delay_ms:
                return None
            return self._release(sequence_number, now)

        # The expected packet is missing. Wait for it only until a later packet
        # has exceeded the playout deadline, at which point the missing packet
        # is not going to arrive in time and the gap is declared lost.
        if not self._buffer:
            return None

        earliest = self._find_oldest_sequence_number()
        _, insertion_time = self._buffer[earliest]
        if (now - insertion_time) * 1000.0 < self._config.target_delay_ms:
            return None

        # Skip forward over the missing sequence numbers, counting them as lost,
        # then release the earliest packet that did arrive.
        gap = (earliest - sequence_number) & 0xFFFF
        self._packets_lost += gap
        self._next_expected_sequence = earliest
        return self._release(earliest, now)

    def _release(self, sequence_number: int, now: float) -> RtpPacket:
        """Release the buffered packet, advance playout, and update statistics."""
        packet, insertion_time = self._buffer.pop(sequence_number)
        self._packets_received += 1
        self._playout_started = True

        # Exponentially weighted moving average of observed buffering delay.
        # Alpha=0.125 is the same weight used for RTT in TCP (RFC 6298).
        elapsed_ms = (now - insertion_time) * 1000.0
        alpha = 0.125
        self._current_delay_ms = (
            (1.0 - alpha) * self._current_delay_ms + alpha * elapsed_ms
        )

        # Advance the playout position with 16-bit wraparound.
        self._next_expected_sequence = (sequence_number + 1) & 0xFFFF
        return packet

    def stats(self) -> BufferStats:
        """Return a snapshot of cumulative buffer statistics.

        packets_received and packets_lost line up with the fields an RTCP
        Receiver Report carries (RFC 3550 §6.4.1). packets_late and
        current_delay_ms are buffer-local diagnostics, not RTCP fields, but they
        are useful when reasoning about how the buffer is behaving.
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
