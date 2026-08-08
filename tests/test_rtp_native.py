"""Parity tests for the optional PyO3 RTP parser."""

from __future__ import annotations

import random
import struct

import pytest

from voip.rtp import parse_rtp as parse_rtp_python

voip_rtp_native = pytest.importorskip("voip_rtp_native")


def make_raw_packet(
    *,
    payload_type: int = 0,
    sequence_number: int = 0,
    timestamp: int = 0,
    ssrc: int = 0,
    marker: bool = False,
    csrcs: tuple[int, ...] = (),
    payload: bytes = b"",
) -> bytes:
    byte0 = 0x80 | len(csrcs)
    byte1 = (int(marker) << 7) | payload_type
    fixed = struct.pack(
        "!BBHII", byte0, byte1, sequence_number, timestamp, ssrc
    )
    csrc_list = b"".join(struct.pack("!I", csrc) for csrc in csrcs)
    return fixed + csrc_list + payload


def assert_same_packet(data: bytes) -> None:
    expected = parse_rtp_python(data)
    actual = voip_rtp_native.parse_rtp(data)

    assert actual.version == expected.header.version
    assert actual.padding == expected.header.padding
    assert actual.extension == expected.header.extension
    assert actual.csrc_count == expected.header.csrc_count
    assert actual.marker == expected.header.marker
    assert actual.payload_type == expected.header.payload_type
    assert actual.sequence_number == expected.header.sequence_number
    assert actual.timestamp == expected.header.timestamp
    assert actual.ssrc == expected.header.ssrc
    assert actual.header_length == 12 + expected.header.csrc_count * 4
    assert actual.payload == expected.payload
    assert voip_rtp_native.parse_rtp_model(data) == expected


def test_known_vector_matches_python_parser() -> None:
    data = make_raw_packet(
        payload_type=0,
        sequence_number=1,
        timestamp=160,
        ssrc=12345,
        marker=True,
        payload=b"\x00" * 160,
    )
    assert_same_packet(data)


def test_csrc_list_and_payload_offset_match_python_parser() -> None:
    data = make_raw_packet(
        payload_type=111,
        sequence_number=65535,
        timestamp=4294967295,
        ssrc=0xDEADBEEF,
        csrcs=(100, 200, 300),
        payload=b"audio",
    )
    assert_same_packet(data)


def test_deterministic_random_packets_match_python_parser() -> None:
    random_source = random.Random(2026)

    for _ in range(500):
        csrcs = tuple(
            random_source.randrange(0, 2**32)
            for _ in range(random_source.randrange(0, 5))
        )
        payload = random_source.randbytes(random_source.randrange(0, 512))
        data = make_raw_packet(
            payload_type=random_source.randrange(0, 128),
            sequence_number=random_source.randrange(0, 2**16),
            timestamp=random_source.randrange(0, 2**32),
            ssrc=random_source.randrange(0, 2**32),
            marker=bool(random_source.randrange(0, 2)),
            csrcs=csrcs,
            payload=payload,
        )
        assert_same_packet(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\x80" * 11, "too short"),
        (struct.pack("!BBHII", 0x40, 0, 0, 0, 0), "version"),
        (struct.pack("!BBHII", 0x82, 0, 0, 0, 0) + b"\x00\x00", "truncated"),
        (struct.pack("!BBHII", 0xA0, 0, 0, 0, 0) + b"payload\x01", "padding"),
        (struct.pack("!BBHII", 0x90, 0, 0, 0, 0) + b"extension", "extension"),
    ],
)
def test_invalid_packet_errors_match_supported_subset(data: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_rtp_python(data)
    with pytest.raises(ValueError, match=message):
        voip_rtp_native.parse_rtp(data)
