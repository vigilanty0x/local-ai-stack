from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_release_evidence import ReleaseEvidenceError, build_release_evidence


class ReleaseEvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        (root / "pyproject.toml").write_text(
            '[project]\nname = "local-ai-stack"\nversion = "0.2.0"\n', encoding="utf-8"
        )
        dist = root / "dist"
        dist.mkdir()
        (dist / "local_ai_stack-0.2.0-py3-none-any.whl").write_bytes(b"wheel")
        (dist / "local_ai_stack-0.2.0.tar.gz").write_bytes(b"sdist")
        return dist, root / "release-evidence"

    def test_prepared_receipt_never_claims_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist, output = self._fixture(root)
            receipt = build_release_evidence(root, dist, output)
            self.assertEqual(receipt["version"], "0.2.0")
            self.assertEqual(receipt["state"], "PREPARED")
            for field in ("signed", "attested", "tagged", "published", "released"):
                self.assertFalse(receipt[field])
            self.assertEqual(len(receipt["artifacts"]), 2)
            sbom = json.loads((output / "local-ai-stack.cdx.json").read_text(encoding="utf-8"))
            self.assertEqual(sbom["bomFormat"], "CycloneDX")
            self.assertEqual(sbom["specVersion"], "1.6")

    def test_ambiguous_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist, output = self._fixture(root)
            (dist / "extra.whl").write_bytes(b"extra")
            with self.assertRaisesRegex(ReleaseEvidenceError, "exactly one wheel"):
                build_release_evidence(root, dist, output)

    def test_wrong_distribution_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist, output = self._fixture(root)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "wrong"\nversion = "0.2.0"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ReleaseEvidenceError, "unexpected distribution"):
                build_release_evidence(root, dist, output)


if __name__ == "__main__":
    unittest.main()
