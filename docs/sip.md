# SIP (RFC 3261)

## Layer 1, Quick Summary

### What is SIP?

SIP (Session Initiation Protocol) is like the phone system's receptionist. It doesn't carry your voice, it knows how to connect you to the right person, ring their phone, and politely hang up when you're done. SIP sets up and tears down calls. The actual audio travels over a different protocol (RTP).

SIP is text-based, like HTTP. If you've read an HTTP request, SIP will look familiar, there's a request line, then headers, then (optionally) a body.

### Where It Fits

A quick but important clarification. The live signaling path in this project
does not send raw SIP text over WebSocket. It sends small JSON messages whose
types borrow their concepts from SIP (register, invite, answer, bye), which
keeps the routing and the logs easy to read. ICE candidate messages have no
SIP equivalent and stay WebRTC-specific. This module is the from-scratch
SIP parser and builder that shows what real SIP text looks like on the wire,
and it is exercised by the tests rather than by the running server.

With that said, the routing shape is the same either way: messages travel over
WebSocket between the browsers and the Python server, which acts as a small
signaling relay. Its JSON messages borrow their call-control concepts from SIP.
The relay parses each message, decides where it goes, and forwards it.

```
  Browser A -- WebSocket --> Server -- WebSocket --> Browser B
                    |          |
                    |  signaling messages flow both ways
                    |
                    v
               SDP body inside INVITE / 200 OK
```

### Key Terms

| Term | Meaning |
|------|---------|
| **User Agent** | A SIP client (your browser, your phone) |
| **Registrar** | A server that tracks who's online (our server has this role) |
| **Proxy** | A server that routes messages between user agents (also our server) |
| **Dialog** | A complete call session between two parties |
| **Transaction** | A single request + its response(s) within a dialog |
| **Via** | Header that records routing path (breadcrumbs for response routing) |
| **Call-ID** | Unique identifier for a call |
| **CSeq** | Sequence number for ordering requests within a dialog |
| **SIP URI** | Address format, e.g. `sip:alice@example.com` |

### What We Implement vs What We Skip

**Implement:**
- Parse request line and status line
- Parse/build headers (case-insensitive keys, multi-line values)
- Methods: REGISTER, INVITE, ACK, BYE, CANCEL
- Responses: 100 Trying, 180 Ringing, 200 OK
- Routing messages between user agents

**Skip:**
- Authentication (digest, OAuth)
- DNS SRV lookups for finding SIP servers
- NAT traversal
- TLS transport (SIP/TLS)
- Forking and multi-party routing
- Full proxy behavior (Record-Route, etc.)

---

## Layer 2, Deep Dive

### SIP Message Format

SIP messages look like HTTP messages. They have a start line, headers, a blank line, then an optional body.

**Request example (INVITE):**

```
INVITE sip:bob@example.com SIP/2.0
Via: SIP/2.0/WS df7jal23ls0d.invalid;branch=z9hG4bK776asdhds
Max-Forwards: 70
From: Alice <sip:alice@example.com>;tag=1928301774
To: Bob <sip:bob@example.com>
Call-ID: a84b4c76e66710@pc33.example.com
CSeq: 314159 INVITE
Contact: <sip:alice@pc33.example.com>
Content-Type: application/sdp
Content-Length: 142

v=0
o=alice 2890844526 2890844526 IN IP4 pc33.example.com
s=-
c=IN IP4 192.0.2.101
t=0 0
m=audio 49170 RTP/AVP 0 8
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
```

**Response example (200 OK):**

```
SIP/2.0 200 OK
Via: SIP/2.0/WS df7jal23ls0d.invalid;branch=z9hG4bK776asdhds
From: Alice <sip:alice@example.com>;tag=1928301774
To: Bob <sip:bob@example.com>;tag=a6c85cf
Call-ID: a84b4c76e66710@pc33.example.com
CSeq: 314159 INVITE
Contact: <sip:bob@pc34.example.com>
Content-Type: application/sdp
Content-Length: 131

v=0
o=bob 2890844527 2890844527 IN IP4 pc34.example.com
...
```

Key structural rules:
- Start line is either `METHOD URI VERSION` (request) or `VERSION CODE REASON` (response)
- Headers use `Name: value` format
- Header names are case-insensitive
- Blank line (`\r\n\r\n`) separates headers from body
- Body is usually SDP for INVITE/200 OK, absent for ACK/BYE

### The INVITE Flow, The SIP Trapezoid

The classic SIP call setup is a three-way handshake, sometimes called the "SIP trapezoid" because of its shape when drawn with two user agents and two proxies:

