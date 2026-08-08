# Learning-module benchmarks

These microbenchmarks make the performance figures discussed in the talk
reproducible:

```bash
python -m bench.bench_rtp --packets 100000 --iterations 5
python -m bench.bench_audio --frames 10000 --iterations 5
```

They measure only the standalone pure-Python functions:

- `voip.rtp.parse_rtp`
- `voip.audio.mix_streams`

They do not measure the WebSocket server, browser WebRTC media, network I/O,
codecs, encryption, jitter buffering, or production call capacity. Results
will vary with the Python version, operating system, CPU, power mode, and
background workload.

## Verified result

The following result was measured from this repository on 27 July 2026 using
Python 3.14.5 on an Apple Silicon Mac:

| Workload | Median result |
| --- | ---: |
| Parse 100,000 RTP packets, repeated 5 times | 385,443 packets/second |
| Mix 10,000 two-stream PCM frames, repeated 5 times | 18,050 frames/second |
| Mix 10,000 four-stream PCM frames, repeated 5 times | 12,253 frames/second |
| Mix 10,000 eight-stream PCM frames, repeated 5 times | 7,774 frames/second |
| Mix 10,000 sixteen-stream PCM frames, repeated 5 times | 4,556 frames/second |

Treat these as one local baseline, not a promise about another machine and not
an estimate of concurrent calls.

## Optional native RTP comparison

After building the release extension, compare the Python reference parser,
the raw PyO3 result, and the native parser converted back to the same Pydantic
model:

```bash
cd native/rtp_parser
maturin develop --release
cd ../..
python -m bench.bench_rtp_native --packets 100000 --iterations 5
```

The following release-build result was measured on 8 August 2026 using Python
3.14.5, Rust 1.94.1, and Maturin 1.14.1 on the same Apple Silicon Mac:

| Parser path | Median result | Relative to Python |
| --- | ---: | ---: |
| Python parser + Pydantic model | 411,987 packets/second | 1.00x |
| Rust parser + lightweight PyO3 object | 10,891,515 packets/second | 26.44x |
| Rust parser + Pydantic adapter | 405,713 packets/second | 0.98x |

The raw native path returns a lighter object, so its result is not an equal-
work replacement for the Pydantic path. The adapter result is the relevant
comparison when callers still require the existing `RtpPacket` model. In this
run, model construction erased the parser-only advantage.

See [docs/native-rtp-parser.md](../docs/native-rtp-parser.md) for supported
fields and interpretation. The comparison remains parser-only. Faster parsing
does not remove packetization delay, network delay, TURN relay delay, jitter
buffering, codec work, encryption, or playback delay.
