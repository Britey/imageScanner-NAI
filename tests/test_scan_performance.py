"""Bounded incremental scans and safety at filesystem/process boundaries."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from imgdupe import db, scan, utils
from imgdupe.config import ScanConfig
from imgdupe.model_paths import ModelPathConfig, make_rule


class ScanPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def run_scan(self, roots=None, model_paths=None, **options):
        return scan.scan_roots(self.conn, [self.root] if roots is None else roots,
                               config=ScanConfig(workers=1, **options),
                               model_paths=model_paths or ModelPathConfig())

    def image(self, name="image.png", size=(80, 80)):
        path = self.root / name
        Image.new("RGB", size, "blue").save(path)
        return path

    def record(self, path, **kwargs):
        return db.upsert_image(self.conn, path=path, size_bytes=kwargs.pop("size_bytes", 100),
                               mtime_ns=1, indexed_at="now", **kwargs)

    def vm_work(self, action):
        instructions = []
        self.conn.set_progress_handler(lambda: instructions.append(1) or 0, 100)
        try:
            result = action()
        finally:
            self.conn.set_progress_handler(None, 0)
        return result, len(instructions)

    def test_tiny_scan_does_not_load_collection_or_resolve_indexed_paths(self):
        # VM instruction count catches full-table work independently of timing.
        self.conn.executemany("INSERT INTO images(path,size_bytes,mtime_ns,indexed_at) VALUES(?,1,1,'now')",
                              ((str(self.root / "offline" / f"{i}.png"),) for i in range(5000)))
        self.run_scan([])  # Legacy databases perform their one-time reconciliation.
        path = self.image()

        def tiny_scans():
            return self.run_scan([]).seen, self.run_scan([path], prompt_index=False).indexed

        results, work = self.vm_work(tiny_scans)
        self.assertEqual(results, (0, 1))
        self.assertLess(work, 100)

    def test_path_lookups_use_bounded_batches(self):
        paths = [self.root / f"{i}.png" for i in range(805)]
        for path in paths:
            self.record(path)
        queries = []
        self.conn.set_trace_callback(queries.append)
        found = list(scan._existing_paths(self.conn, iter(paths)))
        self.conn.set_trace_callback(None)
        self.assertEqual({str(path) for path, row in found}, {str(path) for path in paths})
        self.assertEqual({row["path"] for _, row in found}, {str(path) for path in paths})
        self.assertEqual(len(queries), 3)

    def test_configuration_reconciliation_runs_once_per_rule_change(self):
        ids = [self.record(self.root / "sub" / f"{i}.png") for i in range(805)]
        for image_id in ids:
            db.replace_prompt(self.conn, image_id, prompt=None, metadata_source=None,
                              prompt_error="no_metadata", indexed_at="now", model_type="sd",
                              tags=[("@model:sd", "@model:sd", "model_type")])
        self.run_scan([])
        config = ModelPathConfig((make_rule(self.root / "sub", "My model"),))
        _, first = self.vm_work(lambda: self.run_scan([], model_paths=config))
        labels = dict(self.conn.execute("SELECT configured_model, COUNT(*) FROM image_prompts GROUP BY 1"))
        self.assertEqual(labels, {"My model": 805})
        self.assertEqual(db.fetch_model_options(self.conn), [("My model", "My model")])
        _, repeated = self.vm_work(lambda: self.run_scan([], model_paths=config))
        self.assertLess(repeated, 100)
        self.assertGreater(first, 100 * repeated)
        path = self.image()
        self.run_scan([path], model_paths=ModelPathConfig((make_rule(self.root, "Root"),)))
        self.assertEqual(self.conn.execute("SELECT configured_model FROM image_prompts WHERE image_id=?",
                         (db.get_existing_image(self.conn, path)["id"],)).fetchone()[0], "Root")

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

    def test_actual_spawn_scan_inside_background_thread_with_open_wal_connection(self):
        self.image("one.png")
        self.image("two.png", (96, 96))
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
        self.assertEqual(db.fetch_stats(outer)["hashed"], 2)
        self.assertEqual(db.checkpoint_database(outer, "TRUNCATE")[0], 0)


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
