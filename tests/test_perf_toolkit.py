from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import unittest

from imgdupe.bands import GRID_HASH_TYPES, WHOLE_HASH_TYPES, split_bands
from imgdupe.db import decode_prompt_metadata, init_db
from imgdupe.query import query_prompt_tags
from scripts.perf_toolkit.fixture import MARKER_KIND, MARKER_NAME, generate_fixture
from scripts.perf_toolkit.runner import (
    OWNER_FILE,
    cleanup_workspace,
    make_workspace,
    run_command,
    summarize_samples,
)


class PerformanceFixtureTests(unittest.TestCase):
    def test_tiny_fixture_matches_manifest_and_application_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture"
            with contextlib.redirect_stdout(io.StringIO()):
                manifest = generate_fixture(fixture, 3, seed=51, metadata_bytes=256)
            marker = json.loads((fixture / MARKER_NAME).read_text())
            self.assertEqual(marker["kind"], MARKER_KIND)
            self.assertEqual(marker["work_dir"], str(fixture))
            self.assertEqual(json.loads((fixture / "fixture.json").read_text()), manifest)
            self.assertEqual(manifest["real_image_count"], 3)
            self.assertTrue(all(Path(path).is_file() for path in manifest["image_paths"]))
            self.assertTrue(Path(manifest["query_image"]).is_file())
            self.assertEqual(manifest["sqlite_bytes"], Path(manifest["db_path"]).stat().st_size)

            conn = sqlite3.connect(manifest["db_path"])
            conn.row_factory = sqlite3.Row
            self.addCleanup(conn.close)
            for table, expected in manifest["table_counts"].items():
                with self.subTest(table=table):
                    # table names come only from this generator's schema manifest.
                    self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], expected)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hash_bands").fetchone()[0], 3 * 84)
            for row in conn.execute("SELECT * FROM hashes"):
                expected_bands = {
                    (name, index, value)
                    for name in WHOLE_HASH_TYPES + GRID_HASH_TYPES
                    for index, value in split_bands(row[name])
                }
                actual_bands = {
                    tuple(band)
                    for band in conn.execute(
                        "SELECT hash_type, band_index, band_value FROM hash_bands WHERE image_id = ?",
                        (row["image_id"],),
                    )
                }
                self.assertEqual(actual_bands, expected_bands)
            for row in conn.execute("SELECT prompt, raw_metadata, raw_metadata_codec FROM image_prompts"):
                decoded = decode_prompt_metadata(row["raw_metadata"], row["raw_metadata_codec"])
                self.assertEqual(json.loads(decoded)["prompt"], row["prompt"])
            self.assertEqual(len(query_prompt_tags(conn, "common")), 3)
            rare = query_prompt_tags(conn, "common, rare")
            self.assertEqual([result.image_row["id"] for result in rare], [1])

            # Deferred creation must not leave a benchmark schema with fewer
            # indexes than an ordinary production database.
            baseline = sqlite3.connect(":memory:")
            baseline.row_factory = sqlite3.Row
            self.addCleanup(baseline.close)
            init_db(baseline)
            index_sql = "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            self.assertEqual({tuple(row) for row in conn.execute(index_sql)},
                             {tuple(row) for row in baseline.execute(index_sql)})
            conn.close()
            baseline.close()

    def test_fixture_refuses_overwrite_without_touching_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "existing"
            fixture.mkdir()
            sentinel = fixture / "images.sqlite"
            sentinel.write_bytes(b"user database must survive")
            with self.assertRaisesRegex(ValueError, "must be empty"):
                generate_fixture(fixture, 2)
            self.assertEqual(sentinel.read_bytes(), b"user database must survive")
            self.assertEqual(list(fixture.iterdir()), [sentinel])

    def test_invalid_fixture_settings_do_not_create_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "invalid"
            for kwargs in ({"image_count": 1}, {"image_count": 2, "tags_per_image": 1},
                           {"image_count": 2, "metadata_bytes": -1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    generate_fixture(fixture, **kwargs)
                self.assertFalse(fixture.exists())


class PerformanceRunnerSafetyTests(unittest.TestCase):
    def test_cleanup_requires_correct_token_and_exact_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            workspace, token = make_workspace(parent)
            sentinel = workspace / "retained.sqlite"
            sentinel.write_bytes(b"fixture")
            sibling = parent / "user-file"
            sibling.write_text("must survive")
            with self.assertRaisesRegex(ValueError, "ownership"):
                cleanup_workspace(workspace, "wrong-token")
            self.assertTrue(sentinel.exists())

            marker = workspace / OWNER_FILE
            correct = marker.read_text()
            marker.write_text(json.dumps({"token": token, "path": str(parent)}))
            with self.assertRaisesRegex(ValueError, "ownership"):
                cleanup_workspace(workspace, token)
            self.assertTrue(sentinel.exists())
            marker.write_text(correct)
            cleanup_workspace(workspace, token)
            self.assertFalse(workspace.exists())
            self.assertEqual(sibling.read_text(), "must survive")

    @unittest.skipUnless(os.name == "posix", "symlink setup requires POSIX permissions")
    def test_fixture_creation_and_cleanup_reject_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            workspace, token = make_workspace(parent)
            link = parent / "fixture-link"
            link.symlink_to(workspace, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                cleanup_workspace(link, token)
            with self.assertRaisesRegex(ValueError, "symlink"):
                generate_fixture(link, 2)
            self.assertTrue(workspace.is_dir())
            self.assertTrue((workspace / OWNER_FILE).is_file())
            cleanup_workspace(workspace, token)

    @unittest.skipUnless(os.name == "posix", "process existence assertion uses POSIX signals")
    def test_timeout_terminates_running_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "worker.pid"
            program = (
                "import os, pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "print('worker started', flush=True); time.sleep(60)"
            )
            result = run_command([sys.executable, "-c", program, str(pid_path)],
                                 root / "timeout.log", timeout=1.0)
            self.assertEqual(result["status"], "timeout")
            self.assertTrue(pid_path.is_file(), "worker must actually start before timeout")
            self.assertIn("worker started", (root / "timeout.log").read_text())
            pid = int(pid_path.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "descendant-state assertion requires Linux /proc")
    def test_timeout_kills_descendant_that_ignores_sigterm_after_leader_exits(self) -> None:
        def process_state(pid: int) -> str | None:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except FileNotFoundError:
                return None
            return stat.rsplit(")", 1)[1].split()[0]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leader_pid_path = root / "leader.pid"
            child_pid_path = root / "descendant.pid"
            child_program = (
                "import os, pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "print('descendant ignoring SIGTERM', flush=True); time.sleep(60)"
            )
            leader_program = (
                "import os, pathlib, subprocess, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]]); "
                "time.sleep(60)"
            )
            try:
                result = run_command(
                    [sys.executable, "-c", leader_program, str(leader_pid_path),
                     str(child_pid_path), child_program],
                    root / "descendant-timeout.log", timeout=1.0,
                )
                self.assertEqual(result["status"], "timeout")
                self.assertTrue(child_pid_path.is_file(), "descendant must install its handler and start")
                self.assertIn("descendant ignoring SIGTERM", (root / "descendant-timeout.log").read_text())
                leader_pid = int(leader_pid_path.read_text())
                child_pid = int(child_pid_path.read_text())
                self.assertIsNone(process_state(leader_pid), "leader must be reaped")
                # SIGKILL delivery is asynchronous. Orphan reaping also depends
                # on the machine's init process, so a nonexecuting zombie is OK.
                deadline = time.monotonic() + 1.0
                state = process_state(child_pid)
                while state not in (None, "Z") and time.monotonic() < deadline:
                    time.sleep(.01)
                    state = process_state(child_pid)
                self.assertIn(state, (None, "Z"), "timed-out descendant must not remain executing")
            finally:
                # A regression in the code under test must not leak this test's
                # sleeping process into subsequent benchmarks.
                for path in (child_pid_path, leader_pid_path):
                    if path.exists():
                        pid = int(path.read_text())
                        if process_state(pid) not in (None, "Z"):
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(pid, signal.SIGKILL)

    def test_summary_excludes_errors_timeouts_and_missing_measurements(self) -> None:
        samples = [
            {"status": "error", "wall_seconds": 100.0, "peak_rss_mib": 5000},
            {"status": "ok", "wall_seconds": 2.0, "peak_rss_mib": 20},
            {"status": "timeout", "wall_seconds": 90.0, "peak_rss_mib": 4000},
            {"status": "ok", "wall_seconds": 4.0, "peak_rss_mib": 30},
            {"status": "ok"},
        ]
        summary = summarize_samples(samples)
        self.assertEqual(summary["sample_count"], 5)
        self.assertEqual(summary["successful_samples"], 2)
        self.assertEqual(summary["median_seconds"], 3.0)
        self.assertEqual(summary["min_seconds"], 2.0)
        self.assertEqual(summary["max_seconds"], 4.0)
        self.assertEqual(summary["first_seconds"], 2.0)
        self.assertEqual(summary["max_peak_rss_mib"], 30)
        empty = summarize_samples([{"status": "error", "wall_seconds": 999}])
        self.assertEqual(empty["successful_samples"], 0)
        self.assertNotIn("median_seconds", empty)


if __name__ == "__main__":
    unittest.main()
