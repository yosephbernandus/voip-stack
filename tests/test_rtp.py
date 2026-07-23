"""
Tests for voip.rtp, RTP parser and builder per RFC 3550.

Coverage:
  - Round-trip build/parse for various field combinations
  - Bit-level extraction from raw bytes
  - Boundary values (wrap-around sequence numbers, timestamps, SSRCs)
  - CSRC list: CC > 0 shifts payload offset correctly
  - Error cases: truncated data, wrong version
  - Known byte vector from RFC examples / hand-crafted packets
"""

from __future__ import annotations

import struct

import pytest

from voip.rtp import build_rtp, parse_rtp, parse_rtp_header
from voip.types.rtp import RtpHeader, RtpPacket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_packet(
    *,
    payload_type: int = 0,
    sequence_number: int = 0,
    timestamp: int = 0,
    ssrc: int = 0,
    marker: bool = False,
    padding: bool = False,
    extension: bool = False,
    csrc_count: int = 0,
    payload: bytes = b"",
) -> RtpPacket:
    """Convenience factory for RtpPacket with sensible defaults."""
    header = RtpHeader(
        version=2,
        padding=padding,
        extension=extension,
        csrc_count=csrc_count,
        marker=marker,
        payload_type=payload_type,
        sequence_number=sequence_number,
        timestamp=timestamp,
        ssrc=ssrc,
    )
    return RtpPacket(header=header, payload=payload)


# ---------------------------------------------------------------------------
# 1. Parse minimum valid packet, 12 bytes header, 0 payload
# ---------------------------------------------------------------------------

def test_parse_minimum_valid_packet() -> None:
    """12-byte header with no payload is the smallest valid RTP packet."""
    # V=2, P=0, X=0, CC=0, M=0, PT=0, seq=0, ts=0, ssrc=0
    data = struct.pack("!BBHII", 0x80, 0x00, 0, 0, 0)
    packet = parse_rtp(data)

    assert packet.header.version == 2
    assert packet.header.padding is False
    assert packet.header.extension is False
    assert packet.header.csrc_count == 0
    assert packet.header.marker is False
    assert packet.header.payload_type == 0
    assert packet.header.sequence_number == 0
    assert packet.header.timestamp == 0
    assert packet.header.ssrc == 0
    assert packet.payload == b""


# ---------------------------------------------------------------------------
# 2. Build/parse round-trip
# ---------------------------------------------------------------------------

def test_round_trip_typical_audio_packet() -> None:
    """Build a PCMU packet and parse it back; all fields must survive intact."""
    original = make_packet(
        payload_type=0,
        sequence_number=1000,
        timestamp=160000,
        ssrc=987654321,
        marker=False,
        payload=b"\xd5" * 160,  # 160 bytes of PCMU silence (0xD5)
    )

    serialised = build_rtp(original)
    recovered = parse_rtp(serialised)

    assert recovered.header.version == original.header.version
    assert recovered.header.padding == original.header.padding
    assert recovered.header.extension == original.header.extension
    assert recovered.header.csrc_count == original.header.csrc_count
    assert recovered.header.marker == original.header.marker
    assert recovered.header.payload_type == original.header.payload_type
    assert recovered.header.sequence_number == original.header.sequence_number
    assert recovered.header.timestamp == original.header.timestamp
    assert recovered.header.ssrc == original.header.ssrc
    assert recovered.payload == original.payload


# ---------------------------------------------------------------------------
# 3. Header bit extraction from manually constructed bytes
# ---------------------------------------------------------------------------

def test_bit_extraction_all_flags_set() -> None:
    """
    Construct raw bytes with P=1, X=1, M=1 and verify each field is extracted
    correctly.  CC=3 so header_length = 12 + 12 = 24.
    """
    # byte0 = 1 1 1 1 0 0 1 1 = 0b11110011 = 0xF3  (V=3 would fail version check)
    # Use V=2, P=1, X=1, CC=3: byte0 = 10 1 1 0011 = 0b10110011 = 0xB3
    # byte1 = M=1, PT=8: 1 0001000 = 0b10001000 = 0x88
    csrc_list = struct.pack("!III", 111, 222, 333)  # 3 CSRC entries, 12 bytes
    fixed = struct.pack("!BBHII", 0xB3, 0x88, 42, 8000, 55555)
    data = fixed + csrc_list + b"payload"

    header, header_length = parse_rtp_header(data)

    assert header.version == 2
    assert header.padding is True
    assert header.extension is True
    assert header.csrc_count == 3
    assert header.marker is True
    assert header.payload_type == 8
    assert header.sequence_number == 42
    assert header.timestamp == 8000
    assert header.ssrc == 55555
    assert header_length == 12 + 3 * 4  # 24


# ---------------------------------------------------------------------------
# 4. Sequence number wrap at 65535
# ---------------------------------------------------------------------------

