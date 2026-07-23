# voip-stack

An educational Python protocol lab for SIP, SDP, RTP, and jitter buffering,
with a runnable browser-to-browser WebRTC demo and a Python signaling server.

Each protocol layer is written by hand against its RFC so you can read exactly
what the format looks like, byte by byte. Pydantic models describe every
structure and validate it at parse time, so the type definitions double as the
specification. The point is understanding, not production.

## What actually runs, and what is here to read

Being precise about this matters, because it is easy to overclaim.

**The live call uses three things:**

- **Signaling:** JSON over WebSocket, routed by the Python server in
  `voip/server.py`. Each message type maps one to one onto a SIP concept
  (register, invite, answer, ICE, bye).
- **Media:** encrypted SRTP, set up and carried by the browser's WebRTC stack.
  It flows directly between browsers when it can, and through a TURN relay when
  it cannot, which may include TCP or TLS. The Python server never touches
  audio.
- **The browser client** in `client/index.html`, which drives WebRTC and the UI.

**The protocol modules are standalone learning implementations.** `sip.py`,
`sdp.py` (beyond its `log_sdp` helper), `rtp.py`, `jitter.py`, and `audio.py`
are hand-written parsers and builders with their own test suites. They show how
each format works at the byte level. They are not on the live media path, since
the browser's WebRTC stack handles real SIP-style negotiation, RTP, and
playout. Read them to understand the protocols, not because the running call
imports them.

This split mirrors production, where the signaling plane and the media plane
are separate systems. Here the signaling plane is Python and the media plane is
WebRTC.

## The one dependency that is not from scratch

Everything in the protocol modules is standard library plus Pydantic, no VoIP
libraries. The single exception is the WebSocket transport, which uses the
`websockets` library. A browser cannot open a raw TCP or UDP socket, so
WebSocket is the only way to reach it, and WebSocket is plumbing rather than
VoIP. Keeping the transport off the shelf lets the from-scratch effort go where
it matters.

## Requirements

- Python 3.12 or newer
- A modern browser for the client

## Run it

### With a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m voip
```

Open http://localhost:8080 in two browser tabs. Register a different name in
each, then call one from the other.

### With Docker

```bash
docker compose up --build
```

Then open http://localhost:8080 in two tabs.

### With Nix

```bash
nix develop
python -m voip
```

## Test

```bash
pip install -e ".[dev]"
pytest
```

The suite covers round trips, known-good vectors taken from the RFCs and from
Wireshark captures, malformed input, and boundary conditions.

## How a call flows

```
  Browser tab A                 Python server                 Browser tab B
       |                             |                              |
       |--- register (WebSocket) --->|<--- register (WebSocket) ----|
       |--- invite + SDP offer ----->|--- invite + SDP offer ------>|
       |<-- answer + SDP answer -----|<-- answer + SDP answer ------|
       |--- ICE candidates --------->|--- ICE candidates ---------->|
       |                             |                              |
       |===== encrypted SRTP over WebRTC, direct or via a TURN =====|
       |=====        relay, never through the Python server    =====|
```

Two planes, kept separate:

- **Signaling:** JSON over WebSocket, routed by Python. Each message type maps
  one to one onto a SIP concept, which keeps the routing readable.
- **Media:** encrypted SRTP negotiated and carried by WebRTC. The path is
  chosen by ICE and can be direct or relayed through TURN. The Python server is
  a rendezvous point for signaling and never touches the audio.

## Layout

```
voip/            the stack
  server.py      WebSocket signaling server, routes calls by name
  sip.py         SIP message parser and builder (RFC 3261)
  sdp.py         SDP parser, builder, and codec negotiation (RFC 4566)
  rtp.py         RTP packet parser and builder (RFC 3550)
  jitter.py      jitter buffer, reorders and smooths playout
  audio.py       PCM mixing with saturating arithmetic
  types/         Pydantic models, one module per protocol layer
client/          browser client (WebRTC and UI)
tests/           one test module per protocol layer
docs/            protocol guides for each layer
deploy/          example Kubernetes manifest
```

## Protocol guides

Each layer has a written walkthrough of the RFC and how this code implements it:

- [docs/sip.md](docs/sip.md), session setup and teardown (RFC 3261)
- [docs/sdp.md](docs/sdp.md), codec negotiation (RFC 4566)
- [docs/rtp.md](docs/rtp.md), the media packet format (RFC 3550)
- [docs/jitter-buffer.md](docs/jitter-buffer.md), reordering and playout timing

## NAT traversal

Two browsers behind NAT cannot usually reach each other directly. The client
uses ICE to find a path: a direct host route, a public address discovered
through STUN, or a relay through TURN as a last resort. STUN is free and needs
no setup. TURN relays real audio, so it needs credentials.

To enable a TURN relay, set `TURN_KEY_ID` and `TURN_API_TOKEN` in the
environment. The server mints short-lived credentials per session and delivers
them to the client inside the registration reply. Nothing is baked into the
image or committed to the repo. Without these, the stack runs on STUN alone,
which is enough for two tabs on the same machine or the same network.

## Scope and security boundary

This is a learning project, and the signaling server is deliberately minimal.
If you deploy it somewhere public, know what it does not do:

- **No identity or authentication.** Anyone can register any free name. There
  is no login, no token, and no proof of who a caller is.
- **No authorization on call messages.** The server routes answer, bye, and
  reject by the names in the message. It does not verify that the sender is a
  participant in that call.
- **No rate limiting.** A visitor can register repeatedly. If TURN credentials
  are configured, each registration mints a fresh set, so an open deployment
  could run up relay cost. The long-lived API token stays server side and is
  never exposed, but the minting itself is unthrottled.
- **No horizontal scaling.** The user registry and call state live in process
  memory, so the server runs as a single instance. Two replicas would split
  users across processes and calls between them would fail.

None of this matters for a local demo or a controlled environment. It does
matter before putting the server on the open internet.

## License

MIT. See [LICENSE](LICENSE).
