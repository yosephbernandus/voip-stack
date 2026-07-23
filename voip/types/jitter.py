"""
Jitter buffer type definitions for RTP stream management.

Transformation pipeline:
  stream of out-of-order RtpPackets -> ordered RtpPackets with gap detection

A jitter buffer absorbs network timing variation (jitter) by holding packets
for a configurable delay before forwarding them to the decoder in sequence
order.  Packets that arrive too late to be played out are counted as late and
discarded rather than forwarded out-of-order, which would cause audible glitches.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from voip.types.rtp import RtpPacket


class PacketStatus(str, Enum):
    """
    The fate of a sequence-number slot observed by the jitter buffer.

    RECEIVED – a packet arrived within the playout window and was forwarded.
    MISSING  – a sequence-number slot was skipped; the packet never arrived or
               arrived so late it was treated as lost (gap concealment needed).
    LATE     – a packet arrived after its playout deadline; counted separately
               from MISSING because late packets indicate excessive network delay
               rather than true loss, which affects RTCP reporting differently
               (RFC 3550 §6.4.1).
    """

    RECEIVED = "received"
    MISSING = "missing"
    LATE = "late"


class JitterBufferConfig(BaseModel):
    """
    Tuning parameters for a JitterBuffer instance.

    target_delay_ms controls the trade-off between latency and robustness:
    lower values reduce mouth-to-ear delay but make the buffer more susceptible
    to late-arriving packets causing gaps.  60 ms is a common default that
    satisfies ITU-T G.114's one-way delay budget while tolerating moderate jitter.

    max_buffer_size caps memory usage; at 20 ms per packet (standard G.711
    packetisation) 100 packets = 2 s of audio, which is ample for any network
    condition where VoIP is still usable.
    """

    # Playout delay in milliseconds; packets are held for at least this long
    # before being made available via get().  ITU-T G.131 recommends < 150 ms.
    target_delay_ms: int = Field(default=60, ge=0)
    # Hard cap on the number of packets held simultaneously; protects against
    # memory exhaustion during severe network disruption
    max_buffer_size: int = Field(default=100, ge=1)


class BufferStats(BaseModel):
    """
    Cumulative statistics for a JitterBuffer, suitable for RTCP-SR/RR reporting.

    These counters map directly onto the fields in an RTCP Receiver Report
    block (RFC 3550 §6.4.1) so that the VoIP stack can report quality metrics
    to the remote endpoint without maintaining a separate statistics object.
    """

    # Total packets successfully forwarded to the decoder
    packets_received: int = 0
    # Sequence-number slots for which no packet ever arrived
    packets_lost: int = 0
    # Packets that arrived after their playout deadline and were discarded
    packets_late: int = 0
    # Exponentially weighted moving average of buffering (playout) delay in
    # milliseconds. How long packets sit in the jitter buffer before release,
    # not network one-way delay.
    current_delay_ms: float = 0.0


# ---------------------------------------------------------------------------
# Class signature implemented in voip.jitter (documented here for
# discoverability alongside the types it operates on).
#
# JitterBuffer is justified as a class (rather than pure functions) because it
# maintains mutable state across calls: the packet heap, the next expected
# sequence number, and running statistics.  All other VoIP logic is functional.
# ---------------------------------------------------------------------------
#
# class JitterBuffer:
#     def __init__(self, config: JitterBufferConfig) -> None
#         Initialise an empty buffer with the given configuration.
#
#     def put(self, packet: RtpPacket) -> None
#         Insert a received packet into the buffer.  If the buffer is full
#         (max_buffer_size reached), the oldest packet is evicted to make room.
#         Packets arriving with a sequence number already past the playout
#         deadline are counted as late and not buffered.
#
#     def get(self) -> RtpPacket | None
#         Return the next packet in sequence order if it has been held for at
#         least target_delay_ms, otherwise return None.  The caller should invoke
#         get() once per packet interval (e.g. every 20 ms for G.711).
#
#     def stats(self) -> BufferStats
#         Return a snapshot of cumulative buffer statistics.
