from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_release_policy import ReleasePolicyError, validate_release_policy


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_fixture(root: Path, *, publish_enabled: bool = False, workflow_extra: str = "") -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "release-policy.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "repository": "vigilanty0x/local-ai-stack",
                "product": "Local AI Stack",
                "distribution": "local-ai-stack",
                "version": "0.2.0",
                "proposed_tag": "v0.2.0",
                "state": "PREPARED",
                "publish_enabled": publish_enabled,
                "rollback_version": "0.1.0",
                "requires": [
                    "multi_os_runtime_ci",
                    "wheel_and_sdist",
                    "installed_artifact_smoke",
                    "positive_and_counterproof",
                    "sha256_checksums",
                    "cyclonedx_sbom",
                    "verified_slsa_provenance",
                    "consumer_compatibility",
                    "explicit_publication_decision",
                    "post_publication_verification",
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "local-ai-stack"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    (root / "MIGRATION-0.2.md").write_text(
        "# Migration vers Local AI Stack 0.2.0\n\n## Rollback\nReturn to 0.1.0.\n",
        encoding="utf-8",
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\npermissions:\n  contents: read\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        + workflow_extra,
        encoding="utf-8",
    )


class ReleasePolicyTests(unittest.TestCase):
    def test_repository_candidate_is_prepared_and_disabled(self) -> None:
        policy = validate_release_policy(REPO_ROOT)
        self.assertEqual(policy["version"], "0.2.0")
        self.assertEqual(policy["state"], "PREPARED")
        self.assertFalse(policy["publish_enabled"])
        self.assertEqual(policy["rollback_version"], "0.1.0")

    def test_valid_disabled_fixture_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture(root)
            self.assertFalse(validate_release_policy(root)["publish_enabled"])

    def test_release_command_is_rejected_while_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture(root, workflow_extra="      - run: gh release create v0.2.0\n")
            with self.assertRaisesRegex(ReleasePolicyError, "publication authority"):
                validate_release_policy(root)

    def test_write_permission_is_rejected_while_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture(root, workflow_extra="    permissions:\n      contents: write\n")
            with self.assertRaisesRegex(ReleasePolicyError, "publication authority"):
                validate_release_policy(root)

    def test_publish_enabled_cannot_be_silently_flipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture(root, publish_enabled=True)
            with self.assertRaisesRegex(ReleasePolicyError, "publish_enabled"):
                validate_release_policy(root)


if __name__ == "__main__":
    unittest.main()
