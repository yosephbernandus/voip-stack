"""
SDP (Session Description Protocol) type definitions per RFC 4566.

Transformation pipeline:
  raw text -> SdpSession                              (parse)
  SdpSession -> raw text                             (serialise)
  SdpSession + SdpSession -> list[SdpCodec]          (codec negotiation)
"""

from __future__ import annotations

from pydantic import BaseModel


class SdpCodec(BaseModel):
    """
    A single audio/video codec described by an rtpmap attribute per RFC 4566 §6.

    The payload_type is the dynamic (96–127) or static (0–95) RTP payload type
    number used in the "m=" line and in RTP headers.  Static assignments (e.g.
    PCMU=0, PCMA=8) are defined in RFC 3551; dynamic numbers are negotiated
    per-session via SDP.
    """

    # RTP payload type number (RFC 3551 §6 for static, RFC 4566 §6 for dynamic)
    payload_type: int
    # Codec name as registered with IANA, e.g. "PCMU", "PCMA", "opus", "G722"
    name: str
    # RTP clock rate in Hz (e.g. 8000 for narrowband, 48000 for opus)
    clock_rate: int
    # Number of audio channels; almost always 1 for telephony codecs
    channels: int = 1


class SdpMediaDescription(BaseModel):
    """
    A single media section ("m=" line and its attributes) per RFC 4566 §5.14.

    A session can contain multiple media descriptions (audio + video, for
    example).  Each has its own port and codec list so that offer/answer
    negotiation can accept or reject media types independently (RFC 3264).
    """

    # "audio", "video", "application", etc. (RFC 4566 §8)
    media_type: str
    # UDP port on which the sender will receive RTP data (RFC 4566 §5.14)
    port: int
    # Transport protocol: "RTP/AVP" (RFC 3551) or "RTP/SAVP" (SRTP, RFC 3711)
    protocol: str
    # Codecs listed in order of preference; first is most preferred
    codecs: list[SdpCodec]
    # Parsed "a=" lines, e.g. {"ptime": "20", "sendrecv": ""}
    attributes: dict[str, str]


class SdpOrigin(BaseModel):
    """
    The "o=" (origin) field per RFC 4566 §5.2.

    Uniquely identifies the session and its version.  When the session is
    modified (e.g. a re-INVITE changes codecs), session_version must be
    incremented while session_id stays the same so that endpoints can detect
    the change.
    """

    # "-" by default when the tool generating the SDP has no username concept
    username: str = "-"
    # Globally unique session identifier, typically a NTP timestamp (§5.2)
    session_id: str
    # Monotonically increasing; bump on every session modification (RFC 3264 §8)
    session_version: str
    # Network type; always "IN" (Internet) in practice
    net_type: str = "IN"
    # Address type: "IP4" or "IP6"
    addr_type: str = "IP4"
    # Unicast address of the originating host
    address: str


class SdpSession(BaseModel):
    """
    A complete SDP session description per RFC 4566 §5.

    The field ordering mirrors the mandatory SDP line order:
      v=  version
      o=  origin
      s=  session name
      c=  connection data (optional at session level when each "m=" has its own)
      t=  timing
      m=  media descriptions (one or more)

    Time values of 0/0 mean "unbounded" and are standard for VoIP sessions that
    have no fixed start/end time (RFC 4566 §5.9).
    """

    # Always 0; the SDP version field, not the session version (RFC 4566 §5.1)
    version: int = 0
    origin: SdpOrigin
    # "-" is conventional when the session name is not meaningful
    session_name: str = "-"
    # Session-level connection address; individual media sections may override
    connection_address: str | None = None
    # NTP timestamp; 0 = session start is unspecified / permanent
    time_start: int = 0
    # NTP timestamp; 0 = no fixed end time
    time_stop: int = 0
    # One entry per "m=" block; typically just one for audio-only VoIP
    media: list[SdpMediaDescription]


# ---------------------------------------------------------------------------
# Function signatures implemented in voip.sdp (documented here for
# discoverability alongside the types they operate on).
# ---------------------------------------------------------------------------
#
# parse_sdp(raw: str) -> SdpSession
#   Parse a CRLF- or LF-delimited SDP body into a structured SdpSession.
#   Raises ValueError on malformed input (e.g. missing mandatory lines).
#
# build_sdp(session: SdpSession) -> str
#   Serialise an SdpSession to the canonical line-by-line text format.
#   Lines are separated by CRLF as required by RFC 4566 §5.
#
# negotiate_codecs(offer: SdpSession, answer: SdpSession) -> list[SdpCodec]
#   Return the intersection of codecs in the first audio media descriptions of
#   offer and answer, preserving the offerer's preference order (RFC 3264 §6.1).
#   Returns an empty list if there is no common codec (call should be rejected
#   with 488 Not Acceptable Here).
