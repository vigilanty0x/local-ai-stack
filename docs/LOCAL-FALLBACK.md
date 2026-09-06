# Optional local model fallback

The existing `infer` workflow can try an explicit ordered list of local model
tags. It uses the same native Ollama transport, generation validation and OS
inference lock. It does not create a queue, install/pull a model, request a
remote provider, or claim a persistent circuit breaker.

Without a fallback policy, `runtime.run` keeps the v1 receipt and its existing
exact-model refusal. `doctor` does not accept a fallback policy: an inventory
check alone cannot demonstrate that a fallback inference succeeds.
Both v1 and v2 now reject an incomplete declared HTTP body, including a complete
JSON fragment with a larger `Content-Length`. Previously v1 could falsely accept
that response. This intentional refusal preserves valid v1 behavior.

## Input

```json
{
  "schema_version": "local-ai-stack/fallback-v1",
  "models": ["primary:exact-tag", "alternate:exact-tag"]
}
```

These are placeholders; use only tags you explicitly authorize. No example
asserts that either placeholder is installed. The list contains 1–4 unique
exact printable tags, at most 200 ASCII characters each. Order is significant;
the first tag must equal `--model`. Unknown fields/version, duplicates, booleans,
whitespace tags and ambiguous JSON are refused before contacting the runtime.
The JSON loader is bounded to 8192 bytes and rejects duplicate keys/non-finite
constants. Python callers pass an object; the runtime validates and owns a
copy rather than retaining mutable caller lists.

```bash
local-ai-stack infer --model primary:exact-tag --prompt "Reply with OK." --fallback policy.json --timeout 45 --max-tokens 8 --lock /existing/shared/ollama.lock
```

The lock path must identify the same physical lock used by other consumers.
The command does not provision or repair a shared system lock.

```python
from local_ai_stack.runtime import run
from local_ai_stack.fallback import load_policy

result = run(
    base="http://localhost:11434",
    model="primary:exact-tag",
    prompt="Reply with OK.",
    timeout=45.0,
    max_tokens=8,
    lock_path=existing_shared_lock,
    fallback=load_policy(policy_path),
)
```

All normal endpoint/prompt/token/timeout validations still apply. The native
`transport=` seam remains available for isolated tests/integration; injecting
one delegates transport observations to that caller and is not a production
network attestation.

## Observed execution

One total deadline starts before the single version and inventory requests.
Candidate models must be present as exact names in that observed inventory.
An absent model is recorded `not_installed` and never receives a POST. No alias
is substituted. Model disappearance after inventory may cause a later explicit
HTTP rejection; the workflow does not refresh the inventory or download it.

If any candidate is installed, the existing native inference lock is acquired
once and held across the whole chain. Its existing maximum 30-second lock-wait
bound remains; waiting also consumes the total runtime deadline. Each eligible
candidate receives at most one `/api/generate` POST. All requests use the same
base origin, same total deadline, same prompt, and original deterministic
generation options. No sleep, backoff or fresh per-candidate budget is added.

A next candidate is allowed only after HTTP 404, 429, 500 or 503 with a complete,
bounded, valid JSON object containing a nonempty `error` string. This observes
a terminal error response, not the error's factual explanation. Error bodies
are never returned or journaled. Invalid/ambiguous framing, truncated body,
malformed JSON, redirect, authentication error, incomplete response, different
returned model, broken connection and timeout all stop the chain.

A disconnected HTTP client does not prove that the server stopped computing.
The workflow never follows that uncertainty with another generation. It does
not control server internals or claim an independently verified cancellation.
The native lock eventually releases on return/error; other applications still
need to obey their own shared cancellation/coordination policy.

Remaining time is checked before each attempt, after transport, after generation
validation and after lock release. An exhausted budget yields `timeout`, no
answer and no additional POST. Scheduling/OS latency is measured rather than
claimed to be a hard real-time guarantee. `max_tokens` is a cap on each request;
unknown token consumption of failed requests is not reported as zero or billed
usage. There is no provider quota accounting in this local workflow.

## Receipt v2

Only explicit fallback runs use `local-ai-stack/runtime-v2`. It binds the
normalized policy and its SHA plus all observed results in `evidence_sha256`.

| Field | Meaning |
| --- | --- |
| `model_requested` | First explicitly requested tag |
| `requested_model_installed` | Whether that first tag appeared in inventory |
| `model_selected` | Last candidate selected for an actual generation request |
| `model_installed` | Whether a generation candidate was selected from inventory |
| `model_observed` | Exact model returned by a validated complete generation, if any |
| `attempts` | All candidates in declared order, including absent and not-attempted entries |
| `generation_attempted` | Whether a POST invocation was made for that candidate; not proof the server accepted it |
| `status` per attempt | `not_installed`, `not_attempted`, `terminal_rejection`, `blocked`, `timeout`, or `completed` |
| `http_status` / error fields | Safe classification of failed attempt, with no response body |
| `elapsed_s` | Observed attempt time; zero for unattempted/absent candidates |
| `response` | Final validated answer only when the full workflow completes |
| `persistent_circuit` | Explicitly `not_implemented` |

A successful later attempt preserves the earlier errors. Top-level
`inference_completed` is false after any workflow-level failure, even if an
attempt had returned valid output before a lock-release failure. Its attempt
record remains to preserve that observation; the global answer is withheld.
The original v1 meaning of `model_observed` is unchanged for runs without policy.

Exit 0 means this requested inference completed; exit 2 means refused,
blocked or timed out. No text, healthy fleet or model quality is fabricated.
Model/version labels and token counts are observations supplied by the local
server, not authentication of model weights. The unsigned digest checks
integrity, not authorship. Prompt content is sent only for the requested local
inference; the receipt stores its digest, not a prompt journal. Successful
answer text is returned to the caller as before.

## Historical sources and scope

The retained `model_fallback_proxy.route` sorts and filters caller declarations.
Tests call that real function for ordered availability cases; neutral fixture
quota/context/capability values do not turn those quantities into measurements.
The runtime validates the ordered policy and observes actual inventory/errors
instead of accepting a declared `healthy` label as successful inference.

The retained fleet evaluator requires all declared nodes ready. A successful
fallback to one model does not satisfy that whole-fleet claim. The retained
local benchmark only evaluates supplied throughput/latency scalars; no new model
benchmark score is inferred. `circuit-breaker-lab` is a memory-only simulator,
with no shared durable registry. Its persistent/interprocess circuit criterion
is a separate open item, deliberately outside this approved tranche.

Tests exercise real HTTP fixture servers and the native Windows/POSIX lock,
including a separate interpreter holding that same lock. Fixture connections
remain on registered private loopback ports; real user runtimes and model
accounts are not part of these automated proofs. Operational inference must be
verified separately against the explicitly authorized local runtime.
