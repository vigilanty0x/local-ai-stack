from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingManifestTests(unittest.TestCase):
    def test_sdist_keeps_governance_and_release_inputs(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for required in (
            "include .portfolio-rehearsal.json",
            "include .github/workflows/ci.yml",
            "include MIGRATION-0.2.md",
            "include release-policy.v1.json",
            "include requirements-build.txt",
            "recursive-include scripts *.py",
            "recursive-include tests *.py",
        ):
            self.assertIn(required, manifest)


if __name__ == "__main__":
    unittest.main()
