# RTP (RFC 3550)

## Layer 1, Quick Summary

### What is RTP?

RTP (Real-time Transport Protocol) is the envelope for voice data. Every 20 milliseconds, one envelope goes out with a tiny chunk of audio inside. At 50 envelopes per second, your voice arrives on the other side in near real-time. Each envelope has a sequence number so the receiver can put them back in order if they arrive shuffled, and a timestamp so the receiver knows when to play it.

RTP runs over UDP, not TCP. That's deliberate, for real-time audio, a *late* packet is worse than a *lost* packet. TCP's retransmission would add unpredictable delay. With RTP over UDP, if a packet is lost, the receiver briefly inserts silence (or noise, or last-packet repetition) and moves on.

### Where It Fits

RTP is the media plane of our VoIP call. SIP sets up the call (signaling), SDP negotiates the codec, then RTP carries the actual compressed audio samples between endpoints.

```
   Signaling plane:  Browser <--SIP--> Server <--SIP--> Browser
                                        |
                                        v (call setup done)
   Media plane:      Browser <=====RTP over UDP=====> Browser
                              (peer-to-peer via WebRTC)
```

In our server-centric implementation, we don't handle the full media transport, the browser's WebRTC stack does that. But we parse RTP server-side for inspection (monitoring, recording, conferencing).

### Key Terms

