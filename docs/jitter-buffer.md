# Jitter Buffer

## Layer 1, Quick Summary

### What is a Jitter Buffer?

A jitter buffer is like a post office sorting room. Letters (packets) arrive at random times, some fast, some slow, and occasionally out of order. The sorting room holds them briefly, puts them in the right order, and delivers them on a regular schedule to your mailbox. You trade a tiny, predictable delay for smooth, ordered delivery.

Without a jitter buffer, your audio would sound like stuttering, out-of-order words, or worse, crackling silence when packets arrive 5ms late. With a jitter buffer set to ~60ms, the playback is smooth: the buffer absorbs the network's variability.

### Where It Fits

The jitter buffer sits between the network (receiving RTP packets) and the audio playout (feeding samples to the speaker).

```
  Network -> RTP parse -> [ Jitter Buffer ] -> Audio playout -> Speaker
                                  ^
                                  |
                        smooths out timing variations,
                        reorders out-of-sequence packets,
                        detects gaps
```

### Key Terms

| Term | Meaning |
|------|---------|
| **Jitter** | Variation in packet inter-arrival times |
| **Playout delay** | How long a packet is buffered before being played |
| **Target delay** | The jitter buffer's aim for how much delay to introduce |
| **Packet loss** | Packets that never arrive |
| **Late packet** | Packet that arrives after its playout time (useless, must be dropped) |
| **Reordering** | Putting packets back in sequence-number order |

### What We Implement vs What We Skip

**Implement:**
- Fixed-delay jitter buffer with configurable target delay
- Insert by sequence number (sorted order)
- Reordering of out-of-sequence arrivals
- Gap detection (missing sequence numbers)
- Statistics: received, lost, late, current delay

**Skip:**
- Adaptive jitter buffer (dynamically adjusting delay based on network conditions)
- FEC (forward error correction, extra redundancy packets)
- PLC (packet loss concealment, synthesizing replacement audio)

---

## Layer 2, Deep Dive

### Why Jitter Exists

The internet is not a dedicated circuit. Packets take different paths through different routers, encounter different congestion, and arrive with variable delay. This variation is called *jitter*.

Here's an example, the sender emits a packet every 20ms like clockwork, but the receiver sees:

```
Packet #   Sent at    Arrived at    One-way delay
------------------------------------------------
1          0 ms       45 ms          45 ms
2          20 ms      62 ms          42 ms
3          40 ms      110 ms         70 ms    <- spike!
4          60 ms      85 ms          25 ms    <- arrived before #3!
5          80 ms      128 ms         48 ms
6          100 ms     142 ms         42 ms
```

Without buffering, the receiver would:
- Play #1 at 45ms (25ms late)
- Play #2 at 62ms (just in time)
- Start playing #4 at 85ms (out of order!)
- #3 arrives at 110ms, too late, audio garbled

With a 60ms jitter buffer, the receiver:
- Holds each packet until 60ms after the stream started
- By then, #3 has arrived, and everything plays in order
- Tradeoff: audio is delayed 60ms, unnoticeable in conversation

### The Fundamental Tradeoff

Buffer size vs latency is the central design tension:

| Buffer size | Pros | Cons |
|-------------|------|------|
| Small (20ms) | Low latency, snappy conversation | Packets arriving late become "lost" |
| Medium (60ms) | Good balance | Slight perceptible delay |
| Large (200ms) | Absorbs network spikes | Noticeable conversation lag |

Humans tolerate up to ~150ms of one-way delay before conversations feel awkward (long pauses, talk-overs). Above ~300ms, it's uncomfortable. So a 60-80ms jitter buffer is a common sweet spot for VoIP.

### How Our Jitter Buffer Works

The algorithm is simple:

**put(packet):**
1. If the buffer is full, drop the oldest packet to make room. This is not counted as loss here. The gap it leaves is counted later, when `get()` skips over it, so a packet is never counted twice
2. If the packet's sequence number is older than `next_expected_sequence`, it's too late, increment `packets_late` and drop it
3. Otherwise, insert the packet into its sorted position by `sequence_number`

