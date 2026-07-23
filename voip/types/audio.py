"""
Audio mixing and PCM stream type definitions.

Transformation pipeline:
  list[bytes] -> mixed bytes        (mix multiple PCM streams into one)
  bytes + float -> adjusted bytes   (volume scaling)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AudioFormat(BaseModel):
    """
    Describes the encoding of a raw PCM byte buffer.

    All VoIP codecs decode to linear PCM before mixing so that streams from
    different participants can be combined with simple integer arithmetic.
    The defaults match G.711 (PCMU/PCMA) which is the baseline codec for
    SIP/RTP telephony: 8 kHz, 16-bit signed little-endian, mono.

    sample_width is in bytes: 1 = 8-bit (μ-law/A-law compressed), 2 = 16-bit
    linear PCM.  After G.711 decoding we always work with 16-bit linear PCM
    so the default of 2 is correct for the mixing pipeline.

    Frozen (immutable) so a single shared instance can safely serve as the
    default argument of the mixing functions without the mutable-default
    pitfall, since a format descriptor never changes after construction.
    """

    model_config = ConfigDict(frozen=True)

    # Samples per second; 8000 Hz = narrowband telephony (G.711, G.729)
    sample_rate: int = 8000
    # Bytes per sample: 2 = 16-bit signed linear PCM (standard after codec decode)
    sample_width: int = 2
    # Number of audio channels; telephony is always mono
    channels: int = 1


class PcmStream(BaseModel):
    """
    A buffer of raw linear PCM audio together with its format description.

    Keeping format and data together prevents mismatches when streams from
    different sources (e.g. a narrowband G.711 call and a wideband opus stream
    after resampling) flow through the same mixing pipeline.

    The data bytes are interpreted as signed, little-endian integers whose
    width is format.sample_width.  For 16-bit mono audio, every two consecutive
    bytes form one sample: struct.unpack('<h', data[i:i+2]).
    """

    # Raw PCM bytes; length must be a multiple of (format.sample_width * format.channels)
    data: bytes
    # Use default_factory so each PcmStream gets its own AudioFormat instance
    format: AudioFormat = Field(default_factory=AudioFormat)


# ---------------------------------------------------------------------------
# Function signatures implemented in voip.audio (documented here for
# discoverability alongside the types they operate on).
# ---------------------------------------------------------------------------
#
# mix_streams(streams: list[bytes], format: AudioFormat = AudioFormat()) -> bytes
#   Sum corresponding samples from all streams, clipping to the representable
#   range of format.sample_width to prevent integer overflow wrap-around
#   (which sounds like loud crackling rather than saturation).  All streams
#   must have the same byte length and format; callers should zero-pad shorter
#   streams before mixing.
#
# adjust_volume(data: bytes, factor: float) -> bytes
#   Multiply every sample by factor and clip.  factor=1.0 is unity gain;
#   factor=0.0 produces silence; factor>1.0 amplifies (use with care).
#
# generate_silence(duration_ms: int, format: AudioFormat = AudioFormat()) -> bytes
#   Return a zero-filled byte buffer of the correct length for the given
#   duration.  Used by the jitter buffer for packet-loss concealment (PLC)
#   when a gap is detected and no more sophisticated PLC algorithm is available.
#
# generate_tone(
#     frequency_hz: float,
#     duration_ms: int,
#     format: AudioFormat = AudioFormat(),
# ) -> bytes
#   Synthesise a pure sine wave at frequency_hz for use in dial-tone generation,
#   ringback tone, or DTMF test signals.  Amplitude is set to 50% of full scale
#   to leave headroom for mixing.
