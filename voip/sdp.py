"""
SDP (Session Description Protocol) parser, builder, and codec negotiation
per RFC 4566.

Transformation pipeline:
  raw text -> SdpSession                              (parse_sdp)
  SdpSession -> raw text                             (build_sdp)
  SdpSession + SdpSession -> list[SdpCodec]          (negotiate_codecs)
"""

from __future__ import annotations

import logging

from voip.types.sdp import SdpCodec, SdpMediaDescription, SdpOrigin, SdpSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static payload type registry per RFC 3551 §6
# These are recognised even without an explicit "a=rtpmap:" line.
# ---------------------------------------------------------------------------
_STATIC_CODECS: dict[int, SdpCodec] = {
    0: SdpCodec(payload_type=0, name="PCMU", clock_rate=8000),
    3: SdpCodec(payload_type=3, name="GSM", clock_rate=8000),
    8: SdpCodec(payload_type=8, name="PCMA", clock_rate=8000),
}


# ---------------------------------------------------------------------------
# parse_sdp
# ---------------------------------------------------------------------------

def parse_sdp(raw: str) -> SdpSession:
    """Parse a CRLF- or LF-delimited SDP body into a structured SdpSession.

    Implements the parsing rules from RFC 4566 §5.  Lines have the form
    ``<type>=<value>`` where type is exactly one ASCII letter.  Mandatory
    fields are ``v=``, ``o=``, ``s=``, and ``t=``; ``m=`` is required for a
    useful session but strictly only the first four fields are mandatory per
    the RFC.

    Raises:
        ValueError: When a mandatory field is absent or a line is syntactically
            invalid (e.g. no ``=`` separator, bad ``o=`` field count).
    """
    # Normalise line endings so the rest of the parser only sees plain \n.
    lines = raw.replace("\r\n", "\n").split("\n")
    # Drop trailing empty lines produced by a trailing CRLF.
    lines = [line for line in lines if line]

    # Collect all typed lines into a simple list of (type, value) pairs.
    parsed_lines: list[tuple[str, str]] = []
    for line in lines:
        if "=" not in line:
            # RFC 4566 §5: every line is <type>=<value>; anything else is invalid.
            raise ValueError(f"SDP line missing '=' separator: {line!r}")
        type_char, value = line.split("=", 1)
        if len(type_char) != 1:
            raise ValueError(f"SDP line type must be a single character, got: {type_char!r}")
        parsed_lines.append((type_char, value))

    # -----------------------------------------------------------------------
    # First pass: locate mandatory session-level fields.
    # -----------------------------------------------------------------------
    version: int | None = None
    origin: SdpOrigin | None = None
    session_name: str | None = None
    connection_address: str | None = None
    time_start: int = 0
    time_stop: int = 0

    # Index into parsed_lines where the first "m=" block begins.
    first_media_index: int | None = None

    for index, (type_char, value) in enumerate(parsed_lines):
        if type_char == "v":
            try:
                version = int(value)
            except ValueError:
                raise ValueError(f"SDP v= field must be an integer, got: {value!r}")

        elif type_char == "o":
            origin = _parse_origin(value)

        elif type_char == "s":
            session_name = value

        elif type_char == "c" and first_media_index is None:
            # Session-level connection address (before any "m=" line).
            connection_address = _parse_connection_address(value)

        elif type_char == "t":
            parts = value.split()
            if len(parts) != 2:
                raise ValueError(f"SDP t= field must have exactly two values, got: {value!r}")
            try:
                time_start = int(parts[0])
                time_stop = int(parts[1])
            except ValueError:
                raise ValueError(f"SDP t= fields must be integers, got: {value!r}")

        elif type_char == "m":
            if first_media_index is None:
                first_media_index = index
            # Stop scanning session-level fields once we hit the first "m=".
            break

    # -----------------------------------------------------------------------
    # Validate mandatory fields (RFC 4566 §5).
    # -----------------------------------------------------------------------
    if version is None:
        raise ValueError("SDP body missing mandatory 'v=' field")
    if origin is None:
        raise ValueError("SDP body missing mandatory 'o=' field")
    if session_name is None:
        raise ValueError("SDP body missing mandatory 's=' field")
    if first_media_index is None:
        raise ValueError("SDP body missing mandatory 'm=' field")

    # -----------------------------------------------------------------------
    # Second pass: parse media descriptions.
    # -----------------------------------------------------------------------
    media: list[SdpMediaDescription] = []
    current_media: _MediaBuilder | None = None

    for type_char, value in parsed_lines[first_media_index:]:
        if type_char == "m":
            if current_media is not None:
                media.append(current_media.build())
            current_media = _MediaBuilder(value)
        elif type_char == "a" and current_media is not None:
            current_media.add_attribute(value)
        elif type_char == "c" and current_media is not None:
            # Media-level connection address; not stored on media itself in our
            # type model, but if we encounter it we ignore it (the session-level
            # address already covers the common case).
            pass

    if current_media is not None:
        media.append(current_media.build())

    return SdpSession(
        version=version,
        origin=origin,
        session_name=session_name,
        connection_address=connection_address,
        time_start=time_start,
        time_stop=time_stop,
        media=media,
    )


