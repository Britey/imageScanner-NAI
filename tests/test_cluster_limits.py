"""Large cluster membership must not exceed SQLite's bind-variable ceiling."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from imgdupe.cluster import _choose_representative
from imgdupe.db import init_db, temporary_image_ids
from imgdupe.review import _fetch_cluster_matches, generate_review


class ClusterVariableLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        dimensions = [(100, 100, 1), (200, 100, 1), (100, 200, 20), (200, 100, 20),
                      (None, 99999, 1000), (0, 99999, 1000), (99, 99, 99999), (150, 100, 2)]
        self.conn.executemany(
            "INSERT INTO images(id, path, size_bytes, mtime_ns, indexed_at, width, height) "
            "VALUES (?, ?, ?, 1, 'now', ?, ?)",
            [(image_id, str(self.root / f"missing-{image_id}.png"), size, width, height)
             for image_id, (width, height, size) in enumerate(dimensions, 1)],
        )
        self.conn.executemany(
            "INSERT INTO matches(image_id_a, image_id_b, score, decision, created_at) VALUES (?, ?, ?, ?, 'now')",
            [(1, 8, 89, "probable_duplicate"), (3, 4, 91, "strong_duplicate"),
             (2, 7, 95, "review"), (1, 2, 91, "exact_duplicate"),
             (8, 2, 87, "strong_duplicate"), (3, 3, 80, "review"),
             (1, 99, 100, "exact_duplicate"), (99, 100, 99, "review")],
        )
        self.conn.commit()
        self.members = list(range(1, 9))

    def lower_limit(self, limit):
        original = self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
        self.addCleanup(self.conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original)

    def temp_tables(self):
        return [row[0] for row in self.conn.execute("SELECT name FROM sqlite_temp_master WHERE type='table'")]

    def test_representative_keeps_area_size_and_lower_id_ties_at_small_limits(self):
        for limit in (10, 1):
            with self.subTest(limit=limit):
                self.lower_limit(limit)
                self.assertEqual(_choose_representative(self.conn, self.members * 3 + [99, 100]), 3)
                self.assertEqual(_choose_representative(self.conn, [4, 3, 2]), 3)
                self.assertEqual(_choose_representative(self.conn, [1, 7]), 1)
                self.assertEqual(_choose_representative(self.conn, [2, 3]), 3)
                self.assertEqual(self.temp_tables(), [])

    def test_matches_include_cross_group_pairs_once_and_keep_global_sql_order(self):
        placeholders = ",".join("?" for _ in self.members)
        legacy = (
            f"SELECT * FROM matches WHERE image_id_a IN ({placeholders}) "
            f"AND image_id_b IN ({placeholders}) ORDER BY score DESC, decision"
        )
        expected = [dict(row) for row in self.conn.execute(legacy, self.members * 2)]
        self.assertEqual([(row["image_id_a"], row["image_id_b"]) for row in expected],
                         [(2, 7), (1, 2), (3, 4), (1, 8), (8, 2), (3, 3)])
        for limit in (10, 1):
            with self.subTest(limit=limit):
                self.lower_limit(limit)
                with self.assertRaisesRegex(sqlite3.OperationalError, "too many SQL variables"):
                    # Force a fresh prepare: cached statements compiled before
                    # setlimit retain their original parameter allowance.
                    self.conn.execute(legacy + f" /* variable limit {limit} */", self.members * 2)
                actual = _fetch_cluster_matches(self.conn, self.members[::-1] * 2)
                self.assertEqual([dict(row) for row in actual], expected)
                self.assertEqual(self.temp_tables(), [])

    def test_missing_duplicate_null_and_empty_members_preserve_membership_semantics(self):
        self.lower_limit(1)
        for members in ([], [3], [999], [None, None]):
            with self.subTest(members=members):
                self.assertEqual(_fetch_cluster_matches(self.conn, members), [])
        self.assertEqual([(row["image_id_a"], row["image_id_b"])
                          for row in _fetch_cluster_matches(self.conn, [3, 3])], [(3, 3)])
        self.assertEqual([(row["image_id_a"], row["image_id_b"])
                          for row in _fetch_cluster_matches(self.conn, [99, 100, 99])], [(99, 100)])
        self.assertEqual(_choose_representative(self.conn, [None, 99, 3, 3]), 3)
        for members in ([], [99, 100], [None, None]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                _choose_representative(self.conn, members)
        self.assertEqual(self.temp_tables(), [])

    def test_review_export_works_with_one_bind_variable(self):
        self.conn.executemany(
            "INSERT INTO clusters(cluster_id, image_id, representative) VALUES (1, ?, ?)",
            [(image_id, int(image_id == 3)) for image_id in self.members],
        )
        self.conn.commit()
        self.lower_limit(1)
        with tempfile.TemporaryDirectory() as temporary:
            stats = generate_review(self.conn, Path(temporary), thumbnail_size=16)
            payload = json.loads((stats.out_dir / "clusters.json").read_text())["clusters"][0]
            self.assertEqual((stats.clusters, stats.images, stats.thumbnails), (1, 8, 0))
            self.assertEqual(payload["representative"], str(self.root / "missing-3.png"))
            self.assertEqual([(row["image_id_a"], row["image_id_b"]) for row in payload["matches"]],
                             [(2, 7), (1, 2), (3, 4), (1, 8), (8, 2), (3, 3)])
        self.assertEqual(self.temp_tables(), [])

    def test_small_cluster_work_does_not_grow_with_unrelated_images_and_matches(self):
        def measure(operation):
            ticks = 0

            def progress():
                nonlocal ticks
                ticks += 1
                return 0

            self.conn.set_progress_handler(progress, 100)
            try:
                result = operation()
            finally:
                self.conn.set_progress_handler(None, 0)
            return result, ticks

        operations = {
            "representative": lambda: _choose_representative(self.conn, [1, 2]),
            "matches": lambda: [dict(row) for row in _fetch_cluster_matches(self.conn, [1, 2])],
        }
        baseline = {name: measure(operation) for name, operation in operations.items()}
        self.conn.executemany(
            "INSERT INTO images(id, path, size_bytes, mtime_ns, indexed_at) VALUES (?, ?, 1, 1, 'now')",
            ((image_id, f"unrelated-{image_id}.png") for image_id in range(1000, 6001)),
        )
        self.conn.executemany(
            "INSERT INTO matches(image_id_a, image_id_b, score, decision, created_at) "
            "VALUES (?, ?, 90, 'review', 'now')",
            ((image_id, image_id + 1) for image_id in range(1000, 6000)),
        )
        self.conn.commit()
        self.lower_limit(1)
        for name, operation in operations.items():
            with self.subTest(operation=name):
                result, ticks = measure(operation)
                expected, baseline_ticks = baseline[name]
                self.assertEqual(result, expected)
                # Allow fixed temp-table/planner overhead, but reject a scan of
                # thousands of unrelated rows for every two-member cluster.
                self.assertLessEqual(ticks, baseline_ticks + 10)
                self.assertEqual(self.temp_tables(), [])

    def test_nested_membership_tables_are_independent_and_preserve_existing_temp_tables(self):
        self.conn.execute("CREATE TEMP TABLE caller_data(value)")
        self.lower_limit(1)
        with temporary_image_ids(self.conn, [1, 1, None, 2]) as outer:
            self.assertEqual([row[0] for row in self.conn.execute(f"SELECT image_id FROM {outer} ORDER BY image_id")], [1, 2])
            with temporary_image_ids(self.conn, [3, 4]) as inner:
                self.assertNotEqual(outer, inner)
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {inner}").fetchone()[0], 2)
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {outer}").fetchone()[0], 2)
            self.assertEqual(len(self.temp_tables()), 2)
        self.assertEqual(self.temp_tables(), ["caller_data"])

    def test_iteration_failure_drops_membership_without_committing_caller_work(self):
        self.conn.execute("UPDATE images SET size_bytes = 500 WHERE id = 1")

        def failed_members():
            yield 1
            raise RuntimeError("member list failed")

        with self.assertRaisesRegex(RuntimeError, "member list failed"):
            with temporary_image_ids(self.conn, failed_members()):
                self.fail("iteration should fail before the context body")
        self.assertEqual(self.temp_tables(), [])
        self.assertEqual(self.conn.execute("SELECT size_bytes FROM images WHERE id = 1").fetchone()[0], 500)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT size_bytes FROM images WHERE id = 1").fetchone()[0], 1)

    def test_rollback_after_completed_read_does_not_resurrect_temp_table(self):
        self.assertFalse(self.conn.in_transaction)
        with temporary_image_ids(self.conn, self.members) as membership:
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {membership}").fetchone()[0], 8)
        self.assertEqual(self.temp_tables(), [])
        self.conn.rollback()
        self.assertEqual(self.temp_tables(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 8)

    def test_query_failure_and_caller_rollback_leave_no_membership_table(self):
        self.conn.execute("DROP TABLE matches")
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            _fetch_cluster_matches(self.conn, self.members)
        self.assertEqual(self.temp_tables(), [])
        self.conn.rollback()
        self.conn.execute("UPDATE images SET size_bytes = 500 WHERE id = 1")
        with temporary_image_ids(self.conn, self.members):
            self.assertTrue(self.temp_tables())
            self.conn.rollback()
            self.assertEqual(self.temp_tables(), [])
        self.assertEqual(self.conn.execute("SELECT size_bytes FROM images WHERE id = 1").fetchone()[0], 1)
        self.assertEqual(self.temp_tables(), [])
