from __future__ import annotations

import sqlite3
import unittest

from imgdupe.db import delete_images, init_db


class DeleteImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_large_delete_is_batched(self) -> None:
        image_ids = list(range(1, 16_385))

        deleted = delete_images(self.conn, image_ids)

        self.assertEqual(deleted, len(image_ids))

    def test_deleting_member_invalidates_entire_cluster(self) -> None:
        self._insert_image(1, "first.png")
        self._insert_image(2, "second.png")
        self.conn.execute("INSERT INTO clusters VALUES (1, 1, 1)")
        self.conn.execute("INSERT INTO clusters VALUES (1, 2, 0)")
        self.conn.execute(
            """
            INSERT INTO matches (
                image_id_a, image_id_b, grid_match_count, score, decision, created_at
            )
            VALUES (1, 2, 0, 80, 'probable_duplicate', 'now')
            """
        )

        deleted = delete_images(self.conn, [1])

        self.assertEqual(deleted, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)

    def test_duplicate_ids_are_counted_once(self) -> None:
        self._insert_image(1, "first.png")

        deleted = delete_images(self.conn, [1, 1])

        self.assertEqual(deleted, 1)

    def _insert_image(self, image_id: int, path: str) -> None:
        self.conn.execute(
            """
            INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at)
            VALUES (?, ?, 1, 1, 'now')
            """,
            (image_id, path),
        )


if __name__ == "__main__":
    unittest.main()