```
   Alice                    Server                    Bob
     |                         |                       |
     |----(1) INVITE --------->|                       |
     |<---(2) 100 Trying ------|                       |
     |                         |----(3) INVITE ------->|
     |                         |<---(4) 180 Ringing ---|
     |<---(5) 180 Ringing -----|                       |
     |                         |<---(6) 200 OK --------|
     |<---(7) 200 OK ----------|                       |
     |---(8) ACK ------------->|                       |
     |                         |---(9) ACK ----------->|
     |                         |                       |
     |======= media flows between Alice and Bob =======|
```

What each step does:

1. **INVITE**, Alice's user agent sends the call invitation with an SDP offer
2. **100 Trying**, Server tells Alice "got it, stop retransmitting"
3. **INVITE**, Server forwards the invitation to Bob
4. **180 Ringing**, Bob's phone is ringing
5. **180 Ringing**, Server tells Alice so she hears ringback tone
6. **200 OK**, Bob picked up! SDP answer is in the body
7. **200 OK**, Server forwards the acceptance to Alice
8. **ACK**, Alice acknowledges receiving the 200 OK
9. **ACK**, Server forwards the ACK to Bob

After step 9, the call is established. Media (RTP) flows between Alice and Bob directly (peer-to-peer via WebRTC in our setup), not through the server.

### BYE, Hanging Up

Either side can end the call by sending a BYE:

```
   Alice                    Server                    Bob
     |                         |                       |
     |----BYE ---------------->|                       |
     |                         |----BYE -------------->|
     |                         |<---200 OK ------------|
     |<---200 OK --------------|                       |
```

Two messages each way. Clean and simple.

### Header Deep Dive

A few headers carry most of the meaning. Here's what they do:

**Via**, Breadcrumbs for responses. Each proxy adds a Via header as a message travels forward. Responses follow Via headers in reverse to find their way back to the originator. The `branch` parameter identifies a transaction (starts with `z9hG4bK` for RFC 3261 compliance).

**From / To**, Logical identities. From is who sent the message, To is who it's addressed to. The `tag` parameter on From/To identifies the dialog participants. Important: From and To don't swap in responses, they identify roles in the dialog, not the direction of the current message.

**Call-ID**, Unique per call. Generated by the caller, present in every message of the dialog. Used to correlate related messages.

**CSeq**, "Command Sequence." Combines a number and a method name (e.g., `314159 INVITE`). The number increments per request from the same originator. ACK uses the CSeq number of the INVITE it acknowledges.

**Contact**, Where to reach this user agent directly. Important for subsequent in-dialog messages that skip the proxy.

**Content-Type / Content-Length**, Describe the body, same as HTTP.

### Dialogs and Transactions

SIP has two nested units of communication:

- **Transaction**, A single request and all its responses. The INVITE→100→180→200 sequence is one transaction. The ACK is its own transaction (per RFC 3261's quirky rules).
- **Dialog**, The complete conversation. From INVITE through BYE is one dialog. A dialog is identified by `Call-ID` + `From tag` + `To tag`.

In this project we don't build full dialog or transaction state machines, since the browsers do that heavy lifting. The live server routes each message by its JSON `target` field, not by parsing SIP headers. The header machinery described here lives in the standalone `sip.py` parser, which the tests exercise.

### SIP Body and SDP

When SIP carries a body (in INVITE and its 200 OK), that body is almost always SDP. The SIP side just declares `Content-Type: application/sdp` and passes the text through. Our `sip.py` keeps the body as a string; `sdp.py` parses it when the application needs to inspect or modify the codec list. See [docs/sdp.md](sdp.md) for SDP details.

### Our Types

The SIP types are defined in `voip/types/sip.py`:

- `SipMethod`, Enum: REGISTER, INVITE, ACK, BYE, CANCEL
- `SipRequest`, method, uri, version, headers, body
- `SipResponse`, version, status_code, reason_phrase, headers, body
- `SipMessage`, Union: `SipRequest | SipResponse`

Functions:

- `parse_sip(raw: str) -> SipMessage`, detects request vs response from first line
- `build_sip_request(method, uri, headers, body=None) -> str`
- `build_sip_response(status_code, reason, headers, body=None) -> str`

### RFC Reference

- **RFC 3261 §7**, SIP messages (request/response structure)
- **RFC 3261 §8**, General user agent behavior
- **RFC 3261 §13**, Initiating a session (INVITE)
- **RFC 3261 §15**, Terminating a session (BYE)
- **RFC 3261 §20**, Header field definitions
- **RFC 3261 §24**, Examples
- **RFC 7118**, WebSocket transport for SIP (used when running SIP over WS)
