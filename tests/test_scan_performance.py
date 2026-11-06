"""Bounded incremental scans and safety at filesystem/process boundaries."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from imgdupe import db, scan, utils
from imgdupe.config import ScanConfig
from imgdupe.model_paths import ModelPathConfig, make_rule


class ScanPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def run_scan(self, roots=None, **options):
        return scan.scan_roots(self.conn, [self.root] if roots is None else roots,
                              config=ScanConfig(workers=1, **options), model_paths=ModelPathConfig())

    def image(self, name="image.png", size=(80, 80)):
        path = self.root / name
        Image.new("RGB", size, "blue").save(path)
        return path

    def record(self, path, **kwargs):
        return db.upsert_image(self.conn, path=path, size_bytes=kwargs.pop("size_bytes", 100),
                               mtime_ns=1, indexed_at="now", **kwargs)

    def test_unchanged_decode_failures_skip_but_explicit_prompt_reindex_works(self):
        path = self.root / "broken.png"
        path.write_bytes(b"broken forever")
        self.assertEqual(self.run_scan().failed, 1)

        original_error = self.conn.execute("SELECT decode_error FROM images").fetchone()[0]
        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("decoded twice")):
            self.assertEqual(self.run_scan(crop_index=True).skipped, 1)
            refreshed = self.run_scan(reindex_prompts=True)
        self.assertEqual((refreshed.indexed, refreshed.failed), (1, 0))
        self.assertEqual(self.conn.execute("SELECT decode_error FROM images").fetchone()[0], original_error)
        path.write_bytes(b"different broken contents")
        self.assertEqual(self.run_scan().failed, 1)

    def test_too_small_failure_retries_when_new_dimensions_allow_it(self):
        path = self.image(size=(40, 40))
        self.assertEqual(self.run_scan(min_width=64, min_height=64).failed, 1)
        with patch.object(scan, "compute_image_hashes", side_effect=AssertionError("still too small")):
            self.assertEqual(self.run_scan(min_width=32, min_height=64).skipped, 1)
        self.assertEqual(self.run_scan(min_width=32, min_height=32).indexed, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0], 1)
        self.assertIsNone(db.get_existing_image(self.conn, path)["decode_error"])

    def test_transient_decode_failure_retries_without_content_change(self):
        path = self.image()
        with patch.object(scan, "compute_image_hashes", side_effect=PermissionError("temporary denial")):
            self.assertEqual(self.run_scan().failed, 1)
        self.assertEqual(self.run_scan().indexed, 1)
        self.assertIsNone(db.get_existing_image(self.conn, path)["decode_error"])

    def test_hidden_existing_copy_is_not_mistaken_for_move_and_errors_are_counted(self):
        old = self.image("old.png")
        self.run_scan()
        original_id = db.get_existing_image(self.conn, old)["id"]
        new = self.root / "new.png"
        new.write_bytes(old.read_bytes())

        def partial_walk(_roots, *, on_error):
            on_error(PermissionError("cannot list original directory"))
            yield new

        with patch.object(scan, "iter_image_paths", side_effect=partial_walk):
            stats = self.run_scan()
        self.assertEqual((stats.failed, stats.indexed, stats.moved), (1, 1, 0))
        self.assertEqual(db.get_existing_image(self.conn, old)["id"], original_id)
        self.assertNotEqual(db.get_existing_image(self.conn, new)["id"], original_id)

    def test_permission_errors_never_prove_missing_and_ambiguous_moves_stay_separate(self):
        inaccessible = self.root / "inaccessible.png"
        for error in (PermissionError("denied"), OSError("offline")):
            with patch.object(Path, "stat", side_effect=error):
                self.assertFalse(scan._path_is_missing(inaccessible))
        with patch.object(Path, "stat", side_effect=NotADirectoryError("parent changed")):
            self.assertTrue(scan._path_is_missing(inaccessible))
        first = self.record(inaccessible, sha256=b"same")
        second = self.record(self.root / "other.png", sha256=b"same")
        self.assertIsNone(scan._find_moved_image(self.conn, b"same", 100, [self.root], set()))
        self.assertEqual(scan._find_moved_image(self.conn, b"same", 100, [self.root], {first})["id"], second)
        self.assertIsNone(scan._find_moved_image(self.conn, b"same", 100, [self.root / "elsewhere"], set()))
        with patch.object(scan, "_path_is_missing", return_value=False):
            self.assertEqual(scan._delete_missing_images(self.conn), 0)
            self.assertEqual(scan._missing_hash_candidates(self.conn, known_paths=set(), roots=[self.root]), {})

    def test_indexed_move_does_not_reuse_reserved_id_for_multiple_new_copies(self):
        old = self.image("old.png")
        self.run_scan()
        old_id = db.get_existing_image(self.conn, old)["id"]
        contents = old.read_bytes()
        old.unlink()
        for name in ("new-one.png", "new-two.png"):
            (self.root / name).write_bytes(contents)
        with patch.object(scan, "ProcessPoolExecutor", side_effect=lambda max_workers, mp_context:
                          ThreadPoolExecutor(max_workers=max_workers)):
            result = scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=2),
                                     model_paths=ModelPathConfig())
        self.assertEqual((result.indexed, result.moved), (2, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 2)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM images WHERE id=?", (old_id,)).fetchone())

    def test_tiny_scan_does_not_load_collection_or_resolve_indexed_paths(self):
        # VM instruction count catches full-table work independently of timing.
        self.conn.executemany("INSERT INTO images(path,size_bytes,mtime_ns,indexed_at) VALUES(?,1,1,'now')",
                              ((str(self.root / "offline" / f"{i}.png"),) for i in range(5000)))
        self.run_scan([])  # Legacy databases perform their one-time reconciliation.
        instructions = []
        self.conn.set_progress_handler(lambda: instructions.append(1) or 0, 100)
        with patch.object(scan, "_image_batches", side_effect=AssertionError("full collection read")):
            self.assertEqual(self.run_scan([]).seen, 0)
            path = self.image()
            self.assertEqual(self.run_scan([path], prompt_index=False).indexed, 1)
        self.conn.set_progress_handler(None, 0)
        self.assertLess(len(instructions), 100)

    def test_path_lookup_batches_and_configuration_reconciliation_are_bounded(self):
        paths = [self.root / f"{i}.png" for i in range(805)]
        for path in paths:
            self.record(path)
        queries = []
        self.conn.set_trace_callback(queries.append)
        self.assertEqual(len(list(scan._existing_paths(self.conn, iter(paths)))), 805)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(queries), 3)
        self.assertEqual(len(list(scan._image_batches(self.conn))), 805)
        rule = make_rule(self.root, "My model")
        config = ModelPathConfig((rule,))
        with patch.object(scan, "_refresh_model_config_assignments", wraps=scan._refresh_model_config_assignments) as refresh:
            for _ in range(2):
                scan.scan_roots(self.conn, [], config=ScanConfig(workers=1), model_paths=config)
        self.assertEqual(refresh.call_count, 1)
        # A later inserted row still receives the active assignment on its scan.
        path = self.image()
        scan.scan_roots(self.conn, [path], config=ScanConfig(workers=1), model_paths=config)
        self.assertEqual(self.conn.execute("SELECT configured_model FROM image_prompts WHERE image_id=?",
                         (db.get_existing_image(self.conn, path)["id"],)).fetchone()[0], "My model")

    def test_directory_listing_errors_and_symlink_files(self):
        missing = self.root / "missing"
        errors = []
        self.assertEqual(list(utils.iter_image_paths([missing], on_error=errors.append)), [])
        self.assertEqual(len(errors), 1)
        original = self.image()
        link = self.root / "link.png"
        link.symlink_to(original)
        self.assertEqual(list(utils.iter_image_paths([self.root])).count(original), 2)
        (self.root / "dangling.png").symlink_to(self.root / "absent")
        self.assertEqual(len(list(utils.iter_image_paths([self.root]))), 2)

    def test_file_rule_does_not_leak_into_sibling_configuration(self):
        first = self.image("first.png")
        second = self.image("second.png")
        rule = make_rule(first, "Only first")
        configuration = ModelPathConfig((rule,))
        scan.scan_roots(self.conn, [self.root], config=ScanConfig(workers=1), model_paths=configuration)
        actual = dict(self.conn.execute("SELECT i.path,ip.configured_model FROM images i "
                                        "JOIN image_prompts ip ON ip.image_id=i.id"))
        self.assertEqual(actual, {str(first): "Only first", str(second): None})

    def test_actual_spawn_scan_inside_background_thread_with_open_wal_connection(self):
        self.image("one.png")
        self.image("two.png")
        database = self.root / "index.sqlite"
        outer = db.connect(database)
        db.init_db(outer)
        self.addCleanup(outer.close)

        def background_scan():
            connection = db.connect(database)
            try:
                return scan.scan_roots(connection, [self.root], config=ScanConfig(workers=2),
                                       model_paths=ModelPathConfig())
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            stats = executor.submit(background_scan).result(timeout=30)
        self.assertEqual((stats.indexed, stats.failed), (2, 0))
        self.assertEqual(outer.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(outer.execute("SELECT COUNT(*) FROM hashes").fetchone()[0], 2)
        outer.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class ImageBatchKeyRangeTests(unittest.TestCase):
    def test_batches_include_entire_sqlite_integer_key_range(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE images(id INTEGER PRIMARY KEY,path TEXT)")
            ids = [-(2**63), -1, 0, *range(1, 402), 2**63 - 1]
            connection.executemany("INSERT INTO images VALUES(?,?)", ((i, str(i)) for i in ids))
            self.assertEqual([row["id"] for row in scan._image_batches(connection)], ids)
        finally:
            connection.close()
