from __future__ import annotations

import sqlite3
import unittest

from imgdupe.db import init_db
from imgdupe.query import _prompt_tag_groups, query_prompt_tags


class PromptQueryGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def add_image(
        self,
        image_id: int,
        *tags: tuple[str, str],
        prompt: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at)
            VALUES (?, ?, 1, ?, 'now')
            """,
            (image_id, f"image-{image_id}.png", image_id),
        )
        self.conn.execute(
            """
            INSERT INTO image_prompts (image_id, prompt, prompt_indexed_at)
            VALUES (?, ?, 'now')
            """,
            (image_id, prompt or f"prompt {image_id}"),
        )
        self.conn.executemany(
            """
            INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (image_id, tag, tag.casefold(), position, tag_kind)
                for position, (tag, tag_kind) in enumerate(tags)
            ],
        )

    def result_ids(self, text: str, **kwargs) -> list[int]:
        return [
            int(result.image_row["id"])
            for result in query_prompt_tags(self.conn, text, sort_by="name_asc", **kwargs)
        ]

    def test_or_alternatives_are_grouped_and_comma_groups_are_anded(self) -> None:
        self.add_image(1, ("a", "prompt"), ("c", "prompt"))
        self.add_image(2, ("b", "prompt"), ("c", "prompt"))
        self.add_image(3, ("a", "prompt"))
        self.add_image(4, ("b", "prompt"), ("d", "prompt"))

        self.assertEqual(self.result_ids("a|b,c"), [1, 2])
        self.assertEqual(self.result_ids("(a｜b)，c"), [1, 2])
        self.assertEqual(self.result_ids("a|b,c|d"), [1, 2, 4])

    def test_match_mode_any_counts_satisfied_groups_not_alternatives(self) -> None:
        self.add_image(1, ("a", "prompt"), ("b", "prompt"), ("c", "prompt"))
        self.add_image(2, ("a", "prompt"))

        results = query_prompt_tags(self.conn, "a|b,c", match_mode="any", sort_by="name_asc")

        self.assertEqual([result.matched_count for result in results], [2, 1])
        self.assertEqual(set(results[0].matched_tags), {"a", "b", "c"})

    def test_groups_and_alternatives_are_deduplicated(self) -> None:
        self.assertEqual(
            _prompt_tag_groups("a|a|b,b|a,c,c"),
            [(('a', False), ('b', False)), (('c', False),)],
        )

    def test_negative_terms_still_exclude_independently(self) -> None:
        self.add_image(1, ("cat", "prompt"), ("bad", "prompt"))
        self.add_image(2, ("cat", "prompt"), ("worse", "prompt"))
        self.add_image(3, ("cat", "prompt"))

        self.assertEqual(
            self.result_ids("cat", negative_text_or_tags="bad|worse"),
            [3],
        )

    def test_model_search_accepts_any_non_prompt_metadata_kind(self) -> None:
        self.add_image(1, ("@model:abc", "model_hash"))
        self.add_image(2, ("@model:abc", "model_identifier"))
        self.add_image(3, ("@model:abc", "prompt"))

        self.assertEqual(self.result_ids("@model:abc"), [1, 2])

    def test_configured_category_hides_legacy_detected_category_tag(self) -> None:
        self.add_image(
            1,
            ("cat", "prompt"),
            ("@model:sd", "model_type"),
            ("@model:noob", "model_config"),
            ("@model:abc123", "model_hash"),
        )
        self.conn.execute(
            """
            UPDATE image_prompts
            SET model_type = 'sd', configured_model = 'noob'
            WHERE image_id = 1
            """
        )

        self.assertEqual(self.result_ids("@model:noob"), [1])
        self.assertEqual(self.result_ids("@model:sd"), [])
        self.assertEqual(self.result_ids("@model:abc123"), [1])
        self.assertEqual(self.result_ids("cat", model_type="noob"), [1])
        self.assertEqual(self.result_ids("cat", model_type="sd"), [])


if __name__ == "__main__":
    unittest.main()
