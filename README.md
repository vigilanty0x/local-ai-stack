# Local AI Stack

`observe` collects a bounded CPU/RAM/GPU/Ollama snapshot. `benchmark` explicitly
runs one to three small generations of a fixed synthetic prompt through the
existing model and inference lock. Missing measurements stay unknown; client
elapsed time and server-reported metrics remain distinct. GPU collection is
optional and requires a trusted existing executable selected by the operator.
See [Local observations and benchmark](docs/LOCAL-OBSERVATIONS.md). These
operations do not prove Docker health, port ownership or model quality.

`infer --fallback policy.json` optionally tries up to four explicitly ordered
local models under the same inference lock and total deadline. Each model must
appear in the observed inventory; only a complete terminal HTTP rejection can
permit the next candidate. Timeouts, broken connections and model mismatches
stop the chain. See [Local fallback](docs/LOCAL-FALLBACK.md). No circuit state,
model download or remote provider has been added.

## Integrated local runtime workflow (working-tree candidate)

The `doctor` and `infer` commands interrogate an existing Ollama runtime. They
do not install software or download models. The historical `record.json`
evaluator below remains compatible and validates supplied observations only.

```bash
local-ai-stack doctor --model qwen2.5:3b
local-ai-stack infer --model qwen2.5:3b --prompt "Reply with exactly OK." --max-tokens 8 --timeout 45 --lock /path/to/shared/ollama.lock
```

The same workflow checks the runtime version, obtains its inventory, requires
the exact installed model tag, acquires the configured OS inference lock, and
validates a complete generation from that model. `doctor` returns `available`
with `inference_readiness=not_measured`; only a finished generation returns
`completed`. Output includes actual elapsed time, token count and content
digests. A refusal returns no fabricated answer. Exit 0 means the requested
operation succeeded; exit 2 means blocked, timed out or invalid.

`CC_OLLAMA_BASE` or `--base` selects a local endpoint on port 11434. Only
loopback and the explicitly named Docker service `ollama` are accepted. The
transport pins an allowed resolved IP, ignores proxies, follows no redirects,
bounds JSON/body size, and shares one deadline across all stages. DNS and HTTP
also have deadline watchdogs. Remote provider APIs are unsupported.

Every process sharing a model must use **the same lock file/inode** through
`OLLAMA_INFERENCE_LOCK` or `--lock`; a Windows path and an unrelated Linux
volume are not interchangeable. Without an explicit path the private lock is
under `LOCAL_AI_STATE_ROOT` (default `.local-ai`). Private locks are created
with restrictive POSIX mode and existing permissions are never repaired.
The optional pre-provisioned shared-lock mode preserves stricter ownership
checks. See [runtime contract](docs/RUNTIME-WORKFLOW.md).

This working-tree addition is not a published version or an installed service.

Local AI Stack is a dependency-free, deterministic fail-closed readiness evaluator for local inference runtimes and models. It turns a bounded JSON observation into explicit `passed`, `failed`, or `blocked` evidence with a SHA-256 identifier instead of inferring that a local model is ready from process liveness alone.

**0.2.0 is PREPARED, not published.** `release-policy.v1.json` keeps publication disabled. See [Migration to 0.2](MIGRATION-0.2.md) and [Release contract](docs/RELEASE.md).

## Quick start

```bash
python -m pip install .
local-ai-stack --version
local-ai-stack record.json
```

Required fields are `runtime`, `model`, and `status`. The current root contract passes only when `status` is exactly `ready` and the runtime/model names are non-empty strings.

Example positive evidence:

```json
{"runtime":"ollama","model":"small","status":"ready"}
```

Counter-proof:

```json
{"runtime":"ollama","model":"small","status":"degraded"}
```

A missing required field is `blocked`, not success. The CLI exits `0` only for `passed`; failed/blocked evidence exits `2`.

## Consolidated local-AI utilities

The rehearsal preserves the histories and source trees of:

- `ollama-fleet-manager` → `packages/ollama-fleet-manager`
- `local-model-benchmark` → `packages/local-model-benchmark`
- `model-fallback-proxy` → `packages/model-fallback-proxy`

Consolidation is not archive authorization. Those source repositories remain subject to consumer, compatibility/redirect, rollback, and explicit human archive gates.

## Release-quality evidence

Flagship CI runs Ubuntu, Windows and macOS on CPython 3.11, 3.12 and 3.13. Every matrix job:

- installs an exact pinned build toolchain;
- builds wheel + source distribution;
- installs and tests the built wheel;
- verifies version/metadata and the positive/failed/blocked counter-proof contract;
- smokes the installed CLI outside the checkout;
- safely extracts and tests the complete sdist;
- verifies publication remains disabled;
- generates SHA-256 checksums, CycloneDX 1.6 SBOM, and `RELEASE_EVIDENCE.json` whose state remains `PREPARED` with release booleans false.

After the nine jobs, a guarded owner/same-repository job signs the canonical Ubuntu/Python 3.11 wheel with GitHub/Sigstore SLSA provenance and then independently verifies it with `gh attestation verify` constrained by repository, signer workflow, source ref, source digest and GitHub-hosted runner policy.

Attestation is evidence, not publication permission. Normal 0.2 CI contains no tag, GitHub Release or package-publish step.

## Verify locally

```bash
python -m unittest discover -s tests -v
python scripts/check.py
python scripts/check_release_policy.py
python -m compileall -q src tests scripts
```

## Rollback

0.2 changes release engineering and package verification, not persistent state. Rollback returns to the verified 0.1.0 artifact/commit and preserves the same product/distribution/namespace/CLI identity.

Apache-2.0. Python 3.11+; CI-tested through 3.13. Zero runtime dependencies.
