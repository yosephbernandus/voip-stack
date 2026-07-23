"""
SIP (Session Initiation Protocol) parser and builder per RFC 3261.

Transformation pipeline:
  raw text -> SipMessage (SipRequest | SipResponse)   (parse_sip)
  SipMethod + parts -> raw text                        (build_sip_request, build_sip_response)
"""

from voip.types.sip import SipMessage, SipMethod, SipRequest, SipResponse


def parse_sip(raw: str) -> SipMessage:
    """
    Parse a SIP message per RFC 3261 Section 7.

    A SIP message is either a request or response, distinguished by the first
    line (Request-Line vs Status-Line). Headers are separated from the body by
    a blank line (CRLF CRLF). Multi-line header folding (RFC 3261 §7.3.1) is
    supported: a line beginning with SP or HT continues the previous header.

    Args:
        raw: Raw SIP message text, CRLF-delimited.

    Returns:
        SipRequest for requests, SipResponse for responses.

    Raises:
        ValueError: If the message is malformed (missing first line, bad
                    status line format, unknown method, etc.)
    """
    # Split on the blank line that separates headers from body (RFC 3261 §7)
    if "\r\n\r\n" in raw:
        header_section, body_text = raw.split("\r\n\r\n", 1)
    else:
        # No body separator, so treat the entire message as headers with no body
        header_section = raw
        body_text = ""

    # Body is None rather than empty string when absent
    body: str | None = body_text if body_text else None

    lines = header_section.split("\r\n")
    if not lines or not lines[0].strip():
        raise ValueError("SIP message is missing the start-line (RFC 3261 §7.1/§7.2)")

    first_line = lines[0]

    # Collapse multi-line (folded) headers per RFC 3261 §7.3.1.
    # A header line that starts with SP or HT is a continuation of the previous header.
    unfolded: list[str] = []
    for line in lines[1:]:
        if line and line[0] in (" ", "\t"):
            # Continuation: append (with single space) to the previous header
            if not unfolded:
                raise ValueError(
                    f"SIP header folding on first header line is invalid: {line!r}"
                )
            unfolded[-1] = unfolded[-1] + " " + line.strip()
        else:
            unfolded.append(line)

    headers = _parse_headers(unfolded)

    # Determine message type from first token of the start-line
    if first_line.startswith("SIP/"):
        return _parse_response(first_line, headers, body)
    else:
        return _parse_request(first_line, headers, body)


def _parse_request(first_line: str, headers: dict[str, str], body: str | None) -> SipRequest:
    """
    Parse a SIP request from its Request-Line and pre-parsed headers.

    Request-Line = Method SP Request-URI SP SIP-Version CRLF  (RFC 3261 §7.1)
    """
    parts = first_line.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(
            f"Malformed SIP Request-Line (expected 'METHOD URI SIP/2.0'): {first_line!r}"
        )

    method_str, uri, version = parts

    try:
        method = SipMethod(method_str)
    except ValueError:
        raise ValueError(
            f"Unknown SIP method {method_str!r}. Allowed: "
            f"{[m.value for m in SipMethod]}"
        )

    if not version.startswith("SIP/"):
        raise ValueError(
            f"SIP version must start with 'SIP/' but got: {version!r}"
        )

    return SipRequest(method=method, uri=uri, version=version, headers=headers, body=body)


def _parse_response(first_line: str, headers: dict[str, str], body: str | None) -> SipResponse:
    """
    Parse a SIP response from its Status-Line and pre-parsed headers.

    Status-Line = SIP-Version SP Status-Code SP Reason-Phrase CRLF  (RFC 3261 §7.2)
    """
    parts = first_line.split(" ", 2)
    if len(parts) < 2:
        raise ValueError(
            f"Malformed SIP Status-Line (expected 'SIP/2.0 NNN Reason'): {first_line!r}"
        )

    version = parts[0]
    status_code_str = parts[1]
    reason_phrase = parts[2] if len(parts) == 3 else ""

    try:
        status_code = int(status_code_str)
    except ValueError:
        raise ValueError(
            f"SIP status code must be an integer, got: {status_code_str!r}"
        )

    return SipResponse(
        version=version,
        status_code=status_code,
        reason_phrase=reason_phrase,
        headers=headers,
        body=body,
    )


