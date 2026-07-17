from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from imgdupe.config import ScanConfig
from imgdupe.db import connect, init_db, upsert_image
from imgdupe.query import query_prompt_tags
from imgdupe.scan import scan_roots
from imgdupe.utils import utc_now_sql


def _png_text_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _write_prompt_png(path: Path, parameters: str) -> None:
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_text_chunk(b"tEXt", b"parameters\0" + parameters.encode("utf-8"))
    payload += _png_text_chunk(b"IEND", b"")
    path.write_bytes(payload)


class PromptScanIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "images"
        self.root.mkdir()
        self.image = self.root / "sample.png"
        _write_prompt_png(
            self.image,
            "cat, [cow|horse], <lora:test:0.5>\n"
            "Negative prompt: bad anatomy\n"
            "Steps: 20, Sampler: Euler a, Model hash: A1B2C3D4",
        )
        self.connection = connect(self.base / "images.sqlite")
        init_db(self.connection)
        stat = self.image.stat()
        upsert_image(
            self.connection,
            path=self.image,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            created_ns=None,
            modified_ns=stat.st_mtime_ns,
            indexed_at=utc_now_sql(),
            width=1,
            height=1,
            image_format="PNG",
            sha256=None,
            decode_error=None,
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_marker_parser_model_tag_and_query_filter_integrate(self) -> None:
        (self.root / ".model_type").write_text("noob\n", encoding="utf-8")

        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
        )

        self.assertEqual(stats.indexed, 1)
        prompt = self.connection.execute("SELECT * FROM image_prompts").fetchone()
        self.assertEqual(prompt["model_type"], "noob")
        self.assertEqual(prompt["model_hash"], "a1b2c3d4")
        self.assertIsNone(prompt["model_version"])
        self.assertEqual(prompt["prompt_format"], "a1111")
        tags = self.connection.execute(
            "SELECT tag, tag_kind FROM prompt_tags ORDER BY position"
        ).fetchall()
        self.assertEqual(
            [(row["tag"], row["tag_kind"]) for row in tags],
            [
                ("cat", "prompt"),
                ("cow", "prompt"),
                ("horse", "prompt"),
                ("@model:a1b2c3d4", "model_hash"),
            ],
        )
        self.assertEqual(len(query_prompt_tags(self.connection, "cat", model_type="noob")), 1)
        self.assertEqual(len(query_prompt_tags(self.connection, "cat", model_type="sd")), 0)
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:a1b2c3d4")), 1)
        query_result = query_prompt_tags(self.connection, "cat", model_type="noob")[0]
        self.assertEqual(query_result.image_row["model_hash"], "a1b2c3d4")
        self.assertIsNone(query_result.image_row["model_version"])

    def test_marker_change_reindexes_unchanged_image(self) -> None:
        marker = self.root / ".model_type"
        marker.write_text("noob", encoding="utf-8")
        scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
        )

        marker.write_text("sd", encoding="utf-8")
        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1),
        )

        self.assertEqual(stats.queued, 1)
        self.assertEqual(stats.indexed, 1)
        model_type = self.connection.execute(
            "SELECT model_type FROM image_prompts"
        ).fetchone()[0]
        self.assertEqual(model_type, "sd")

    def test_prompt_only_reindex_commits_each_configured_batch(self) -> None:
        second = self.root / "second.png"
        _write_prompt_png(second, "dog\nSteps: 20, Sampler: Euler a")
        stat = second.stat()
        upsert_image(
            self.connection,
            path=second,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            created_ns=None,
            modified_ns=stat.st_mtime_ns,
            indexed_at=utc_now_sql(),
            width=1,
            height=1,
            image_format="PNG",
            sha256=None,
            decode_error=None,
        )
        self.connection.commit()

        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        try:
            stats = scan_roots(
                self.connection,
                [self.root],
                config=ScanConfig(workers=1, reindex_prompts=True, batch_size=1),
            )
        finally:
            self.connection.set_trace_callback(None)

        self.assertEqual(stats.indexed, 2)
        self.assertGreaterEqual(
            sum(statement.strip().upper() == "COMMIT" for statement in statements),
            2,
        )


if __name__ == "__main__":
    unittest.main()