# ---------------------------------------------------------------------------
# Internal helpers for parse_sdp
# ---------------------------------------------------------------------------

def _parse_origin(value: str) -> SdpOrigin:
    """Parse the value of an 'o=' line per RFC 4566 §5.2.

    Format: ``<username> <session-id> <session-version> <nettype> <addrtype> <unicast-address>``
    """
    parts = value.split()
    if len(parts) != 6:
        raise ValueError(
            f"SDP o= field must have exactly 6 space-separated tokens, got: {value!r}"
        )
    username, session_id, session_version, net_type, addr_type, address = parts
    return SdpOrigin(
        username=username,
        session_id=session_id,
        session_version=session_version,
        net_type=net_type,
        addr_type=addr_type,
        address=address,
    )


def _parse_connection_address(value: str) -> str:
    """Extract the unicast address from a 'c=' line per RFC 4566 §5.7.

    Format: ``<nettype> <addrtype> <connection-address>``
    """
    parts = value.split()
    if len(parts) != 3:
        raise ValueError(
            f"SDP c= field must have exactly 3 space-separated tokens, got: {value!r}"
        )
    # We only store the connection address itself; net_type and addr_type are
    # implicit (always "IN IP4" or "IN IP6" in practice).
    return parts[2]


class _MediaBuilder:
    """Accumulates "a=" lines for a single "m=" block and builds an SdpMediaDescription.

    This is an internal implementation detail of parse_sdp; not part of the
    public API.
    """

    def __init__(self, m_line_value: str) -> None:
        """Parse the value of an 'm=' line per RFC 4566 §5.14.

        Format: ``<media-type> <port> <proto> <fmt-list>``
        """
        parts = m_line_value.split()
        if len(parts) < 4:
            raise ValueError(
                f"SDP m= line must have at least 4 tokens, got: {m_line_value!r}"
            )
        self.media_type = parts[0]
        try:
            self.port = int(parts[1])
        except ValueError:
            raise ValueError(f"SDP m= port must be an integer, got: {parts[1]!r}")
        self.protocol = parts[2]
        # Remaining tokens are payload type numbers.
        self.payload_type_numbers: list[int] = []
        for token in parts[3:]:
            try:
                self.payload_type_numbers.append(int(token))
            except ValueError:
                raise ValueError(f"SDP m= payload type must be an integer, got: {token!r}")

        # Maps payload_type -> SdpCodec populated by a=rtpmap lines.
        self._rtpmap: dict[int, SdpCodec] = {}
        # Other "a=" lines stored as key -> value.
        self._attributes: dict[str, str] = {}

    def add_attribute(self, value: str) -> None:
        """Process one "a=<value>" line belonging to this media description."""
        if value.startswith("rtpmap:"):
            # a=rtpmap:<payload-type> <encoding-name>/<clock-rate>[/<channels>]
            rest = value[len("rtpmap:"):]
            space_index = rest.index(" ")
            payload_type_str = rest[:space_index]
            encoding = rest[space_index + 1:]
            try:
                payload_type = int(payload_type_str)
            except ValueError:
                raise ValueError(f"SDP a=rtpmap payload type must be an integer: {value!r}")
            codec = _parse_rtpmap_encoding(payload_type, encoding)
            self._rtpmap[payload_type] = codec
        else:
            # Generic "a=<key>" or "a=<key>:<value>" attribute.
            if ":" in value:
                key, attr_value = value.split(":", 1)
            else:
                key = value
                attr_value = ""
            self._attributes[key] = attr_value

    def build(self) -> SdpMediaDescription:
        """Resolve payload type numbers to SdpCodec objects and return the description."""
        codecs: list[SdpCodec] = []
        for payload_type in self.payload_type_numbers:
            if payload_type in self._rtpmap:
                codecs.append(self._rtpmap[payload_type])
            elif payload_type in _STATIC_CODECS:
                # Static assignment per RFC 3551 §6; no rtpmap needed.
                codecs.append(_STATIC_CODECS[payload_type])
            # Unknown dynamic payload types without rtpmap are silently dropped;
            # RFC 4566 §6 says rtpmap is optional for static types but required
            # for dynamic (96-127).  We skip rather than crash to be lenient.

        return SdpMediaDescription(
            media_type=self.media_type,
            port=self.port,
            protocol=self.protocol,
            codecs=codecs,
            attributes=self._attributes,
        )