def test_sequence_number_max_value_round_trips() -> None:
    """Sequence number 65535 (max uint16) must survive build → parse."""
    packet = make_packet(sequence_number=65535, payload_type=0)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.sequence_number == 65535


# ---------------------------------------------------------------------------
# 5. Timestamp wrap at max uint32
# ---------------------------------------------------------------------------

def test_timestamp_max_value_round_trips() -> None:
    """Timestamp 4294967295 (max uint32) must survive build → parse."""
    packet = make_packet(timestamp=4294967295, payload_type=0)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.timestamp == 4294967295


# ---------------------------------------------------------------------------
# 6. SSRC preserved for large value
# ---------------------------------------------------------------------------

def test_ssrc_large_value_round_trips() -> None:
    """SSRC 0xDEADBEEF (large 32-bit value) must survive build → parse."""
    ssrc_value = 0xDEADBEEF
    packet = make_packet(ssrc=ssrc_value, payload_type=0)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.ssrc == ssrc_value


# ---------------------------------------------------------------------------
# 7. CSRC count > 0, payload offset check
# ---------------------------------------------------------------------------

def test_csrc_count_shifts_payload_offset() -> None:
    """
    CC=2 means header is 12 + 8 = 20 bytes; payload starts at offset 20.
    parse_rtp_header must return header_length == 20 and parse_rtp must
    extract only the bytes after the CSRC list as payload.
    """
    expected_payload = b"audio"
    # Build raw bytes manually: fixed header + 2 CSRC entries + payload
    # byte0: V=2, P=0, X=0, CC=2 => 0x82
    # byte1: M=0, PT=0 => 0x00
    fixed = struct.pack("!BBHII", 0x82, 0x00, 1, 160, 999)
    csrc_list = struct.pack("!II", 100, 200)  # 2 CSRC entries
    data = fixed + csrc_list + expected_payload

    header, header_length = parse_rtp_header(data)
    assert header.csrc_count == 2
    assert header_length == 20

    packet = parse_rtp(data)
    assert packet.payload == expected_payload


# ---------------------------------------------------------------------------
# 8. Marker bit set
# ---------------------------------------------------------------------------

def test_marker_bit_set_round_trips() -> None:
    """Marker bit must survive build → parse."""
    packet = make_packet(marker=True, payload_type=0)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.marker is True


def test_marker_bit_clear_round_trips() -> None:
    """Marker bit False must also survive build → parse (not just True)."""
    packet = make_packet(marker=False, payload_type=0)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.marker is False


# ---------------------------------------------------------------------------
# 9. Various payload types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload_type", [0, 8, 18, 96, 111, 127])
def test_payload_type_round_trips(payload_type: int) -> None:
    """Payload types covering static (0=PCMU, 8=PCMA) and dynamic (96–127)."""
    packet = make_packet(payload_type=payload_type)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.header.payload_type == payload_type


# ---------------------------------------------------------------------------
# 10. Truncated data raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [0, 1, 5, 11])
def test_truncated_data_raises_value_error(length: int) -> None:
    """Any input shorter than 12 bytes must raise ValueError."""
    with pytest.raises(ValueError, match="too short"):
        parse_rtp(b"\x80" * length)


# ---------------------------------------------------------------------------
# 11. Wrong version raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_version", [0, 1, 3])
def test_wrong_version_raises_value_error(bad_version: int) -> None:
    """Version field != 2 must raise ValueError (RFC 3550 uses version 2 only)."""
    # Construct byte0 with the bad version in the top two bits.
    byte0 = (bad_version << 6) & 0xFF
    data = struct.pack("!BBHII", byte0, 0x00, 0, 0, 0)
    with pytest.raises(ValueError, match="version"):
        parse_rtp(data)


# ---------------------------------------------------------------------------
# 12. Truncated CSRC list raises ValueError
# ---------------------------------------------------------------------------

def test_truncated_csrc_list_raises_value_error() -> None:
    """
    CC=2 declares 8 bytes of CSRC list (2 × 4), but only 2 extra bytes are
    present (14 total).  Must raise ValueError.
    """
    # byte0: V=2, CC=2 => 0x82
    fixed = struct.pack("!BBHII", 0x82, 0x00, 0, 0, 0)  # 12 bytes
    truncated_csrc = b"\x00\x00"  # only 2 of the required 8 bytes
    data = fixed + truncated_csrc
    assert len(data) == 14

    with pytest.raises(ValueError, match="truncated"):
        parse_rtp(data)


# ---------------------------------------------------------------------------
# 13. Large payload round-trips
# ---------------------------------------------------------------------------

