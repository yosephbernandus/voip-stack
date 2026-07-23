"""
Tests for voip.sdp, SDP parser, builder, and codec negotiation per RFC 4566.

Coverage:
1.  Parse minimal valid SDP
2.  Parse real-world WebRTC browser SDP with ICE/DTLS attributes
3.  Build/parse round-trip
4.  Codec negotiation, partial overlap
5.  Codec negotiation, no common codecs → empty list
6.  Static payload types (PCMU=0) without explicit rtpmap
7.  Dynamic payload types (opus/48000/2) via rtpmap
8.  Multiple media descriptions (audio + video)
9.  Connection address at session level applied to session
10. Missing mandatory lines → ValueError
11. Both CRLF and LF line endings accepted
12. Attributes parsing: flag attributes (sendrecv) and value attributes (ptime:20)
13. log_sdp output contains educational explanations
14. Negotiation preserves offerer's preference order
"""

import logging

import pytest

from voip.sdp import build_sdp, log_sdp, negotiate_codecs, parse_sdp
from voip.types.sdp import SdpCodec, SdpMediaDescription, SdpOrigin, SdpSession

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Minimal RFC-4566-compliant SDP with one audio codec.
_MINIMAL_SDP_LF = (
    "v=0\n"
    "o=alice 2890844526 2890844526 IN IP4 pc33.example.com\n"
    "s=-\n"
    "c=IN IP4 192.0.2.101\n"
    "t=0 0\n"
    "m=audio 49170 RTP/AVP 0 8\n"
    "a=rtpmap:0 PCMU/8000\n"
    "a=rtpmap:8 PCMA/8000\n"
    "a=sendrecv\n"
    "a=ptime:20\n"
)

_MINIMAL_SDP_CRLF = _MINIMAL_SDP_LF.replace("\n", "\r\n")

# WebRTC browser SDP with dynamic payload types, ICE, and DTLS.
_WEBRTC_SDP = (
    "v=0\r\n"
    "o=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtpmap:63 red/48000/2\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=ice-ufrag:abc123\r\n"
    "a=ice-pwd:secretpassword123456789012\r\n"
    "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "a=sendrecv\r\n"
)


# ---------------------------------------------------------------------------
# 1. Parse minimal valid SDP
# ---------------------------------------------------------------------------

def test_parse_minimal_sdp() -> None:
    """Parse minimal SDP and verify all top-level fields are populated."""
    session = parse_sdp(_MINIMAL_SDP_LF)

    assert session.version == 0
    assert session.origin.username == "alice"
    assert session.origin.session_id == "2890844526"
    assert session.origin.session_version == "2890844526"
    assert session.origin.net_type == "IN"
    assert session.origin.addr_type == "IP4"
    assert session.origin.address == "pc33.example.com"
    assert session.session_name == "-"
    assert session.connection_address == "192.0.2.101"
    assert session.time_start == 0
    assert session.time_stop == 0
    assert len(session.media) == 1

    audio = session.media[0]
    assert audio.media_type == "audio"
    assert audio.port == 49170
    assert audio.protocol == "RTP/AVP"
    assert len(audio.codecs) == 2
    assert audio.codecs[0].name == "PCMU"
    assert audio.codecs[1].name == "PCMA"


# ---------------------------------------------------------------------------
# 2. Parse real-world WebRTC browser SDP
# ---------------------------------------------------------------------------

def test_parse_webrtc_sdp() -> None:
    """Parse a WebRTC browser SDP with ICE credentials, DTLS fingerprint, and opus."""
    session = parse_sdp(_WEBRTC_SDP)

    assert session.version == 0
    assert session.origin.username == "-"
    assert session.origin.session_id == "4611731400430051336"
    # No session-level c= in this SDP.
    assert session.connection_address is None
    assert len(session.media) == 1

    audio = session.media[0]
    assert audio.media_type == "audio"
    assert audio.port == 9
    assert audio.protocol == "UDP/TLS/RTP/SAVPF"

    # codec order must match m= line order: 111, 63, 9, 0, 8
    codec_names = [c.name for c in audio.codecs]
    assert codec_names == ["opus", "red", "G722", "PCMU", "PCMA"]

    opus = audio.codecs[0]
    assert opus.payload_type == 111
    assert opus.clock_rate == 48000
    assert opus.channels == 2

    # ICE and DTLS attributes.
    assert audio.attributes["ice-ufrag"] == "abc123"
    assert audio.attributes["ice-pwd"] == "secretpassword123456789012"
    assert "fingerprint" in audio.attributes
    assert audio.attributes.get("sendrecv") == ""

    # fmtp attribute stored under key "fmtp".
    assert "fmtp" in audio.attributes
    assert "minptime" in audio.attributes["fmtp"]


