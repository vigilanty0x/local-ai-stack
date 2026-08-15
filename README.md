# Local Model Benchmark

Record reproducible local model latency and throughput.

## Quick start

```bash
python -m pip install -e .
local-model-benchmark record.json
```

The CLI emits deterministic fail-closed JSON plus a SHA-256 evidence identifier. Required fields: `model`, `tokens_per_second`, `latency_ms`. Rule: throughput must be positive and latency bounded.

## Verify

```bash
python -m unittest discover -s tests -v
python scripts/check.py
```

Apache-2.0. Python 3.11+. Zero runtime dependencies.

