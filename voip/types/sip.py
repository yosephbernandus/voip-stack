"""
SIP (Session Initiation Protocol) type definitions per RFC 3261.

Transformation pipeline:
  raw text -> SipMessage (SipRequest | SipResponse)   (parse)
  SipMessage -> raw text                              (serialise)
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class SipMethod(str, Enum):
    """
    SIP request methods per RFC 3261 Section 7.1 and related extensions.

    REGISTER and INVITE initiate sessions; ACK, BYE, and CANCEL manage their
    lifecycle. The str mixin means instances compare equal to plain strings
    (e.g. method == "INVITE") which simplifies header construction.
    """

    # Register a contact address with a registrar (§10)
    REGISTER = "REGISTER"
    # Initiate a session, carries an SDP offer in the body (§13)
    INVITE = "INVITE"
    # Acknowledge a final response to INVITE; not retransmitted (§13.2.2.4)
    ACK = "ACK"
    # Terminate an established or pending session (§15)
    BYE = "BYE"
    # Abandon a pending INVITE before a final response arrives (§9)
    CANCEL = "CANCEL"


class SipRequest(BaseModel):
    """
    SIP request message per RFC 3261 Section 7.1.

    Request-Line format: `<Method> <Request-URI> <SIP-Version> CRLF`

    The body carries SDP when this is an INVITE or re-INVITE.  When a body is
    present, the caller is responsible for setting the Content-Type and
    Content-Length headers. The serialiser does not add them automatically, so
    that the model stays a pure data container.
    """

    method: SipMethod
    # The Request-URI identifies the user or resource being called (§19.1)
    uri: str
    # Only "SIP/2.0" exists in practice; stored explicitly for wire-format fidelity
    version: str = "SIP/2.0"
    # SIP headers are multi-valued in the spec but the most common subset is
    # single-valued; a flat dict suffices for our purposes and avoids complexity
    headers: dict[str, str]
    # Typically an SDP offer/answer (RFC 4566); None for ACK, CANCEL, BYE
    body: str | None = None


class SipResponse(BaseModel):
    """
    SIP response message per RFC 3261 Section 7.2.

    Status-Line format: `<SIP-Version> <Status-Code> <Reason-Phrase> CRLF`

    Status code classes (§21):
      1xx – Provisional (e.g. 100 Trying, 180 Ringing)
      2xx – Success    (e.g. 200 OK)
      3xx – Redirection
      4xx – Client error (e.g. 404 Not Found, 486 Busy Here)
      5xx – Server error
      6xx – Global failure
    """

    version: str = "SIP/2.0"
    # Three-digit integer; class determined by first digit (§7.2)
    status_code: int
    reason_phrase: str
    headers: dict[str, str]
    # Present on 200 OK to INVITE (carries SDP answer) and some 4xx responses
    body: str | None = None


# Type alias. Callers that parse raw SIP text receive one of these two types.
# Use isinstance(msg, SipRequest) / isinstance(msg, SipResponse) to dispatch.
SipMessage = SipRequest | SipResponse


# ---------------------------------------------------------------------------
# Function signatures implemented in voip.sip (documented here for
# discoverability alongside the types they operate on).
# ---------------------------------------------------------------------------
#
# parse_sip(raw: str) -> SipMessage
#   Determine from the first line whether this is a request or response, then
#   parse headers and optional body into the appropriate model.
#
# build_sip_request(
#     method: SipMethod,
#     uri: str,
#     headers: dict[str, str],
#     body: str | None = None,
# ) -> str
#   Serialise a SIP request to a CRLF-delimited string ready to write to the
#   transport (UDP datagram or TCP stream).
#
# build_sip_response(
#     status_code: int,
#     reason: str,
#     headers: dict[str, str],
#     body: str | None = None,
# ) -> str
#   Serialise a SIP response.  The caller must set Content-Length if body is
#   non-empty (RFC 3261 §20.14 makes Content-Length mandatory for TCP).
