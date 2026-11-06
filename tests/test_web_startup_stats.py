from __future__ import annotations

import sqlite3
import unittest

from imgdupe.db import init_db
from imgdupe.web import _stats


LEGACY_UNIQUE_TAGS = """
SELECT COUNT(DISTINCT pt.tag_norm)
FROM prompt_tags pt
JOIN image_prompts ip ON ip.image_id = pt.image_id
WHERE NOT (pt.tag_kind = 'model_type' AND ip.configured_model IS NOT NULL)
"""


class WebStartupStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def add_image(self, image_id: int, configured_model=None, *, with_prompt=True) -> None:
        self.conn.execute(
            "INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at) "
            "VALUES (?, ?, 1, 1, 'now')", (image_id, f"{image_id}.png"),
        )
        if with_prompt:
            self.conn.execute(
                "INSERT INTO image_prompts (image_id, prompt_indexed_at, configured_model) "
                "VALUES (?, 'now', ?)", (image_id, configured_model),
            )

    def add_tag(self, image_id: int, tag: str, kind="prompt") -> None:
        self.conn.execute(
            "INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind) "
            "VALUES (?, ?, ?, 0, ?)", (image_id, tag, tag, kind),
        )

    def test_empty_database_has_no_unique_tags(self) -> None:
        self.assertEqual(_stats(self.conn)["unique_tags"], 0)

    def test_unique_tag_visibility_is_decided_across_all_occurrences(self) -> None:
        self.add_image(1, "custom")
        self.add_image(2)
        self.add_image(3, "")  # Empty labels are non-NULL, matching existing semantics.
        self.add_image(4, with_prompt=False)
        for image_id in (1, 2):
            self.add_tag(image_id, "cat")
            self.add_tag(image_id, "@model:sd", "model_type")
        # The first occurrence is suppressed, but another typed occurrence counts.
        self.add_tag(1, "@model:custom", "model_type")
        self.add_tag(2, "@model:custom", "model_config")
        self.add_tag(1, "@model:hash", "model_hash")
        self.add_tag(1, "@model:ordinary", "model_type")
        self.add_tag(2, "@model:ordinary")
        self.add_tag(1, "@model:hidden", "model_type")
        self.add_tag(3, "@model:empty", "model_type")
        self.add_tag(4, "orphan")
        # NULL prompt text still contributes tags; a prompt row must exist.
        self.add_tag(2, "null prompt")

        stats = _stats(self.conn)

        self.assertEqual(stats["unique_tags"], 6)
        self.assertEqual(stats["indexed"], 4)
        self.assertEqual(stats["prompts"], 0)
        self.assertEqual(stats["unique_tags"], self.conn.execute(LEGACY_UNIQUE_TAGS).fetchone()[0])

    def test_repeated_tags_avoid_per_occurrence_prompt_lookups(self) -> None:
        for image_id in range(1, 129):
            self.add_image(image_id)
            for tag_id in range(32):
                self.add_tag(image_id, f"tag-{tag_id}")

        def measure(operation):
            ticks = 0

            def progress():
                nonlocal ticks
                ticks += 1
                return 0

            self.conn.set_progress_handler(progress, 100)
            try:
                result = operation()
            finally:
                self.conn.set_progress_handler(None, 0)
            return result, ticks

        legacy_count, legacy_ticks = measure(
            lambda: self.conn.execute(LEGACY_UNIQUE_TAGS).fetchone()[0]
        )
        stats, optimized_ticks = measure(lambda: _stats(self.conn))

        self.assertEqual(stats["unique_tags"], legacy_count)
        self.assertEqual(legacy_count, 32)
        # Compare VM work, not wall time: include all other home-page counters
        # and still require a substantial reduction against the old tag count.
        self.assertLess(optimized_ticks, legacy_ticks // 2)


if __name__ == "__main__":
    unittest.main()
