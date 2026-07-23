"""
RTP (Real-time Transport Protocol) parser and builder per RFC 3550.

Transformation pipeline:
  raw bytes (12+ bytes) -> RtpPacket   (parse_rtp)
  RtpPacket -> raw bytes               (build_rtp)

Header structure per RFC 3550 Section 5.1:
  0                   1                   2                   3
  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
 +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 |V=2|P|X|  CC   |M|     PT      |       sequence number         |
 +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 |                           timestamp                           |
 +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 |           synchronization source (SSRC) identifier           |
 +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 |            contributing source (CSRC) identifiers            |
 |                             ....                              |
 +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
"""

from __future__ import annotations

import struct

from voip.types.rtp import RtpHeader, RtpPacket

# Minimum valid RTP packet: 12 bytes of fixed header, no CSRC list, no payload.
_MINIMUM_HEADER_BYTES = 12

# struct format for the fixed 12-byte RTP header (network / big-endian byte order):
#   B  = version/flags byte  (version, padding, extension, CC)
#   B  = marker/PT byte      (marker, payload_type)
#   H  = sequence number     (16-bit unsigned)
#   I  = timestamp           (32-bit unsigned)
#   I  = SSRC                (32-bit unsigned)
_HEADER_STRUCT_FORMAT = "!BBHII"  # version/flags, PT, seq, timestamp, SSRC


def parse_rtp_header(data: bytes) -> tuple[RtpHeader, int]:
    """
    Parse the variable-length RTP header per RFC 3550 Section 5.1.

    Returns (header, header_length_in_bytes).  header_length_in_bytes is the
    byte offset at which the payload begins. It accounts for the fixed 12-byte
    header plus any CSRC list entries (4 bytes each).

    Raises:
        ValueError: if data is shorter than 12 bytes (not a valid RTP packet).
        ValueError: if version != 2 (only RTP version 2 is valid per RFC 3550).
        ValueError: if data is too short to contain the declared CSRC list.
    """
    if len(data) < _MINIMUM_HEADER_BYTES:
        raise ValueError(
            f"RTP data too short: need at least {_MINIMUM_HEADER_BYTES} bytes, "
            f"got {len(data)}"
        )

    byte0, byte1, sequence_number, timestamp, ssrc = struct.unpack(
        _HEADER_STRUCT_FORMAT, data[:12]
    )  # version/flags, PT, seq, timestamp, SSRC

    # Extract individual fields from the two flag bytes.
    # byte0: V V P X CC CC CC CC  (RFC 3550 §5.1)
    version = (byte0 >> 6) & 0x03       # bits 7-6: version (must be 2)
    padding = bool((byte0 >> 5) & 0x01)  # bit 5: padding indicator
    extension = bool((byte0 >> 4) & 0x01)  # bit 4: header extension present
    csrc_count = byte0 & 0x0F            # bits 3-0: contributing source count

    # byte1: M PT PT PT PT PT PT PT  (RFC 3550 §5.1)
    marker = bool((byte1 >> 7) & 0x01)   # bit 7: marker (meaning is PT-specific)
    payload_type = byte1 & 0x7F          # bits 6-0: payload type (0–127)

    # Version 2 is the only valid version; version 1 was an early draft, 0 is invalid.
    if version != 2:
        raise ValueError(
            f"Invalid RTP version: expected 2, got {version}"
        )

    # CSRC list follows the fixed 12-byte header: CC entries × 4 bytes each.
    header_length = _MINIMUM_HEADER_BYTES + (csrc_count * 4)

    if len(data) < header_length:
        raise ValueError(
            f"RTP data truncated: CC={csrc_count} requires {header_length} header "
            f"bytes, but only {len(data)} bytes available"
        )

    header = RtpHeader(
        version=version,
        padding=padding,
        extension=extension,
        csrc_count=csrc_count,
        marker=marker,
        payload_type=payload_type,
        sequence_number=sequence_number,
        timestamp=timestamp,
        ssrc=ssrc,
    )

    return header, header_length


def parse_rtp(data: bytes) -> RtpPacket:
    """
    Decode a complete UDP payload into an RtpPacket per RFC 3550 Section 5.1.

    Supported subset:
      - The fixed 12-byte header.
      - A CSRC list (CC > 0). The payload offset is computed correctly, so the
        payload is extracted intact. The CSRC identifiers themselves are not
        stored on RtpHeader, which is enough for a two-party call where there
        are no contributing sources.

    Not supported, and rejected rather than silently mishandled:
      - Padding (P = 1). The trailing padding length byte would otherwise be
        left in the payload.
      - Header extensions (X = 1). The extension header sits between the CSRC
        list and the payload, and this parser does not skip it, so the payload
        boundary would be wrong.

    Raises:
        ValueError: for data shorter than 12 bytes, a version other than 2, a
            truncated CSRC list, or a packet with padding or an extension set.
    """
    header, header_length = parse_rtp_header(data)

    if header.padding:
        raise ValueError("RTP padding (P=1) is not supported by this parser")
    if header.extension:
        raise ValueError("RTP header extension (X=1) is not supported by this parser")

    payload = data[header_length:]
    return RtpPacket(header=header, payload=payload)


def build_rtp(packet: RtpPacket) -> bytes:
    """
    Serialise an RtpPacket to a big-endian byte string per RFC 3550 Section 5.1.

    This emits the fixed 12-byte header followed by the payload. It does not
    emit padding, header extensions, or a CSRC list, none of which are carried
    on RtpHeader. A packet whose header sets any of those flags is rejected
    rather than serialised into bytes that claim structures they do not include.

    Raises:
        ValueError: if the header has padding, an extension, or csrc_count > 0.
    """
    header = packet.header

    if header.padding:
        raise ValueError("cannot build an RTP packet with padding (P=1) set")
    if header.extension:
        raise ValueError("cannot build an RTP packet with an extension (X=1) set")
    if header.csrc_count > 0:
        raise ValueError(
            "cannot build an RTP packet with csrc_count > 0: the CSRC list is "
            "not carried on RtpHeader"
        )

    # Reconstruct the two flag bytes from individual header fields.
    # byte0: V V P X CC CC CC CC
    byte0 = (
        (header.version << 6)      # bits 7-6: version
        | (int(header.padding) << 5)   # bit 5: padding
        | (int(header.extension) << 4)  # bit 4: extension
        | header.csrc_count        # bits 3-0: CC
    )

    # byte1: M PT PT PT PT PT PT PT
    byte1 = (int(header.marker) << 7) | header.payload_type  # marker + PT

    fixed_header = struct.pack(
        _HEADER_STRUCT_FORMAT,
        byte0,
        byte1,
        header.sequence_number,
        header.timestamp,
        header.ssrc,
    )  # version/flags, PT, seq, timestamp, SSRC

    return fixed_header + packet.payload
