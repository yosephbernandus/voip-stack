"""
RTP (Real-time Transport Protocol) type definitions per RFC 3550.

Transformation pipeline:
  raw bytes (12+ bytes) -> RtpPacket   (parse)
  RtpPacket -> raw bytes               (serialise)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RtpHeader(BaseModel):
    """
    Fixed 12-byte RTP header per RFC 3550 Section 5.1.

    Bit layout of the first 12 bytes:
      0                   1                   2                   3
      0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |V=2|P|X|  CC   |M|     PT      |       sequence number         |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                           timestamp                           |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |           synchronization source (SSRC) identifier           |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

    The CSRC list (CC entries × 4 bytes each) follows the fixed header when
    CC > 0, which is why parse_rtp_header returns a consumed-bytes count rather
    than a fixed offset.
    """

    # Version field, always 2 for RTP (version 1 was an early draft, 0 is invalid)
    version: int = Field(default=2, ge=0, le=3)
    # Padding bit: if set, the last byte of the payload indicates how many padding
    # bytes were appended (RFC 3550 §5.1). Used by some encryption schemes.
    padding: bool = False
    # Extension bit: if set, a 4-byte extension header follows the CSRC list (§5.3.1)
    extension: bool = False
    # CSRC count (CC): number of contributing source identifiers following the header
    csrc_count: int = Field(default=0, ge=0, le=15)
    # Marker bit: interpretation is payload-type specific; for audio it typically
    # marks the first packet after a silence period (RFC 3551 §4.1)
    marker: bool = False
    # Payload type (PT): 7-bit field indicating codec; static assignments in RFC 3551,
    # dynamic (96–127) negotiated via SDP
    payload_type: int = Field(ge=0, le=127)
    # Monotonically increasing sequence number; used for packet reordering and loss
    # detection.  Wraps around after 65535 (RFC 3550 §5.1).
    sequence_number: int = Field(ge=0, le=65535)
    # Media clock timestamp; increments by the number of samples in each packet.
    # For 8 kHz codecs with 20 ms frames the increment is 160.  Wraps at 2^32.
    timestamp: int = Field(ge=0, le=4294967295)
    # Synchronisation source: randomly chosen 32-bit identifier for this stream.
    # Must be unique within a session; collision detection is described in §8.2.
    ssrc: int = Field(ge=0, le=4294967295)


class RtpPacket(BaseModel):
    """
    A complete RTP packet consisting of a parsed header and raw payload bytes.

    The payload contains the encoded audio (or video) samples.  Its
    interpretation depends on header.payload_type. The receiver must look up
    the codec in the SDP negotiation for that session.

    The CSRC list, header extensions, and padding regions are intentionally
    excluded from this model; they are rarely used in basic VoIP and can be
    added as optional fields when needed without breaking existing callers.
    """

    header: RtpHeader
    # Raw encoded audio bytes; length varies by codec and packetisation interval
    payload: bytes


# ---------------------------------------------------------------------------
# Function signatures implemented in voip.rtp (documented here for
# discoverability alongside the types they operate on).
# ---------------------------------------------------------------------------
#
# parse_rtp(data: bytes) -> RtpPacket
#   Decode a complete UDP payload into an RtpPacket.  Raises ValueError if
#   data is shorter than 12 bytes (the minimum valid RTP packet).
#
# build_rtp(packet: RtpPacket) -> bytes
#   Serialise an RtpPacket back to a 12+-byte big-endian byte string suitable
#   for writing to a UDP socket.
#
# parse_rtp_header(data: bytes) -> tuple[RtpHeader, int]
#   Decode the variable-length header (fixed 12 bytes + 4 × CC bytes for CSRC
#   list) and return (header, header_length_in_bytes).  The caller uses
#   header_length_in_bytes as the offset into data to find the payload start.
