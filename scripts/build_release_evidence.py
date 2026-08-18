"""Generate bounded release evidence for a Local AI Stack candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import tomllib
from typing import Any


MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
EXPECTED_DISTRIBUTION = "local-ai-stack"


class ReleaseEvidenceError(ValueError):
    """Candidate artifacts or metadata are missing, ambiguous, or unsafe."""


def _sha(path: Path) -> tuple[int, str]:
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ReleaseEvidenceError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {path.name}")
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return size, digest.hexdigest()


def _metadata(root: Path) -> tuple[str, str]:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseEvidenceError("cannot read project metadata") from exc
    name = project.get("name")
    version = project.get("version")
    if name != EXPECTED_DISTRIBUTION or not isinstance(version, str) or not version:
        raise ReleaseEvidenceError("unexpected distribution identity or version")
    return name, version


def build_release_evidence(root: Path, dist: Path, output: Path) -> dict[str, Any]:
    root = root.resolve()
    dist = dist.resolve()
    output.mkdir(parents=True, exist_ok=True)
    distribution, version = _metadata(root)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseEvidenceError(
            f"expected exactly one wheel and one sdist; found wheels={len(wheels)} sdists={len(sdists)}"
        )
    rows = []
    for path in (wheels[0], sdists[0]):
        size, digest = _sha(path)
        rows.append({"name": path.name, "size": size, "sha256": digest})
    rows.sort(key=lambda item: item["name"])
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{row['sha256']}  {row['name']}\n" for row in rows), encoding="utf-8"
    )
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Local AI Stack",
                "version": version,
                "purl": f"pkg:pypi/{distribution}@{version}",
                "properties": [
                    {"name": "local-ai-stack:runtime-dependencies", "value": "0"},
                ],
            }
        },
        "components": [],
        "properties": [
            {"name": f"local-ai-stack:artifact:{row['name']}:sha256", "value": row["sha256"]}
            for row in rows
        ],
    }
    (output / "local-ai-stack.cdx.json").write_text(
        json.dumps(bom, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    receipt = {
        "schema_version": "1.0",
        "product": "Local AI Stack",
        "repository": "vigilanty0x/local-ai-stack",
        "distribution": distribution,
        "version": version,
        "state": "PREPARED",
        "source_sha": os.environ.get("GITHUB_SHA") or "not-recorded",
        "source_ref": os.environ.get("GITHUB_REF") or "not-recorded",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "artifacts": rows,
        "checksums": "SHA256SUMS.txt",
        "sbom": "local-ai-stack.cdx.json",
        "tests_verified": os.environ.get("LOCAL_AI_STACK_TESTS_VERIFIED") == "1",
        "counterproof_verified": os.environ.get("LOCAL_AI_STACK_COUNTERPROOF_VERIFIED") == "1",
        "signed": False,
        "attested": False,
        "tagged": False,
        "published": False,
        "released": False,
    }
    (output / "RELEASE_EVIDENCE.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, default=Path("release-evidence"))
    args = parser.parse_args(argv)
    try:
        receipt = build_release_evidence(args.root, args.dist, args.output)
    except (OSError, ReleaseEvidenceError) as exc:
        raise SystemExit(f"release evidence: {exc}") from exc
    print(
        f"release evidence prepared: version={receipt['version']} artifacts={len(receipt['artifacts'])} "
        f"state={receipt['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
