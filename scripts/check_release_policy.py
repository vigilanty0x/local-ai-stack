"""Fail closed when Local AI Stack state or release authority drifts."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
MERGE_SHA = "8d2aed4b57576e54744efb22d8335891c0d3b0f8"


class ReleasePolicyError(ValueError):
    """Prepared release policy and repository surfaces disagree."""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleasePolicyError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleasePolicyError(f"{path.name} root must be an object")
    return value


def validate_release_policy(root: Path = ROOT) -> dict:
    root = Path(root)
    try:
        policy = _load_json(root / "release-policy.v1.json")
        rehearsal = _load_json(root / ".portfolio-rehearsal.json")
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        migration = (root / "MIGRATION-0.2.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleasePolicyError("cannot read release policy inputs") from exc

    expected = {
        "schema_version": "1.0",
        "repository": "vigilanty0x/local-ai-stack",
        "product": "Local AI Stack",
        "distribution": "local-ai-stack",
        "version": "0.2.0",
        "proposed_tag": "v0.2.0",
        "state": "PREPARED",
        "publish_enabled": False,
        "release_authorized": False,
        "consumer_mutation_authorized": False,
        "archive_authorized": False,
        "rollback_version": "0.1.0",
    }
    for field, value in expected.items():
        if policy.get(field) != value:
            raise ReleasePolicyError(f"{field} must equal {value!r}")

    if project.get("name") != policy["distribution"] or project.get("version") != policy["version"]:
        raise ReleasePolicyError("pyproject identity/version does not match release policy")
    classifiers = project.get("classifiers")
    if not isinstance(classifiers, list) or "Programming Language :: Python :: 3.14" not in classifiers:
        raise ReleasePolicyError("pyproject classifiers must include Python 3.14")

    required = policy.get("requires")
    if not isinstance(required, list) or len(required) != len(set(required)) or len(required) < 9:
        raise ReleasePolicyError("release policy requires must be a unique complete gate list")
    for gate in (
        "multi_os_runtime_ci",
        "wheel_and_sdist",
        "installed_artifact_smoke",
        "positive_and_counterproof",
        "cyclonedx_sbom",
        "verified_slsa_provenance",
        "consumer_compatibility",
        "explicit_publication_decision",
        "post_publication_verification",
    ):
        if gate not in required:
            raise ReleasePolicyError(f"release policy is missing gate {gate}")

    if rehearsal.get("target") != "local-ai-stack":
        raise ReleasePolicyError("portfolio rehearsal target must be local-ai-stack")
    if rehearsal.get("state") != "MERGED":
        raise ReleasePolicyError("merged consolidation must not remain REHEARSAL_ONLY")
    if rehearsal.get("mergeCommitSha") != MERGE_SHA:
        raise ReleasePolicyError("portfolio mergeCommitSha does not match the reviewed merge")
    if rehearsal.get("archiveGate") != "BLOCKED":
        raise ReleasePolicyError("source archive gate must remain BLOCKED")
    sources = rehearsal.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        raise ReleasePolicyError("portfolio rehearsal must retain all three imported sources")
    for source in sources:
        if not isinstance(source, dict) or source.get("ancestor") is not True or source.get("treeMatch") is not True:
            raise ReleasePolicyError("every imported source must retain ancestor/treeMatch proof")

    if 'python-version: ["3.11", "3.12", "3.13", "3.14"]' not in workflow:
        raise ReleasePolicyError("CI must explicitly test Python 3.11 through 3.14")
    forbidden = (
        "publish-release:",
        "gh release create",
        "contents: write",
        "git tag",
        "twine upload",
        "pypa/gh-action-pypi-publish",
    )
    for marker in forbidden:
        if marker in workflow:
            raise ReleasePolicyError(
                f"publication is disabled but CI contains publication authority {marker!r}"
            )
    if "Rollback" not in migration or "0.1.0" not in migration:
        raise ReleasePolicyError("migration guide must contain explicit 0.1.0 Rollback")
    return policy


def main() -> int:
    try:
        policy = validate_release_policy()
    except ReleasePolicyError as exc:
        raise SystemExit(f"release policy gate: {exc}") from exc
    print(
        f"release policy verified: version={policy['version']} state={policy['state']} "
        f"publish_enabled={str(policy['publish_enabled']).lower()} "
        f"release_authorized={str(policy['release_authorized']).lower()} "
        f"archive_authorized={str(policy['archive_authorized']).lower()} rollback={policy['rollback_version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