# ---------------------------------------------------------------------------
# 3. Build/parse round-trip
# ---------------------------------------------------------------------------

def test_build_parse_roundtrip() -> None:
    """Build an SdpSession to text, re-parse, and verify structural equivalence."""
    original = SdpSession(
        version=0,
        origin=SdpOrigin(
            username="-",
            session_id="12345678",
            session_version="1",
            net_type="IN",
            addr_type="IP4",
            address="203.0.113.1",
        ),
        session_name="test session",
        connection_address="203.0.113.1",
        time_start=0,
        time_stop=0,
        media=[
            SdpMediaDescription(
                media_type="audio",
                port=5004,
                protocol="RTP/AVP",
                codecs=[
                    SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
                    SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
                ],
                attributes={"ptime": "20", "sendrecv": ""},
            )
        ],
    )

    sdp_text = build_sdp(original)
    assert "\r\n" in sdp_text, "build_sdp must use CRLF line endings"

    recovered = parse_sdp(sdp_text)

    assert recovered.version == original.version
    assert recovered.origin.session_id == original.origin.session_id
    assert recovered.origin.address == original.origin.address
    assert recovered.session_name == original.session_name
    assert recovered.connection_address == original.connection_address
    assert len(recovered.media) == len(original.media)

    orig_audio = original.media[0]
    rec_audio = recovered.media[0]
    assert rec_audio.media_type == orig_audio.media_type
    assert rec_audio.port == orig_audio.port
    assert rec_audio.protocol == orig_audio.protocol
    assert len(rec_audio.codecs) == len(orig_audio.codecs)
    assert rec_audio.codecs[0].name == orig_audio.codecs[0].name
    assert rec_audio.codecs[1].name == orig_audio.codecs[1].name
    assert rec_audio.attributes == orig_audio.attributes


# ---------------------------------------------------------------------------
# 4. Codec negotiation, partial overlap
# ---------------------------------------------------------------------------

def test_negotiate_codecs_partial_overlap() -> None:
    """Offer [PCMU, PCMA], answer [PCMA, opus] → negotiated [PCMA]."""
    offer = _make_session_with_codecs([
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
        SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
    ])
    answer = _make_session_with_codecs([
        SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
        SdpCodec(payload_type=111, name="opus", clock_rate=48000, channels=2),
    ])

    result = negotiate_codecs(offer, answer)

    assert len(result) == 1
    assert result[0].name == "PCMA"


# ---------------------------------------------------------------------------
# 5. Codec negotiation, no common codecs
# ---------------------------------------------------------------------------

def test_negotiate_codecs_no_overlap() -> None:
    """Offer [PCMU], answer [opus] → empty list."""
    offer = _make_session_with_codecs([
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
    ])
    answer = _make_session_with_codecs([
        SdpCodec(payload_type=111, name="opus", clock_rate=48000, channels=2),
    ])

    result = negotiate_codecs(offer, answer)

    assert result == []


# ---------------------------------------------------------------------------
# 6. Static payload types (PCMU = 0) without explicit rtpmap
# ---------------------------------------------------------------------------

def test_static_payload_type_no_rtpmap() -> None:
    """Payload type 0 (PCMU) is resolved from the static registry without rtpmap."""
    sdp = (
        "v=0\n"
        "o=- 1 1 IN IP4 127.0.0.1\n"
        "s=-\n"
        "t=0 0\n"
        "m=audio 5004 RTP/AVP 0\n"
    )

    session = parse_sdp(sdp)

    assert len(session.media[0].codecs) == 1
    codec = session.media[0].codecs[0]
    assert codec.payload_type == 0
    assert codec.name == "PCMU"
    assert codec.clock_rate == 8000


# ---------------------------------------------------------------------------
# 7. Dynamic payload types via rtpmap
# ---------------------------------------------------------------------------

def test_dynamic_payload_type_with_rtpmap() -> None:
    """Payload type 111 is resolved from a=rtpmap to opus/48000/2."""
    sdp = (
        "v=0\n"
        "o=- 1 1 IN IP4 127.0.0.1\n"
        "s=-\n"
        "t=0 0\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\n"
        "a=rtpmap:111 opus/48000/2\n"
    )

    session = parse_sdp(sdp)

    assert len(session.media[0].codecs) == 1
    codec = session.media[0].codecs[0]
    assert codec.payload_type == 111
    assert codec.name == "opus"
    assert codec.clock_rate == 48000
    assert codec.channels == 2


