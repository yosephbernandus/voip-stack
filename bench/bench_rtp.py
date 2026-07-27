"""Benchmark the pure-Python RTP parser.

This is a microbenchmark for ``voip.rtp.parse_rtp``. It does not measure the
WebSocket server, WebRTC media path, network I/O, codecs, encryption, or call
capacity.
"""

from __future__ import annotations

import argparse
import platform
import random
import statistics
import sys
import time

from voip.rtp import build_rtp, parse_rtp
from voip.types.rtp import RtpHeader, RtpPacket


def generate_test_packets(count: int, seed: int) -> list[bytes]:
    """Generate deterministic 20 ms G.711 RTP packets."""
    random.seed(seed)
    packets: list[bytes] = []

    for index in range(count):
        header = RtpHeader(
            version=2,
            padding=False,
            extension=False,
            csrc_count=0,
            marker=index % 50 == 0,
            payload_type=random.choice([0, 8]),
            sequence_number=index & 0xFFFF,
            timestamp=(index * 160) & 0xFFFFFFFF,
            ssrc=0xDEADBEEF,
        )
        payload = bytes(random.randint(0, 255) for _ in range(160))
        packets.append(build_rtp(RtpPacket(header=header, payload=payload)))

    return packets


def benchmark_parse_rtp(packets: list[bytes], iterations: int) -> list[float]:
    """Return one packets-per-second measurement per iteration."""
    runs: list[float] = []

    for _ in range(iterations):
        started_at = time.perf_counter()
        for packet in packets:
            parse_rtp(packet)
        elapsed = time.perf_counter() - started_at
        runs.append(len(packets) / elapsed)

    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    packets = generate_test_packets(args.packets, args.seed)
    runs = benchmark_parse_rtp(packets, args.iterations)

    print("RTP parser microbenchmark")
    print(f"Python       : {sys.version.split()[0]}")
    print(f"Platform     : {platform.platform()}")
    print(f"Packet count : {args.packets:,} per iteration")
    print(f"Packet size  : {len(packets[0])} bytes")
    print(f"Iterations   : {args.iterations}")
    print(f"Median       : {statistics.median(runs):,.0f} packets/second")
    print(f"Mean         : {statistics.mean(runs):,.0f} packets/second")
    if len(runs) > 1:
        print(f"Std deviation: {statistics.stdev(runs):,.0f}")
    print()
    print("Standalone parser throughput only. This is not a call-capacity claim.")


if __name__ == "__main__":
    main()
