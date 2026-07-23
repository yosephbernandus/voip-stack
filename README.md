# voip-stack

A working VoIP stack built from scratch in Python. It parses SIP, negotiates
codecs with SDP, frames audio into RTP, smooths arrival timing with a jitter
buffer, and ships a browser client so you can place a real call between two
tabs.

The point is understanding, not production. Every protocol layer is written by
hand against its RFC so you can read exactly what crosses the wire, byte by
byte. Pydantic models describe each structure and validate it at parse time, so
the type definitions double as the specification.

## What is and is not from scratch

The SIP, SDP, RTP, and jitter buffer code is all original, standard library
plus Pydantic, no VoIP libraries. The one exception is the WebSocket transport,
which uses the `websockets` library. A browser cannot open a raw TCP or UDP
socket, so WebSocket is the only way to reach it, and WebSocket is plumbing
rather than VoIP. Keeping the transport off the shelf lets the from-scratch
effort go where it matters.

Media is carried by the browser. The signaling plane (register, invite,
answer, ICE, bye) runs through the Python server. The voice itself flows
directly between browsers over UDP through WebRTC, exactly as it does in
production, where the signaling plane and the media plane are also separate.

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
       |============ RTP audio, directly over UDP, WebRTC ==========|
```

The server is a rendezvous point. It routes JSON messages by name and never
touches the audio. Each JSON message type maps one to one onto a SIP concept,
which keeps the routing readable while staying faithful to the protocol.

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

## License

MIT. See [LICENSE](LICENSE).