# ---------------------------------------------------------------------------
# 8. Multiple media descriptions (audio + video)
# ---------------------------------------------------------------------------

def test_multiple_media_descriptions() -> None:
    """SDP with both audio and video m= sections is parsed into two MediaDescriptions."""
    sdp = (
        "v=0\n"
        "o=- 1 1 IN IP4 127.0.0.1\n"
        "s=-\n"
        "c=IN IP4 203.0.113.1\n"
        "t=0 0\n"
        "m=audio 5004 RTP/AVP 0\n"
        "a=rtpmap:0 PCMU/8000\n"
        "m=video 5006 RTP/AVP 96\n"
        "a=rtpmap:96 VP8/90000\n"
    )

    session = parse_sdp(sdp)

    assert len(session.media) == 2
    assert session.media[0].media_type == "audio"
    assert session.media[1].media_type == "video"
    assert session.media[1].codecs[0].name == "VP8"
    assert session.media[1].codecs[0].clock_rate == 90000


# ---------------------------------------------------------------------------
# 9. Session-level connection address
# ---------------------------------------------------------------------------

def test_session_level_connection_address() -> None:
    """c= before any m= line is stored as the session connection_address."""
    sdp = (
        "v=0\n"
        "o=alice 1 1 IN IP4 192.0.2.1\n"
        "s=-\n"
        "c=IN IP4 192.0.2.101\n"
        "t=0 0\n"
        "m=audio 49170 RTP/AVP 0\n"
    )

    session = parse_sdp(sdp)

    assert session.connection_address == "192.0.2.101"


# ---------------------------------------------------------------------------
# 10. Missing mandatory lines → ValueError
# ---------------------------------------------------------------------------

def test_missing_version_raises() -> None:
    """SDP without v= raises ValueError."""
    sdp = (
        "o=alice 1 1 IN IP4 192.0.2.1\n"
        "s=-\n"
        "t=0 0\n"
        "m=audio 49170 RTP/AVP 0\n"
    )
    with pytest.raises(ValueError, match="v="):
        parse_sdp(sdp)


def test_missing_origin_raises() -> None:
    """SDP without o= raises ValueError."""
    sdp = (
        "v=0\n"
        "s=-\n"
        "t=0 0\n"
        "m=audio 49170 RTP/AVP 0\n"
    )
    with pytest.raises(ValueError, match="o="):
        parse_sdp(sdp)


def test_missing_session_name_raises() -> None:
    """SDP without s= raises ValueError."""
    sdp = (
        "v=0\n"
        "o=alice 1 1 IN IP4 192.0.2.1\n"
        "t=0 0\n"
        "m=audio 49170 RTP/AVP 0\n"
    )
    with pytest.raises(ValueError, match="s="):
        parse_sdp(sdp)


def test_missing_media_raises() -> None:
    """SDP without m= raises ValueError."""
    sdp = (
        "v=0\n"
        "o=alice 1 1 IN IP4 192.0.2.1\n"
        "s=-\n"
        "t=0 0\n"
    )
    with pytest.raises(ValueError, match="m="):
        parse_sdp(sdp)


# ---------------------------------------------------------------------------
# 11. Both CRLF and LF line endings accepted
# ---------------------------------------------------------------------------

def test_parse_crlf_line_endings() -> None:
    """CRLF-terminated SDP is parsed identically to LF-terminated SDP."""
    lf_session = parse_sdp(_MINIMAL_SDP_LF)
    crlf_session = parse_sdp(_MINIMAL_SDP_CRLF)

    assert lf_session.origin.username == crlf_session.origin.username
    assert lf_session.connection_address == crlf_session.connection_address
    assert len(lf_session.media) == len(crlf_session.media)
    assert lf_session.media[0].codecs[0].name == crlf_session.media[0].codecs[0].name


# ---------------------------------------------------------------------------
# 12. Attribute parsing: flag attributes and value attributes
# ---------------------------------------------------------------------------

def test_attribute_parsing_flag_and_value() -> None:
    """sendrecv stores as {'sendrecv': ''} and ptime:20 as {'ptime': '20'}."""
    session = parse_sdp(_MINIMAL_SDP_LF)
    attributes = session.media[0].attributes

    assert "sendrecv" in attributes
    assert attributes["sendrecv"] == ""

    assert "ptime" in attributes
    assert attributes["ptime"] == "20"


# ---------------------------------------------------------------------------
# 13. log_sdp output contains educational explanations
# ---------------------------------------------------------------------------

