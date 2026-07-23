"""
Audio mixing and PCM stream processing for VoIP conference bridging.

Implements the mixing pipeline that combines multiple participant audio streams
into a single output stream. All operations use saturating (clamping) arithmetic
so overflow produces clean saturation rather than the loud crackling caused by
signed integer wrap-around.

mix_streams is the hot path here. It runs once per RTP packet interval, every
20 ms at 8 kHz, and the per-sample loop is the natural bottleneck.
"""

import math
import struct

from voip.types.audio import AudioFormat

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mix_streams(
    streams: list[bytes],
    format: AudioFormat = AudioFormat(),
) -> bytes:
    """
    Mix multiple raw PCM streams by summing corresponding samples.

    Implements conference bridge mixing: each participant's decoded audio is
    one stream; the output is the sum of all streams, clipped to the
    representable range of format.sample_width (16-bit signed: [-32768, 32767]).

    Saturating arithmetic is used deliberately: when several participants speak
    at once, the sum is clipped rather than wrapping, which sounds like
    momentary loudness rather than a harsh click.

    Args:
        streams: List of raw PCM byte buffers.  All must have the same length.
        format:  Describes the encoding of every stream in the list.

    Returns:
        Mixed PCM bytes of the same length as each input stream.

    Raises:
        ValueError: If streams have different lengths.
    """
    if not streams:
        return b''

    if len(streams) == 1:
        return streams[0]

    # Validate all streams have equal length before processing.
    first_length = len(streams[0])
    for stream in streams[1:]:
        if len(stream) != first_length:
            raise ValueError(
                f'All streams must have the same length: '
                f'expected {first_length} bytes, got {len(stream)} bytes'
            )

    # Length must be a whole number of samples. Without this check a trailing
    # partial sample would be silently truncated by integer division, so reject
    # it explicitly and fail loudly instead.
    if first_length % format.sample_width != 0:
        raise ValueError(
            f'stream length must be a multiple of {format.sample_width} '
            f'({format.sample_width * 8}-bit PCM), got {first_length} bytes'
        )

    num_samples = first_length // format.sample_width
    mixed: list[bytes] = []

    for i in range(num_samples):
        offset = i * format.sample_width
        total = 0
        for stream in streams:
            # '<h' = little-endian signed 16-bit integer (one sample)
            sample = struct.unpack_from('<h', stream, offset)[0]
            total += sample
        # Saturating clamp to 16-bit signed range
        total = max(-32768, min(32767, total))
        mixed.append(struct.pack('<h', total))

    return b''.join(mixed)


def adjust_volume(data: bytes, factor: float) -> bytes:
    """
    Scale every PCM sample by a floating-point factor with saturation clamping.

    factor=1.0 is unity gain (no change).
    factor=0.0 produces silence.
    factor>1.0 amplifies; samples that exceed the 16-bit range are clipped.
    factor<0.0 inverts polarity (phase flip).

    Args:
        data:   Raw PCM bytes encoded as 16-bit signed little-endian samples.
        factor: Linear gain multiplier applied to every sample.

    Returns:
        Volume-adjusted PCM bytes, same length as input.
    """
    num_samples = len(data) // 2  # 2 bytes per 16-bit sample
    adjusted: list[bytes] = []

    for i in range(num_samples):
        # '<h' = little-endian signed 16-bit integer
        sample = struct.unpack_from('<h', data, i * 2)[0]
        scaled = int(sample * factor)
        # Saturating clamp to 16-bit signed range
        scaled = max(-32768, min(32767, scaled))
        adjusted.append(struct.pack('<h', scaled))

    return b''.join(adjusted)


def generate_silence(
    duration_ms: int,
    format: AudioFormat = AudioFormat(),
) -> bytes:
    """
    Generate a zero-filled PCM buffer representing silence.

    Used by the jitter buffer for packet-loss concealment (PLC): when a
    sequence gap is detected, silence is inserted rather than repeating the
    last packet, giving a brief mute that is perceptually less disruptive than
    random noise or packet repetition.

    Args:
        duration_ms: Duration of silence in milliseconds.
        format:      Describes sample rate, width, and channel count.

    Returns:
        Zero-filled bytes; length = (sample_rate * duration_ms // 1000)
                                    * sample_width * channels.
    """
    num_samples = (format.sample_rate * duration_ms) // 1000
    return b'\x00' * (num_samples * format.sample_width * format.channels)


def generate_tone(
    frequency_hz: float,
    duration_ms: int,
    format: AudioFormat = AudioFormat(),
) -> bytes:
    """
    Synthesise a pure sine wave tone at the given frequency.

    Useful for generating dial tones, ringback tones, and DTMF test signals
    without external audio assets.  Amplitude is set to 50% of full scale
    (16383 for 16-bit audio) to leave headroom so mixing two tones does not
    clip.

    The sine wave is computed as:
        value[i] = amplitude * sin(2π * frequency_hz * i / sample_rate)

    which places a zero crossing at i=0, matching the convention used by
    most telephony tone generators.

    Args:
        frequency_hz: Frequency of the synthesised tone in hertz.
        duration_ms:  Duration of the tone in milliseconds.
        format:       Describes sample rate and encoding.

    Returns:
        Raw PCM bytes containing the synthesised sine wave.
    """
    num_samples = (format.sample_rate * duration_ms) // 1000
    # 50% of full scale leaves headroom when mixing with a second tone
    amplitude = 32767 // 2  # 16383

    samples: list[bytes] = []
    for i in range(num_samples):
        t = i / format.sample_rate
        value = int(amplitude * math.sin(2 * math.pi * frequency_hz * t))
        samples.append(struct.pack('<h', value))

    return b''.join(samples)
