# Model Fallback Proxy

## Purpose

Select the first eligible model from bounded caller-declared health, quota, context, and capability metadata.

## Non-goals

It is not a network proxy, health checker, quota authority, model client, or retry executor.

## Install

Requires Python 3.11 or newer.

```console
python -m pip install .
```

## CLI and API

Run the built-in positive and negative control:

```console
model-fallback probe
```

Process JSON from a file:

```console
model-fallback route --input examples/basic.json
```

The public Python seam is `model_fallback_proxy.route`:

```python
from model_fallback_proxy import route
```

Functions return structured JSON-compatible results and reject malformed input without raising validation exceptions.

## Example

A runnable input is provided at `examples/basic.json`. CLI output is deterministic and includes either a SHA-256 evidence field or an explicit validation failure.

## Security and trust model

Requests and model entries are untrusted and strictly structured. Names and capabilities are bounded and unique, and numeric fields are non-boolean integers without coercion. The tool performs no network calls.

## Limitations

At most 100 models and 100 capabilities per list are evaluated; health and quota claims are not externally verified.

## Tests

Run the same local gates used by CI:

```console
python -m unittest discover -s tests -v
python scripts/check.py
python -m build --no-isolation
model-fallback probe
model-fallback route --input examples/basic.json
```

CI tests Python 3.11 and 3.12, installs the project and rebuilt wheel, imports the installed package, and exercises both the probe and example.

## AI disclosure

AI assistance supported defensive implementation, adversarial test design, and documentation. See [AI_ASSISTANCE.md](AI_ASSISTANCE.md) for scope and review expectations.

## License

Apache-2.0. See [LICENSE](LICENSE).

