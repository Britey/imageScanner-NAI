from __future__ import annotations

import contextlib
import io
import os
import random
import runpy
import warnings
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from imgdupe import cli, scan, single_instance
from imgdupe.db import connect, init_db, is_crop_index_ready
from imgdupe.model_paths import load_model_paths, make_rule, path_key, save_model_paths
from imgdupe.single_instance import SingleInstanceLock


REPOSITORY = Path(__file__).resolve().parents[1]
TAG_LINE = re.compile(r"^matches=\s*(\d+) (\d+)x(\d+) (.+)$")
LOCK_HOLDER = (
    "import sys\n"
    "from imgdupe.single_instance import SingleInstanceLock\n"
    "lock = SingleInstanceLock()\n"
    "print('acquired' if lock.acquire() else 'busy', flush=True)\n"
    "sys.stdin.readline()\n"
    "lock.release()\n"
)


def write_prompt_image(path: Path, seed: int, prompt: str, model_hash: str = "a1b2c3d4") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = random.Random(seed).randbytes(128 * 128)
    image = Image.frombytes("L", (128, 128), pixels).convert("RGB")
    info = PngImagePlugin.PngInfo()
    info.add_text(
        "parameters",
        f"{prompt}\nNegative prompt: lowres\nSteps: 20, Sampler: Euler a, Model hash: {model_hash}",
    )
    image.save(path, pnginfo=info)
    return path


class CliCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.images = self.root / "images"
        self.images.mkdir()
        self.db = self.root / "index.sqlite"
        self.model_paths = self.root / "model_paths.ini"
        for target in (cli, scan):
            patcher = patch.object(target, "DEFAULT_MODEL_PATHS_PATH", self.model_paths)
            patcher.start()
            self.addCleanup(patcher.stop)
        lock_directory = patch.object(single_instance.tempfile, "gettempdir", return_value=str(self.root))
        lock_directory.start()
        self.addCleanup(lock_directory.stop)

    def run_cli(self, *arguments, expect=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([str(argument) for argument in arguments])
        self.assertEqual(code, expect, stderr.getvalue())
        return stdout.getvalue()

    def cli_error(self, *arguments):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_:
            cli.main([str(argument) for argument in arguments])
        self.assertEqual(exit_.exception.code, 2)
        return stderr.getvalue()

    def scan(self, *arguments):
        output = self.run_cli("scan", *arguments, "--db", self.db, "--workers", 1)
        return output.strip().splitlines()[-1]

    def tags(self, *arguments):
        lines = self.run_cli("tags", *arguments, "--db", self.db).strip().splitlines()
        results = [TAG_LINE.match(line) for line in lines[:-1]]
        self.assertTrue(all(results), lines)
        self.assertEqual(lines[-1], f"tag results: {len(results)}")
        return [Path(match.group(4)).name for match in results]

    def query_paths(self, *arguments):
        lines = self.run_cli("query", *arguments, "--db", self.db).strip().splitlines()
        return [Path(line.rsplit(" ", 1)[-1]).name for line in lines if not line.startswith("wrote ")]

    def test_scan_indexes_positive_prompt_tags_by_default(self):
        cat = write_prompt_image(self.images / "cat.png", 1, "cat, smile")
        write_prompt_image(self.images / "nested" / "dog.png", 2, "dog, smile")
        (self.images / "notes.txt").write_text("not an image")

        self.assertEqual(
            self.scan(self.images),
            "scan complete: seen=2 queued=2 indexed=2 moved=0 deleted=0 skipped=0 failed=0",
        )
        lines = self.run_cli("tags", "cat", "--db", self.db).splitlines()
        self.assertEqual(lines, [f"matches= 1 128x128 {cat}", "tag results: 1"])
        self.assertEqual(self.tags("lowres"), [])
        self.assertEqual(
            self.scan(self.images),
            "scan complete: seen=2 queued=0 indexed=0 moved=0 deleted=0 skipped=2 failed=0",
        )

    def test_tags_options_combine_filter_sort_and_page_results(self):
        write_prompt_image(self.images / "cat.png", 1, "cat, smile")
        write_prompt_image(self.images / "dog.png", 2, "dog, smile")
        self.scan(self.images)

        self.assertEqual(self.tags("smile", "--sort", "name-desc"), ["dog.png", "cat.png"])
        self.assertEqual(self.tags("smile", "--sort", "name-asc"), ["cat.png", "dog.png"])
        self.assertEqual(self.tags("smile", "--sort", "name-asc", "--limit", 1, "--offset", 1), ["dog.png"])
        self.assertEqual(self.tags("cat", "dog"), [])
        self.assertEqual(self.tags("cat", "dog", "--mode", "any", "--sort", "name-asc"), ["cat.png", "dog.png"])
        self.assertEqual(self.tags("cat | dog, smile", "--sort", "name-asc"), ["cat.png", "dog.png"])
        self.assertEqual(self.tags("smile", "--not-tags", "dog"), ["cat.png"])
        self.assertEqual(self.tags("--not-tags", "cat"), ["dog.png"])
        self.assertEqual(self.tags("smile", "--model-type", "sd", "--sort", "name-asc"), ["cat.png", "dog.png"])
        self.assertEqual(self.tags("smile", "--model-type", "nai4"), [])

    def test_searches_reject_nonpositive_limits_before_opening_database_and_empty_tag_queries(self):
        for command in (("tags", "cat"), ("query", self.root / "query.png")):
            for limit in (0, -1):
                with self.subTest(command=command[0], limit=limit):
                    self.assertIn("--limit must be greater than zero",
                                  self.cli_error(*command, "--db", self.db, "--limit", limit))
        self.assertFalse(self.db.exists())
        self.assertIn("requires at least one tag", self.cli_error("tags", "--db", self.db))

    def test_no_prompt_index_skips_prompts_until_a_normal_scan_backfills_them(self):
        write_prompt_image(self.images / "cat.png", 1, "cat, smile")

        self.assertIn("indexed=1", self.scan(self.images, "--no-prompt-index"))
        self.assertEqual(self.tags("cat"), [])
        self.assertEqual(
            self.scan(self.images),
            "scan complete: seen=1 queued=1 indexed=1 moved=0 deleted=0 skipped=0 failed=0",
        )
        self.assertEqual(self.tags("cat"), ["cat.png"])

    def test_reindex_prompts_rereads_metadata_of_unchanged_files(self):
        image = write_prompt_image(self.images / "cat.png", 1, "cat, smile")
        self.scan(self.images)
        before = image.stat()
        write_prompt_image(image, 1, "cat, frown")
        os.utime(image, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(image.stat().st_size, before.st_size)

        self.scan(self.images)
        self.assertEqual((self.tags("smile"), self.tags("frown")), (["cat.png"], []))
        self.scan(self.images, "--reindex-prompts")
        self.assertEqual((self.tags("smile"), self.tags("frown")), ([], ["cat.png"]))

    def test_delete_missing_removes_only_records_of_missing_files(self):
        cat = write_prompt_image(self.images / "cat.png", 1, "cat, smile")
        dog = write_prompt_image(self.images / "dog.png", 2, "dog, smile")
        self.scan(self.images)
        dog.unlink()

        self.assertIn("deleted=0", self.scan(self.images))
        self.assertIn("deleted=1", self.scan(self.images, "--delete-missing"))
        self.assertEqual(self.tags("smile"), ["cat.png"])
        self.assertTrue(cat.is_file())
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual([row["path"] for row in conn.execute("SELECT path FROM images")], [str(cat)])

    def test_crop_index_option_builds_the_optional_crop_index(self):
        write_prompt_image(self.images / "cat.png", 1, "cat")
        self.scan(self.images)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertFalse(is_crop_index_ready(conn))
        self.scan(self.images, "--crop-index")
        self.assertTrue(is_crop_index_ready(conn))

    def test_model_label_is_saved_once_for_unconfigured_folders_and_nearest_rule_wins(self):
        special = self.images / "special"
        write_prompt_image(special / "bird.png", 3, "bird")
        cat = write_prompt_image(self.images / "cat.png", 1, "cat")
        dog = write_prompt_image(self.images / "dog.png", 2, "dog")
        save_model_paths(self.model_paths, [make_rule(special, "special")])

        self.scan(special, cat, dog, "--model-label", "noob75")

        labels = {path_key(rule.path): rule.label for rule in load_model_paths(self.model_paths).rules}
        self.assertEqual(labels, {path_key(special): "special", path_key(self.images): "noob75"})
        self.assertEqual(self.tags("bird", "--model-type", "special"), ["bird.png"])
        self.assertEqual(self.tags("bird", "--model-type", "noob75"), [])
        self.assertEqual(self.tags("cat | dog", "--model-type", "noob75", "--sort", "name-asc"),
                         ["cat.png", "dog.png"])
        self.assertEqual(self.tags("cat", "--model-type", "sd"), [])

        saved = self.model_paths.read_bytes()
        self.scan(special, cat, "--model-label", "other")
        self.assertEqual(self.model_paths.read_bytes(), saved)

    def test_model_label_refuses_to_extend_an_invalid_model_paths_file(self):
        write_prompt_image(self.images / "cat.png", 1, "cat")
        self.model_paths.write_text(f"[rule.1]\npath = {self.images}\nlabel = a|b\n", encoding="utf-8")
        original = self.model_paths.read_bytes()

        error = self.cli_error("scan", self.images, "--db", self.db, "--model-label", "noob75")

        self.assertIn("model_paths.ini contains invalid rules", error)
        self.assertEqual(self.model_paths.read_bytes(), original)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_query_lists_visual_matches_excluding_itself_and_optionally_exact_copies(self):
        cat = write_prompt_image(self.images / "cat.png", 1, "cat")
        write_prompt_image(self.images / "dog.png", 2, "dog")
        self.scan(self.images)
        query = self.root / "query.png"
        shutil.copyfile(cat, query)

        output = self.run_cli("query", query, "--db", self.db, "--min-score", 90)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("exact_duplicate", output)
        self.assertTrue(output.rstrip().endswith(str(cat)))
        self.assertEqual(self.query_paths(query, "--min-score", 90, "--hide-exact"), [])
        self.assertEqual(self.query_paths(cat, "--min-score", 90), [])

    def test_query_html_report_is_written_for_tryhard_search(self):
        cat = write_prompt_image(self.images / "cat.png", 1, "cat")
        self.scan(self.images)
        query = self.root / "query & copy.png"
        shutil.copyfile(cat, query)
        report = self.root / "reports" / "result.html"

        output = self.run_cli("query", query, "--db", self.db, "--tryhard", "--html", report)

        self.assertIn(f"wrote {report}", output)
        document = report.read_text(encoding="utf-8")
        self.assertIn("query &amp; copy.png", document)
        self.assertIn(f'<td class="path">{cat}</td>', document)

    def test_cluster_and_review_group_duplicate_images(self):
        cat = write_prompt_image(self.images / "cat.png", 1, "cat")
        (self.images / "copies").mkdir()
        shutil.copyfile(cat, self.images / "copies" / "cat-copy.png")
        write_prompt_image(self.images / "dog.png", 2, "dog")
        self.scan(self.images)

        self.assertIn("clusters=1 clustered_images=2", self.run_cli("cluster", "--db", self.db))
        out = self.root / "review"
        output = self.run_cli("review", "--db", self.db, "--out", out, "--thumbnail-size", 32)

        self.assertIn(f"review complete: clusters=1 images=2 thumbnails=2 out={out}", output)
        self.assertTrue((out / "index.html").is_file())
        thumbnails = sorted((out / "thumbnails").iterdir())
        self.assertEqual(len(thumbnails), 2)
        with Image.open(thumbnails[0]) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 32)
        self.assertIn("clusters=0", self.run_cli("cluster", "--db", self.db, "--min-score", 101))

    def test_failures_lists_files_that_could_not_be_decoded(self):
        write_prompt_image(self.images / "cat.png", 1, "cat")
        broken = self.images / "broken.png"
        broken.write_bytes(b"this is not a png")

        self.assertIn("indexed=1 moved=0 deleted=0 skipped=0 failed=1", self.scan(self.images))
        lines = self.run_cli("failures", "--db", self.db).splitlines()

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(f"{broken}\t"), lines)
        self.assertEqual(lines[1], "failures: 1")

    def test_failures_combines_decode_and_prompt_errors_and_ignores_missing_metadata(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        init_db(conn)
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

        self.assertEqual(self.run_cli("failures", "--db", self.db).splitlines(), [
            "1.png\tdecode; prompt: metadata", "2.png\tdecode", "3.png\tprompt: metadata", "failures: 3",
        ])
        self.assertEqual(self.run_cli("failures", "--db", self.db, "--limit", 1).splitlines(),
                         ["1.png\tdecode; prompt: metadata", "failures: 1"])

    def test_busy_wal_checkpoint_on_exit_is_reported(self):
        with patch.object(cli, "checkpoint_database", return_value=(1, 4, 2)):
            output = self.run_cli("failures", "--db", self.db)
        self.assertIn("WAL checkpoint remained busy: (1, 4, 2)", output)
        self.assertNotIn("WAL checkpoint", self.run_cli("failures", "--db", self.db))

    def test_serve_holds_the_single_instance_lock_until_the_server_stops(self):
        observed = {}

        def fake_serve(conn, db_path, **options):
            observed.update(db=db_path, options=options)
            observed["images"] = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            second = SingleInstanceLock()
            observed["second_launch"] = second.acquire()
            second.release()

        with patch.object(cli, "serve", fake_serve):
            self.run_cli("serve", "--db", self.db, "--port", 9999, "--min-score", 60, "--open-browser")

        self.assertEqual(observed["db"], self.db)
        self.assertEqual(observed["images"], 0)
        self.assertFalse(observed["second_launch"])
        self.assertEqual(observed["options"], dict(
            host="127.0.0.1", port=9999, limit=100, min_score=60.0, thumbnail_size=256, open_browser=True,
        ))
        later = SingleInstanceLock()
        self.addCleanup(later.release)
        self.assertTrue(later.acquire())

    def test_second_serve_launch_from_another_process_exits_before_opening_database(self):
        elsewhere = self.root / "another-copy"
        elsewhere.mkdir()
        environment = dict(os.environ, TMPDIR=str(self.root), PYTHONPATH=str(REPOSITORY))
        holder = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER], cwd=elsewhere, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "acquired")

        with patch.object(cli, "serve") as serve:
            output = self.run_cli("serve", "--db", self.db, expect=1)
            self.assertIn("already running", output)
            serve.assert_not_called()
            self.assertFalse(self.db.exists())

            holder.communicate("\n", timeout=10)
            self.run_cli("serve", "--db", self.db)
            serve.assert_called_once()

    def test_serve_releases_the_lock_when_the_database_cannot_be_opened(self):
        unusable = self.root / "database-directory"
        unusable.mkdir()
        with patch.object(cli, "serve") as serve, self.assertRaises(sqlite3.Error):
            cli.main(["serve", "--db", str(unusable)])
        serve.assert_not_called()
        later = SingleInstanceLock()
        self.addCleanup(later.release)
        self.assertTrue(later.acquire())


    def test_module_entrypoint_prints_help_for_all_commands(self):
        with patch('sys.argv', ['imgdupe', '--help']), contextlib.redirect_stdout(io.StringIO()) as output, \
                warnings.catch_warnings(), self.assertRaises(SystemExit) as exit_:
            warnings.simplefilter('ignore', RuntimeWarning)
            runpy.run_module('imgdupe.cli', run_name='__main__')
        self.assertEqual(exit_.exception.code, 0)
        for command in ('scan', 'query', 'tags', 'cluster', 'review', 'serve', 'failures'):
            self.assertIn(command, output.getvalue())


if __name__ == "__main__":
    unittest.main()
