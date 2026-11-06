"""Scan lifecycle, moves, failures, and optional dependency behavior."""

from __future__ import annotations

import builtins
import dataclasses
import runpy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from imgdupe import db, scan
from imgdupe.config import ScanConfig
from imgdupe.model_paths import ModelPathConfig, make_rule
from imgdupe.nai_prompt import PromptResult


class ScanCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def image(self, name="image.png", size=(80, 80)):
        path = self.root / name
        Image.new("RGB", size, "red").save(path)
        return path

    def record(self, path, **changes):
        values = dict(path=path, size_bytes=1, mtime_ns=1, indexed_at="now")
        values.update(changes)
        return db.upsert_image(self.conn, **values)

    def task(self, path, **changes):
        values = dict(path=path, size_bytes=1, mtime_ns=1, created_ns=2, modified_ns=3,
                      had_existing_hashes=False, prompt_only=False, date_only=False,
                      existing_image_id=None, classification_root=str(self.root),
                      configured_model=None, model_config_root=None, model_config_rule_id=None,
                      classification_status="unconfigured")
        values.update(changes)
        return scan.ScanTask(**values)

    def result(self, path, **changes):
        task_fields = dataclasses.asdict(self.task(path))
        task_fields.pop("precomputed_sha256")
        values = dict(task_fields, width=80, height=80, image_format="PNG", sha256=b"sha",
                      hashes={"phash256": b"\0" * 32}, decode_error=None, moved=False,
                      prompt_result=None)
        values.update(changes)
        return scan.ScanResult(**values)

    def run_scan(self, paths, **changes):
        return scan.scan_roots(self.conn, paths, config=ScanConfig(workers=1, **changes),
                               model_paths=ModelPathConfig())

    def test_real_hash_scan_refresh_dates_crop_state_and_delete_missing(self):
        path = self.image()
        stats = self.run_scan([self.root], crop_index=True)
        self.assertEqual((stats.indexed, stats.failed), (1, 0))
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")
        self.assertTrue(db.fetch_crop_hashes(self.conn, 1))
        self.conn.execute("UPDATE images SET created_ns = NULL, modified_ns = NULL")
        stats = self.run_scan([path], prompt_index=False)
        self.assertEqual(stats.indexed, 1)
        self.assertIsNotNone(self.conn.execute("SELECT modified_ns FROM images").fetchone()[0])
        path.unlink()
        stats = self.run_scan([self.root], delete_missing=True)
        self.assertEqual(stats.deleted, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM images").fetchone()[0], 0)

    def test_parallel_futures_are_collected_and_replenished(self):
        for index in range(7):
            self.image(f"{index}.png")
        # Thread futures obey the same executor/wait contract while keeping
        # worker execution visible to the coverage collector.
        with patch.object(scan, "ProcessPoolExecutor", wraps=ThreadPoolExecutor) as executor:
            stats = scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=2, delete_missing=True),
                                    model_paths=ModelPathConfig())
        executor.assert_called_once_with(max_workers=2)
        self.assertEqual((stats.seen, stats.indexed, stats.failed), (7, 7, 0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hashes").fetchone()[0], 7)

    def test_real_process_workers_return_serializable_results(self):
        self.image("first.png")
        self.image("second.png")
        stats = scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=2, prompt_index=False),
                                model_paths=ModelPathConfig())
        self.assertEqual((stats.indexed, stats.failed), (2, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0], 2)

    def test_stat_failure_and_moved_hash_failure_do_not_stop_scan(self):
        bad = Mock(spec=Path)
        bad.stat.side_effect = OSError("disappeared")
        with patch.object(scan, "iter_image_paths", return_value=[bad]):
            stats = self.run_scan([])
        self.assertEqual((stats.seen, stats.failed), (1, 1))
        path = self.image()
        self.record(self.root / "old.png", size_bytes=path.stat().st_size, sha256=b"old")
        with patch.object(scan, "_claim_moved_image", side_effect=OSError("unreadable")):
            stats = self.run_scan([self.root])
        self.assertEqual((stats.seen, stats.failed, stats.queued), (1, 1, 0))
        stats = self.run_scan([self.root])
        self.assertEqual((stats.indexed, stats.moved), (1, 0))

    def test_renamed_image_preserves_id_for_date_and_prompt_updates(self):
        path = self.image()
        self.run_scan([self.root])
        image_id = self.conn.execute("SELECT id FROM images").fetchone()[0]
        moved = self.root / "moved.png"
        path.rename(moved)
        stats = self.run_scan([self.root])
        self.assertEqual((stats.indexed, stats.moved), (1, 1))
        self.assertEqual(db.get_existing_image(self.conn, moved)["id"], image_id)
        final = self.root / "final.png"
        moved.rename(final)
        stats = self.run_scan([self.root], reindex_prompts=True)
        self.assertEqual((stats.indexed, stats.moved), (1, 1))
        self.assertEqual(db.get_existing_image(self.conn, final)["id"], image_id)

    def test_scan_root_resolution_and_stat_dates(self):
        path = self.image()
        unrelated = self.root / "other"
        unrelated.mkdir()
        self.assertEqual(scan._scan_root_for_path(path, [unrelated]), self.root)
        self.assertEqual(scan._scan_root_for_path(path, [self.root, path]), self.root)
        self.assertEqual(scan._created_ns_from_stat(SimpleNamespace(st_birthtime_ns=7)), 7)
        self.assertEqual(scan._created_ns_from_stat(SimpleNamespace(st_ctime_ns=8)), 8)
        self.assertIsNone(scan._created_ns_from_stat(SimpleNamespace()))
        self.assertIsNone(scan._optional_int(None))

    def test_crop_state_interruption_and_incomplete_index(self):
        image_id = self.record(self.root / "large.png", width=80, height=80)
        db.replace_hashes(self.conn, image_id, {"phash256": b"\0" * 32})
        for state, requested, expected in (("building", False, "incomplete"),
                                           ("ready", False, "incomplete"),
                                           ("ready", True, "incomplete"),
                                           ("disabled", False, "disabled")):
            db.set_crop_index_state(self.conn, state)
            scan._finish_crop_index_state(self.conn, requested)
            self.assertEqual(db.get_crop_index_state(self.conn), expected)
        db.replace_crop_hashes(self.conn, image_id, {"crop:center_50": b"crop"})
        db.set_crop_index_state(self.conn, "ready")
        scan._finish_crop_index_state(self.conn, False)
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")

    def test_refresh_assignments_updates_only_configuration_when_category_matches(self):
        path = self.image()
        rule = make_rule(self.root, "sd")
        image_id = self.record(path)
        db.replace_prompt(self.conn, image_id, prompt="cat", metadata_source=None, prompt_error=None,
                          indexed_at="now", configured_model="sd", model_type="sd",
                          tags=[("@model:sd", "@model:sd", "model_config")])
        rows = self.conn.execute("SELECT * FROM images").fetchall()
        scan._refresh_model_config_assignments(self.conn, rows, ModelPathConfig((rule,)))
        self.assertEqual(self.conn.execute("SELECT model_config_root FROM image_prompts").fetchone()[0], str(self.root))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prompt_tags").fetchone()[0], 1)
        # Two images in one directory exercise the directory rule cache.
        self.record(self.root / "without-prompt.png")
        second = self.record(self.root / "second.png")
        db.replace_prompt(self.conn, second, prompt="dog", metadata_source=None, prompt_error=None,
                          indexed_at="now", model_type="sd", tags=[])
        rows = self.conn.execute("SELECT * FROM images").fetchall()
        scan._refresh_model_config_assignments(self.conn, rows, ModelPathConfig((rule,)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_prompts").fetchone()[0], 2)

    def test_store_missing_ids_counts_failure_without_touching_database(self):
        for flags in ({"date_only": True}, {"prompt_only": True}):
            stats = scan.ScanStats()
            scan._store_result(self.conn, self.result(self.root / "missing.png", **flags),
                               ScanConfig(batch_size=1), stats)
            self.assertEqual((stats.indexed, stats.failed), (0, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_store_full_success_and_decode_failure_for_existing_moves(self):
        image_id = self.record(self.root / "old.png")
        stats = scan.ScanStats()
        result = self.result(self.root / "new.png", existing_image_id=image_id, moved=True,
                             had_existing_hashes=True)
        scan._store_result(self.conn, result, ScanConfig(prompt_index=False, batch_size=0), stats)
        self.assertEqual((stats.indexed, stats.moved), (1, 1))
        self.assertIsNotNone(db.fetch_hash_row(self.conn, image_id))
        failed = dataclasses.replace(result, path=self.root / "failed.png", decode_error="broken", hashes={})
        scan._store_result(self.conn, failed, ScanConfig(prompt_index=False), stats)
        self.assertEqual((stats.failed, stats.moved), (1, 2))
        self.assertIsNone(db.fetch_hash_row(self.conn, image_id))
        self.assertEqual(db.get_existing_image(self.conn, failed.path)["decode_error"], "broken")
        # A newly encountered corrupt file records its own failure and prompt status.
        scan._store_result(self.conn, self.result(self.root / "corrupt.png", decode_error="bad", hashes={}),
                           ScanConfig(), stats)
        self.assertEqual(stats.failed, 2)

    def test_hash_task_errors_keep_precomputed_sha_or_handle_missing_file(self):
        path = self.root / "bad.png"
        path.write_bytes(b"not an image")
        result = scan._hash_task(self.task(path, precomputed_sha256=b"cached"), 32, 32, False, False, False)
        self.assertEqual(result.sha256, b"cached")
        self.assertIn("UnidentifiedImageError", result.decode_error)
        result = scan._hash_task(self.task(path), 32, 32, False, False, False)
        self.assertIsNotNone(result.sha256)
        path.unlink()
        result = scan._hash_task(self.task(path), 32, 32, False, False, False)
        self.assertIsNone(result.sha256)
        self.assertIn("FileNotFoundError", result.decode_error)

    def test_moved_candidates_respect_roots_known_paths_and_ambiguity(self):
        path = self.image()
        present = self.record(path, size_bytes=2, sha256=b"present")
        missing = self.record(self.root / "missing.png", size_bytes=2, sha256=b"missing")
        outside = self.record(self.root.parent / "elsewhere.png", size_bytes=3, sha256=b"outside")
        candidates = scan._missing_hash_candidates(self.conn)
        self.assertEqual(candidates, {2: {b"missing": [missing]}, 3: {b"outside": [outside]}})
        self.assertEqual(scan._missing_hash_candidates(self.conn, known_paths={str(path)}, roots=[self.root]),
                         {2: {b"missing": [missing]}})
        self.assertIsNone(scan._claim_moved_image(path, 99, candidates))
        with patch.object(scan, "sha256_file", return_value=b"missing"):
            cache = {}
            ambiguous = {2: {b"missing": [present, missing]}}
            self.assertIsNone(scan._claim_moved_image(path, 2, ambiguous, precomputed_hashes=cache))
            self.assertEqual(cache[path], b"missing")
            unique = {2: {b"missing": [missing], b"other": [outside]}}
            self.assertEqual(scan._claim_moved_image(path, 2, unique), missing)
            self.assertEqual(unique, {2: {b"other": [outside]}})

    def test_store_prompt_category_conflicts_and_metadata_limit(self):
        image_id = self.record(self.root / "image.png")
        parsed = PromptResult("cat", "test", None, model_type="sd", model_hash="ABCDEF",
                              model_identity="custom", tags=("", "cat", "cat", "@model:sd", "@model:custom abcdef"),
                              raw_metadata="small")
        result = self.result(self.root / "image.png", prompt_result=parsed)
        scan._store_prompt(self.conn, image_id, result, "now")
        row = self.conn.execute("SELECT * FROM image_prompts").fetchone()
        self.assertEqual(row["model_identity"], "custom abcdef")
        rows = self.conn.execute("SELECT tag_kind FROM prompt_tags ORDER BY position").fetchall()
        self.assertEqual([row[0] for row in rows], ["prompt", "model_type", "model_identity", "model_hash"])
        # Reusing a model identity/hash is deduplicated without creating a
        # second metadata tag, including when the earlier tag was not a prompt.
        parsed = dataclasses.replace(parsed, model_hash="sd", model_identity="sd", tags=("cat",))
        scan._store_prompt(self.conn, image_id, dataclasses.replace(result, prompt_result=parsed), "later")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prompt_tags WHERE tag_norm='@model:sd'").fetchone()[0], 1)
        with patch.object(scan, "encode_prompt_metadata", side_effect=ValueError("too big")):
            scan._store_prompt(self.conn, image_id, result, "later")
        row = self.conn.execute("SELECT prompt_status, prompt_error, raw_metadata FROM image_prompts").fetchone()
        self.assertEqual(tuple(row), ("prompt_metadata_too_large", "prompt_metadata_too_large", None))
        minimal = PromptResult("cat", "test", None, tags=("cat",))
        scan._store_prompt(self.conn, image_id, dataclasses.replace(result, prompt_result=minimal), "minimal")
        self.assertIsNone(self.conn.execute("SELECT model_identity FROM image_prompts").fetchone()[0])


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
        empty = namespace["tqdm"]()
        self.assertEqual(list(empty), [])
        progress = namespace["tqdm"]([1, 2], desc="ignored", total=2)
        self.assertEqual(list(progress), [1, 2])
        self.assertIsNone(progress.update())
        self.assertIsNone(progress.close())
