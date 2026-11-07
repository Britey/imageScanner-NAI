"""Incremental scan lifecycle: indexing, failures, crop state and timestamps."""

from __future__ import annotations

import builtins
from contextlib import closing
import os
import json
import uuid
import runpy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe import db, scan
from imgdupe.config import ScanConfig
from imgdupe.failures import iter_failures
from imgdupe.model_paths import ModelPathConfig, make_rule
from imgdupe.query import query_prompt_tags


def write_image(path: Path, seed: int = 0, size: tuple[int, int] = (80, 80)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = size
    pixels = bytes((x * (seed + 3) + y * (seed + 7) + channel * 50 + seed * 31) % 256
                   for y in range(height) for x in range(width) for channel in range(3))
    Image.frombytes("RGB", size, pixels).save(path)
    return path


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "images"
        self.root.mkdir()
        self.conn = db.connect(self.base / "index.sqlite")
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def image(self, name, seed=0, size=(80, 80)):
        return write_image(self.root / name, seed, size)

    def scan(self, *roots, model_paths=None, **options):
        return scan.scan_roots(self.conn, list(roots) or [self.root],
                               config=ScanConfig(workers=1, **options),
                               model_paths=model_paths or ModelPathConfig())

    def row(self, path):
        return db.get_existing_image(self.conn, path)

    def count(self, table, where="1", parameters=()):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", parameters).fetchone()[0]

    def counts(self, stats):
        return {key: value for key, value in vars(stats).items() if value}


class ScanLifecycleTests(ScanTestCase):
    def test_incremental_scan_indexes_new_skips_unchanged_and_rehashes_modified(self):
        first = self.image("first.png", 1)
        second = self.image("nested/second.png", 2)
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 2, "queued": 2, "indexed": 2})
        self.assertEqual(self.count("hashes"), 2)
        for path in (first, second):
            stat = os.stat(path)
            row = self.row(path)
            self.assertEqual((row["width"], row["height"], row["format"]), (80, 80, "PNG"))
            self.assertEqual(row["sha256"], scan.sha256_file(path))
            self.assertEqual(row["modified_ns"], stat.st_mtime_ns)
            self.assertEqual(row["created_ns"], getattr(stat, "st_birthtime_ns", stat.st_ctime_ns))
        original_id = self.row(first)["id"]

        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")):
            self.assertEqual(self.counts(self.scan()), {"seen": 2, "skipped": 2})

        write_image(first, 9, (96, 80))
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 2, "queued": 1, "indexed": 1, "skipped": 1})
        row = self.row(first)
        self.assertEqual((row["id"], row["width"], row["sha256"]),
                         (original_id, 96, scan.sha256_file(first)))
        self.assertEqual(self.count("images"), 2)

    def test_platform_birthtime_is_preferred_for_creation_dates(self):
        path = self.image("image.png")
        real_stat = Path.stat

        class BirthtimeStat:
            def __init__(self, result):
                self._result = result
                self.st_birthtime_ns = 123_456_789

            def __getattr__(self, name):
                return getattr(self._result, name)

        with patch.object(Path, "stat", lambda item, *args, **kwargs: BirthtimeStat(real_stat(item, *args, **kwargs))):
            self.scan()
        self.assertEqual(self.row(path)["created_ns"], 123_456_789)

    def test_timestamp_backfill_keeps_hashes_and_prompt_metadata(self):
        path = self.image("image.png")
        self.scan()
        image_id = self.row(path)["id"]
        hashes = tuple(db.fetch_hash_row(self.conn, image_id))
        prompt = tuple(self.conn.execute("SELECT * FROM image_prompts").fetchone())
        stat = os.stat(path)
        for created, modified in ((None, None), (1, stat.st_mtime_ns), (stat.st_ctime_ns, 1)):
            self.conn.execute("UPDATE images SET created_ns=?, modified_ns=?", (created, modified))
            self.conn.commit()
            with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")), \
                    patch.object(scan, "extract_nai_prompt", side_effect=AssertionError("reparsed")):
                stats = self.scan()
            self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "indexed": 1})
            row = self.row(path)
            self.assertEqual((row["created_ns"], row["modified_ns"]), (stat.st_ctime_ns, stat.st_mtime_ns))
        self.assertEqual(tuple(db.fetch_hash_row(self.conn, image_id)), hashes)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM image_prompts").fetchone()), prompt)

    def test_corrupt_file_is_reported_and_not_decoded_again_until_it_changes(self):
        path = self.root / "broken.png"
        path.write_bytes(b"broken forever")
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "failed": 1})
        row = self.row(path)
        self.assertTrue(row["decode_error"].startswith("UnidentifiedImageError:"))
        self.assertEqual(row["sha256"], scan.sha256_file(path))
        [failure] = iter_failures(self.conn)
        self.assertEqual(failure["path"], str(path))
        self.assertTrue(failure["decode_error"].startswith(row["decode_error"]))

        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("decoded twice")):
            self.assertEqual(self.counts(self.scan(crop_index=True)), {"seen": 1, "skipped": 1})
            refreshed = self.scan(reindex_prompts=True)
        self.assertEqual(self.counts(refreshed), {"seen": 1, "queued": 1, "indexed": 1})
        self.assertEqual(self.row(path)["decode_error"], row["decode_error"])

        path.write_bytes(b"different broken contents")
        self.assertEqual(self.scan().failed, 1)
        self.assertEqual(self.count("images"), 1)

    def test_image_overwritten_with_corrupt_bytes_drops_stale_hashes(self):
        path = self.image("image.png")
        self.scan(crop_index=True)
        image_id = self.row(path)["id"]
        self.assertGreater(self.count("hash_bands", "image_id=?", (image_id,)), 0)
        self.assertGreater(self.count("crop_hashes", "image_id=?", (image_id,)), 0)
        path.write_bytes(b"truncated download")
        stats = self.scan(prompt_index=False)
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "failed": 1})
        row = self.row(path)
        self.assertEqual(row["id"], image_id)
        self.assertIsNotNone(row["decode_error"])
        self.assertIsNone(db.fetch_hash_row(self.conn, image_id))
        self.assertEqual(self.count("hash_bands", "image_id=?", (image_id,)), 0)
        self.assertEqual(self.count("crop_hashes", "image_id=?", (image_id,)), 0)
        self.assertEqual(db.fetch_stats(self.conn)["hashed"], 0)

    def test_too_small_failure_retries_only_when_new_minimums_allow_it(self):
        path = self.image("small.png", size=(40, 40))
        self.assertEqual(self.scan(min_width=64, min_height=64).failed, 1)
        self.assertEqual(self.row(path)["decode_error"], "ImageTooSmallError: image is too small: 40x40")
        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("still too small")):
            self.assertEqual(self.scan(min_width=32, min_height=64).skipped, 1)
        self.assertEqual(self.scan(min_width=32, min_height=32).indexed, 1)
        self.assertEqual(self.count("hashes"), 1)
        self.assertIsNone(self.row(path)["decode_error"])

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: -1)() == 0, "root ignores file permissions")
    def test_unreadable_file_is_retried_after_access_returns(self):
        path = self.image("locked.png")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "failed": 1})
        row = self.row(path)
        self.assertTrue(row["decode_error"].startswith("PermissionError:"))
        self.assertIsNone(row["sha256"])
        path.chmod(0o644)
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "indexed": 1})
        row = self.row(path)
        self.assertIsNone(row["decode_error"])
        self.assertEqual(row["sha256"], scan.sha256_file(path))
        self.assertEqual(list(iter_failures(self.conn)), [])

    def test_vanished_file_between_listing_and_stat_is_counted_as_failure(self):
        kept = self.image("kept.png")
        vanished = self.root / "vanished.png"
        with patch.object(scan, "iter_image_paths", return_value=[vanished, kept]):
            stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 2, "queued": 1, "indexed": 1, "failed": 1})
        self.assertIsNone(self.row(vanished))

    def test_delete_missing_removes_only_records_and_related_rows(self):
        gone = self.image("gone.png", 1)
        kept = self.image("kept.png", 2)
        nested = self.image("folder/nested.png", 3)
        self.scan()
        ids = {path: self.row(path)["id"] for path in (gone, kept, nested)}
        self.conn.execute("INSERT INTO matches(image_id_a,image_id_b,score,decision,created_at) "
                          "VALUES(?,?,90,'strong_duplicate','now')", (ids[gone], ids[kept]))
        self.conn.executemany("INSERT INTO clusters VALUES(1,?,?)", ((ids[gone], 1), (ids[kept], 0)))
        self.conn.commit()
        gone.unlink()
        nested.unlink()
        (self.root / "folder").rmdir()
        (self.root / "folder").write_text("a file replaced the folder")

        self.assertEqual(self.scan().deleted, 0)
        self.assertEqual(self.count("images"), 3)
        stats = self.scan(delete_missing=True)
        self.assertEqual(stats.deleted, 2)
        self.assertEqual([row[0] for row in self.conn.execute("SELECT path FROM images")], [str(kept)])
        self.assertEqual((self.count("matches"), self.count("clusters"), self.count("hashes")), (0, 0, 1))
        self.assertTrue(kept.exists())
        self.assertEqual(db.fetch_stats(self.conn)["indexed"], 1)

    def test_crop_index_state_tracks_completeness_across_scans(self):
        large = self.image("a/large.png", 1)
        self.image("a/small.png", 2, (40, 40))
        self.scan()
        self.assertEqual(db.get_crop_index_state(self.conn), "disabled")
        self.assertEqual(self.count("crop_hashes"), 0)

        stats = self.scan(crop_index=True)
        self.assertEqual(self.counts(stats), {"seen": 2, "queued": 1, "indexed": 1, "skipped": 1})
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")
        self.assertEqual(db.fetch_stats(self.conn)["crop_ready"], 1)
        self.assertIn("crop:center_50", db.fetch_crop_hashes(self.conn, self.row(large)["id"]))
        self.assertEqual(self.counts(self.scan(crop_index=True)), {"seen": 2, "skipped": 2})
        self.scan()
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")

        added = self.image("b/added.png", 3)
        self.scan()
        self.assertEqual(db.get_crop_index_state(self.conn), "incomplete")
        self.assertFalse(db.is_crop_index_ready(self.conn))
        self.scan(self.root / "a", crop_index=True)
        self.assertEqual(db.get_crop_index_state(self.conn), "incomplete")
        self.scan(crop_index=True)
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")
        self.assertTrue(db.fetch_crop_hashes(self.conn, self.row(added)["id"]))

        db.set_crop_index_state(self.conn, "building")
        self.conn.commit()
        self.scan()
        self.assertEqual(db.get_crop_index_state(self.conn), "incomplete")

    def test_prompt_index_can_be_skipped_and_backfilled_without_rehashing(self):
        path = self.image("image.png")
        self.scan(prompt_index=False)
        self.assertEqual(self.count("hashes"), 1)
        self.assertEqual(self.count("image_prompts"), 0)
        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")):
            self.assertEqual(self.counts(self.scan(prompt_index=False)), {"seen": 1, "skipped": 1})
            stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "indexed": 1})
        prompt = self.conn.execute("SELECT model_type, prompt_error FROM image_prompts WHERE image_id=?",
                                   (self.row(path)["id"],)).fetchone()
        self.assertEqual(tuple(prompt), ("no_metadata", "no_metadata"))

    def test_exact_file_model_rule_does_not_label_its_sibling(self):
        first = self.image("first.png", 1)
        second = self.image("second.png", 2)
        configuration = ModelPathConfig((make_rule(first, "Only first"),))

        stats = self.scan(model_paths=configuration)

        self.assertEqual((stats.indexed, stats.failed), (2, 0))
        labels = dict(self.conn.execute(
            "SELECT i.path,ip.configured_model FROM images i JOIN image_prompts ip ON ip.image_id=i.id"))
        self.assertEqual(labels, {str(first): "Only first", str(second): None})
        labelled_ids = [row[0] for row in self.conn.execute(
            "SELECT image_id FROM prompt_tags WHERE tag='@model:Only first'")]
        self.assertEqual(labelled_ids, [self.row(first)["id"]])

    def test_replacing_a_rule_id_with_the_same_label_preserves_tags_without_reparsing(self):
        path = self.image("image.png")
        original = make_rule(self.root, "Custom")
        self.scan(model_paths=ModelPathConfig((original,)))
        image_id = self.row(path)["id"]
        tags = [tuple(row) for row in self.conn.execute("SELECT * FROM prompt_tags ORDER BY position")]
        replacement = make_rule(self.root, "Custom", str(uuid.uuid4()))

        with patch.object(scan, "extract_nai_prompt", side_effect=AssertionError("reparsed")), \
                patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")):
            stats = self.scan(model_paths=ModelPathConfig((replacement,)))

        self.assertEqual((stats.indexed, stats.skipped, stats.failed), (0, 1, 0))
        assigned = self.conn.execute(
            "SELECT configured_model,model_config_root,model_config_rule_id FROM image_prompts WHERE image_id=?",
            (image_id,)).fetchone()
        self.assertEqual(tuple(assigned), ("Custom", str(self.root), replacement.rule_id))
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM prompt_tags ORDER BY position")], tags)
        self.assertEqual([row.image_row["id"] for row in query_prompt_tags(self.conn, "@model:Custom")], [image_id])

    def test_parallel_scan_keeps_submission_bounded(self):
        paths = [self.image(f"{index}.png", index) for index in range(7)]
        executors = []

        class RecordingExecutor(ThreadPoolExecutor):
            def __init__(inner, *, max_workers, mp_context):
                self.assertEqual(mp_context.get_start_method(), "spawn")
                super().__init__(max_workers=max_workers)
                inner.outstanding = inner.peak = 0
                executors.append(inner)

            def submit(inner, function, *args):
                inner.outstanding += 1
                inner.peak = max(inner.peak, inner.outstanding)
                future = super().submit(function, *args)
                result = future.result

                def collected(*result_args, **result_kwargs):
                    inner.outstanding -= 1
                    return result(*result_args, **result_kwargs)

                future.result = collected
                return future

        with patch.object(scan, "ProcessPoolExecutor", RecordingExecutor):
            stats = scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=2),
                                    model_paths=ModelPathConfig())
        self.assertEqual(self.counts(stats), {"seen": 7, "queued": 7, "indexed": 7})
        self.assertEqual((len(executors), executors[0].peak, executors[0].outstanding), (1, 4, 0))
        self.assertEqual(self.count("hashes"), 7)
        self.assertTrue(all(self.row(path)["sha256"] for path in paths))


