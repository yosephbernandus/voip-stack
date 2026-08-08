"""Compare pure Python and optional PyO3 RTP parsing."""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any

from bench.bench_rtp import generate_test_packets
from voip.rtp import parse_rtp as parse_rtp_python

try:
    from voip_rtp_native import parse_rtp as parse_rtp_native
    from voip_rtp_native import parse_rtp_model
except ImportError as error:
    raise SystemExit(
        "Native parser is not installed. Run `cd native/rtp_parser && "
        "maturin develop --release`."
    ) from error


Parser = Callable[[bytes], Any]


def benchmark(parser: Parser, packets: list[bytes], iterations: int) -> list[float]:
    """Return packets-per-second measurements for one parser."""
    for packet in packets[: min(1_000, len(packets))]:
        parser(packet)

    runs: list[float] = []
    for _ in range(iterations):
        started_at = time.perf_counter()
        for packet in packets:
            parser(packet)
        elapsed = time.perf_counter() - started_at
        runs.append(len(packets) / elapsed)
    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def print_result(label: str, runs: list[float], baseline: float) -> None:
    median = statistics.median(runs)
    microseconds = 1_000_000 / median
    print(
        f"{label:<27} {median:>12,.0f} packets/s  "
        f"{microseconds:>7.3f} us/packet  {median / baseline:>5.2f}x"
    )


def main() -> None:
    args = parse_args()
    packets = generate_test_packets(args.packets, args.seed)
    results = {
        "Python + Pydantic": benchmark(
            parse_rtp_python, packets, args.iterations
        ),
        "Rust + PyO3 fields": benchmark(
            parse_rtp_native, packets, args.iterations
        ),
        "Rust + Pydantic adapter": benchmark(
            parse_rtp_model, packets, args.iterations
        ),
    }
    baseline = statistics.median(results["Python + Pydantic"])

    print("Optional native RTP parser microbenchmark")
    print(f"Python       : {sys.version.split()[0]}")
    print(f"Platform     : {platform.platform()}")
    print(f"Packet count : {args.packets:,} per iteration")
    print(f"Packet size  : {len(packets[0])} bytes")
    print(f"Iterations   : {args.iterations}")
    print()
    for label, runs in results.items():
        print_result(label, runs, baseline)
    print()
    print("Parser-only throughput. Outputs differ where Pydantic is omitted.")
    print("Not call capacity and not an end-to-end latency measurement.")


if __name__ == "__main__":
    main()
