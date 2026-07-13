from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from imgdupe.db import init_db
from imgdupe.scan import _claim_moved_image, _missing_hash_candidates


class MovedImageCandidateTests(unittest.TestCase):
    def test_candidates_are_indexed_by_size_then_hash(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing.png"
            conn.execute(
                """
                INSERT INTO images (
                    id, path, size_bytes, mtime_ns, sha256, indexed_at
                )
                VALUES (1, ?, 123, 1, ?, 'now')
                """,
                (str(missing_path), b"digest"),
            )

            candidates = _missing_hash_candidates(conn)

        conn.close()
        self.assertEqual(candidates, {123: {b"digest": [1]}})

    def test_claim_removes_the_unique_candidate(self) -> None:
        candidates = {123: {b"digest": [7]}}
        with patch("imgdupe.scan.sha256_file", return_value=b"digest"):
            image_id = _claim_moved_image(Path("moved.png"), 123, candidates)

        self.assertEqual(image_id, 7)
        self.assertEqual(candidates, {})

    def test_ambiguous_hash_is_not_claimed(self) -> None:
        candidates = {123: {b"digest": [7, 8]}}
        with patch("imgdupe.scan.sha256_file", return_value=b"digest"):
            image_id = _claim_moved_image(Path("moved.png"), 123, candidates)

        self.assertIsNone(image_id)
        self.assertEqual(candidates, {123: {b"digest": [7, 8]}})


if __name__ == "__main__":
    unittest.main()