**get():**
1. If the expected packet is present and has been held for `target_delay_ms`, pop and return it, and advance `next_expected_sequence`
2. If the expected packet is missing, wait for it, but only until a later packet has itself waited `target_delay_ms`. That playout deadline is what keeps one lost packet from stalling the stream forever
3. Once the deadline passes with the expected packet still missing, declare the gap lost (add it to `packets_lost`), advance `next_expected_sequence` over it, and return the earliest packet that did arrive
4. If nothing is playable yet, return `None`

**stats():**
Returns a `BufferStats` with counters: packets_received, packets_lost, packets_late, current_delay_ms.

### Sequence Number Wraparound

RTP sequence numbers are 16 bits, they wrap from 65535 back to 0. Comparing sequence numbers requires modular arithmetic:

```python
# NAIVE (wrong):
if next_packet_seq > current_seq:
    # ... but 0 < 65535, even though 0 comes AFTER 65535 in the stream!
    pass

# CORRECT:
def is_after(b: int, a: int) -> bool:
    """True if b comes after a in the sequence, handling wraparound."""
    return (b - a) % 65536 < 32768  # Within half the space ahead
```

The 32768 (half of 65536) is the cutoff for determining which side of the wraparound point a number is on. If the delta is less than 32768, `b` is "after" `a`; if more, `a` is after `b`. This gives us a signed distance on a circular number line.

### Buffer Statistics

Our `BufferStats` is designed to align with RTCP receiver report fields (RFC 3550 §6.4.1):

| Field | Meaning |
|-------|---------|
| `packets_received` | Total packets successfully delivered to `get()` |
| `packets_lost` | Gap count, packets that were never received |
| `packets_late` | Packets dropped because they arrived after playout |
| `current_delay_ms` | Current effective delay between receive and playout |

These counters are exactly what an endpoint would include in an RTCP Receiver Report to tell the sender about network quality. We collect them for potential future RTCP implementation.

### Why This is a Class (Not a Function)

Everything else in this codebase is stateless, we use functions because they're simpler and typed signatures tell the whole story. The jitter buffer is the exception. It maintains:

- The packet buffer (sorted data structure)
- `next_expected_sequence` (state across calls)
- Timing information (when packets arrived, playout clock)
- Running statistics counters

None of this can be expressed as `f: A -> B`. A function with state would need to pass all of this through parameters, which is just a class with more steps. Every other parser in this codebase is a plain function, so the jitter buffer is the one deliberate exception, and it is a class because it genuinely owns state.

### Our Types

The jitter buffer types are defined in `voip/types/jitter.py`:

- `PacketStatus`, Enum: RECEIVED, MISSING, LATE
- `JitterBufferConfig`, target_delay_ms, max_buffer_size
- `BufferStats`, packets_received, packets_lost, packets_late, current_delay_ms

The class signature (implemented in `voip/jitter.py`):

```python
class JitterBuffer:
    def __init__(self, config: JitterBufferConfig) -> None: ...
    def put(self, packet: RtpPacket) -> None: ...
    def get(self) -> RtpPacket | None: ...
    def stats(self) -> BufferStats: ...
```

### Not a Protocol, an Implementation Technique

Unlike the other docs in this folder, jitter buffering isn't defined by an RFC. It's an implementation concern, every VoIP receiver needs one, but the RFCs don't specify *how* to implement it. Different codecs and different network conditions call for different strategies.

That said, RFC 3550 §6.4.1 defines the RTCP Receiver Report fields (`fraction lost`, `cumulative packets lost`, `interarrival jitter`, etc.) that a sender uses to communicate network quality back to the source. Our `BufferStats` aligns with these so we could one day emit RTCP reports.

### References

- **RFC 3550 §6.4.1**, RTCP Receiver Report (source of the fields we track)
- **RFC 5109**, RTP payload format for generic FEC (future work if we ever add FEC)
- "VoIP Jitter Buffer Design", various industry whitepapers (search: "adaptive jitter buffer algorithm")
