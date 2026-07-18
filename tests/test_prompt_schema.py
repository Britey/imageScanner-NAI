from __future__ import annotations

import sqlite3
import unittest
import zlib

from imgdupe.db import (
    MAX_PROMPT_METADATA_BYTES,
    PROMPT_SCHEMA_VERSION,
    PROMPT_SCHEMA_VERSION_KEY,
    decode_prompt_metadata,
    encode_prompt_metadata,
    get_metadata,
    init_db,
    replace_prompt,
)


OLD_PROMPT_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE images (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    format TEXT,
    sha256 BLOB,
    indexed_at TEXT NOT NULL,
    missing_at TEXT,
    decode_error TEXT
);
CREATE TABLE image_prompts (
    image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    prompt TEXT,
    metadata_source TEXT,
    prompt_indexed_at TEXT NOT NULL,
    prompt_error TEXT
);
CREATE TABLE prompt_tags (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    tag_norm TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (image_id, tag_norm)
);
CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


class PromptSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()

    def add_image(self, image_id: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at) "
            "VALUES (?, ?, 1, 1, 'now')",
            (image_id, f"image-{image_id}.png"),
        )

    def test_old_database_migrates_additively_and_idempotently(self) -> None:
        self.conn.executescript(OLD_PROMPT_SCHEMA)
        self.add_image()
        self.conn.execute(
            "INSERT INTO image_prompts "
            "(image_id, prompt, metadata_source, prompt_indexed_at) "
            "VALUES (1, 'blue_hair', 'text', 'then')"
        )
        self.conn.execute(
            "INSERT INTO prompt_tags (image_id, tag, tag_norm, position) "
            "VALUES (1, 'blue_hair', 'blue hair', 0)"
        )

        init_db(self.conn)
        init_db(self.conn)

        self.assertTrue(
            {
                "model_type",
                "model_hash",
                "model_identity",
                "model_version",
                "configured_model",
                "model_config_root",
                "model_config_rule_id",
                "prompt_format",
                "raw_metadata",
                "raw_metadata_codec",
                "parser_version",
                "classification_root",
                "model_marker_path",
                "prompt_status",
                "classification_status",
            }.issubset(column_names(self.conn, "image_prompts"))
        )
        self.assertIn("tag_kind", column_names(self.conn, "prompt_tags"))
        row = self.conn.execute("SELECT * FROM prompt_tags").fetchone()
        self.assertEqual(row["tag_kind"], "prompt")
        self.assertEqual(row["tag"], "blue_hair")
        self.assertEqual(
            get_metadata(self.conn, PROMPT_SCHEMA_VERSION_KEY), PROMPT_SCHEMA_VERSION
        )
        indexes = {
            str(row[1]) for row in self.conn.execute("PRAGMA index_list(prompt_tags)")
        }
        self.assertIn("idx_prompt_tags_norm_image", indexes)
        self.assertIn("idx_prompt_tags_kind_norm_image", indexes)

    def test_replace_prompt_accepts_old_and_typed_tags(self) -> None:
        init_db(self.conn)
        self.add_image()
        raw, codec = encode_prompt_metadata('{"prompt":"blue hair"}')

        replace_prompt(
            self.conn,
            1,
            prompt="blue hair",
            metadata_source="png_comment",
            prompt_error=None,
            indexed_at="now",
            tags=[("blue hair", "blue hair"), ("@model:abc123", "@model:abc123", "model_hash")],
            model_type="nai4",
            model_hash="abc123",
            model_identity="NovelAI Diffusion V4.5 ABC123",
            model_version="4.5",
            configured_model="nai-custom",
            model_config_root="C:/images",
            model_config_rule_id="rule-id",
            prompt_format="novelai",
            raw_metadata=raw,
            raw_metadata_codec=codec,
            parser_version=2,
            classification_root="C:/images",
            model_marker_path="C:/images/.model_type",
            prompt_status="ok",
            classification_status="marker",
        )

        prompt = self.conn.execute("SELECT * FROM image_prompts").fetchone()
        self.assertEqual(prompt["model_type"], "nai4")
        self.assertEqual(prompt["model_hash"], "abc123")
        self.assertEqual(prompt["model_identity"], "NovelAI Diffusion V4.5 ABC123")
        self.assertEqual(prompt["configured_model"], "nai-custom")
        self.assertEqual(prompt["model_version"], "4.5")
        self.assertEqual(prompt["parser_version"], 2)
        self.assertEqual(
            decode_prompt_metadata(prompt["raw_metadata"], prompt["raw_metadata_codec"]),
            '{"prompt":"blue hair"}',
        )
        tags = self.conn.execute(
            "SELECT tag, tag_kind FROM prompt_tags ORDER BY position"
        ).fetchall()
        self.assertEqual(
            [(row["tag"], row["tag_kind"]) for row in tags],
            [("blue hair", "prompt"), ("@model:abc123", "model_hash")],
        )

    def test_reopening_database_does_not_overwrite_configured_category(self) -> None:
        init_db(self.conn)
        self.add_image()
        replace_prompt(
            self.conn,
            1,
            prompt="cat",
            metadata_source="test",
            prompt_error=None,
            indexed_at="now",
            tags=[("@model:sd", "@model:sd", "model_config")],
            model_type="sd",
            model_hash="sd",
            configured_model="sd",
        )
        self.conn.commit()

        init_db(self.conn)

        row = self.conn.execute(
            "SELECT tag_kind FROM prompt_tags WHERE image_id = 1 AND tag_norm = '@model:sd'"
        ).fetchone()
        self.assertEqual(row["tag_kind"], "model_config")

    def test_raw_metadata_codec_is_bounded(self) -> None:
        raw, codec = encode_prompt_metadata("same prompt " * 100)
        self.assertEqual(codec, "zlib+utf8")
        self.assertEqual(decode_prompt_metadata(raw, codec), "same prompt " * 100)

        with self.assertRaisesRegex(ValueError, "prompt_metadata_too_large"):
            encode_prompt_metadata(b"x" * (MAX_PROMPT_METADATA_BYTES + 1))
        oversized = zlib.compress(b"x" * (MAX_PROMPT_METADATA_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "prompt_metadata_too_large"):
            decode_prompt_metadata(oversized, "zlib+utf8")


if __name__ == "__main__":
    unittest.main()
