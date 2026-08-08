"""Optional native RTP parser and adapter for the Python learning model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._native import ParsedRtp, parse_rtp

if TYPE_CHECKING:
    from voip.types.rtp import RtpPacket

__all__ = ["ParsedRtp", "parse_rtp", "parse_rtp_model"]


def parse_rtp_model(data: bytes) -> RtpPacket:
    """Parse with Rust, then return the same Pydantic model as ``voip.rtp``."""
    from voip.types.rtp import RtpHeader, RtpPacket

    parsed = parse_rtp(data)
    header = RtpHeader(
        version=parsed.version,
        padding=parsed.padding,
        extension=parsed.extension,
        csrc_count=parsed.csrc_count,
        marker=parsed.marker,
        payload_type=parsed.payload_type,
        sequence_number=parsed.sequence_number,
        timestamp=parsed.timestamp,
        ssrc=parsed.ssrc,
    )
    return RtpPacket(header=header, payload=parsed.payload)
