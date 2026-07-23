# SDP (RFC 4566)

## Layer 1, Quick Summary

### What is SDP?

SDP (Session Description Protocol) is like two people agreeing on which language to speak before starting a conversation.

> "I can speak English, Japanese, and Indonesian."
> "I can speak Indonesian and French."
> "Great, let's use Indonesian."

That negotiation *is* SDP. It's a small text format that describes the media capabilities of an endpoint: what codecs it supports, which UDP port to send audio to, and other parameters the two sides need to agree on before media can flow.

### Where It Fits

SDP is never standalone. It rides inside other protocols, in our case, inside the body of SIP messages. When Alice sends an `INVITE`, her SDP offer is in the body. When Bob sends `200 OK`, his SDP answer is in the body.

```
 SIP INVITE  -->  Content-Type: application/sdp
                 |
                 v
                 [SDP offer]  <-- Alice says: "I support PCMU, PCMA, Opus"

 SIP 200 OK  -->  Content-Type: application/sdp
                 |
                 v
                 [SDP answer] <-- Bob replies: "Let's use Opus"
```

This exchange is called the **offer/answer model** and is defined in RFC 3264.

### Key Terms

| Term | Meaning |
|------|---------|
| **Offer** | SDP sent first, listing all supported options |
| **Answer** | SDP sent in response, choosing a subset of the offer |
| **Media description** | An `m=` line plus its attributes, describes one stream (audio/video) |
| **Codec** | An algorithm for encoding audio (PCMU, PCMA, Opus, etc.) |
| **Payload type** | Small integer identifying a codec on the wire (travels in RTP) |
| **rtpmap** | Attribute that binds a payload type number to a codec name |
| **Session** | The complete SDP block (one per SIP message body) |

### What We Implement vs What We Skip

**Implement:**
- Parse SDP text into structured types
- Build SDP from types
- Codec negotiation (find the intersection of offer and answer)
- Audio codecs: PCMU, PCMA, Opus

**Skip:**
- Video media descriptions
- ICE candidates (WebRTC NAT traversal)
- DTLS fingerprints (WebRTC security)
- Bandwidth negotiation (`b=` lines)
- Encryption keys (`k=` lines)

---

## Layer 2, Deep Dive

### SDP Format

SDP is line-based. Every line has the format `<letter>=<value>`. The letter tells you what kind of information follows:

```
v=0
o=alice 2890844526 2890844526 IN IP4 host.example.com
s=-
c=IN IP4 192.0.2.101
t=0 0
m=audio 49170 RTP/AVP 0 8 96
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=rtpmap:96 opus/48000/2
a=sendrecv
```

### Line Types

