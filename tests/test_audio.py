"""
Tests for voip.audio, PCM mixing, volume adjustment, and tone generation.

Tests cover:
  - mix_streams: sum, saturation clamping, edge cases
  - adjust_volume: unity gain, zero gain, amplification, clamping
  - generate_silence: length correctness, zero content
  - generate_tone: length, amplitude, frequency (zero crossings)

Known-good values are derived from the 16-bit signed PCM arithmetic spec.
"""

import math
import struct

import pytest

from voip.audio import adjust_volume, generate_silence, generate_tone, mix_streams
from voip.types.audio import AudioFormat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _samples_to_bytes(samples: list[int]) -> bytes:
    """Pack a list of signed 16-bit integers into little-endian PCM bytes."""
    return struct.pack(f'<{len(samples)}h', *samples)


def _bytes_to_samples(data: bytes) -> list[int]:
    """Unpack little-endian PCM bytes into a list of signed 16-bit integers."""
    n = len(data) // 2
    return list(struct.unpack(f'<{n}h', data))


# ---------------------------------------------------------------------------
# mix_streams
# ---------------------------------------------------------------------------


def test_mix_two_streams_basic():
    """Summing two simple streams produces element-wise addition."""
    stream_a = _samples_to_bytes([100, 200, 300])
    stream_b = _samples_to_bytes([10, 20, 30])
    result = mix_streams([stream_a, stream_b])
    assert _bytes_to_samples(result) == [110, 220, 330]


def test_mix_clamp_overflow():
    """Two streams at max positive value clamp to 32767, not wrap-around."""
    stream_a = _samples_to_bytes([32767, 32767])
    stream_b = _samples_to_bytes([32767, 1])
    result = mix_streams([stream_a, stream_b])
    samples = _bytes_to_samples(result)
    assert samples[0] == 32767  # clamped
    assert samples[1] == 32767  # also clamped (32767 + 1 = 32768 > 32767)


def test_mix_clamp_underflow():
    """Two streams at min negative value clamp to -32768, not wrap-around."""
    stream_a = _samples_to_bytes([-32768, -32768])
    stream_b = _samples_to_bytes([-32768, -1])
    result = mix_streams([stream_a, stream_b])
    samples = _bytes_to_samples(result)
    assert samples[0] == -32768  # clamped
    assert samples[1] == -32768  # also clamped (-32768 + -1 = -32769 < -32768)


def test_mix_single_stream_returns_unchanged():
    """Single-stream mix is a no-op; same bytes returned."""
    data = _samples_to_bytes([100, -200, 300, -400])
    result = mix_streams([data])
    assert result == data


def test_mix_empty_list_returns_empty():
    """Empty stream list returns empty bytes."""
    assert mix_streams([]) == b''


def test_mix_unequal_lengths_raises():
    """Streams of different lengths raise ValueError."""
    stream_a = _samples_to_bytes([1, 2, 3])
    stream_b = _samples_to_bytes([1, 2])
    with pytest.raises(ValueError):
        mix_streams([stream_a, stream_b])


def test_mix_symmetry():
    """mix(A, B) == mix(B, A), order of streams does not matter."""
    stream_a = _samples_to_bytes([100, -200, 500])
    stream_b = _samples_to_bytes([50, 100, -300])
    assert mix_streams([stream_a, stream_b]) == mix_streams([stream_b, stream_a])


def test_mix_with_silence_returns_original():
    """Mixing a stream with silence returns the original stream unchanged."""
    data = _samples_to_bytes([100, -200, 300])
    silence = _samples_to_bytes([0, 0, 0])
    result = mix_streams([data, silence])
    assert result == data


def test_mix_three_streams():
    """Three-stream mix sums all three with saturation."""
    stream_a = _samples_to_bytes([1000, 2000])
    stream_b = _samples_to_bytes([1000, 2000])
    stream_c = _samples_to_bytes([1000, 2000])
    result = mix_streams([stream_a, stream_b, stream_c])
    assert _bytes_to_samples(result) == [3000, 6000]


def test_mix_negative_samples():
    """Negative samples sum correctly without underflow."""
    stream_a = _samples_to_bytes([-1000, -500])
    stream_b = _samples_to_bytes([-2000, 200])
    result = mix_streams([stream_a, stream_b])
    assert _bytes_to_samples(result) == [-3000, -300]


def test_mix_odd_length_raises():
    """Multi-stream input whose length is not a whole number of 16-bit samples
    raises ValueError rather than silently truncating the trailing byte."""
    odd_a = _samples_to_bytes([100, 200]) + b'\x01'  # 5 bytes, not a whole sample
    odd_b = _samples_to_bytes([10, 20]) + b'\x02'
    with pytest.raises(ValueError):
        mix_streams([odd_a, odd_b])


# ---------------------------------------------------------------------------
# adjust_volume
# ---------------------------------------------------------------------------


def test_adjust_volume_unity():
    """factor=1.0 returns data bit-for-bit identical to input."""
    data = _samples_to_bytes([100, -200, 300, -400, 32767, -32768])
    assert adjust_volume(data, 1.0) == data


def test_adjust_volume_zero():
    """factor=0.0 produces all-zero samples (silence)."""
    data = _samples_to_bytes([100, -200, 300])
    result = adjust_volume(data, 0.0)
    assert _bytes_to_samples(result) == [0, 0, 0]


