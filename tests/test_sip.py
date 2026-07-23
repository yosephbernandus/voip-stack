"""
Tests for voip.sip, SIP parser and builder per RFC 3261.

Coverage:
- Parse INVITE request with SDP body
- Parse 200 OK response with SDP body
- Build/parse round-trip for each SipMethod
- Build/parse round-trip for common response codes
- Multi-line header continuation (RFC 3261 §7.3.1)
- Missing start-line raises ValueError
- Case-insensitive header parsing
- Body absent (no body, or empty after CRLF CRLF)
- RFC 3261 §24 example messages
"""

import pytest

from voip.sip import build_sip_request, build_sip_response, parse_sip
from voip.types.sip import SipMethod, SipRequest, SipResponse

# ---------------------------------------------------------------------------
# Fixtures, reusable raw SIP strings
# ---------------------------------------------------------------------------

# Minimal SDP body used in INVITE examples (not RFC-valid SDP, just for testing)
_SDP_BODY = (
    "v=0\r\n"
    "o=alice 2890844526 2890844526 IN IP4 pc33.example.com\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.1\r\n"
    "t=0 0\r\n"
    "m=audio 49170 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
)

# Full INVITE message closely following RFC 3261 §24.2
_INVITE_RAW = (
    "INVITE sip:bob@biloxi.example.com SIP/2.0\r\n"
    "Via: SIP/2.0/UDP pc33.atlanta.example.com;branch=z9hG4bK776asdhds\r\n"
    "Max-Forwards: 70\r\n"
    "To: Bob <sip:bob@biloxi.example.com>\r\n"
    "From: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
    "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
    "CSeq: 314159 INVITE\r\n"
    "Contact: <sip:alice@pc33.atlanta.example.com>\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 142\r\n"
    "\r\n"
    + _SDP_BODY
)

# 200 OK response to the above INVITE
_OK_RAW = (
    "SIP/2.0 200 OK\r\n"
    "Via: SIP/2.0/UDP pc33.atlanta.example.com;branch=z9hG4bK776asdhds;received=192.0.2.1\r\n"
    "To: Bob <sip:bob@biloxi.example.com>;tag=a6c85cf\r\n"
    "From: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
    "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
    "CSeq: 314159 INVITE\r\n"
    "Contact: <sip:bob@192.0.2.4>\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 131\r\n"
    "\r\n"
    + _SDP_BODY
)

# ---------------------------------------------------------------------------
# 1. Parse INVITE request with SDP body
# ---------------------------------------------------------------------------