def _parse_rtpmap_encoding(payload_type: int, encoding: str) -> SdpCodec:
    """Parse the encoding description from an 'a=rtpmap' attribute.

    Format: ``<name>/<clock-rate>[/<channels>]``
    """
    parts = encoding.split("/")
    if len(parts) < 2:
        raise ValueError(f"SDP a=rtpmap encoding must be name/rate[/channels], got: {encoding!r}")
    name = parts[0]
    try:
        clock_rate = int(parts[1])
    except ValueError:
        raise ValueError(f"SDP a=rtpmap clock rate must be an integer, got: {parts[1]!r}")
    channels = 1
    if len(parts) >= 3:
        try:
            channels = int(parts[2])
        except ValueError:
            raise ValueError(f"SDP a=rtpmap channels must be an integer, got: {parts[2]!r}")
    return SdpCodec(
        payload_type=payload_type,
        name=name,
        clock_rate=clock_rate,
        channels=channels,
    )


# ---------------------------------------------------------------------------
# build_sdp
# ---------------------------------------------------------------------------

def build_sdp(session: SdpSession) -> str:
    """Serialise an SdpSession to the canonical line-by-line text format.

    Lines are separated by CRLF as required by RFC 4566 §5.  The field order
    follows the mandatory ordering in RFC 4566 §5:
      v=, o=, s=, [c=,] t=, [m= blocks]
    """
    lines: list[str] = []

    # v= is the SDP protocol version (RFC 4566 §5.1). Always 0.
    lines.append(f"v={session.version}")

    # o= is the origin field (RFC 4566 §5.2).
    origin = session.origin
    lines.append(
        f"o={origin.username} {origin.session_id} {origin.session_version}"
        f" {origin.net_type} {origin.addr_type} {origin.address}"
    )

    # s= is the session name (RFC 4566 §5.3).
    lines.append(f"s={session.session_name}")

    # c= is session-level connection data (RFC 4566 §5.7). Omitted when absent.
    if session.connection_address is not None:
        lines.append(f"c=IN IP4 {session.connection_address}")

    # t= is timing (RFC 4566 §5.9). "0 0" means an unbounded session.
    lines.append(f"t={session.time_start} {session.time_stop}")

    # m= blocks, one per media description (RFC 4566 §5.14).
    for media in session.media:
        payload_type_tokens = " ".join(str(codec.payload_type) for codec in media.codecs)
        lines.append(f"m={media.media_type} {media.port} {media.protocol} {payload_type_tokens}")

        # a=rtpmap holds codec mappings (RFC 4566 §6).
        for codec in media.codecs:
            if codec.channels != 1:
                lines.append(
                    f"a=rtpmap:{codec.payload_type} {codec.name}/{codec.clock_rate}/{codec.channels}"
                )
            else:
                lines.append(
                    f"a=rtpmap:{codec.payload_type} {codec.name}/{codec.clock_rate}"
                )

        # Other attributes in insertion order.
        for key, value in media.attributes.items():
            if value:
                lines.append(f"a={key}:{value}")
            else:
                lines.append(f"a={key}")

    # RFC 4566 §5 mandates CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# negotiate_codecs
# ---------------------------------------------------------------------------

def negotiate_codecs(offer: SdpSession, answer: SdpSession) -> list[SdpCodec]:
    """Return the intersection of codecs from the first audio media of offer and answer.

    Preserves the offerer's preference order per RFC 3264 §6.1.  Matching is
    case-insensitive on codec name and exact on clock_rate.  Returns an empty
    list when no common codec exists (the call should be rejected with SIP
    488 Not Acceptable Here).
    """
    offer_audio = _first_audio_media(offer)
    answer_audio = _first_audio_media(answer)

    if offer_audio is None or answer_audio is None:
        return []

    # Build a lookup keyed by (name.lower(), clock_rate) from the answer.
    answer_lookup: dict[tuple[str, int], SdpCodec] = {
        (codec.name.lower(), codec.clock_rate): codec
        for codec in answer_audio.codecs
    }

    # Walk offer codecs in preference order; keep those present in the answer.
    result: list[SdpCodec] = []
    for codec in offer_audio.codecs:
        key = (codec.name.lower(), codec.clock_rate)
        if key in answer_lookup:
            # Use the offer's codec object to preserve the offerer's payload_type.
            result.append(codec)

    return result


