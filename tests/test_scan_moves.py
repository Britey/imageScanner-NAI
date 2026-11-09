"""Moved-file detection by unique SHA-256 + size, and its safety limits."""

from __future__ import annotations

import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import tempfile
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe import db, scan
from imgdupe.config import ScanConfig
from imgdupe.hashing import sha256_file
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


class MovedFileTests(ScanTestCase):
    def test_renamed_folder_keeps_image_id_hashes_prompts_and_matches(self):
        first = self.image("old/first.png", 1)
        metadata = PngInfo()
        metadata.add_text("parameters", "cat, blue hair\nNegative prompt: dog\nSteps: 20, Model hash: a1b2c3")
        with Image.open(first) as image:
            image.save(first, pnginfo=metadata)
        second = self.image("second.png", 2)
        self.scan()
        first_id, second_id = self.row(first)["id"], self.row(second)["id"]
        self.conn.execute("INSERT INTO matches(image_id_a,image_id_b,score,decision,created_at) "
                          "VALUES(?,?,90,'strong_duplicate','now')", (first_id, second_id))
        self.conn.executemany("INSERT INTO clusters VALUES(1,?,?)", ((first_id, 1), (second_id, 0)))
        self.conn.commit()
        hashes = tuple(db.fetch_hash_row(self.conn, first_id))
        prompt = tuple(self.conn.execute("SELECT * FROM image_prompts WHERE image_id=?", (first_id,)).fetchone())
        tags = [tuple(row) for row in self.conn.execute(
            "SELECT tag,tag_kind FROM prompt_tags WHERE image_id=? ORDER BY position", (first_id,))]
        self.assertIn(("blue hair", "prompt"), tags)
        self.assertNotIn(("dog", "prompt"), tags)
        (self.root / "old").rename(self.root / "renamed")
        moved = self.root / "renamed" / "first.png"

        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")):
            stats = self.scan(delete_missing=True)
        self.assertEqual(self.counts(stats), {"seen": 2, "queued": 1, "indexed": 1, "moved": 1, "skipped": 1})
        self.assertIsNone(self.row(first))
        row = self.row(moved)
        self.assertEqual((row["id"], row["mtime_ns"]), (first_id, os.stat(moved).st_mtime_ns))
        self.assertEqual(tuple(db.fetch_hash_row(self.conn, first_id)), hashes)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM image_prompts WHERE image_id=?",
                                                 (first_id,)).fetchone()), prompt)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            "SELECT tag,tag_kind FROM prompt_tags WHERE image_id=? ORDER BY position", (first_id,))], tags)
        self.assertEqual((self.count("images"), self.count("matches"), self.count("clusters")), (2, 1, 2))

    def test_same_size_different_content_is_never_claimed_as_a_move(self):
        old = self.image("old.bmp", seed=1)
        self.scan()
        old_id = self.row(old)["id"]
        new = self.image("new.bmp", seed=2)
        self.assertEqual(old.stat().st_size, new.stat().st_size)
        self.assertNotEqual(sha256_file(old), sha256_file(new))
        old.unlink()

        stats = self.scan()

        self.assertEqual((stats.indexed, stats.moved), (1, 0))
        self.assertEqual(self.row(old)["id"], old_id)
        self.assertNotEqual(self.row(new)["id"], old_id)
        self.assertEqual(self.row(new)["sha256"], sha256_file(new))

    def test_move_into_labelled_folder_reparses_prompt_with_the_new_label(self):
        labelled = self.root / "noob"
        labelled.mkdir()
        config = ModelPathConfig((make_rule(labelled, "noob"),))
        path = self.image("image.png")
        self.scan(model_paths=config)
        image_id = self.row(path)["id"]
        path.rename(labelled / "image.png")
        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")):
            stats = self.scan(model_paths=config)
        self.assertEqual((stats.indexed, stats.moved), (1, 1))
        prompt = self.conn.execute("SELECT image_id, configured_model, classification_root "
                                   "FROM image_prompts").fetchone()
        self.assertEqual(tuple(prompt), (image_id, "noob", str(self.root)))
        tags = [row[0] for row in self.conn.execute("SELECT tag FROM prompt_tags")]
        self.assertIn("@model:noob", tags)

    def test_moves_refresh_folder_and_file_labels_without_prompt_indexing(self):
        labelled = self.root / "noob"
        labelled.mkdir()
        folder_rule = make_rule(labelled, "Noob")
        file_rule = make_rule(labelled / "single.png", "One file")
        configuration = ModelPathConfig((folder_rule, file_rule))
        original = self.image("plain/original.png")
        plain = original.parent
        metadata = PngInfo()
        metadata.add_text("parameters", "cat, blue hair\nNegative prompt: dog\n"
                          "Steps: 20, Model: Custom SD, Model hash: ABC123")
        with Image.open(original) as image:
            pixels = image.copy()
        pixels.save(original, pnginfo=metadata)
        self.scan(plain, labelled, model_paths=configuration, crop_index=True)
        image_id = self.row(original)["id"]
        before = dict(self.conn.execute("SELECT * FROM image_prompts WHERE image_id=?", (image_id,)).fetchone())
        path_fields = {"configured_model", "model_config_root", "model_config_rule_id",
                       "classification_root", "classification_status"}
        unchanged_prompt = {key: value for key, value in before.items() if key not in path_fields}
        hashes = tuple(db.fetch_hash_row(self.conn, image_id))
        crop_hashes = db.fetch_crop_hashes(self.conn, image_id)
        tags = [tuple(row) for row in self.conn.execute(
            "SELECT * FROM prompt_tags WHERE image_id=? AND tag_kind NOT IN ('model_type','model_config') "
            "ORDER BY position", (image_id,))]
        current = original
        previous_label = "sd"
        for destination, rule in ((labelled / "inside.png", folder_rule),
                                  (labelled / "single.png", file_rule),
                                  (labelled / "inside.png", folder_rule),
                                  (plain / "final.png", None)):
            current.rename(destination)
            with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")), \
                    patch.object(scan, "extract_nai_prompt", side_effect=AssertionError("reparsed")):
                stats = self.scan(plain, labelled, model_paths=configuration, prompt_index=False)
            self.assertEqual((stats.indexed, stats.moved, stats.failed), (1, 1, 0))
            self.assertEqual(self.row(destination)["id"], image_id)
            self.assertIsNone(self.row(current))
            prompt = dict(self.conn.execute(
                "SELECT * FROM image_prompts WHERE image_id=?", (image_id,)).fetchone())
            self.assertEqual({key: value for key, value in prompt.items() if key not in path_fields}, unchanged_prompt)
            self.assertEqual({key: prompt[key] for key in path_fields}, {
                "configured_model": rule.label if rule else None,
                "model_config_root": str(rule.path) if rule else None,
                "model_config_rule_id": rule.rule_id if rule else None,
                "classification_root": str(destination.parent),
                "classification_status": "configured" if rule else "unconfigured",
            })
            self.assertEqual(tuple(db.fetch_hash_row(self.conn, image_id)), hashes)
            self.assertEqual(db.fetch_crop_hashes(self.conn, image_id), crop_hashes)
            self.assertEqual([tuple(row) for row in self.conn.execute(
                "SELECT * FROM prompt_tags WHERE image_id=? AND tag_kind NOT IN ('model_type','model_config') "
                "ORDER BY position", (image_id,))], tags)
            label = rule.label if rule else "sd"
            self.assertEqual([result.image_row["id"] for result in
                              query_prompt_tags(self.conn, f"cat, @model:{label}")], [image_id])
            self.assertEqual(query_prompt_tags(self.conn, f"@model:{previous_label}"), [])
            self.assertEqual(db.fetch_model_options(self.conn), [(label, label.upper() if label == "sd" else label)])
            current, previous_label = destination, label

    def test_move_without_prompt_indexing_does_not_create_prompt_metadata(self):
        labelled = self.root / "noob"
        labelled.mkdir()
        configuration = ModelPathConfig((make_rule(labelled, "Noob"),))
        original = self.image("original.png")
        self.scan(model_paths=configuration, prompt_index=False)
        image_id = self.row(original)["id"]
        destination = labelled / "moved.png"
        original.rename(destination)

        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("rehashed")), \
                patch.object(scan, "extract_nai_prompt", side_effect=AssertionError("reparsed")):
            stats = self.scan(model_paths=configuration, prompt_index=False)

        self.assertEqual((stats.indexed, stats.moved, stats.failed), (1, 1, 0))
        self.assertEqual(self.row(destination)["id"], image_id)
        self.assertEqual(self.count("image_prompts"), 0)
        self.assertEqual(self.count("prompt_tags"), 0)

    def test_moved_file_missing_crop_hashes_is_rehashed_under_its_original_id(self):
        path = self.image("image.png")
        self.scan()
        image_id = self.row(path)["id"]
        moved = self.root / "moved.png"
        path.rename(moved)
        stats = self.scan(crop_index=True)
        self.assertEqual((stats.indexed, stats.moved, self.count("images")), (1, 1, 1))
        self.assertEqual(self.row(moved)["id"], image_id)
        self.assertIn("crop:center_50", db.fetch_crop_hashes(self.conn, image_id))
        self.assertEqual(db.get_crop_index_state(self.conn), "ready")

    def test_moved_file_with_earlier_transient_failure_is_retried_at_its_new_path(self):
        path = self.root / "broken.png"
        path.write_bytes(b"not really an image")
        with patch.object(scan, "compute_image_hashes", side_effect=PermissionError("temporary denial")):
            self.assertEqual(self.scan().failed, 1)
        image_id = self.row(path)["id"]
        moved = self.root / "moved.png"
        path.rename(moved)
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "failed": 1, "moved": 1})
        row = self.row(moved)
        self.assertEqual(row["id"], image_id)
        self.assertTrue(row["decode_error"].startswith("UnidentifiedImageError:"))
        self.assertEqual(row["sha256"], sha256_file(moved))

    def test_identical_missing_copies_are_ambiguous_and_never_claimed(self):
        first = self.image("x/copy.png")
        second = self.root / "y" / "copy.png"
        second.parent.mkdir()
        shutil.copyfile(first, second)
        self.scan()
        old_ids = {self.row(first)["id"], self.row(second)["id"]}
        self.assertEqual(len(old_ids), 2)
        third = self.root / "z.png"
        shutil.copyfile(first, third)
        first.unlink()
        second.unlink()
        stats = self.scan()
        self.assertEqual((stats.indexed, stats.moved), (1, 0))
        self.assertNotIn(self.row(third)["id"], old_ids)
        self.assertEqual(self.count("images"), 3)
        self.assertEqual(self.scan(delete_missing=True).deleted, 2)

    def test_present_original_makes_a_copy_a_new_image(self):
        original = self.image("original.png")
        self.scan()
        copy = self.root / "copy.png"
        shutil.copyfile(original, copy)
        stats = self.scan()
        self.assertEqual((stats.indexed, stats.moved), (1, 0))
        self.assertNotEqual(self.row(copy)["id"], self.row(original)["id"])

    def test_move_detection_only_claims_old_paths_inside_scanned_folders(self):
        old = self.image("x/image.png")
        self.scan()
        old_id = self.row(old)["id"]
        new = self.root / "y" / "image.png"
        new.parent.mkdir()
        old.rename(new)
        stats = self.scan(self.root / "y")
        self.assertEqual((stats.indexed, stats.moved), (1, 0))
        self.assertEqual(self.row(old)["id"], old_id)
        self.assertNotEqual(self.row(new)["id"], old_id)
        single = self.root / "single.png"
        new.rename(single)
        self.assertEqual(self.scan(single).moved, 0)

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: -1)() == 0, "root ignores directory permissions")
    def test_inaccessible_original_is_neither_moved_nor_deleted(self):
        locked = self.root / "locked"
        old = self.image("locked/old.png")
        self.scan()
        original_id = self.row(old)["id"]
        new = self.root / "new.png"
        shutil.copyfile(old, new)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        stats = self.scan(delete_missing=True)
        self.assertEqual(self.counts(stats), {"seen": 1, "queued": 1, "indexed": 1, "failed": 1})
        self.assertEqual(self.row(old)["id"], original_id)
        self.assertNotEqual(self.row(new)["id"], original_id)
        self.assertEqual(self.count("images"), 2)

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: -1)() == 0, "root ignores file permissions")
    def test_unreadable_copy_is_reported_without_being_indexed_or_claimed(self):
        original = self.image("original.png")
        self.scan()
        copy = self.root / "copy.png"
        shutil.copyfile(original, copy)
        copy.chmod(0)
        self.addCleanup(copy.chmod, 0o644)
        original.unlink()
        stats = self.scan()
        self.assertEqual(self.counts(stats), {"seen": 1, "failed": 1})
        self.assertIsNone(self.row(copy))
        self.assertIsNotNone(self.row(original))

    def test_parallel_scan_never_assigns_one_moved_id_to_two_copies(self):
        old = self.image("old.png")
        self.scan()
        old_id = self.row(old)["id"]
        contents = old.read_bytes()
        old.unlink()
        for name in ("new-one.png", "new-two.png"):
            (self.root / name).write_bytes(contents)
        with patch.object(scan, "ProcessPoolExecutor", side_effect=lambda max_workers, mp_context:
                          ThreadPoolExecutor(max_workers=max_workers)):
            stats = scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=2),
                                    model_paths=ModelPathConfig())
        self.assertEqual((stats.indexed, stats.moved), (2, 1))
        ids = sorted(self.row(self.root / name)["id"] for name in ("new-one.png", "new-two.png"))
        self.assertIn(old_id, ids)
        self.assertEqual(len(set(ids)), 2)