| Letter | Meaning | Notes |
|--------|---------|-------|
| `v=` | Version | Always `0`, SDP version, not session version |
| `o=` | Origin | Who created this session (six fields, see below) |
| `s=` | Session name | Often just `-` (SIP doesn't use it) |
| `c=` | Connection | Network type + address type + address for media |
| `t=` | Timing | Start and stop times; `0 0` means permanent |
| `m=` | Media | The media description, one per stream |
| `a=` | Attribute | Belongs to the preceding `m=` (or session-level if before any `m=`) |

There are more (`i=`, `u=`, `e=`, `p=`, `b=`, `k=`, `r=`, `z=`) but they're rare in VoIP. We skip them.

### The `o=` (Origin) Line

```
o=<username> <session-id> <session-version> <net-type> <addr-type> <address>
```

Example: `o=alice 2890844526 2890844526 IN IP4 pc33.example.com`

| Field | Meaning |
|-------|---------|
| username | Usually `-` for privacy; can be a user name |
| session-id | A unique number (often timestamp-based) identifying this session |
| session-version | Increments when the SDP is updated within the same session |
| net-type | `IN` for Internet |
| addr-type | `IP4` or `IP6` |
| address | The IP or hostname of the origin machine |

### The `m=` (Media) Line

This is the core of SDP, each `m=` line describes one media stream.

```
m=<media> <port> <protocol> <fmt> ...
```

Example: `m=audio 49170 RTP/AVP 0 8 96`

| Field | Meaning |
|-------|---------|
| media | `audio` or `video` |
| port | UDP port number for RTP |
| protocol | `RTP/AVP` (RTP Audio/Video Profile), `RTP/SAVP` (secure), `UDP/TLS/RTP/SAVPF` (WebRTC) |
| fmt | Space-separated payload type numbers, in preference order |

The `fmt` list above means: "I can accept payload types 0, 8, and 96, and I prefer them in that order." Each number maps to a codec via a subsequent `a=rtpmap:` attribute.

### The `a=rtpmap` Attribute

```
a=rtpmap:<payload-type> <codec-name>/<clock-rate>[/<channels>]
```

Examples:
```
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=rtpmap:96 opus/48000/2
```

For static payload types (0-95), the codec is pre-assigned by RFC 3551, so `rtpmap` is technically optional. For dynamic types (96-127), `rtpmap` is required because the number is chosen by the offerer and the answerer needs to know what codec it represents.

### The Offer/Answer Model

The rules (RFC 3264):

1. **The offerer** lists *all* codecs it's willing to accept, in preference order.
2. **The answerer** responds with a subset, only codecs it also supports. The answer's order is its preference.
3. After negotiation, both sides send audio using the first codec in the answer's list.
4. Either side may send additional codecs from the list if needed (e.g., for codec switching).

**Example negotiation:**

Alice's offer:
```
m=audio 49170 RTP/AVP 96 0 8
a=rtpmap:96 opus/48000/2
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
```
(Alice prefers Opus, falls back to PCMU, then PCMA.)

Bob's answer:
```
m=audio 51372 RTP/AVP 0 8
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
```
(Bob doesn't do Opus. He accepts PCMU and PCMA, prefers PCMU.)

**Result:** Both sides use PCMU. Alice would have preferred Opus but Bob can't speak it, so they settle on the best common codec.

This is exactly what our `negotiate_codecs(offer, answer)` function does, returns the intersection, preserving one side's preference order.

### Common Audio Codecs

| Codec | Payload Type | Clock Rate | Bitrate | Notes |
|-------|-------------|-----------|---------|-------|
| **PCMU** (G.711 μ-law) | 0 | 8000 Hz | 64 kbps | Simple, uncompressed, North American standard |
| **PCMA** (G.711 A-law) | 8 | 8000 Hz | 64 kbps | European variant of G.711 |
| **Opus** | 96-127 (dynamic) | 48000 Hz | 6-510 kbps | Modern, excellent quality, WebRTC default |

G.711 uses 8 kHz sample rate, so-called "narrowband" voice, traditional phone quality. Opus supports up to 48 kHz, "fullband," CD-like quality. The tradeoff is bandwidth vs quality.

For 8 kHz PCMU at 20ms packetization: 160 samples × 1 byte = 160 bytes of audio per packet, plus 12 bytes of RTP header = 172 bytes per packet × 50 packets/sec = 68.8 kbps per direction. That's the math behind "64 kbps per call" you see quoted for G.711.

### Our Types

The SDP types are defined in `voip/types/sdp.py`:

- `SdpCodec`, payload_type, name, clock_rate, channels
- `SdpMediaDescription`, media_type, port, protocol, codecs (list), attributes (dict)
- `SdpOrigin`, the `o=` line fields
- `SdpSession`, complete parsed SDP: version, origin, session_name, connection_address, timing, media (list)

Functions:

- `parse_sdp(raw: str) -> SdpSession`
- `build_sdp(session: SdpSession) -> str`
- `negotiate_codecs(offer: SdpSession, answer: SdpSession) -> list[SdpCodec]`

### RFC Reference

- **RFC 4566 §5**, SDP specification (line format)
- **RFC 4566 §6**, SDP attributes (`a=` lines)
- **RFC 4566 §9**, SDP grammar (ABNF)
- **RFC 3264**, Offer/answer model with SDP
- **RFC 3551**, RTP profile for audio/video conferences (static payload type assignments)
- **RFC 7587**, RTP payload format for Opus speech and audio codec
