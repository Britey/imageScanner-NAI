from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from imgdupe.config import ScanConfig
from imgdupe.db import connect, init_db, upsert_image
from imgdupe.model_paths import ConfigIssue, ModelPathConfig, make_rule
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

    def test_configured_model_parser_tags_and_query_filter_integrate(self) -> None:
        model_paths = ModelPathConfig((make_rule(self.root, "noob"),))

        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=model_paths,
        )

        self.assertEqual(stats.indexed, 1)
        prompt = self.connection.execute("SELECT * FROM image_prompts").fetchone()
        self.assertEqual(prompt["model_type"], "sd")
        self.assertEqual(prompt["configured_model"], "noob")
        self.assertEqual(prompt["model_identity"], "其他模型 other_model")
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
                ("@model:noob", "model_config"),
                ("@model:其他模型 other_model", "model_identity"),
                ("@model:a1b2c3d4", "model_hash"),
            ],
        )
        self.assertEqual(len(query_prompt_tags(self.connection, "cat", model_type="noob")), 1)
        self.assertEqual(len(query_prompt_tags(self.connection, "cat", model_type="sd")), 0)
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:sd")), 0)
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:a1b2c3d4")), 1)
        query_result = query_prompt_tags(self.connection, "cat", model_type="noob")[0]
        self.assertEqual(query_result.image_row["model_hash"], "a1b2c3d4")
        self.assertIsNone(query_result.image_row["model_version"])

    def test_configured_label_change_reindexes_unchanged_image(self) -> None:
        rule = make_rule(self.root, "noob")
        scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=ModelPathConfig((rule,)),
        )

        changed_rule = make_rule(self.root, "sd-custom", rule.rule_id)
        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1),
            model_paths=ModelPathConfig((changed_rule,)),
        )

        self.assertEqual(stats.queued, 0)
        self.assertEqual(stats.skipped, 1)
        configured_model = self.connection.execute(
            "SELECT configured_model FROM image_prompts"
        ).fetchone()[0]
        self.assertEqual(configured_model, "sd-custom")
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:noob")), 0)
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:sd-custom")), 1)
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:sd")), 0)

    def test_category_refresh_removes_legacy_dual_tag_and_restores_fallback(self) -> None:
        rule = make_rule(self.root, "noob")
        configured = ModelPathConfig((rule,))
        scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=configured,
        )
        image_id = int(self.connection.execute("SELECT id FROM images").fetchone()[0])
        self.connection.execute(
            """
            INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind)
            VALUES (?, '@model:sd', '@model:sd', 1000000001, 'model_type')
            """,
            (image_id,),
        )
        self.connection.commit()

        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1),
            model_paths=configured,
        )

        self.assertEqual(stats.queued, 0)
        categories = self.connection.execute(
            """
            SELECT tag, tag_kind FROM prompt_tags
            WHERE tag_kind IN ('model_type', 'model_config')
            """
        ).fetchall()
        self.assertEqual(
            [(row["tag"], row["tag_kind"]) for row in categories],
            [("@model:noob", "model_config")],
        )

        scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1),
            model_paths=ModelPathConfig(),
        )
        categories = self.connection.execute(
            """
            SELECT tag, tag_kind FROM prompt_tags
            WHERE tag_kind IN ('model_type', 'model_config')
            """
        ).fetchall()
        self.assertEqual(
            [(row["tag"], row["tag_kind"]) for row in categories],
            [("@model:sd", "model_type")],
        )

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

    def test_missing_model_hash_is_recorded_without_stopping_other_images(self) -> None:
        missing_hash = self.root / "missing-hash.png"
        _write_prompt_png(
            missing_hash,
            "dog\nSteps: 20, Sampler: Euler a, Model: example-model",
        )
        stat = missing_hash.stat()
        upsert_image(
            self.connection,
            path=missing_hash,
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

        stats = scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=ModelPathConfig(),
        )

        self.assertEqual(stats.indexed, 2)
        row = self.connection.execute(
            """
            SELECT ip.prompt_error, ip.model_identity
            FROM image_prompts ip JOIN images i ON i.id = ip.image_id
            WHERE i.path = ?
            """,
            (str(missing_hash.resolve()),),
        ).fetchone()
        self.assertEqual(row["prompt_error"], "missing_model_hash")
        self.assertEqual(row["model_identity"], "其他模型 other_model")
        self.assertEqual(len(query_prompt_tags(self.connection, "dog")), 1)

    def test_no_metadata_uses_a_separate_hidden_identity(self) -> None:
        plain = self.root / "plain.png"
        plain.write_bytes(b"\x89PNG\r\n\x1a\n" + _png_text_chunk(b"IEND", b""))
        stat = plain.stat()
        upsert_image(
            self.connection,
            path=plain,
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

        scan_roots(
            self.connection,
            [plain],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=ModelPathConfig(),
        )

        row = self.connection.execute(
            """
            SELECT model_type, model_identity, prompt_error
            FROM image_prompts ip JOIN images i ON i.id = ip.image_id
            WHERE i.path = ?
            """,
            (str(plain.resolve()),),
        ).fetchone()
        self.assertEqual(row["model_type"], "no_metadata")
        self.assertEqual(row["model_identity"], "无元数据 no_metadata")
        self.assertEqual(row["prompt_error"], "no_metadata")
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:no_metadata")), 1)

    def test_invalid_path_config_fails_closed_without_erasing_existing_label(self) -> None:
        configured = ModelPathConfig((make_rule(self.root, "keep-me"),))
        scan_roots(
            self.connection,
            [self.root],
            config=ScanConfig(workers=1, reindex_prompts=True),
            model_paths=configured,
        )

        invalid = ModelPathConfig(issues=(ConfigIssue("rule.bad", "missing label"),))
        with self.assertRaisesRegex(ValueError, "invalid model_paths.ini"):
            scan_roots(
                self.connection,
                [self.root],
                config=ScanConfig(workers=1),
                model_paths=invalid,
            )

        row = self.connection.execute(
            "SELECT configured_model FROM image_prompts"
        ).fetchone()
        self.assertEqual(row["configured_model"], "keep-me")
        self.assertEqual(len(query_prompt_tags(self.connection, "@model:keep-me")), 1)


if __name__ == "__main__":
    unittest.main()
