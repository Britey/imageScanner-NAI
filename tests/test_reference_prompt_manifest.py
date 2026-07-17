from __future__ import annotations

import unittest
from pathlib import Path

from scripts.compare_reference_prompt_tags import compare_manifest, load_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "tests" / "fixtures" / "reference_prompt_tags.json"
REFERENCE_ROOT = PROJECT_ROOT / "参考图示例"


class ReferencePromptManifestTests(unittest.TestCase):
    def test_manifest_is_complete_and_well_formed(self) -> None:
        entries = load_manifest(MANIFEST)["images"]
        self.assertEqual(len(entries), 30)
        self.assertEqual(len({entry["path"] for entry in entries}), 30)
        self.assertTrue(all(len(entry["sha256"]) == 64 for entry in entries))
        self.assertTrue(all(entry["prompt_format"] in {"a1111", "novelai"} for entry in entries))
        self.assertTrue(all(entry["tags"] for entry in entries))
        self.assertEqual(sum(len(entry["tags"]) for entry in entries), 751)

    @unittest.skipUnless(REFERENCE_ROOT.is_dir(), "local ignored reference corpus is unavailable")
    def test_extractor_matches_every_reviewed_reference(self) -> None:
        mismatches = compare_manifest(MANIFEST, REFERENCE_ROOT)
        self.assertEqual(mismatches, [], "\n" + "\n".join(mismatches))


if __name__ == "__main__":
    unittest.main()
