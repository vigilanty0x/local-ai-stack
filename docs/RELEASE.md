# Local AI Stack release contract

Local AI Stack treats candidate quality, provenance, publication, and post-publication verification as separate states.

## States

- `PREPARED`: source SHA has verified tests, counter-proofs, installable artifacts, checksums and SBOM.
- `ATTESTED`: the approved wheel has verified GitHub/Sigstore SLSA provenance.
- `TAGGED`: an explicit tag was created after approval.
- `RELEASED`: immutable expected assets were published under that tag.
- `VERIFIED`: a separate read-back verified tag target, hashes, provenance, installability and smoke behavior.
- `BLOCKED`: required evidence is missing or red.
- `ROLLED_BACK`: consumers were returned to the last-known-good 0.1.0 path.

Normal 0.2 PR CI can establish PREPARED and ATTESTED evidence. Because `release-policy.v1.json` has `publish_enabled=false`, it cannot establish TAGGED, RELEASED or VERIFIED.

## Pre-publication proof

The candidate must pass the complete 3 OS × 3 Python matrix, wheel and sdist verification, installed CLI smoke, positive/negative/blocked evidence semantics, static boundary checks, release policy checks, SHA-256 generation, CycloneDX 1.6 SBOM generation, and strict signed provenance verification.

## Publication

Publication requires a separate reviewed change that enables publication for one exact version and source SHA, retains rollback, defines immutable release assets, and adds post-publication read-back verification. CI success alone is not publication authority.

## Rollback

0.2 changes packaging and release evidence but introduces no database, remote-state or secret migration. Rollback returns consumers to verified 0.1.0 behavior and artifacts.

## Archive gate

The imported source repositories remain unarchived. Consolidation does not grant archive authority; archive requires consumer inventory, compatibility or redirects, rollback proof, and explicit human approval.
