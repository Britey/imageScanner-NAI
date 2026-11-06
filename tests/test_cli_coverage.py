from __future__ import annotations

import argparse
import contextlib
import io
import runpy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imgdupe import cli
from imgdupe.model_paths import ConfigIssue, ModelPathConfig, build_config, make_rule
from imgdupe.scan import ScanStats


class CliCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite"

    def run_cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main([*map(str, arguments), "--db", str(self.db)])
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_scan_options_and_default_values(self):
        with patch.object(cli, "scan_roots", return_value=ScanStats()) as scan, patch.object(
            cli, "load_model_paths", return_value=ModelPathConfig()
        ):
            self.assertIn("scan complete:", self.run_cli("scan", self.root))
            self.assertEqual(scan.call_args.kwargs["config"].workers, 4)
            self.assertEqual(scan.call_args.args[1], [self.root])
            self.assertTrue(scan.call_args.kwargs["config"].prompt_index)
            self.run_cli("scan", self.root, "--workers", 2, "--batch-size", 3,
                         "--crop-index", "--no-prompt-index", "--stealth-prompt",
                         "--reindex-prompts", "--delete-missing")
            config = scan.call_args.kwargs["config"]
            self.assertEqual((config.workers, config.batch_size), (2, 3))
            self.assertFalse(config.prompt_index)
            self.assertTrue(all((config.crop_index, config.stealth_prompt,
                                 config.reindex_prompts, config.delete_missing)))

    def test_scan_adds_labels_once_per_unconfigured_directory(self):
        child = self.root / "child"
        child.mkdir()
        existing = build_config((make_rule(child, "existing"),))
        with patch.object(cli, "load_model_paths", return_value=existing), patch.object(
            cli, "save_model_paths"
        ) as save, patch.object(cli, "scan_roots", return_value=ScanStats()) as scan:
            self.run_cli("scan", self.root, self.root / "image.png", child,
                         "--model-label", "custom")
            saved = save.call_args.args[1]
            self.assertEqual(len(saved.rules), 2)
            self.assertEqual(saved.resolve(self.root).label, "custom")
            self.assertEqual(saved.resolve(child).label, "existing")
            self.assertEqual(scan.call_args.kwargs["model_paths"], saved)
            save.reset_mock()
            self.run_cli("scan", child, "--model-label", "custom")
            save.assert_not_called()

    def test_scan_rejects_invalid_configuration_before_writing(self):
        bad = ModelPathConfig(issues=(ConfigIssue("rule.1", "invalid"),))
        with patch.object(cli, "load_model_paths", return_value=bad), patch.object(
            cli, "save_model_paths"
        ) as save, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            cli.main(["scan", str(self.root), "--db", str(self.db), "--model-label", "A"])
        self.assertEqual(exc.exception.code, 2)
        save.assert_not_called()

    def test_tags_and_query_forward_arguments_and_print_results(self):
        tag_result = SimpleNamespace(matched_count=2, image_row={"width": 3, "height": 4, "path": "x.png"})
        with patch.object(cli, "query_prompt_tags", return_value=[tag_result]) as query:
            output = self.run_cli("tags", "cat", "--not-tags", "dog", "--mode", "any",
                                  "--sort", "name-desc", "--limit", 7, "--offset", 2,
                                  "--model-type", "nai4")
            self.assertIn("3x4 x.png", output)
            self.assertIn("tag results: 1", output)
            self.assertEqual(query.call_args.args[1], ["cat"])
            self.assertEqual(query.call_args.kwargs, dict(negative_text_or_tags=["dog"],
                match_mode="any", limit=7, offset=2, sort_by="name_desc", model_type="nai4"))
        score = SimpleNamespace(score=95.0, decision="strong", phash_dist=1, whash_dist=2,
                                dhash_dist=3, grid_match_count=9, crop_min_dist=None)
        match = ({"path": "match.png"}, SimpleNamespace(total_hits=10), score)
        with patch.object(cli, "query_image", return_value=[match]) as query, patch.object(
            cli, "write_query_html"
        ) as write:
            output = self.run_cli("query", self.root / "query.png", "--hide-exact", "--tryhard",
                                  "--limit", 8, "--min-score", 60, "--html", self.root / "out.html")
            self.assertIn("match.png", output)
            self.assertIn("wrote", output)
            self.assertEqual(query.call_args.kwargs, dict(limit=8, min_score=60., include_exact=False, tryhard=True))
            query_path = (self.root / "query.png").resolve()
            self.assertEqual(query.call_args.args[1], query_path)
            write.assert_called_once_with(self.root / "out.html", query_path, [match])
            write.reset_mock()
            self.run_cli("query", self.root / "query.png")
            write.assert_not_called()

    def test_tags_requires_a_positive_or_negative_query(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            cli.main(["tags", "--db", str(self.db)])
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("tag results: 0", self.run_cli("tags", "--not-tags", "dog"))

    def test_cluster_review_and_failures(self):
        stats = SimpleNamespace(images=3, candidate_pairs=2, scored_pairs=2,
                                stored_matches=1, clusters=1, clustered_images=2)
        with patch.object(cli, "build_clusters", return_value=stats) as cluster:
            self.assertIn("clusters=1", self.run_cli("cluster", "--min-score", 80))
            self.assertEqual(cluster.call_args.kwargs["min_score"], 80.)
        stats = SimpleNamespace(clusters=1, images=2, thumbnails=2, out_dir=self.root)
        with patch.object(cli, "generate_review", return_value=stats) as review:
            self.assertIn("thumbnails=2", self.run_cli("review", "--out", self.root,
                                                    "--thumbnail-size", 64))
            self.assertEqual(review.call_args.kwargs["thumbnail_size"], 64)
            self.assertEqual(review.call_args.args[1], self.root)
        with patch.object(cli, "iter_failures", return_value=[dict(path="bad.png", decode_error="bad")]):
            output = self.run_cli("failures")
            self.assertIn("bad.png\tbad", output)
            self.assertIn("failures: 1", output)

    def test_failures_reports_database_errors_and_ignores_missing_metadata(self):
        conn = cli.connect(self.db)
        self.addCleanup(conn.close)
        cli.init_db(conn)
        for index, decode_error, prompt_error in (
            (1, "decode", "metadata"), (2, "decode", None),
            (3, None, "metadata"), (4, None, "no_metadata"),
            (5, None, "no_prompt"), (6, None, None),
        ):
            conn.execute("INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at,decode_error) "
                         "VALUES(?,?,1,1,'now',?)", (index, f"{index}.png", decode_error))
            conn.execute("INSERT INTO image_prompts(image_id,prompt_indexed_at,prompt_error) "
                         "VALUES(?,'now',?)", (index, prompt_error))
        conn.commit()
        output = self.run_cli("failures")
        self.assertIn("1.png\tdecode; prompt: metadata", output)
        self.assertIn("2.png\tdecode", output)
        self.assertIn("3.png\tprompt: metadata", output)
        self.assertIn("failures: 3", output)
        self.assertNotIn("4.png", output)
        self.assertIn("failures: 1", self.run_cli("failures", "--limit", 1))

    def test_serve_lock_lifecycle_and_busy_checkpoint(self):
        lock = Mock()
        lock.acquire.return_value = True
        with patch.object(cli, "SingleInstanceLock", return_value=lock), patch.object(
            cli, "serve"
        ) as serve, patch.object(cli, "checkpoint_database", return_value=(1, 2, 3)):
            self.assertIn("WAL checkpoint remained busy", self.run_cli("serve", "--open-browser"))
            self.assertTrue(serve.call_args.kwargs["open_browser"])
        lock.release.assert_called_once()
        lock.reset_mock()
        lock.acquire.return_value = False
        with patch.object(cli, "SingleInstanceLock", return_value=lock), patch.object(
            cli, "notify_already_running"
        ) as notify, patch.object(cli, "connect") as connect:
            self.assertEqual(cli.main(["serve", "--db", str(self.db)]), 1)
            notify.assert_called_once()
            connect.assert_not_called()
            lock.release.assert_not_called()

    def test_connection_and_command_errors_release_resources(self):
        lock = Mock()
        with patch.object(cli, "SingleInstanceLock", return_value=lock), patch.object(
            cli, "connect", side_effect=OSError("offline")
        ), self.assertRaisesRegex(OSError, "offline"):
            cli.main(["serve", "--db", str(self.db)])
        lock.release.assert_called_once()
        conn = Mock()
        with patch.object(cli, "connect", return_value=conn), patch.object(
            cli, "init_db", side_effect=sqlite3.DatabaseError("broken")
        ), patch.object(cli, "checkpoint_database", return_value=None), self.assertRaises(sqlite3.DatabaseError):
            cli.main(["failures", "--db", str(self.db)])
        conn.close.assert_called_once()

    def test_unknown_internal_command_errors(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            cli._run_command(SimpleNamespace(command="unknown"), argparse.ArgumentParser(), Mock())
        self.assertEqual(exc.exception.code, 2)

    def test_module_entrypoint(self):
        # Exercise the actual `python -m` guard in this process so coverage sees it.
        with patch("sys.argv", ["imgdupe", "--help"]), contextlib.redirect_stdout(io.StringIO()), \
             __import__("warnings").catch_warnings(), self.assertRaises(SystemExit) as exc:
            __import__("warnings").simplefilter("ignore", RuntimeWarning)
            runpy.run_module("imgdupe.cli", run_name="__main__")
        self.assertEqual(exc.exception.code, 0)
