from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from imgdupe.db import (
    crop_index_is_complete,
    get_crop_index_state,
    init_db,
    is_crop_index_ready,
    set_crop_index_state,
    set_metadata,
)
from imgdupe.web import (
    WebState,
    _index_page,
    _prompt_model_label,
    _prune_thumbnail_cache,
)


class CropIndexStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_crop_index_defaults_to_disabled(self) -> None:
        self.assertEqual(get_crop_index_state(self.conn), "disabled")
        self.assertFalse(is_crop_index_ready(self.conn))

    def test_ready_requires_current_version(self) -> None:
        set_crop_index_state(self.conn, "ready")
        self.assertTrue(is_crop_index_ready(self.conn))
        set_metadata(self.conn, "crop_index_version", "outdated")
        self.assertFalse(is_crop_index_ready(self.conn))
        set_crop_index_state(self.conn, "ready")
        self.assertTrue(is_crop_index_ready(self.conn))

    def test_completeness_ignores_images_too_small_for_center_crop(self) -> None:
        self._insert_hashed_image(1, 32, 32)
        self.assertTrue(crop_index_is_complete(self.conn))

    def test_completeness_requires_crop_for_eligible_images(self) -> None:
        self._insert_hashed_image(1, 256, 256)
        self.assertFalse(crop_index_is_complete(self.conn))
        self.conn.execute(
            "INSERT INTO crop_hashes (image_id, region, phash256) VALUES (1, 'center_50', ?)",
            (b"crop",),
        )
        self.assertTrue(crop_index_is_complete(self.conn))

    def _insert_hashed_image(self, image_id: int, width: int, height: int) -> None:
        self.conn.execute(
            """
            INSERT INTO images (id, path, size_bytes, mtime_ns, width, height, indexed_at)
            VALUES (?, ?, 1, 1, ?, ?, 'now')
            """,
            (image_id, f"{image_id}.png", width, height),
        )
        self.conn.execute("INSERT INTO hashes (image_id, phash256) VALUES (?, ?)", (image_id, b"hash"))


class ThumbnailCacheTests(unittest.TestCase):
    def test_cache_prunes_oldest_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            base_time = time.time() - 10
            for index in range(5):
                path = cache_dir / f"{index}.jpg"
                path.write_bytes(b"thumb")
                os.utime(path, (base_time + index, base_time + index))

            _prune_thumbnail_cache(cache_dir, 2)

            self.assertEqual(
                {path.name for path in cache_dir.glob("*.jpg")},
                {"3.jpg", "4.jpg"},
            )



class SettingsTests(unittest.TestCase):
    def test_advanced_sections_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            state = WebState(
                conn=conn,
                db_path=root / "images.sqlite",
                limit=100,
                min_score=55,
                thumbnail_size=256,
                work_dir=root / "work",
                thumbs_dir=root / "thumbs",
                thumbnail_cache_limit=1000,
                disable_advanced_options=True,
            )

            page = _index_page(state, "en")

            self.assertNotIn('class="advanced"', page)
            self.assertNotIn('name="tryhard"', page)
            self.assertNotIn('name="crop_index"', page)
            conn.close()

    def test_boolean_search_help_and_dynamic_models_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            conn.execute(
                """
                INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at)
                VALUES (1, 'one.png', 1, 1, 'now')
                """
            )
            conn.execute(
                """
                INSERT INTO image_prompts (
                    image_id, prompt_indexed_at, model_type, configured_model
                ) VALUES (1, 'now', 'sd', '自定义 model')
                """
            )
            conn.executemany(
                """
                INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind)
                VALUES (1, ?, ?, ?, ?)
                """,
                [
                    ("@model:sd", "@model:sd", 0, "model_type"),
                    ("@model:自定义 model", "@model:自定义 model", 1, "model_config"),
                ],
            )
            state = WebState(
                conn=conn,
                db_path=root / "images.sqlite",
                limit=100,
                min_score=55,
                thumbnail_size=256,
                work_dir=root / "work",
                thumbs_dir=root / "thumbs",
                thumbnail_cache_limit=1000,
                disable_advanced_options=False,
            )

            page = _index_page(state, "en")

            self.assertIn("Comma means AND; | means OR", page)
            self.assertIn('value="自定义 model"', page)
            self.assertNotIn('value="sd"', page)
            self.assertIn('name="match_mode" value="all"', page)
            self.assertIn("同一提示词仅显示一次", _index_page(state, "zh"))
            self.assertIn('class="configured-rescan-form"', page)
            conn.close()


class PromptPaginationTests(unittest.TestCase):
    def test_prompt_model_label_includes_hash_and_nai45_variant(self) -> None:
        self.assertEqual(
            _prompt_model_label(
                {
                    "model_type": "nai4",
                    "model_version": "4.5",
                    "model_hash": "4bde2a90",
                    "configured_model": None,
                }
            ),
            "NAI4 (4.5, 4bde2a90)",
        )
        self.assertEqual(
            _prompt_model_label(
                {
                    "model_type": "sd",
                    "model_version": None,
                    "model_hash": "516047db97",
                    "configured_model": None,
                }
            ),
            "SD (516047db97)",
        )
        self.assertEqual(
            _prompt_model_label(
                {
                    "model_type": "sd",
                    "model_version": None,
                    "model_hash": "caa8fa5606",
                    "configured_model": "noob",
                }
            ),
            "NOOB (caa8fa5606)",
        )





if __name__ == "__main__":
    unittest.main()