def _first_audio_media(session: SdpSession) -> SdpMediaDescription | None:
    """Return the first audio SdpMediaDescription in a session, or None."""
    for media in session.media:
        if media.media_type == "audio":
            return media
    return None


# ---------------------------------------------------------------------------
# log_sdp
# ---------------------------------------------------------------------------

def log_sdp(label: str, sdp_text: str) -> None:
    """Log an SDP body with per-line educational explanations.

    Parses the raw SDP text and emits one log line per SDP field, annotated
    with a plain-English explanation of what the field means.  Intended for
    educational/debugging use; mirrors the Go reference project's logSDP
    function.

    Uses Python's standard logging module with a ``[SDP]`` prefix so callers
    can configure verbosity independently.
    """
    logger.info("[SDP] --- %s ---", label)

    # Normalise line endings.
    lines = sdp_text.replace("\r\n", "\n").split("\n")
    lines = [line for line in lines if line]

    for line in lines:
        if "=" not in line:
            logger.info("[SDP]   (unrecognised line) %s", line)
            continue

        type_char, value = line.split("=", 1)

        if type_char == "v":
            logger.info("[SDP]   v=%s  →  SDP version, always 0", value)

        elif type_char == "o":
            parts = value.split()
            if len(parts) == 6:
                logger.info(
                    "[SDP]   o=  →  origin: user=%s sess-id=%s version=%s addr=%s",
                    parts[0], parts[1], parts[2], parts[5],
                )
            else:
                logger.info("[SDP]   o=%s  →  origin (malformed)", value)

        elif type_char == "s":
            logger.info("[SDP]   s=%s  →  session name", value)

        elif type_char == "c":
            logger.info(
                "[SDP]   c=%s  →  connection address, where to send media", value
            )

        elif type_char == "t":
            logger.info(
                "[SDP]   t=%s  →  timing: 0 0 means session has no fixed start/end", value
            )

        elif type_char == "m":
            parts = value.split()
            logger.info(
                "[SDP]   m=  →  media line: type, port, transport, codec IDs"
            )
            if len(parts) >= 1:
                logger.info("[SDP]       type      = %s", parts[0])
            if len(parts) >= 2:
                logger.info("[SDP]       port      = %s", parts[1])
            if len(parts) >= 3:
                logger.info("[SDP]       transport = %s", parts[2])
            if len(parts) >= 4:
                logger.info("[SDP]       codec IDs = %s", " ".join(parts[3:]))

        elif type_char == "a":
            _log_attribute(value)

        else:
            logger.info("[SDP]   %s=%s", type_char, value)

    logger.info("[SDP] --- end %s ---", label)


def _log_attribute(value: str) -> None:
    """Emit an educational log line for a single 'a=' attribute value."""
    if value.startswith("rtpmap:"):
        rest = value[len("rtpmap:"):]
        logger.info(
            "[SDP]   a=rtpmap:%s  →  codec mapping: payload-type → name/rate/channels",
            rest,
        )
    elif value.startswith("fmtp:"):
        rest = value[len("fmtp:"):]
        logger.info(
            "[SDP]   a=fmtp:%s  →  format parameters, codec-specific settings", rest
        )
    elif value.startswith("ice-ufrag:"):
        rest = value[len("ice-ufrag:"):]
        logger.info(
            "[SDP]   a=ice-ufrag:%s  →  ICE credentials, username fragment for STUN auth",
            rest,
        )
    elif value.startswith("ice-pwd:"):
        rest = value[len("ice-pwd:"):]
        logger.info(
            "[SDP]   a=ice-pwd:%s  →  ICE credentials, password for STUN HMAC", rest
        )
    elif value.startswith("fingerprint:"):
        rest = value[len("fingerprint:"):]
        logger.info(
            "[SDP]   a=fingerprint:%s  →  DTLS cert fingerprint, peer identity verification",
            rest,
        )
    elif value.startswith("candidate:"):
        rest = value[len("candidate:"):]
        logger.info(
            "[SDP]   a=candidate:%s  →  ICE candidate, network address where peer can be reached",
            rest,
        )
    elif value in ("sendrecv", "sendonly", "recvonly", "inactive"):
        logger.info("[SDP]   a=%s  →  media direction", value)
    else:
        logger.info("[SDP]   a=%s", value)
