"""
Public re-exports for the voip.types package.

Import from here rather than from submodules to keep call-site imports short:

    from voip.types import SipRequest, SipResponse, SipMethod, SipMessage
    from voip.types import SdpSession, SdpCodec
    from voip.types import RtpPacket, RtpHeader
    from voip.types import JitterBufferConfig, BufferStats
    from voip.types import AudioFormat, PcmStream

The JitterBuffer class itself lives in voip.jitter. It is a stateful
implementation, not a type.

Submodule imports remain valid for code that prefers to be explicit about
which protocol layer a symbol comes from.
"""

from __future__ import annotations

# SIP (RFC 3261)
from voip.types.sip import (
    SipMessage,
    SipMethod,
    SipRequest,
    SipResponse,
)

# SDP (RFC 4566)
from voip.types.sdp import (
    SdpCodec,
    SdpMediaDescription,
    SdpOrigin,
    SdpSession,
)

# RTP (RFC 3550)
from voip.types.rtp import (
    RtpHeader,
    RtpPacket,
)

# Jitter buffer
from voip.types.jitter import (
    BufferStats,
    JitterBufferConfig,
    PacketStatus,
)

# Audio mixing and PCM
from voip.types.audio import (
    AudioFormat,
    PcmStream,
)

__all__ = [
    # SIP
    "SipMethod",
    "SipRequest",
    "SipResponse",
    "SipMessage",
    # SDP
    "SdpCodec",
    "SdpMediaDescription",
    "SdpOrigin",
    "SdpSession",
    # RTP
    "RtpHeader",
    "RtpPacket",
    # Jitter buffer
    "PacketStatus",
    "JitterBufferConfig",
    "BufferStats",
    # Audio
    "AudioFormat",
    "PcmStream",
]