class ScanPromptPersistenceTests(ScanTestCase):
    def attach_metadata(self, path, fields):
        metadata = PngInfo()
        for key, value in fields.items():
            metadata.add_text(key, value)
        with Image.open(path) as image:
            pixels = image.copy()
        pixels.save(path, pnginfo=metadata)

    def test_model_tags_colliding_with_prompt_text_keep_their_metadata_type(self):
        for index, category in enumerate(("sd", "Cool Mix abc123")):
            with self.subTest(category=category):
                path = self.image(f"collision-{index}.png", seed=index)
                self.attach_metadata(path, {"parameters":
                    f"cat, @model:{category}\nNegative prompt: dog\n"
                    "Steps: 20, Model: Cool Mix, Model hash: ABC123"})
                configuration = (ModelPathConfig() if category == "sd" else
                                 ModelPathConfig((make_rule(path, category),)))

                stats = self.scan(path, model_paths=configuration)

                self.assertEqual((stats.indexed, stats.failed), (1, 0))
                image_id = self.row(path)["id"]
                prompt = self.conn.execute(
                    "SELECT model_identity FROM image_prompts WHERE image_id=?", (image_id,)).fetchone()
                self.assertEqual(prompt["model_identity"], "Cool Mix abc123")
                tags = dict(self.conn.execute(
                    "SELECT tag,tag_kind FROM prompt_tags WHERE image_id=?", (image_id,)))
                expected = {"cat": "prompt", "@model:abc123": "model_hash"}
                if category == "sd":
                    expected.update({"@model:sd": "model_type", "@model:Cool Mix abc123": "model_identity"})
                else:
                    expected["@model:Cool Mix abc123"] = "model_config"
                self.assertEqual(tags, expected)
                found = query_prompt_tags(self.conn, f"cat, @model:{category}", model_type=category)
                self.assertEqual([row.image_row["id"] for row in found], [image_id])

    def test_metadata_expanding_past_storage_limit_keeps_searchable_tags_and_reports_failure(self):
        oversized = self.image("oversized.png")
        comment = json.dumps({"prompt": "cat", "uc": "x" * (db.MAX_PROMPT_METADATA_BYTES - 40)},
                             separators=(",", ":"))
        # Each source chunk is legal, but including the Software and Source
        # fields in the stored JSON pushes the combined metadata over the limit.
        self.assertLess(len(comment.encode()), db.MAX_PROMPT_METADATA_BYTES)
        self.attach_metadata(oversized, {"Software": "NovelAI", "Source": "NovelAI Diffusion V3 ABC123",
                                         "Comment": comment})
        kept = self.image("small.png", seed=2)
        self.attach_metadata(kept, {"parameters": "dog\nSteps: 20, Model hash: abc123"})

        stats = self.scan()

        self.assertEqual((stats.indexed, stats.failed), (2, 0))
        row = self.conn.execute(
            "SELECT prompt,prompt_error,prompt_status,raw_metadata,raw_metadata_codec FROM image_prompts WHERE image_id=?",
            (self.row(oversized)["id"],)).fetchone()
        self.assertEqual(tuple(row), ("cat", "prompt_metadata_too_large", "prompt_metadata_too_large", None, None))
        self.assertEqual([item.image_row["id"] for item in query_prompt_tags(self.conn, "cat")], [self.row(oversized)["id"]])
        self.assertEqual([item.image_row["id"] for item in query_prompt_tags(self.conn, "dog")], [self.row(kept)["id"]])
        [failure] = iter_failures(self.conn)
        self.assertEqual(failure["path"], str(oversized))
        self.assertIn("prompt_metadata_too_large", failure["decode_error"])


