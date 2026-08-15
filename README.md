# Local AI Stack

Verify local inference runtimes, models, and readiness.

## Quick start

```bash
python -m pip install -e .
local-ai-stack record.json
```

The CLI emits deterministic fail-closed JSON plus a SHA-256 evidence identifier. Required fields: `runtime`, `model`, `status`. Rule: runtime status must be ready.

## Verify

```bash
python -m unittest discover -s tests -v
python scripts/check.py
```

Apache-2.0. Python 3.11+. Zero runtime dependencies.

