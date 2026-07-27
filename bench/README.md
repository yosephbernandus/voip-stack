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
