"""Database cleanup removes requested records and invalidates derived data safely."""
from __future__ import annotations

import sqlite3
import unittest

from imgdupe import db


class DeleteImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def add_images(self, ids) -> None:
        self.conn.executemany(
            "INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at) VALUES (?, ?, 1, 1, 'now')",
            ((image_id, f"image-{image_id}.png") for image_id in ids),
        )

    def test_large_delete_removes_existing_rows_under_sqlite_variable_limit(self) -> None:
        image_ids = list(range(1, 1206))
        self.add_images([*image_ids, 2000])
        # Force batching even on SQLite builds with a very generous default limit.
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        for image_id in (1, 400, 401, 800, 1205, 2000):
            db.replace_hashes(self.conn, image_id, {"phash256": bytes([image_id % 256]) * 32})
            db.replace_crop_hashes(self.conn, image_id, {"crop:center_50": b"crop"})
            db.replace_prompt(self.conn, image_id, prompt="cat", metadata_source="test",
                              prompt_error=None, indexed_at="now", tags=[("cat", "cat")])

        self.assertEqual(db.delete_images(self.conn, image_ids), len(image_ids))

        for table, key in (("images", "id"), ("hashes", "image_id"), ("crop_hashes", "image_id"),
                           ("image_prompts", "image_id"), ("prompt_tags", "image_id")):
            with self.subTest(table=table):
                self.assertEqual([row[0] for row in self.conn.execute(f"SELECT {key} FROM {table}")], [2000])
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_deleting_member_invalidates_only_affected_cluster_and_matches(self) -> None:
        self.add_images(range(1, 5))
        self.conn.executemany("INSERT INTO clusters VALUES (?, ?, ?)",
                              [(1, 1, 1), (1, 2, 0), (2, 3, 1), (2, 4, 0)])
        self.conn.executemany(
            "INSERT INTO matches(image_id_a,image_id_b,grid_match_count,score,decision,created_at) "
            "VALUES (?, ?, 0, 80, 'probable_duplicate', 'now')", [(1, 2), (2, 3), (3, 4)])

        self.assertEqual(db.delete_images(self.conn, [2]), 1)

        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM clusters ORDER BY image_id")],
                         [(2, 3, 1), (2, 4, 0)])
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT image_id_a,image_id_b FROM matches")],
                         [(3, 4)])
        self.assertEqual([row[0] for row in self.conn.execute("SELECT id FROM images ORDER BY id")], [1, 3, 4])

    def test_duplicate_and_empty_requests_preserve_unrequested_images(self) -> None:
        self.add_images([1, 2])
        self.assertEqual(db.delete_images(self.conn, []), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 2)
        self.assertEqual(db.delete_images(self.conn, [1, 1]), 1)
        self.assertEqual([row[0] for row in self.conn.execute("SELECT id FROM images")], [2])


if __name__ == "__main__":
    unittest.main()
