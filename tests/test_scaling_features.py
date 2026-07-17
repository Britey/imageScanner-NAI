from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from imgdupe.db import (
    crop_index_is_complete,
    get_crop_index_state,
    init_db,
    is_crop_index_ready,
    set_crop_index_state,
)
from imgdupe.query import query_image, query_prompt_tags
from imgdupe.web import (
    PromptSearchOptions,
    WebState,
    _index_page,
    _prompt_results_page,
    _prune_thumbnail_cache,
)
from imgdupe.settings import load_settings
from imgdupe.single_instance import SingleInstanceLock


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


class CropQueryGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    @patch("imgdupe.query.compute_image_hashes", return_value=({"phash256": b"hash", "sha256": b"sha"}, {}))
    @patch("imgdupe.query.find_candidates", return_value=[])
    @patch("imgdupe.query.fetch_crop_hashes")
    def test_disabled_crop_index_is_never_queried(self, fetch_crop_hashes, _find, _compute) -> None:
        query_image(self.conn, Path("query.png"), tryhard=False)
        fetch_crop_hashes.assert_not_called()


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

    def test_clear_cache_removes_cached_thumbnails(self) -> None:
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
                disable_advanced_options=False,
            )
            (state.thumbs_dir / "1.jpg").write_bytes(b"thumb")
            (state.thumbs_dir / "2.jpg").write_bytes(b"thumb")

            self.assertEqual(state.clear_thumbnail_cache(), 2)
            self.assertEqual(list(state.thumbs_dir.glob("*.jpg")), [])
            conn.close()


class SettingsTests(unittest.TestCase):
    def test_settings_allow_unbounded_user_selected_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            path.write_text(
                "[web]\nthumbnail_cache_limit = 12345\ndisable_advanced_options = true\n",
                encoding="utf-8",
            )

            settings = load_settings(path)

            self.assertEqual(settings.thumbnail_cache_limit, 12345)
            self.assertTrue(settings.disable_advanced_options)

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


class SingleInstanceTests(unittest.TestCase):
    def test_second_lock_is_refused_until_first_releases(self) -> None:
        name = f"imgdupe-test-{uuid.uuid4()}"
        first = SingleInstanceLock(name)
        second = SingleInstanceLock(name)
        third = SingleInstanceLock(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()

    @patch("imgdupe.cli.notify_already_running")
    @patch("imgdupe.cli.SingleInstanceLock")
    @patch("imgdupe.cli.connect")
    def test_second_web_launch_exits_before_opening_database(
        self,
        connect,
        lock_type,
        notify,
    ) -> None:
        from imgdupe.cli import main

        lock_type.return_value.acquire.return_value = False

        exit_code = main(["serve", "--db", "unused.sqlite"])

        self.assertEqual(exit_code, 1)
        notify.assert_called_once_with()
        connect.assert_not_called()


class PromptPaginationTests(unittest.TestCase):
    def test_pager_preserves_search_state(self) -> None:
        options = PromptSearchOptions(
            tags="blue eyes",
            negative_tags="text",
            match_mode="all",
            sort_by="created_desc",
            limit=100,
            offset=100,
            lang="en",
            unique_prompts=True,
        )

        page = _prompt_results_page([], options, has_next=True)

        self.assertIn("Previous", page)
        self.assertIn("Next", page)
        self.assertIn('name="offset" value="0"', page)
        self.assertIn('name="offset" value="200"', page)
        self.assertIn('name="unique_prompts" value="1"', page)
        self.assertIn('name="negative_tags" value="text"', page)

    def test_adjacent_query_pages_do_not_overlap(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for image_id in range(1, 4):
            conn.execute(
                """
                INSERT INTO images (
                    id, path, size_bytes, mtime_ns, width, height, indexed_at
                ) VALUES (?, ?, 1, ?, 256, 256, 'now')
                """,
                (image_id, f"image-{image_id}.png", image_id),
            )
            conn.execute(
                """
                INSERT INTO image_prompts (
                    image_id, prompt, metadata_source, prompt_indexed_at
                ) VALUES (?, ?, 'test', 'now')
                """,
                (image_id, f"prompt {image_id}"),
            )
            conn.execute(
                "INSERT INTO prompt_tags (image_id, tag, tag_norm, position) VALUES (?, 'tag', 'tag', 0)",
                (image_id,),
            )

        first_page = query_prompt_tags(conn, "tag", limit=2, offset=0, sort_by="name_asc")
        second_page = query_prompt_tags(conn, "tag", limit=2, offset=2, sort_by="name_asc")
        conn.close()

        first_ids = {int(result.image_row["id"]) for result in first_page}
        second_ids = {int(result.image_row["id"]) for result in second_page}
        self.assertEqual(len(first_page), 2)
        self.assertEqual(len(second_page), 1)
        self.assertFalse(first_ids & second_ids)


if __name__ == "__main__":
    unittest.main()