def test_large_payload_round_trips() -> None:
    """1000-byte payload must be preserved exactly through build → parse."""
    large_payload = bytes(i % 256 for i in range(1000))
    packet = make_packet(payload_type=96, payload=large_payload)
    recovered = parse_rtp(build_rtp(packet))
    assert recovered.payload == large_payload
    assert len(recovered.payload) == 1000


# ---------------------------------------------------------------------------
# 14. Empty payload is valid
# ---------------------------------------------------------------------------

def test_empty_payload_is_valid() -> None:
    """A 12-byte packet with no payload bytes is a valid RTP packet."""
    packet = make_packet(payload_type=0, payload=b"")
    serialised = build_rtp(packet)
    assert len(serialised) == 12
    recovered = parse_rtp(serialised)
    assert recovered.payload == b""


# ---------------------------------------------------------------------------
# 15. Known byte vector
# ---------------------------------------------------------------------------

def test_known_byte_vector() -> None:
    """
    Hand-crafted byte vector with known field values; parsing must produce
    exact expected field values.

    Packet:  V=2, P=0, X=0, CC=0, M=1, PT=0 (PCMU), seq=1, ts=160, ssrc=12345
      byte0 = 0x80  (V=2, P=0, X=0, CC=0)
      byte1 = 0x80  (M=1, PT=0)
      seq   = 0x0001
      ts    = 0x000000A0  (160, one 20 ms frame at 8 kHz)
      ssrc  = 0x00003039  (12345)

    This vector would appear in Wireshark for a standard PCMU audio stream.
    """
    header_bytes = bytes([
        0x80, 0x80,              # V=2 P=0 X=0 CC=0 | M=1 PT=0
        0x00, 0x01,              # sequence number = 1
        0x00, 0x00, 0x00, 0xA0,  # timestamp = 160
        0x00, 0x00, 0x30, 0x39,  # SSRC = 12345
    ])
    payload = b"\x00" * 160  # 160 samples of silence (PCMU, 20 ms at 8 kHz)
    data = header_bytes + payload

    packet = parse_rtp(data)

    assert packet.header.version == 2
    assert packet.header.padding is False
    assert packet.header.extension is False
    assert packet.header.csrc_count == 0
    assert packet.header.marker is True
    assert packet.header.payload_type == 0
    assert packet.header.sequence_number == 1
    assert packet.header.timestamp == 160
    assert packet.header.ssrc == 12345
    assert packet.payload == payload


def test_known_byte_vector_build_matches() -> None:
    """
    Build the same known packet from an RtpPacket and verify the output bytes
    match the hand-crafted vector exactly.
    """
    expected_header_bytes = bytes([
        0x80, 0x80,
        0x00, 0x01,
        0x00, 0x00, 0x00, 0xA0,
        0x00, 0x00, 0x30, 0x39,
    ])
    payload = b"\x00" * 160
    packet = make_packet(
        payload_type=0,
        sequence_number=1,
        timestamp=160,
        ssrc=12345,
        marker=True,
        payload=payload,
    )
    serialised = build_rtp(packet)
    assert serialised[:12] == expected_header_bytes
    assert serialised[12:] == payload


# ---------------------------------------------------------------------------
# Unsupported header fields fail explicitly rather than corrupting the payload
# ---------------------------------------------------------------------------

def test_parse_rejects_padding() -> None:
    """P=1 would leave the padding length byte in the payload, so parse_rtp
    refuses it rather than returning a wrong payload."""
    # byte0: V=2, P=1, X=0, CC=0 => 0xA0
    data = struct.pack("!BBHII", 0xA0, 0x00, 1, 160, 999) + b"payload\x01"
    with pytest.raises(ValueError, match="padding"):
        parse_rtp(data)


def test_parse_rejects_extension() -> None:
    """X=1 puts an extension header before the payload, which this parser does
    not skip, so parse_rtp refuses it rather than mislabeling the boundary."""
    # byte0: V=2, P=0, X=1, CC=0 => 0x90
    data = struct.pack("!BBHII", 0x90, 0x00, 1, 160, 999) + b"\x00\x00\x00\x01payload"
    with pytest.raises(ValueError, match="extension"):
        parse_rtp(data)


def test_build_rejects_padding() -> None:
    """build_rtp cannot emit padding bytes, so it refuses a padded packet."""
    with pytest.raises(ValueError, match="padding"):
        build_rtp(make_packet(padding=True, payload=b"x"))


def test_build_rejects_extension() -> None:
    """build_rtp cannot emit an extension header, so it refuses one."""
    with pytest.raises(ValueError, match="extension"):
        build_rtp(make_packet(extension=True, payload=b"x"))


def test_build_rejects_csrc() -> None:
    """build_rtp cannot emit a CSRC list, so it refuses csrc_count > 0."""
    with pytest.raises(ValueError, match="csrc"):
        build_rtp(make_packet(csrc_count=2, payload=b"x"))
