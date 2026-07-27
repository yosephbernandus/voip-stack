"""Benchmark the pure-Python PCM mixer.

This is a microbenchmark for ``voip.audio.mix_streams`` using 20 ms frames of
8 kHz, signed 16-bit mono PCM. It does not include codec work, network I/O,
encryption, jitter buffering, or the browser media path.
"""

from __future__ import annotations

import argparse
import platform
import random
import statistics
import struct
import sys
import time

from voip.audio import mix_streams
from voip.types.audio import AudioFormat


def generate_test_streams(
    num_streams: int,
    frame_samples: int,
    seed: int,
) -> list[bytes]:
    """Generate deterministic signed 16-bit PCM frames."""
    random.seed(seed)
    streams: list[bytes] = []

    for _ in range(num_streams):
        samples = [random.randint(-16_000, 16_000) for _ in range(frame_samples)]
        streams.append(struct.pack(f"<{frame_samples}h", *samples))

    return streams


def benchmark_mix_streams(
    num_streams: int,
    frame_samples: int,
    num_frames: int,
    iterations: int,
    seed: int,
) -> list[float]:
    """Return one frames-per-second measurement per iteration."""
    audio_format = AudioFormat()
    streams = generate_test_streams(num_streams, frame_samples, seed)
    runs: list[float] = []

    for _ in range(iterations):
        started_at = time.perf_counter()
        for _ in range(num_frames):
            mix_streams(streams, audio_format)
        elapsed = time.perf_counter() - started_at
        runs.append(num_frames / elapsed)

    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=10_000)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--streams", type=int, nargs="+", default=[2, 4, 8, 16])
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame_samples = 160

    print("PCM mixer microbenchmark")
    print(f"Python       : {sys.version.split()[0]}")
    print(f"Platform     : {platform.platform()}")
    print(f"Frame        : {frame_samples} samples (20 ms at 8 kHz)")
    print(f"Frames/run   : {args.frames:,}")
    print(f"Iterations   : {args.iterations}")
    print()

    for num_streams in args.streams:
        runs = benchmark_mix_streams(
            num_streams=num_streams,
            frame_samples=frame_samples,
            num_frames=args.frames,
            iterations=args.iterations,
            seed=args.seed,
        )
        median = statistics.median(runs)
        print(
            f"{num_streams:>2} streams: "
            f"{median:>10,.0f} frames/second "
            f"({median / 50:>6.0f}x one real-time 20 ms frame rate)"
        )

    print()
    print("Standalone mixer throughput only. This is not a capacity claim.")


if __name__ == "__main__":
    main()
