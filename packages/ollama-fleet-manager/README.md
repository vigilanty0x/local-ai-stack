# Ollama Fleet Manager

Track Ollama nodes, ready capacity, and model placement.

## Quick start

```bash
python -m pip install -e .
ollama-fleet-manager record.json
```

The CLI emits deterministic fail-closed JSON plus a SHA-256 evidence identifier. Required fields: `model`, `node_count`, `ready_nodes`. Rule: every registered node must be ready.

## Verify

```bash
python -m unittest discover -s tests -v
python scripts/check.py
```

Apache-2.0. Python 3.11+. Zero runtime dependencies.

