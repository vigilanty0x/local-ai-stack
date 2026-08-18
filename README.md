# Local AI Stack

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