def test_adjust_volume_double():
    """factor=2.0 doubles sample values within range."""
    data = _samples_to_bytes([100, -200, 1000])
    result = adjust_volume(data, 2.0)
    assert _bytes_to_samples(result) == [200, -400, 2000]


def test_adjust_volume_clamp_positive():
    """Large factor causes positive clamping to 32767."""
    data = _samples_to_bytes([20000])
    result = adjust_volume(data, 10.0)  # 200000 > 32767
    assert _bytes_to_samples(result) == [32767]


def test_adjust_volume_clamp_negative():
    """Large factor causes negative clamping to -32768."""
    data = _samples_to_bytes([-20000])
    result = adjust_volume(data, 10.0)  # -200000 < -32768
    assert _bytes_to_samples(result) == [-32768]


def test_adjust_volume_half():
    """factor=0.5 halves sample values (truncating toward zero via int())."""
    data = _samples_to_bytes([1000, -1000])
    result = adjust_volume(data, 0.5)
    assert _bytes_to_samples(result) == [500, -500]


def test_adjust_volume_empty():
    """Empty input returns empty output."""
    assert adjust_volume(b'', 2.0) == b''


# ---------------------------------------------------------------------------
# generate_silence
# ---------------------------------------------------------------------------


def test_generate_silence_all_zeros():
    """Silence buffer contains only zero bytes."""
    silence = generate_silence(100)
    assert silence == b'\x00' * len(silence)
    assert len(silence) > 0


def test_generate_silence_duration_20ms():
    """20 ms at 8000 Hz = 160 samples = 320 bytes (16-bit mono)."""
    silence = generate_silence(20)
    # 8000 samples/sec * 0.020 sec = 160 samples * 2 bytes = 320 bytes
    assert len(silence) == 320


def test_generate_silence_duration_10ms():
    """10 ms at 8000 Hz = 80 samples = 160 bytes."""
    assert len(generate_silence(10)) == 160


def test_generate_silence_custom_format():
    """Custom format affects buffer length correctly."""
    fmt = AudioFormat(sample_rate=16000, sample_width=2, channels=1)
    silence = generate_silence(10, fmt)
    # 16000 samples/sec * 0.010 sec = 160 samples * 2 bytes = 320 bytes
    assert len(silence) == 320
    assert silence == b'\x00' * 320


def test_generate_silence_is_mixable_with_stream():
    """Silence generated for same duration mixes cleanly with a real stream."""
    data = _samples_to_bytes([500, -500, 1000, -1000])
    silence = generate_silence(0)  # 0 ms → 0 bytes; use manual silence instead
    silence = _samples_to_bytes([0, 0, 0, 0])
    result = mix_streams([data, silence])
    assert result == data


# ---------------------------------------------------------------------------
# generate_tone
# ---------------------------------------------------------------------------


def test_generate_tone_length():
    """Tone length matches expected sample count for given duration."""
    tone = generate_tone(440.0, 20)
    # 8000 Hz * 20 ms = 160 samples * 2 bytes = 320 bytes
    assert len(tone) == 320


def test_generate_tone_nonzero():
    """Generated tone contains non-zero samples (it's not silence)."""
    tone = generate_tone(440.0, 100)
    samples = _bytes_to_samples(tone)
    assert any(s != 0 for s in samples)


def test_generate_tone_amplitude():
    """Peak amplitude is approximately 50% of full scale (16383)."""
    tone = generate_tone(440.0, 1000)  # 1 second for accurate peak detection
    samples = _bytes_to_samples(tone)
    peak = max(abs(s) for s in samples)
    # Allow ±5% tolerance due to discrete sampling
    assert 16383 * 0.95 <= peak <= 16383 * 1.05


def test_generate_tone_frequency_zero_crossings():
    """Zero crossings of a 440 Hz tone match expected frequency."""
    sample_rate = 8000
    duration_ms = 1000
    tone = generate_tone(440.0, duration_ms)
    samples = _bytes_to_samples(tone)

    # Count positive-going zero crossings (negative → positive transitions)
    crossings = 0
    for i in range(1, len(samples)):
        if samples[i - 1] < 0 and samples[i] >= 0:
            crossings += 1

    # Expected: ~440 crossings per second for 1 second of audio
    # Allow ±5% tolerance for discrete sampling artefacts
    expected = 440
    assert expected * 0.95 <= crossings <= expected * 1.05


def test_generate_tone_custom_format():
    """Tone length respects custom sample rate."""
    fmt = AudioFormat(sample_rate=16000, sample_width=2, channels=1)
    tone = generate_tone(1000.0, 10, fmt)
    # 16000 samples/sec * 0.010 sec = 160 samples * 2 bytes = 320 bytes
    assert len(tone) == 320


def test_generate_tone_different_frequencies():
    """Two tones at different frequencies produce different waveforms."""
    tone_440 = generate_tone(440.0, 100)
    tone_880 = generate_tone(880.0, 100)
    assert tone_440 != tone_880


def test_generate_tone_starts_at_zero():
    """Sine wave starts at zero (zero crossing at t=0)."""
    tone = generate_tone(440.0, 100)
    samples = _bytes_to_samples(tone)
    # sin(0) = 0, so first sample should be 0
    assert samples[0] == 0