def _parse_headers(lines: list[str]) -> dict[str, str]:
    """
    Parse SIP header lines into a dict with canonical-capitalized keys.

    Header syntax: field-name ":" SP field-value  (RFC 3261 §7.3)

    Keys are stored with canonical capitalization (e.g. "Content-Type") but
    lookup is case-insensitive because SIP header names are case-insensitive
    per RFC 3261 §7.3.1.
    """
    headers: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            # Skip blank lines (may appear between headers in some implementations)
            continue
        if ":" not in line:
            raise ValueError(f"SIP header line missing ':' separator: {line!r}")
        name, _, value = line.partition(":")
        canonical_name = _canonicalize_header_name(name.strip())
        headers[canonical_name] = value.strip()
    return headers


# Canonical capitalization for common SIP headers per RFC 3261.
# Any header not in this map gets Title-Cased automatically.
_CANONICAL_HEADER_NAMES: dict[str, str] = {
    "via": "Via",
    "from": "From",
    "to": "To",
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "contact": "Contact",
    "max-forwards": "Max-Forwards",
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "authorization": "Authorization",
    "www-authenticate": "WWW-Authenticate",
    "proxy-authenticate": "Proxy-Authenticate",
    "proxy-authorization": "Proxy-Authorization",
    "record-route": "Record-Route",
    "route": "Route",
    "subject": "Subject",
    "allow": "Allow",
    "supported": "Supported",
    "require": "Require",
    "expires": "Expires",
    "min-expires": "Min-Expires",
    "user-agent": "User-Agent",
    "server": "Server",
    "accept": "Accept",
    "accept-encoding": "Accept-Encoding",
    "accept-language": "Accept-Language",
}


def _canonicalize_header_name(name: str) -> str:
    """
    Return canonical capitalization for a SIP header name.

    Per RFC 3261 §7.3.1, header field names are case-insensitive. We normalize
    to well-known canonical forms (e.g. "call-id" -> "Call-ID") so that
    consumers can rely on consistent key names.
    """
    lower = name.lower()
    if lower in _CANONICAL_HEADER_NAMES:
        return _CANONICAL_HEADER_NAMES[lower]
    # Fall back to title-casing each hyphen-separated word
    return "-".join(word.capitalize() for word in name.split("-"))


def build_sip_request(
    method: SipMethod,
    uri: str,
    headers: dict[str, str],
    body: str | None = None,
) -> str:
    """
    Serialise a SIP request to a CRLF-delimited wire-format string per RFC 3261 §7.1.

    The caller is responsible for setting Content-Type and Content-Length when
    a body is present. RFC 3261 §20.14 requires Content-Length on TCP streams.

    Request-Line = Method SP Request-URI SP SIP-Version CRLF

    Args:
        method: The SIP method (INVITE, REGISTER, etc.)
        uri: The Request-URI (e.g. "sip:bob@example.com")
        headers: SIP headers as a dict; serialised in iteration order.
        body: Optional message body (e.g. SDP offer).

    Returns:
        Complete SIP request as a string with CRLF line endings.
    """
    request_line = f"{method.value} {uri} SIP/2.0"
    return _build_message(request_line, headers, body)


def build_sip_response(
    status_code: int,
    reason: str,
    headers: dict[str, str],
    body: str | None = None,
) -> str:
    """
    Serialise a SIP response to a CRLF-delimited wire-format string per RFC 3261 §7.2.

    Status-Line = SIP-Version SP Status-Code SP Reason-Phrase CRLF

    Args:
        status_code: Three-digit status code (e.g. 200, 180, 486).
        reason: Human-readable reason phrase (e.g. "OK", "Ringing").
        headers: SIP headers as a dict; serialised in iteration order.
        body: Optional message body (e.g. SDP answer on 200 OK to INVITE).

    Returns:
        Complete SIP response as a string with CRLF line endings.
    """
    status_line = f"SIP/2.0 {status_code} {reason}"
    return _build_message(status_line, headers, body)


def _build_message(start_line: str, headers: dict[str, str], body: str | None) -> str:
    """
    Assemble a SIP message from a start-line, headers dict, and optional body.

    Format per RFC 3261 §7:
      start-line CRLF
      *(header-field CRLF)
      CRLF
      [message-body]
    """
    parts: list[str] = [start_line]
    for name, value in headers.items():
        parts.append(f"{name}: {value}")
    # Blank line separates headers from body (RFC 3261 §7)
    parts.append("")
    if body:
        parts.append(body)
    return "\r\n".join(parts)