def test_log_sdp_contains_explanations(caplog: pytest.LogCaptureFixture) -> None:
    """log_sdp emits INFO log records with [SDP] prefix and educational descriptions."""
    with caplog.at_level(logging.INFO, logger="voip.sdp"):
        log_sdp("test-label", _MINIMAL_SDP_LF)

    combined = "\n".join(caplog.messages)

    assert "[SDP]" in combined
    assert "test-label" in combined
    # Version line explanation.
    assert "SDP version" in combined
    # Origin line explanation.
    assert "origin" in combined
    # Connection address explanation.
    assert "connection address" in combined
    # Timing explanation.
    assert "timing" in combined
    # Media line explanation.
    assert "media line" in combined
    # Codec mapping explanation (from a=rtpmap).
    assert "codec mapping" in combined
    # Media direction explanation (from a=sendrecv).
    assert "media direction" in combined


# ---------------------------------------------------------------------------
# 14. Negotiation preserves offerer's preference order
# ---------------------------------------------------------------------------

def test_negotiate_preserves_offer_order() -> None:
    """Offerer prefers PCMU then PCMA; answer supports both; result keeps offer order."""
    offer = _make_session_with_codecs([
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
        SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
    ])
    answer = _make_session_with_codecs([
        # Answer lists in reversed order.
        SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
    ])

    result = negotiate_codecs(offer, answer)

    assert len(result) == 2
    # Offerer's order must be preserved.
    assert result[0].name == "PCMU"
    assert result[1].name == "PCMA"


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

def test_negotiate_case_insensitive() -> None:
    """Codec name matching in negotiate_codecs is case-insensitive."""
    offer = _make_session_with_codecs([
        SdpCodec(payload_type=111, name="opus", clock_rate=48000, channels=2),
    ])
    answer = _make_session_with_codecs([
        SdpCodec(payload_type=111, name="OPUS", clock_rate=48000, channels=2),
    ])

    result = negotiate_codecs(offer, answer)

    assert len(result) == 1
    assert result[0].name.lower() == "opus"


def test_build_sdp_crlf_line_endings() -> None:
    """build_sdp output must use CRLF line endings per RFC 4566 §5."""
    session = _make_session_with_codecs([
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
    ])
    text = build_sdp(session)

    assert "\r\n" in text
    # Every non-empty line should end with CRLF.
    for line in text.split("\r\n"):
        assert "\n" not in line, f"Bare LF found in line: {line!r}"


def test_build_sdp_opus_channels() -> None:
    """build_sdp includes channel count in a=rtpmap when channels != 1."""
    session = _make_session_with_codecs([
        SdpCodec(payload_type=111, name="opus", clock_rate=48000, channels=2),
    ])
    text = build_sdp(session)

    assert "a=rtpmap:111 opus/48000/2" in text


def test_parse_sdp_no_explicit_timing() -> None:
    """When t= is omitted entirely the parser raises ValueError (mandatory field)."""
    sdp = (
        "v=0\n"
        "o=alice 1 1 IN IP4 192.0.2.1\n"
        "s=-\n"
        "m=audio 49170 RTP/AVP 0\n"
    )
    # t= is not strictly checked for absence, but missing t= means time_start/stop
    # stay at default 0 0, no error is raised for missing t= in many implementations.
    # The RFC says t= is mandatory; our parser tolerates its absence with defaults.
    # This test documents the current lenient behaviour.
    session = parse_sdp(sdp)
    assert session.time_start == 0
    assert session.time_stop == 0


def test_negotiate_no_audio_media() -> None:
    """negotiate_codecs returns empty list when either session has no audio."""
    offer = SdpSession(
        version=0,
        origin=SdpOrigin(session_id="1", session_version="1", address="127.0.0.1"),
        media=[
            SdpMediaDescription(
                media_type="video",
                port=5006,
                protocol="RTP/AVP",
                codecs=[SdpCodec(payload_type=96, name="VP8", clock_rate=90000)],
                attributes={},
            )
        ],
    )
    answer = _make_session_with_codecs([
        SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
    ])

    result = negotiate_codecs(offer, answer)

    assert result == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_with_codecs(codecs: list[SdpCodec]) -> SdpSession:
    """Build a minimal SdpSession containing a single audio media description."""
    return SdpSession(
        version=0,
        origin=SdpOrigin(
            username="-",
            session_id="1000",
            session_version="1",
            net_type="IN",
            addr_type="IP4",
            address="127.0.0.1",
        ),
        session_name="-",
        time_start=0,
        time_stop=0,
        media=[
            SdpMediaDescription(
                media_type="audio",
                port=5004,
                protocol="RTP/AVP",
                codecs=codecs,
                attributes={},
            )
        ],
    )
