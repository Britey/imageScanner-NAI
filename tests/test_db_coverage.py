"""Storage contracts: metadata bounds, WAL durability and replacement semantics."""

from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from imgdupe import db


class StorageBehaviorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "index.sqlite"
        self.conn = db.connect(self.database)
        self.addCleanup(self.conn.close)
        db.init_db(self.conn)

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
            for payload in (b"12345", b"123456"):
                with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "too_large"):
                    db.decode_prompt_metadata(zlib.compress(payload), "zlib+utf8")

    def test_model_options_ignore_malformed_legacy_display_tags(self):
        image_id = self.image()
        db.replace_prompt(self.conn, image_id, prompt="cat", metadata_source="test",
                          prompt_error=None, indexed_at="now", tags=[
                              ("SD", "@model:sd", "model_type"),
                              ("@model:Custom", "@model:custom", "model_config"),
                          ])
        # A legacy normalized key must not turn an ordinary display tag into a
        # model label. Valid categories remain available in the same database.
        self.assertEqual(db.fetch_model_options(self.conn), [("Custom", "Custom")])

    def test_checkpoint_preserves_committed_data_and_leaves_active_transaction_intact(self):
        image_id = self.image()
        self.conn.commit()
        for mode in ("passive", "FULL", "RESTART", "TRUNCATE"):
            with self.subTest(mode=mode):
                result = db.checkpoint_database(self.conn, mode)
                self.assertIsNotNone(result)
                self.assertEqual(result[0], 0)
                self.assertEqual(result[1], result[2])
                reader = db.connect(self.database)
                try:
                    self.assertEqual(db.get_existing_image(reader, Path("test.png"))["id"], image_id)
                finally:
                    reader.close()
        with self.assertRaisesRegex(ValueError, "invalid WAL"):
            db.checkpoint_database(self.conn, "DELETE")

        self.conn.execute("UPDATE images SET size_bytes=9 WHERE id=?", (image_id,))
        # SQLite rejects a checkpoint while this connection has a write transaction.
        self.assertIsNone(db.checkpoint_database(self.conn))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(db.get_existing_image(self.conn, Path("test.png"))["size_bytes"], 1)

    def test_crop_state_validation(self):
        with self.assertRaisesRegex(ValueError, "invalid crop"):
            db.set_crop_index_state(self.conn, "invalid")
        db.set_crop_index_state(self.conn, "ready")
        self.assertTrue(db.is_crop_index_ready(self.conn))
        db.set_metadata(self.conn, db.CROP_INDEX_VERSION_KEY, "outdated")
        self.assertFalse(db.is_crop_index_ready(self.conn))

    def test_image_record_and_path_updates_keep_identity(self):
        image_id = self.image()
        db.update_image_record(self.conn, image_id, path=Path("new.png"), size_bytes=9,
                               mtime_ns=10, created_ns=11, modified_ns=12, indexed_at="later",
                               width=64, height=80, image_format="PNG", sha256=b"sha")
        row = db.get_existing_image(self.conn, Path("new.png"))
        expected = {"id": image_id, "path": "new.png", "size_bytes": 9, "mtime_ns": 10,
                    "created_ns": 11, "modified_ns": 12, "indexed_at": "later", "width": 64,
                    "height": 80, "format": "PNG", "sha256": b"sha", "decode_error": None}
        self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertIsNone(db.get_existing_image(self.conn, Path("test.png")))
        db.update_image_path(self.conn, image_id, path=Path("moved.png"), size_bytes=13,
                             mtime_ns=14, created_ns=15, modified_ns=16, indexed_at="moved")
        row = db.get_existing_image(self.conn, Path("moved.png"))
        expected.update(path="moved.png", size_bytes=13, mtime_ns=14, created_ns=15,
                        modified_ns=16, indexed_at="moved")
        self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertIsNone(db.get_existing_image(self.conn, Path("new.png")))

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
