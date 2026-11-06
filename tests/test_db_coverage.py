"""Storage contract tests, including bounded metadata and failed SQLite calls."""

from __future__ import annotations

import sqlite3
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from imgdupe import db


class StorageCoverageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def image(self):
        return db.upsert_image(self.conn, path=Path("test.png"), size_bytes=1,
                               mtime_ns=2, indexed_at="now")

    def test_raw_metadata_codecs_and_size_limits(self):
        self.assertIsNone(db.decode_prompt_metadata(None, None))
        for raw in (b"cat", b"a" * 100):
            encoded, codec = db.encode_prompt_metadata(raw)
            self.assertEqual(db.decode_prompt_metadata(encoded, codec), raw.decode())
        with self.assertRaisesRegex(ValueError, "unsupported"):
            db.decode_prompt_metadata(b"cat", "unknown")
        with patch.object(db, "MAX_PROMPT_METADATA_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "too_large"):
                db.decode_prompt_metadata(b"12345", "utf8")
            with self.assertRaisesRegex(ValueError, "too_large"):
                db.decode_prompt_metadata(zlib.compress(b"12345"), "zlib+utf8")
            # The independent post-flush guard also rejects a decompressor
            # whose buffered output crosses the ceiling during flush.
            decompressor = Mock(unconsumed_tail=b"")
            decompressor.decompress.return_value = b"1234"
            decompressor.flush.return_value = b"5"
            with patch.object(db.zlib, "decompressobj", return_value=decompressor):
                with self.assertRaisesRegex(ValueError, "too_large"):
                    db.decode_prompt_metadata(b"compressed", "zlib+utf8")

    def test_checkpoint_validates_mode_and_handles_sqlite_failures(self):
        for mode in ("passive", "FULL", "RESTART", "TRUNCATE"):
            result = db.checkpoint_database(self.conn, mode)
            self.assertEqual(len(result), 3)
            self.assertTrue(all(isinstance(value, int) for value in result))
        with self.assertRaisesRegex(ValueError, "invalid WAL"):
            db.checkpoint_database(self.conn, "DELETE")
        failed = Mock()
        failed.execute.side_effect = sqlite3.OperationalError("locked")
        self.assertIsNone(db.checkpoint_database(failed))
        failed.execute.side_effect = None
        failed.execute.return_value.fetchone.return_value = None
        self.assertIsNone(db.checkpoint_database(failed))

    def test_crop_state_validation(self):
        with self.assertRaisesRegex(ValueError, "invalid crop"):
            db.set_crop_index_state(self.conn, "invalid")
        db.set_crop_index_state(self.conn, "ready")
        self.assertTrue(db.is_crop_index_ready(self.conn))
        db.set_metadata(self.conn, db.CROP_INDEX_VERSION_KEY, "outdated")
        self.assertFalse(db.is_crop_index_ready(self.conn))

    def test_upsert_reports_missing_post_insert_row(self):
        with patch.object(db, "get_existing_image", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Failed to upsert"):
                self.image()

    def test_image_record_and_path_updates_keep_identity(self):
        image_id = self.image()
        db.update_image_record(self.conn, image_id, path=Path("new.png"), size_bytes=9,
                               mtime_ns=10, created_ns=11, modified_ns=12, indexed_at="later",
                               width=64, height=80, image_format="PNG", sha256=b"sha")
        row = db.get_existing_image(self.conn, Path("new.png"))
        self.assertEqual((row["id"], row["width"], row["sha256"]), (image_id, 64, b"sha"))
        db.update_image_path(self.conn, image_id, path=Path("moved.png"), size_bytes=13,
                             mtime_ns=14, indexed_at="moved")
        row = db.get_existing_image(self.conn, Path("moved.png"))
        self.assertEqual((row["id"], row["width"], row["size_bytes"]), (image_id, 64, 13))
        self.assertEqual(db.delete_images(self.conn, []), 0)

    def test_hash_replacement_and_clearing(self):
        image_id = self.image()
        hashes = {"phash256": b"hash", "grid0": b"grid", "crop:center_50": b"crop"}
        db.replace_hashes(self.conn, image_id, hashes, clear_existing=False)
        self.assertEqual(db.hash_row_to_dict(db.fetch_hash_row(self.conn, image_id)),
                         {"phash256": b"hash", "grid0": b"grid"})
        db.replace_hashes(self.conn, image_id, {"dhash256": b"new"})
        self.assertEqual(db.hash_row_to_dict(db.fetch_hash_row(self.conn, image_id)), {"dhash256": b"new"})
        db.replace_crop_hashes(self.conn, image_id, hashes, clear_existing=False)
        self.assertEqual(db.fetch_crop_hashes(self.conn, image_id), {"crop:center_50": b"crop"})
        db.replace_crop_hashes(self.conn, image_id, {"crop:center_50": b"updated"})
        self.assertEqual(db.fetch_crop_hashes(self.conn, image_id), {"crop:center_50": b"updated"})
        db.replace_crop_hashes(self.conn, image_id, {})
        self.assertEqual(db.fetch_crop_hashes(self.conn, image_id), {})
        db.clear_hashes(self.conn, image_id)
        self.assertIsNone(db.fetch_hash_row(self.conn, image_id))

    def test_prompt_status_and_empty_replacement(self):
        image_id = self.image()
        self.assertFalse(db.has_prompt_status(self.conn, image_id))
        db.replace_prompt(self.conn, image_id, prompt="cat", metadata_source="test",
                          prompt_error=None, indexed_at="now", tags=[("cat", "cat")])
        self.assertTrue(db.has_prompt_status(self.conn, image_id))
        db.replace_prompt(self.conn, image_id, prompt=None, metadata_source=None,
                          prompt_error="no_prompt", indexed_at="later", tags=[])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prompt_tags").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT prompt_error FROM image_prompts").fetchone()[0], "no_prompt")