| Term | Meaning |
|------|---------|
| **SSRC** | Synchronization source, random 32-bit ID for a stream |
| **Sequence number** | 16-bit counter, increments per packet, used for ordering |
| **Timestamp** | 32-bit media clock (sample count), not wall-clock time |
| **Payload type** | 7-bit codec identifier (matches SDP's rtpmap) |
| **Marker bit** | Payload-type-specific flag; for audio, marks first packet after silence |
| **CSRC** | Contributing source, used by mixers to list sources of mixed audio |

### What We Implement vs What We Skip

**Implement:**
- Parse and build the 12-byte fixed header
- Extract and verify bit fields (version, flags, counts)
- Handle the payload
- Sequence number and timestamp handling

**Skip:**
- RTCP (the sibling control protocol, sender/receiver reports)
- Header extensions (`X=1` case)
- CSRC list processing (we don't build mixers)
- Padding handling (rarely used outside encryption)

---

## Layer 2, Deep Dive

### The 12-Byte Fixed Header

Every RTP packet starts with exactly 12 bytes of header, followed by the payload. If CSRCs are present (rare in peer-to-peer calls), additional 4-byte entries follow.

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|V=2|P|X|  CC   |M|     PT      |       sequence number         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           timestamp                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           synchronization source (SSRC) identifier            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|            contributing source (CSRC) identifiers             |
|                             ....                              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### Field-by-Field

| Field | Bits | Description |
|-------|------|-------------|
| **V** | 2 | Version. Always `2` for current RTP. Version 1 was an early draft, 0 is invalid. |
| **P** | 1 | Padding. If set, the last byte of the payload indicates how many padding bytes were added (used with encryption). |
| **X** | 1 | Extension. If set, a 4-byte extension header follows the CSRC list. |
| **CC** | 4 | CSRC Count. Number of 4-byte CSRC entries following the fixed header. 0 for one-to-one calls. |
| **M** | 1 | Marker. Payload-type-specific. For audio: first packet after a silence period. |
| **PT** | 7 | Payload Type. Identifies the codec. 0 = PCMU, 8 = PCMA, 96-127 dynamic (negotiated via SDP). |
| **Sequence Number** | 16 | Increments by 1 per packet. Wraps at 65535. Random initial value. |
| **Timestamp** | 32 | Media clock. Increments by number of samples per packet (160 for 8kHz/20ms). Random initial value. |
| **SSRC** | 32 | Randomly chosen stream identifier. Must be unique within a session. |

### Parsing with struct.unpack

The standard way to parse this in Python:

```python
import struct

# Fixed 12 bytes: 1+1+2+4+4 = 12, big-endian
byte0, byte1, sequence_number, timestamp, ssrc = struct.unpack('!BBHII', data[:12])

# Byte 0: version(2) + padding(1) + extension(1) + csrc_count(4)
version     = (byte0 >> 6) & 0x03
padding     = bool((byte0 >> 5) & 0x01)
extension   = bool((byte0 >> 4) & 0x01)
csrc_count  = byte0 & 0x0F

# Byte 1: marker(1) + payload_type(7)
marker       = bool((byte1 >> 7) & 0x01)
payload_type = byte1 & 0x7F

# Payload starts after fixed header + CSRC list
header_length = 12 + (csrc_count * 4)
payload = data[header_length:]
```

This is simple and clear, and it is also the natural hot path. `struct.unpack` has per-call overhead, and constructing a `RtpHeader` Pydantic model for every packet adds more. At 50 packets/sec per call across hundreds of calls, this cost adds up.

### The Math of Voice Packets

Let's walk through the numbers for a PCMU (G.711 μ-law) call:

| Parameter | Value |
|-----------|-------|
| Sample rate | 8,000 Hz (samples per second) |
| Packetization interval | 20 ms |
| Samples per packet | 8000 × 0.020 = 160 |
| Bits per sample | 8 (μ-law compressed) |
| Audio payload size | 160 × 1 byte = 160 bytes |
| RTP header size | 12 bytes |
| UDP header | 8 bytes |
| IP header | 20 bytes |
| Total packet size | 200 bytes on the wire |
| Packets per second | 1000 / 20 = 50 |
| Bandwidth per direction | 200 × 50 × 8 = 80,000 bps = 80 kbps |

So a G.711 VoIP call consumes about 80 kbps per direction, 160 kbps total. That's why G.711 is sometimes called "64 kbps", the 64k is just the audio payload, excluding headers.

For Opus at 48 kHz with 20ms frames:
- Samples per packet: 48000 × 0.020 = 960
- Audio payload: variable (Opus is VBR), typically 40-120 bytes for voice
- Total packet size: ~70-150 bytes
- Bandwidth: 30-60 kbps per direction

Opus is more efficient *and* higher quality than G.711. It's the right choice when both endpoints support it, which is why WebRTC uses it by default.

### Sequence Number vs Timestamp

These look similar but do different things:

**Sequence number**, Detects loss and enables reordering. Every packet increments it by 1. If you receive `42, 43, 45, 44, 46`, you know:
- `44` arrived out of order, reorder it
- `46` after `44` means you've seen all of 42-46, so move on

**Timestamp**, Tells the receiver *when* to play this packet relative to other packets in the same stream. It's not wall-clock time, it's the media clock, which increments by samples-per-packet. For 8 kHz audio with 20ms packets: `ts + 160` in each successive packet. For 48 kHz: `ts + 960`.

Why both? The timestamp can skip forward (e.g., during silence suppression, you send fewer packets but the timestamp jumps by the skipped audio). The sequence number cannot, it's always +1. So the sequence number detects loss, and the timestamp schedules playout.

### Why UDP, Not TCP

TCP guarantees delivery via retransmission. If a packet is lost, TCP holds back all subsequent packets until the lost one is retransmitted. For real-time audio, this is disastrous: a single lost packet could cause a 200ms gap (one RTT for the retransmission) instead of the 20ms gap that losing the packet entirely would cause.

UDP has no such guarantees. If a packet is lost, it's just gone. The jitter buffer detects the gap (see [docs/jitter-buffer.md](jitter-buffer.md)) and the codec's PLC (packet loss concealment) fills in plausible-sounding replacement audio. To your ear, a 20ms gap is barely noticeable; a 200ms gap is unmissable.

This is the fundamental reason RTP uses UDP: trading guaranteed delivery for predictable low latency.

### SSRC Collision

The SSRC is 32 bits of randomness. With one call, you pick one SSRC. In a large conference with many participants, there's a small chance two participants pick the same SSRC. RFC 3550 §8.2 defines a detection-and-resolution procedure: if you see a packet with your SSRC that isn't yours, pick a new random SSRC and signal the change via RTCP.

For our implementation, we don't implement SSRC collision detection, it's vanishingly rare in peer-to-peer calls.

### Hot Path Context

RTP parsing is the busiest code path in the whole stack:

- At 50 packets/sec per call across 100 calls, that is 5000 parse operations per second
- Each parse runs `struct.unpack` (C level, but with Python call overhead) and constructs a Pydantic model

If you ever needed to scale this beyond a demo, this function is where the
profiler would point first, and where a compiled extension would earn its keep.

### Our Types

The RTP types are defined in `voip/types/rtp.py`:

- `RtpHeader`, version, padding, extension, csrc_count, marker, payload_type, sequence_number, timestamp, ssrc (with Field constraints matching bit widths)
- `RtpPacket`, header (RtpHeader), payload (bytes)

Functions:

- `parse_rtp(data: bytes) -> RtpPacket`
- `build_rtp(packet: RtpPacket) -> bytes`
- `parse_rtp_header(data: bytes) -> tuple[RtpHeader, int]`, returns header + bytes consumed

### RFC Reference

- **RFC 3550 §5.1**, RTP fixed header fields
- **RFC 3550 §5.3**, RTP header extension
- **RFC 3550 §6**, RTCP (not implemented, but worth reading)
- **RFC 3550 §7**, Translators and mixers (CSRC semantics)
- **RFC 3550 §8.2**, SSRC collision detection
- **RFC 3551**, RTP profile for audio/video (static payload type assignments)
- **RFC 7587**, RTP payload format for Opus