class TestParseInviteRequest:
    def test_method_is_invite(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.method == SipMethod.INVITE

    def test_uri(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.uri == "sip:bob@biloxi.example.com"

    def test_version(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.version == "SIP/2.0"

    def test_via_header(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert "Via" in msg.headers
        assert "z9hG4bK776asdhds" in msg.headers["Via"]

    def test_call_id_header(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.headers["Call-ID"] == "a84b4c76e66710@pc33.atlanta.example.com"

    def test_content_type_header(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.headers["Content-Type"] == "application/sdp"

    def test_body_present(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.body is not None
        assert "v=0" in msg.body

    def test_max_forwards_header(self) -> None:
        msg = parse_sip(_INVITE_RAW)
        assert isinstance(msg, SipRequest)
        assert msg.headers["Max-Forwards"] == "70"


# ---------------------------------------------------------------------------
# 2. Parse 200 OK response with SDP body
# ---------------------------------------------------------------------------


class TestParse200OkResponse:
    def test_is_response(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)

    def test_status_code(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.status_code == 200

    def test_reason_phrase(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.reason_phrase == "OK"

    def test_version(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.version == "SIP/2.0"

    def test_call_id_header(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.headers["Call-ID"] == "a84b4c76e66710@pc33.atlanta.example.com"

    def test_body_present(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.body is not None
        assert "v=0" in msg.body

    def test_content_type_header(self) -> None:
        msg = parse_sip(_OK_RAW)
        assert isinstance(msg, SipResponse)
        assert msg.headers["Content-Type"] == "application/sdp"


# ---------------------------------------------------------------------------
# 3. Build/parse round-trip for each SipMethod
# ---------------------------------------------------------------------------


class TestRequestRoundTrip:
    """Build a request then parse it back; result must match original data."""

    _BASE_HEADERS = {
        "Via": "SIP/2.0/UDP pc33.example.com;branch=z9hG4bKnashds8",
        "Max-Forwards": "70",
        "From": "Alice <sip:alice@example.com>;tag=9fxced76sl",
        "To": "Bob <sip:bob@example.com>",
        "Call-ID": "3848276298220188511@pc33.example.com",
        "CSeq": "1 {method}",
    }

    def _headers(self, method: str) -> dict[str, str]:
        return {k: v.format(method=method) for k, v in self._BASE_HEADERS.items()}

    @pytest.mark.parametrize("method", list(SipMethod))
    def test_method_round_trip(self, method: SipMethod) -> None:
        uri = "sip:bob@example.com"
        headers = self._headers(method.value)
        raw = build_sip_request(method, uri, headers)
        parsed = parse_sip(raw)
        assert isinstance(parsed, SipRequest)
        assert parsed.method == method
        assert parsed.uri == uri
        assert parsed.version == "SIP/2.0"
        assert parsed.body is None

    def test_invite_with_body_round_trip(self) -> None:
        headers = {
            **self._headers("INVITE"),
            "Content-Type": "application/sdp",
            "Content-Length": str(len(_SDP_BODY)),
        }
        raw = build_sip_request(SipMethod.INVITE, "sip:bob@example.com", headers, _SDP_BODY)
        parsed = parse_sip(raw)
        assert isinstance(parsed, SipRequest)
        assert parsed.method == SipMethod.INVITE
        assert parsed.body == _SDP_BODY

    def test_headers_preserved_in_round_trip(self) -> None:
        headers = self._headers("BYE")
        raw = build_sip_request(SipMethod.BYE, "sip:bob@example.com", headers)
        parsed = parse_sip(raw)
        assert isinstance(parsed, SipRequest)
        assert parsed.headers["Call-ID"] == "3848276298220188511@pc33.example.com"
        assert parsed.headers["Max-Forwards"] == "70"


# ---------------------------------------------------------------------------
# 4. Build/parse round-trip for responses
# ---------------------------------------------------------------------------


class TestResponseRoundTrip:
    _BASE_HEADERS = {
        "Via": "SIP/2.0/UDP pc33.example.com;branch=z9hG4bKnashds8",
        "From": "Alice <sip:alice@example.com>;tag=9fxced76sl",
        "To": "Bob <sip:bob@example.com>",
        "Call-ID": "3848276298220188511@pc33.example.com",
        "CSeq": "1 INVITE",
    }

    @pytest.mark.parametrize(
        "status_code,reason",
        [
            (100, "Trying"),
            (180, "Ringing"),
            (200, "OK"),
            (404, "Not Found"),
            (486, "Busy Here"),
        ],
    )
    def test_response_round_trip(self, status_code: int, reason: str) -> None:
        raw = build_sip_response(status_code, reason, self._BASE_HEADERS)
        parsed = parse_sip(raw)
        assert isinstance(parsed, SipResponse)
        assert parsed.status_code == status_code
        assert parsed.reason_phrase == reason
        assert parsed.version == "SIP/2.0"
        assert parsed.body is None

    def test_200_ok_with_sdp_body_round_trip(self) -> None:
        headers = {
            **self._BASE_HEADERS,
            "Content-Type": "application/sdp",
            "Content-Length": str(len(_SDP_BODY)),
        }
        raw = build_sip_response(200, "OK", headers, _SDP_BODY)
        parsed = parse_sip(raw)
        assert isinstance(parsed, SipResponse)
        assert parsed.status_code == 200
        assert parsed.body == _SDP_BODY


# ---------------------------------------------------------------------------
# 5. Multi-line header continuation (RFC 3261 §7.3.1)
# ---------------------------------------------------------------------------


class TestMultiLineHeaderFolding:
    def test_folded_header_is_joined(self) -> None:
        # A Contact header folded over two lines with a leading space
        raw = (
            "REGISTER sip:registrar.example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "From: Alice <sip:alice@example.com>;tag=abc\r\n"
            "To: Alice <sip:alice@example.com>\r\n"
            "Call-ID: fold-test@example.com\r\n"
            "CSeq: 1 REGISTER\r\n"
            "Contact: <sip:alice@pc33.example.com>\r\n"
            " ;expires=3600\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        # Both parts of the folded Contact header must be present in a single value
        assert ";expires=3600" in msg.headers["Contact"]

    def test_tab_continuation(self) -> None:
        # RFC 3261 §7.3.1 allows HT (tab) as folding indicator too
        raw = (
            "REGISTER sip:registrar.example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "From: Alice <sip:alice@example.com>;tag=abc\r\n"
            "To: Alice <sip:alice@example.com>\r\n"
            "Call-ID: tab-fold@example.com\r\n"
            "CSeq: 1 REGISTER\r\n"
            "Subject: I know you're\r\n"
            "\tthere, pick up the phone\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert "there, pick up the phone" in msg.headers["Subject"]


# ---------------------------------------------------------------------------
# 6. Malformed input raises ValueError
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="missing the start-line"):
            parse_sip("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="missing the start-line"):
            parse_sip("   \r\n")

    def test_bad_request_line_raises(self) -> None:
        raw = "INVITE sip:bob@example.com\r\n\r\n"
        with pytest.raises(ValueError, match="Malformed SIP Request-Line"):
            parse_sip(raw)

    def test_unknown_method_raises(self) -> None:
        raw = "FROBBLE sip:bob@example.com SIP/2.0\r\n\r\n"
        with pytest.raises(ValueError, match="Unknown SIP method"):
            parse_sip(raw)

    def test_status_code_not_integer_raises(self) -> None:
        raw = "SIP/2.0 ABC OK\r\n\r\n"
        with pytest.raises(ValueError, match="status code must be an integer"):
            parse_sip(raw)

    def test_header_missing_colon_raises(self) -> None:
        raw = "REGISTER sip:example.com SIP/2.0\r\nVia SIP/2.0/UDP pc33\r\n\r\n"
        with pytest.raises(ValueError, match="missing ':' separator"):
            parse_sip(raw)


# ---------------------------------------------------------------------------
# 7. Case-insensitive header parsing
# ---------------------------------------------------------------------------


class TestCaseInsensitiveHeaders:
    def test_lowercase_content_type_canonicalized(self) -> None:
        raw = (
            "INVITE sip:bob@example.com SIP/2.0\r\n"
            "content-type: application/sdp\r\n"
            "call-id: test-case@example.com\r\n"
            "cseq: 1 INVITE\r\n"
            "from: Alice <sip:alice@example.com>;tag=1\r\n"
            "to: Bob <sip:bob@example.com>\r\n"
            "via: SIP/2.0/UDP pc33.example.com\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        # Keys must be canonically capitalized regardless of input case
        assert "Content-Type" in msg.headers
        assert msg.headers["Content-Type"] == "application/sdp"

    def test_mixed_case_call_id_canonicalized(self) -> None:
        raw = (
            "INVITE sip:bob@example.com SIP/2.0\r\n"
            "CALL-ID: mixed-case@example.com\r\n"
            "CSeq: 1 INVITE\r\n"
            "From: Alice <sip:alice@example.com>;tag=1\r\n"
            "To: Bob <sip:bob@example.com>\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert "Call-ID" in msg.headers
        assert msg.headers["Call-ID"] == "mixed-case@example.com"

    def test_uppercase_headers_canonicalized(self) -> None:
        raw = (
            "SIP/2.0 200 OK\r\n"
            "VIA: SIP/2.0/UDP pc33.example.com\r\n"
            "FROM: Alice <sip:alice@example.com>;tag=1\r\n"
            "TO: Bob <sip:bob@example.com>\r\n"
            "CALL-ID: upper@example.com\r\n"
            "CSEQ: 1 INVITE\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipResponse)
        assert "Via" in msg.headers
        assert "From" in msg.headers
        assert "To" in msg.headers


# ---------------------------------------------------------------------------
# 8. Body absent
# ---------------------------------------------------------------------------


class TestBodyAbsent:
    def test_request_no_body_after_blank_line(self) -> None:
        raw = (
            "ACK sip:bob@example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "From: Alice <sip:alice@example.com>;tag=1\r\n"
            "To: Bob <sip:bob@example.com>\r\n"
            "Call-ID: ack-test@example.com\r\n"
            "CSeq: 1 ACK\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.body is None

    def test_request_no_crlf_crlf_separator(self) -> None:
        # Some minimalist implementations may omit the trailing blank line
        raw = (
            "BYE sip:bob@example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "From: Alice <sip:alice@example.com>;tag=1\r\n"
            "To: Bob <sip:bob@example.com>\r\n"
            "Call-ID: bye-test@example.com\r\n"
            "CSeq: 2 BYE"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.body is None

    def test_response_no_body(self) -> None:
        raw = (
            "SIP/2.0 100 Trying\r\n"
            "Via: SIP/2.0/UDP pc33.example.com\r\n"
            "From: Alice <sip:alice@example.com>;tag=1\r\n"
            "To: Bob <sip:bob@example.com>\r\n"
            "Call-ID: trying-test@example.com\r\n"
            "CSeq: 1 INVITE\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipResponse)
        assert msg.body is None

    def test_cancel_no_body(self) -> None:
        headers = {
            "Via": "SIP/2.0/UDP pc33.example.com;branch=z9hG4bK1",
            "From": "Alice <sip:alice@example.com>;tag=1",
            "To": "Bob <sip:bob@example.com>",
            "Call-ID": "cancel-test@example.com",
            "CSeq": "1 CANCEL",
        }
        raw = build_sip_request(SipMethod.CANCEL, "sip:bob@example.com", headers)
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.body is None


# ---------------------------------------------------------------------------
# 9. RFC 3261 §24 example messages
# ---------------------------------------------------------------------------


class TestRfc3261Examples:
    """
    RFC 3261 §24 contains a complete call-flow example.
    We test the key messages from §24.2 (Session Establishment).
    """

    def test_invite_from_rfc_24_2(self) -> None:
        """
        INVITE from Alice to the proxy per RFC 3261 §24.2.
        Verifies all header fields parse correctly.
        """
        raw = (
            "INVITE sip:bob@biloxi.example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.example.com;branch=z9hG4bKnashds8\r\n"
            "Max-Forwards: 70\r\n"
            "To: Bob <sip:bob@biloxi.example.com>\r\n"
            "From: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
            "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
            "CSeq: 314159 INVITE\r\n"
            "Contact: <sip:alice@pc33.atlanta.example.com>\r\n"
            "Content-Type: application/sdp\r\n"
            "Content-Length: 4\r\n"
            "\r\n"
            "Test"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.method == SipMethod.INVITE
        assert msg.uri == "sip:bob@biloxi.example.com"
        assert msg.headers["CSeq"] == "314159 INVITE"
        assert msg.headers["Max-Forwards"] == "70"
        assert msg.body == "Test"

    def test_180_ringing_from_rfc_24_2(self) -> None:
        """180 Ringing provisional response per RFC 3261 §24.2."""
        raw = (
            "SIP/2.0 180 Ringing\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.example.com;branch=z9hG4bKnashds8"
            ";received=192.0.2.1\r\n"
            "To: Bob <sip:bob@biloxi.example.com>;tag=a6c85cf\r\n"
            "From: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
            "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
            "CSeq: 314159 INVITE\r\n"
            "Contact: <sip:bob@192.0.2.4>\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipResponse)
        assert msg.status_code == 180
        assert msg.reason_phrase == "Ringing"
        assert msg.body is None

    def test_bye_request_from_rfc_24_2(self) -> None:
        """BYE request from Bob to terminate the session per RFC 3261 §24.2."""
        raw = (
            "BYE sip:alice@pc33.atlanta.example.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP 192.0.2.4;branch=z9hG4bKnashds10\r\n"
            "Max-Forwards: 70\r\n"
            "From: Bob <sip:bob@biloxi.example.com>;tag=a6c85cf\r\n"
            "To: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
            "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
            "CSeq: 231 BYE\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.method == SipMethod.BYE
        assert msg.uri == "sip:alice@pc33.atlanta.example.com"
        assert msg.headers["CSeq"] == "231 BYE"

    def test_ack_request(self) -> None:
        """ACK confirms the final response to INVITE per RFC 3261 §13.2.2.4."""
        raw = (
            "ACK sip:bob@192.0.2.4 SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.example.com;branch=z9hG4bKnashds9\r\n"
            "Max-Forwards: 70\r\n"
            "To: Bob <sip:bob@biloxi.example.com>;tag=a6c85cf\r\n"
            "From: Alice <sip:alice@atlanta.example.com>;tag=1928301774\r\n"
            "Call-ID: a84b4c76e66710@pc33.atlanta.example.com\r\n"
            "CSeq: 314159 ACK\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.method == SipMethod.ACK
        assert msg.body is None

    def test_register_request(self) -> None:
        """REGISTER binds Alice's AOR to her current IP per RFC 3261 §10."""
        raw = (
            "REGISTER sips:ss2.biloxi.example.com SIP/2.0\r\n"
            "Via: SIP/2.0/TLS client.biloxi.example.com:5061;branch=z9hG4bKnashds10\r\n"
            "Max-Forwards: 70\r\n"
            "From: Bob <sip:bob@biloxi.example.com>;tag=a73kszlfl\r\n"
            "To: Bob <sip:bob@biloxi.example.com>\r\n"
            "Call-ID: 1j9FpLxk3uxtm8tn@biloxi.example.com\r\n"
            "CSeq: 1 REGISTER\r\n"
            "Contact: <sips:bob@client.biloxi.example.com>\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = parse_sip(raw)
        assert isinstance(msg, SipRequest)
        assert msg.method == SipMethod.REGISTER
        assert msg.uri == "sips:ss2.biloxi.example.com"


# ---------------------------------------------------------------------------
# 10. Build output format verification
# ---------------------------------------------------------------------------


class TestBuildOutputFormat:
    def test_request_uses_crlf(self) -> None:
        raw = build_sip_request(
            SipMethod.INVITE,
            "sip:bob@example.com",
            {"From": "Alice <sip:alice@example.com>"},
        )
        assert "\r\n" in raw
        # Must not contain bare LF without preceding CR
        lines = raw.split("\r\n")
        assert lines[0] == "INVITE sip:bob@example.com SIP/2.0"

    def test_response_uses_crlf(self) -> None:
        raw = build_sip_response(200, "OK", {"From": "Alice <sip:alice@example.com>"})
        lines = raw.split("\r\n")
        assert lines[0] == "SIP/2.0 200 OK"

    def test_headers_appear_after_start_line(self) -> None:
        raw = build_sip_request(
            SipMethod.BYE,
            "sip:bob@example.com",
            {"Call-ID": "test-build@example.com", "CSeq": "2 BYE"},
        )
        assert "Call-ID: test-build@example.com" in raw
        assert "CSeq: 2 BYE" in raw

    def test_body_appears_after_blank_line(self) -> None:
        body = "v=0\r\nm=audio 49170 RTP/AVP 0\r\n"
        raw = build_sip_request(
            SipMethod.INVITE,
            "sip:bob@example.com",
            {"Content-Type": "application/sdp"},
            body,
        )
        # Blank line (double CRLF) separates headers from body
        assert "\r\n\r\n" in raw
        _, parsed_body = raw.split("\r\n\r\n", 1)
        assert parsed_body == body
