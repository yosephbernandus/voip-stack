# voip-rtp-native

Optional PyO3 and Maturin experiment for parsing the RTP subset implemented by
`voip.rtp.parse_rtp`.

The pure-Python parser remains the readable reference implementation. This
package exists to measure the cost of moving one packet-heavy function across
the Python/Rust boundary. It does not replace the server, process live WebRTC
media, or claim lower end-to-end call latency.

From the repository root:

```bash
pip install -e ".[dev,native]"
cd native/rtp_parser
maturin develop --release
cd ../..
pytest tests/test_rtp_native.py
python -m bench.bench_rtp_native
```

The native parser supports the fixed RTP header and CSRC list. Like the Python
parser, it rejects padding and header extensions rather than returning an
incorrect payload boundary.