class OptionalProgressTests(unittest.TestCase):
    def test_scanner_still_works_without_tqdm(self):
        original_import = builtins.__import__

        def without_tqdm(name, *args, **kwargs):
            if name == "tqdm":
                raise ImportError("optional progress dependency absent")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_tqdm):
            namespace = runpy.run_path(str(Path(scan.__file__)), run_name="imgdupe._scan_without_tqdm",
                                       init_globals={"__package__": "imgdupe"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_image(root / "image.png")
            with closing(db.connect(root / "index.sqlite")) as conn:
                db.init_db(conn)
                for workers in (1, 2):
                    with self.subTest(workers=workers):
                        # The fallback is used for both iteration and manual progress.
                        # Threads keep the loaded module's function local to this test.
                        with patch.dict(namespace["scan_roots"].__globals__, {
                            "ProcessPoolExecutor": lambda max_workers, mp_context:
                                ThreadPoolExecutor(max_workers=max_workers),
                        }):
                            stats = namespace["scan_roots"](
                                conn, [root], config=ScanConfig(workers=workers, reindex_prompts=True),
                                model_paths=ModelPathConfig())
                        self.assertEqual((stats.indexed, stats.failed), (1, 0))
                        self.assertEqual(db.get_existing_image(conn, path)["sha256"], scan.sha256_file(path))
                        self.assertEqual(db.fetch_stats(conn)["hashed"], 1)
