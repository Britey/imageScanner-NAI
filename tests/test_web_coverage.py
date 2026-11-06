from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from email.message import Message

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe import scan as scan_module, web
from imgdupe.config import ScanConfig
from imgdupe.db import init_db
from imgdupe.match import PairScore
from imgdupe.model_paths import build_config, make_rule
from imgdupe.query import PromptSearchResult
from imgdupe.scan import ScanStats
from imgdupe.settings import AppSettings


class WebFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.state = web.WebState(
            conn=self.conn,
            db_path=self.root / "images.sqlite",
            limit=10,
            min_score=55,
            thumbnail_size=32,
            work_dir=self.root / "work",
            thumbs_dir=self.root / "thumbs",
            thumbnail_cache_limit=2,
            disable_advanced_options=False,
        )
        self.handler_type = web._make_handler(self.state)

    def image(self, image_id=1, *, prompt=None, error=None):
        path = self.root / f"image-{image_id}.png"
        Image.new("RGB", (40, 24), "red").save(path)
        self.conn.execute(
            "INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at,width,height,decode_error) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (image_id, str(path), path.stat().st_size, 123, "now", 40, 24, error),
        )
        self.conn.execute(
            "INSERT INTO image_prompts(image_id,prompt,prompt_indexed_at) VALUES(?,?,?)",
            (image_id, prompt, "now"),
        )
        return path

    def request(
        self, method="GET", path="/", fields=None, *, body=None, content_type=None
    ):
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.client_address = ("127.0.0.1", 1234)
        handler.close_connection = True
        if body is None:
            body = urlencode(
                {"csrf_token": self.state.csrf_token, **(fields or {})}
            ).encode()
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["Content-Type"] = (
            content_type or "application/x-www-form-urlencoded"
        )
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.connection = Mock()
        handler.connection.getsockname.return_value = ("127.0.0.1", 8765)
        handler._header_timer = Mock()
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        return handler

    def response(self, handler):
        getattr(handler, "do_" + handler.command)()
        header, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        status = int(header.split(b" ", 2)[1])
        return status, header.decode(), body.decode("utf-8", "replace")

    def scan_fields(self, **changes):
        return {"roots": str(self.root), "lang": "en", "prompt_index": "1", **changes}

    def scan_request(self):
        return web._scan_request_from_fields(self.scan_fields())


class WebStateTests(WebFixture):
    def test_scan_thread_lifecycle_and_busy_rejection(self):
        request = self.scan_request()
        thread = Mock()
        with patch.object(
            web.threading, "Thread", return_value=thread
        ) as create_thread:
            self.assertTrue(self.state.start_scan(request))
            self.assertFalse(self.state.start_scan(request))
        create_thread.assert_called_once_with(
            target=web._run_scan_job,
            args=(self.state, request),
            name="imgdupe-web-scan",
            daemon=False,
        )
        self.assertEqual(self.state.get_scan_status().state, "running")
        thread.start.assert_called_once()
        thread.is_alive.return_value = True
        self.state.wait_for_scan()
        thread.join.assert_called_once()
        thread.is_alive.return_value = False
        self.state.wait_for_scan()
        self.state.scan_thread = None
        self.state.wait_for_scan()

    def test_thumbnail_counts_cleanup_and_failures(self):
        for name in ["a.jpg", "b.jpg", "c.jpg"]:
            (self.state.thumbs_dir / name).write_bytes(b"jpg")
            self.state.thumbnail_created()
        self.assertEqual(self.state.thumbnail_cache_count, 2)
        self.assertEqual(len(list(self.state.thumbs_dir.glob("*.jpg"))), 2)
        self.state.thumbnail_removed()
        self.assertEqual(self.state.thumbnail_cache_count, 1)
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            self.assertEqual(self.state.clear_thumbnail_cache(), 0)
        self.assertEqual(self.state.clear_thumbnail_cache(), 2)
        self.state.thumbnail_removed()
        self.assertEqual(self.state.thumbnail_cache_count, 0)

    def test_background_scan_success_and_failure(self):
        request = web.ScanRequest(
            roots=(self.root,),
            workers=3,
            crop_index=True,
            prompt_index=True,
            stealth_prompt=True,
            reindex_prompts=True,
            delete_missing=True,
            lang="en",
        )
        connection = Mock()
        stats = ScanStats()
        with (
            patch.object(web, "connect", return_value=connection),
            patch.object(web, "init_db"),
            patch.object(web, "scan_roots", return_value=stats) as scan,
        ):
            web._run_scan_job(self.state, request)
        status = self.state.get_scan_status()
        self.assertEqual(status.state, "completed")
        self.assertIs(status.stats, stats)
        scan.assert_called_once_with(
            connection,
            [self.root],
            config=ScanConfig(
                workers=3,
                crop_index=True,
                prompt_index=True,
                stealth_prompt=True,
                reindex_prompts=True,
                delete_missing=True,
            ),
        )
        self.assertEqual(status.roots, (str(self.root),))
        self.assertIsNotNone(status.started_at)
        self.assertIsNotNone(status.finished_at)
        connection.close.assert_called_once()
        self.state.finish_scan(
            web.WebScanStatus(state="running", started_at="original start")
        )
        with patch.object(web, "connect", side_effect=OSError("unavailable")):
            web._run_scan_job(self.state, request)
        status = self.state.get_scan_status()
        self.assertEqual(status.state, "failed")
        self.assertEqual(status.started_at, "original start")
        self.assertIn("OSError: unavailable", status.error)
        with (
            patch.object(web, "connect", return_value=connection),
            patch.object(web, "init_db", side_effect=ValueError("schema")),
        ):
            web._run_scan_job(self.state, request)
        self.assertEqual(connection.close.call_count, 2)

    def test_real_background_scan_persists_hashes_and_prompt_tags(self):
        source_dir = self.root / "scan-source"
        source_dir.mkdir()
        source = source_dir / "cat.png"
        metadata = PngInfo()
        metadata.add_text(
            "parameters",
            "red cat, blue eyes\nNegative prompt: blurry\nSteps: 20, Seed: 123, Model hash: ABCDEF12",
        )
        Image.new("RGB", (128, 96), "red").save(source, pnginfo=metadata)
        request = web.ScanRequest(
            roots=(source_dir,),
            workers=1,
            crop_index=False,
            prompt_index=True,
            stealth_prompt=False,
            reindex_prompts=False,
            delete_missing=False,
            lang="en",
        )
        # Keep actual threading, SQLite, image hashing, and metadata extraction.
        # Only the configuration location is isolated from the user's settings.
        with patch.object(
            scan_module, "DEFAULT_MODEL_PATHS_PATH", self.root / "model-paths.ini"
        ):
            self.assertTrue(self.state.start_scan(request))
            self.state.scan_thread.join(timeout=15)
            self.assertFalse(
                self.state.scan_thread.is_alive(), "background scan did not finish"
            )
        status = self.state.get_scan_status()
        self.assertEqual(status.state, "completed", status.error)
        self.assertEqual(
            (status.stats.seen, status.stats.indexed, status.stats.failed), (1, 1, 0)
        )
        with contextlib.closing(sqlite3.connect(self.state.db_path)) as indexed:
            row = indexed.execute(
                "SELECT i.path, i.width, i.height, length(i.sha256), length(h.phash256), "
                "ip.prompt, ip.prompt_error FROM images i "
                "JOIN hashes h ON h.image_id=i.id "
                "JOIN image_prompts ip ON ip.image_id=i.id"
            ).fetchone()
            self.assertEqual(
                row,
                (str(source.resolve()), 128, 96, 32, 32, "red cat, blue eyes", None),
            )
            tags = indexed.execute(
                "SELECT tag_norm FROM prompt_tags WHERE tag_kind='prompt' ORDER BY tag_norm"
            ).fetchall()
            self.assertEqual(tags, [("blue eyes",), ("red cat",)])

    def test_serve_closes_server_and_removes_workdir(self):
        for browser, checkpoint in [
            (False, None),
            (True, (1, 2, 3)),
            (False, (0, 0, 0)),
        ]:
            with self.subTest(browser=browser, checkpoint=checkpoint):
                work = self.root / f"serve-{browser}-{checkpoint}"
                server = Mock(server_port=8765)
                with (
                    patch.object(web.tempfile, "mkdtemp", return_value=str(work)),
                    patch.object(web, "HTTPServer", return_value=server),
                    patch.object(web, "checkpoint_database", return_value=checkpoint),
                    patch.object(web.webbrowser, "open") as opened,
                    patch.object(web, "load_settings", return_value=AppSettings()),
                ):
                    url = web.serve(
                        self.conn,
                        self.state.db_path,
                        open_browser=browser,
                        settings=AppSettings() if browser else None,
                    )
                self.assertEqual(url, "http://127.0.0.1:8765")
                server.serve_forever.assert_called_once()
                server.server_close.assert_called_once()
                self.assertEqual(opened.call_count, int(browser))
                self.assertFalse(work.exists())

    def test_serve_interrupt_still_closes_and_cleans_up(self):
        work = self.root / "interrupted-server"
        server = Mock(server_port=8765)
        server.serve_forever.side_effect = KeyboardInterrupt()
        with (
            patch.object(web.tempfile, "mkdtemp", return_value=str(work)),
            patch.object(web, "HTTPServer", return_value=server),
            patch.object(
                web, "checkpoint_database", return_value=(0, 0, 0)
            ) as checkpoint,
            patch.object(web.WebState, "wait_for_scan") as wait,
        ):
            with self.assertRaises(KeyboardInterrupt):
                web.serve(self.conn, self.state.db_path, settings=AppSettings())
        server.server_close.assert_called_once()
        wait.assert_called_once()
        checkpoint.assert_called_once_with(self.conn, "TRUNCATE")
        self.assertFalse(work.exists())


class WebPageTests(WebFixture):
    def test_scan_panel_states_and_settings(self):
        for status, expected in [
            (web.WebScanStatus(), "only run after"),
            (web.WebScanStatus(state="running", started_at="<now>"), "&lt;now&gt;"),
            (
                web.WebScanStatus(state="completed", stats=ScanStats()),
                "Last update finished",
            ),
            (web.WebScanStatus(state="completed"), "only run after"),
            (web.WebScanStatus(state="failed", error="<failed>"), "&lt;failed&gt;"),
        ]:
            self.state.finish_scan(status)
            self.assertIn(expected, web._scan_panel(self.state, "en"))
        self.state.disable_advanced_options = True
        self.assertNotIn('name="crop_index"', web._scan_panel(self.state, "en"))
        self.assertNotIn('name="tryhard"', web._index_page(self.state, "en"))

    def test_index_page_cache_notice(self):
        self.assertIn(
            "Cleared 3", web._index_page(self.state, "en", "thumbnail_cache_cleared=3")
        )
        self.assertIn(
            "Cleared 0", web._index_page(self.state, "en", "thumbnail_cache_cleared=no")
        )

    def test_image_results_and_labels(self):
        path = self.image()
        row = self.conn.execute("SELECT * FROM images").fetchone()
        candidate = SimpleNamespace(total_hits=4)
        score = PairScore(1, 2, 3, 4, 5, 6, 88.5, "strong_duplicate")
        for include_exact, tryhard in [(True, False), (False, True)]:
            options = web.SearchOptions(10, 55, include_exact, tryhard, "strict", "en")
            page = web._results_page([(row, candidate, score)], options)
            self.assertIn("88.50", page)
            self.assertIn(path.as_uri(), page)
            self.assertIn("Strong visual match", page)
            self.assertIn("No results", web._results_page([], options))
        self.assertEqual(web._label("new_label", "en"), "new label")
        self.assertEqual(web._quality_label("custom", "en"), "custom")

    def test_prompt_rendering_paging_highlighting_and_escaping(self):
        self.image(prompt="<cat> & dog")
        row = self.conn.execute(
            "SELECT i.*, ip.* FROM images i JOIN image_prompts ip ON i.id=ip.image_id"
        ).fetchone()
        for text in ["<cat> & dog", None]:
            result = PromptSearchResult(row, text, 1, ("<cat>", "@model:sd"))
            options = web.PromptSearchOptions(
                "cat", "<bad>", "any", "name_asc", 1, 1, "en", True, "NAI4"
            )
            page = web._prompt_results_page([result], options, True)
            self.assertIn("Previous", page)
            self.assertIn("Next", page)
            self.assertIn("&lt;bad&gt;", page)
            self.assertNotIn("@model:sd", page)
            if text:
                self.assertIn("<mark>&lt;cat&gt;</mark>", page)
        options = web.PromptSearchOptions("", "", "all", "bad", 10, 0, "en", False)
        self.assertIn(
            "No indexed prompts", web._prompt_results_page([], options, False)
        )
        self.assertNotIn("Previous</button>", web._prompt_pager(options, False))
        self.assertEqual(web._highlight_prompt("cat", (" ",)), "cat")
        self.assertEqual(web._highlight_prompt("", ("cat",)), "")
        self.assertEqual(
            web._highlight_prompt("Cat & dog", ("cat",)), "<mark>Cat</mark> &amp; dog"
        )

    def test_prompt_model_labels(self):
        base = {
            "configured_model": None,
            "model_type": None,
            "model_hash": None,
            "model_version": None,
        }
        self.assertEqual(web._prompt_model_label(base), "SD")
        self.assertEqual(
            web._prompt_model_label({**base, "configured_model": " custom "}), "CUSTOM"
        )
        self.assertEqual(
            web._prompt_model_label(
                {
                    **base,
                    "model_type": "nai4",
                    "model_version": "4.5",
                    "model_hash": "abc",
                }
            ),
            "NAI4 (4.5, abc)",
        )

    def test_failures_and_suggestions_visibility(self):
        self.assertIn("No indexing failures", web._failures_page(self.state, "en"))
        self.image(error="<decode>", prompt="cat")
        self.conn.execute(
            "UPDATE image_prompts SET prompt_error='bad metadata' WHERE image_id=1"
        )
        self.conn.execute("INSERT INTO prompt_tags VALUES(1,'Cat','cat',0,'prompt')")
        self.conn.execute("INSERT INTO prompt_tags VALUES(1,'','empty',1,'prompt')")
        page = web._failures_page(self.state, "en")
        self.assertIn("&lt;decode&gt;; prompt: bad metadata", page)
        self.assertEqual(web._tag_suggestions(self.state, ""), [])
        self.assertEqual(
            web._tag_suggestions(self.state, "q=CAT"), [{"tag": "Cat", "count": 1}]
        )
        self.assertEqual(web._tag_suggestions(self.state, "q=empty"), [])

    def test_model_filter_options(self):
        for image_id, label, normalized, kind, configured in [
            (1, "@model:SD", "@model:sd", "model_type", None),
            (2, "@model:Custom", "@model:custom", "model_config", "custom"),
            (3, "@model:NAI4", "@model:nai4", "model_type", "custom"),
            (4, "invalid", "@model:invalid", "model_config", "custom"),
        ]:
            self.image(image_id)
            self.conn.execute(
                "UPDATE image_prompts SET configured_model=? WHERE image_id=?",
                (configured, image_id),
            )
            self.conn.execute(
                "INSERT INTO prompt_tags VALUES(?,?,?,?,?)",
                (image_id, label, normalized, 0, kind),
            )
        self.assertEqual(
            web._model_filter_options(self.conn), [("SD", "SD"), ("Custom", "Custom")]
        )

    def test_scan_configuration_and_hidden_fields(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            web._scan_request_from_fields({})
        with self.assertRaisesRegex(ValueError, "does not exist"):
            web._scan_request_from_fields({"roots": str(self.root / "absent")})
        request = web._scan_request_from_fields(
            self.scan_fields(
                roots="\n" + str(self.root) + "\n\n",
                workers="100",
                crop_index="1",
                stealth_prompt="1",
                reindex_prompts="1",
                delete_missing="1",
            )
        )
        self.assertEqual(request.workers, 64)
        self.assertIn('name="crop_index"', web._scan_request_hidden_fields(request))
        self.assertIn(
            'name="model_target_0"', web._model_path_prompt_page(request, request.roots)
        )
        self.assertEqual(web._configured_target(self.root / "file.png"), self.root)
        config = build_config(())
        with patch.object(web, "load_model_paths", return_value=config):
            self.assertEqual(
                web._unconfigured_scan_roots((self.root, self.root / "file.png")),
                (self.root,),
            )
        config = build_config((make_rule(self.root, "custom"),))
        with patch.object(web, "load_model_paths", return_value=config):
            self.assertEqual(web._unconfigured_scan_roots((self.root,)), ())
        with patch.object(
            web,
            "load_model_paths",
            return_value=SimpleNamespace(
                issues=[SimpleNamespace(section="x", message="bad")]
            ),
        ):
            with self.assertRaisesRegex(ValueError, "x: bad"):
                web._unconfigured_scan_roots((self.root,))

    def test_options_validation(self):
        options = web._options_from_fields(
            {
                "tryhard": "1",
                "min_score": "bad",
                "quality": "strict",
                "include_exact": "1",
                "limit": "1000",
                "lang": "en",
            },
            self.state,
        )
        self.assertEqual(
            (options.limit, options.min_score, options.tryhard), (500, 1, True)
        )
        options = web._options_from_fields(
            {"quality": "bad", "limit": "no", "min_score": "-2"}, self.state
        )
        self.assertEqual(
            (options.limit, options.min_score, options.quality), (10, 0, "balanced")
        )
        self.assertEqual(web._float_field({"x": "2.5"}, "x", 0), 2.5)


class WebHandlerTests(WebFixture):
    def test_get_routes_and_not_found(self):
        for path, fragment in [
            ("/?lang=en", "Image Search"),
            ("/failures?lang=en", "Indexing Failures"),
            ("/suggest-tags?q=", "[]"),
        ]:
            status, header, body = self.response(self.request(path=path))
            self.assertEqual(status, 200)
            self.assertIn(fragment, body)
        self.assertEqual(self.response(self.request(path="/unknown"))[0], 404)
        self.assertEqual(
            self.response(self.request(method="POST", path="/unknown"))[0], 404
        )

    def test_show_in_folder_validation_and_success(self):
        self.assertEqual(
            self.response(self.request("POST", "/show-in-folder", {"id": "bad"}))[0],
            400,
        )
        self.assertEqual(
            self.response(self.request("POST", "/show-in-folder", {"id": "500"}))[0],
            404,
        )
        path = self.image()
        with patch.object(web, "_show_path_in_folder") as show:
            status, _, body = self.response(
                self.request("POST", "/show-in-folder", {"id": "1", "lang": "en"})
            )
        self.assertEqual(status, 200)
        self.assertIn("Folder Opened", body)
        show.assert_called_once_with(path)

    def test_cache_clear_redirect(self):
        (self.state.thumbs_dir / "old.jpg").write_bytes(b"old")
        status, header, _ = self.response(
            self.request("POST", "/clear-thumbnail-cache", {"lang": "en"})
        )
        self.assertEqual(status, 303)
        self.assertIn("Location: /?lang=en&thumbnail_cache_cleared=1", header)

    def test_scan_form_and_busy_errors(self):
        with patch.object(web, "_unconfigured_scan_roots", return_value=(self.root,)):
            status, _, body = self.response(
                self.request("POST", "/scan", self.scan_fields())
            )
        self.assertEqual(status, 200)
        self.assertIn("Classify folders", body)
        with (
            patch.object(web, "_unconfigured_scan_roots", return_value=()),
            patch.object(self.state, "start_scan", return_value=True),
        ):
            self.assertEqual(
                self.response(self.request("POST", "/scan", self.scan_fields()))[0], 303
            )
        with (
            patch.object(web, "_unconfigured_scan_roots", return_value=()),
            patch.object(self.state, "start_scan", return_value=False),
        ):
            status, _, body = self.response(
                self.request("POST", "/scan", self.scan_fields())
            )
        self.assertEqual(status, 400)
        self.assertIn("already running", body)
        status, _, body = self.response(
            self.request(
                "POST", "/scan", self.scan_fields(), content_type="application/json"
            )
        )
        self.assertEqual(status, 400)
        self.assertIn("expected form-urlencoded", body)

    def test_scan_classification_saves_only_complete_valid_requests(self):
        config = build_config(())
        fields = self.scan_fields(model_target_0=str(self.root), model_label_0="custom")
        with (
            patch.object(web, "load_model_paths", return_value=config),
            patch.object(web, "_unconfigured_scan_roots", return_value=(self.root,)),
            patch.object(web, "save_model_paths") as save,
            patch.object(self.state, "start_scan", return_value=True),
        ):
            self.assertEqual(
                self.response(self.request("POST", "/scan-configure", fields))[0], 303
            )
            self.assertEqual(save.call_args.args[1].rules[0].label, "custom")
        cases = [
            ({}, (), config, "running", True, "already running"),
            ({}, (), SimpleNamespace(issues=[1]), "idle", True, "invalid rules"),
            (
                {"model_target_0": str(self.root.parent), "model_label_0": "custom"},
                (self.root,),
                config,
                "idle",
                True,
                "stale or invalid",
            ),
            (
                {
                    "model_target_0": str(self.root),
                    "model_target_1": str(self.root),
                    "model_label_0": "custom",
                },
                (self.root,),
                config,
                "idle",
                True,
                "stale or invalid",
            ),
            ({}, (self.root,), config, "idle", True, "incomplete or stale"),
            ({}, (), config, "idle", False, "already running"),
        ]
        for extra, roots, loaded, state, starts, error in cases:
            with (
                self.subTest(error=error, extra=extra),
                patch.object(web, "_unconfigured_scan_roots", return_value=roots),
                patch.object(web, "load_model_paths", return_value=loaded),
                patch.object(self.state, "start_scan", return_value=starts),
            ):
                self.state.finish_scan(web.WebScanStatus(state=state))
                status, _, body = self.response(
                    self.request("POST", "/scan-configure", self.scan_fields(**extra))
                )
                self.assertEqual(status, 400)
                self.assertIn(error, body)
        self.state.finish_scan(web.WebScanStatus())
        with (
            patch.object(web, "_unconfigured_scan_roots", return_value=()),
            patch.object(web, "load_model_paths", return_value=config),
            patch.object(self.state, "start_scan", return_value=True),
            patch.object(web, "save_model_paths") as save,
        ):
            self.assertEqual(
                self.response(
                    self.request("POST", "/scan-configure", self.scan_fields())
                )[0],
                303,
            )
            save.assert_not_called()

    def test_configured_rescan(self):
        config = build_config(
            (make_rule(self.root, "custom"), make_rule(self.root / "absent", "other"))
        )
        with (
            patch.object(web, "load_model_paths", return_value=config),
            patch.object(self.state, "start_scan", return_value=True) as start,
        ):
            self.assertEqual(
                self.response(self.request("POST", "/scan-configured", {"lang": "en"}))[
                    0
                ],
                303,
            )
            self.assertEqual(start.call_args.args[0].roots, (self.root,))
        for loaded, start_result, error in [
            (SimpleNamespace(issues=[1]), True, "invalid rules"),
            (build_config(()), True, "no configured folders"),
            (config, False, "already running"),
        ]:
            with (
                patch.object(web, "load_model_paths", return_value=loaded),
                patch.object(self.state, "start_scan", return_value=start_result),
            ):
                status, _, body = self.response(
                    self.request("POST", "/scan-configured", {"lang": "en"})
                )
                self.assertEqual(status, 400)
                self.assertIn(error, body)

    def test_prompt_search_validates_fields_and_paginates(self):
        self.image(prompt="cat")
        self.conn.execute("INSERT INTO prompt_tags VALUES(1,'cat','cat',0,'prompt')")
        status, _, body = self.response(
            self.request(
                "POST",
                "/prompt-search",
                {
                    "tags": "cat",
                    "limit": "1",
                    "match_mode": "any",
                    "lang": "en",
                    "unique_prompts": "1",
                },
            )
        )
        self.assertEqual(status, 200)
        self.assertIn("<mark>cat</mark>", body)
        for fields, content_type, error in [
            ({}, None, "enter at least one"),
            ({"tags": "cat"}, "application/json", "expected form-urlencoded"),
        ]:
            status, _, body = self.response(
                self.request(
                    "POST", "/prompt-search", fields, content_type=content_type
                )
            )
            self.assertEqual(status, 400)
            self.assertIn(error, body)
        handler = self.request(
            "POST",
            "/prompt-search",
            {
                "negative_tags": "bad",
                "match_mode": "oops",
                "sort_by": "oops",
                "limit": "999",
                "offset": "-2",
                "model_type": " ",
            },
        )
        handler._prepare_post()
        options = handler._save_prompt_search()
        self.assertEqual(
            (options.match_mode, options.limit, options.offset, options.model_type),
            ("all", 500, 0, "all"),
        )
        row = self.conn.execute(
            "SELECT i.*, ip.* FROM images i JOIN image_prompts ip ON i.id=ip.image_id"
        ).fetchone()
        result = PromptSearchResult(row, "cat", 1, ("cat",))
        with patch.object(web, "query_prompt_tags", return_value=[result, result]):
            status, _, body = self.response(
                self.request(
                    "POST",
                    "/prompt-search",
                    {"tags": "cat", "limit": "1", "lang": "en"},
                )
            )
        self.assertEqual(status, 200)
        self.assertIn("Next</button>", body)

    def multipart(self, filename="test.png"):
        body = (
            b'--B\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n'
            + self.state.csrf_token.encode()
            + b"\r\n"
            + b'--B\r\nContent-Disposition: form-data; name="lang"\r\n\r\nen\r\n'
            b'--B\r\nContent-Disposition: form-data; name="image"; filename="'
            + filename.encode()
            + b'"\r\nContent-Type: image/png\r\n\r\nIMAGE\r\n--B--\r\n'
        )
        return body, "multipart/form-data; boundary=B"

    def test_image_upload_and_search_errors(self):
        upload_names = []
        for filename, suffix in [("query.png", ".png"), ("query.unknown", ".upload")]:
            body, ctype = self.multipart(filename)
            seen = []

            def query_upload(conn, path, **options):
                seen.append((path, path.read_bytes()))
                return []

            with patch.object(web, "query_image", side_effect=query_upload) as query:
                status, _, page = self.response(
                    self.request("POST", "/search", body=body, content_type=ctype)
                )
            self.assertEqual(status, 200)
            self.assertIn("No results", page)
            uploaded = query.call_args.args[1]
            upload_names.append(uploaded.name)
            self.assertEqual(uploaded.suffix, suffix)
            self.assertEqual(seen[-1], (uploaded, b"IMAGE"))
            self.assertFalse(uploaded.exists())
            self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        self.assertEqual(len(set(upload_names)), 2)
        self.assertEqual(self.response(self.request("POST", "/search"))[0], 400)
        body, ctype = self.multipart()
        with patch.object(web, "query_image", side_effect=ValueError("<bad image>")):
            status, _, page = self.response(
                self.request("POST", "/search", body=body, content_type=ctype)
            )
        self.assertEqual(status, 400)
        self.assertIn("&lt;bad image&gt;", page)
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])

    def test_thumbnail_generation_cache_refresh_and_not_found(self):
        self.assertEqual(self.response(self.request(path="/thumb/bad"))[0], 404)
        self.assertEqual(self.response(self.request(path="/thumb/999"))[0], 404)
        self.image()
        stale = self.state.thumbs_dir / "1-stale.jpg"
        stale.write_bytes(b"old")
        self.state.thumbnail_cache_count = 1
        handler = self.request(path="/thumb/1.jpg")
        handler.do_GET()
        header, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertIn(b"200 OK", header)
        self.assertIn(b"image/jpeg", header)
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertFalse(stale.exists())
        with patch.object(web.os, "utime", side_effect=OSError("busy")):
            self.assertEqual(self.response(self.request(path="/thumb/1.jpg"))[0], 200)
        self.assertEqual(self.response(self.request(path="/thumb/1.jpg"))[0], 200)
        self.state.clear_thumbnail_cache()
        stale.write_bytes(b"old")
        with (
            patch.object(Path, "unlink", side_effect=OSError("busy")),
            patch.object(web, "_write_thumbnail", return_value=False),
        ):
            self.assertEqual(self.response(self.request(path="/thumb/1.jpg"))[0], 404)

    def test_send_file_guesses_content_type(self):
        for filename, expected in [
            ("file.txt", "text/plain"),
            ("file.unknown", "application/octet-stream"),
        ]:
            path = self.root / filename
            path.write_bytes(b"payload")
            handler = self.request()
            handler._send_file(path)
            self.assertIn(expected.encode(), handler.wfile.getvalue())
            self.assertTrue(handler.wfile.getvalue().endswith(b"payload"))


class WebUtilityTests(WebFixture):
    def test_multipart_fields_missing_files_and_headers(self):
        self.assertEqual(
            web._boundary_from_content_type('multipart/form-data; boundary="abc"'),
            b"--abc",
        )
        with self.assertRaisesRegex(ValueError, "missing multipart boundary"):
            web._boundary_from_content_type("multipart/form-data")
        body = (
            b'--x\r\nignore\r\n\r\nignore\r\n--x\r\nname="field"\r\n\r\nvalue\r\n'
            b'--x\r\nname="image"\r\n\r\nraw'
        )
        fields, data, filename = web._extract_multipart(body, b"--x")
        self.assertEqual(
            (fields, data, filename), ({"field": "value"}, b"raw", "query.upload")
        )
        self.assertEqual(web._filename_from_header('filename=""'), "query.upload")
        self.assertEqual(web._filename_from_header("filename="), "query.upload")
        self.assertEqual(
            web._filename_from_header('filename="image.png"; type=x'), "image.png"
        )
        self.assertIsNone(web._field_name_from_header("no disposition"))
        with self.assertRaisesRegex(ValueError, "no image file"):
            web._extract_multipart(b'--x\r\nname="field"\r\n\r\nvalue', b"--x")

    def test_thumbnail_creation_failure_and_pruning_errors(self):
        source = self.image()
        target = self.root / "thumbnail.jpg"
        self.assertTrue(web._write_thumbnail(source, target, 32))
        with Image.open(target) as thumbnail:
            self.assertEqual(thumbnail.size, (32, 32))
            # JPEG compression can shift channel values slightly. Preserve the
            # red source in the center and the light letterbox canvas at top.
            center = thumbnail.getpixel((16, 16))
            self.assertGreater(center[0], 230)
            self.assertLess(max(center[1:]), 20)
            border = thumbnail.getpixel((16, 0))
            self.assertTrue(all(channel > 230 for channel in border), border)
        self.assertFalse(web._write_thumbnail(self.root / "absent", target, 32))
        for index in range(3):
            path = self.state.thumbs_dir / f"{index}.jpg"
            path.write_bytes(b"thumb")
            os.utime(
                path,
                ns=(
                    1_000_000_000 + index * 1_000_000,
                    1_000_000_000 + index * 1_000_000,
                ),
            )
        with (
            patch.object(Path, "stat", side_effect=OSError("unavailable")),
            patch.object(Path, "mkdir"),
        ):
            web._prune_thumbnail_cache(self.state.thumbs_dir, 1)
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            web._prune_thumbnail_cache(self.state.thumbs_dir, 1)
        self.assertEqual(len(list(self.state.thumbs_dir.glob("*.jpg"))), 3)
        web._prune_thumbnail_cache(self.state.thumbs_dir, 1)
        self.assertEqual(
            [path.name for path in self.state.thumbs_dir.glob("*.jpg")], ["2.jpg"]
        )

    def test_show_in_folder_windows_api_and_fallback(self):
        path = self.image()
        windows = SimpleNamespace(name="nt")
        with (
            patch.object(web, "os", windows),
            patch.object(web, "_sh_open_folder_and_select") as select,
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            select.assert_called_once_with(path)
            popen.assert_not_called()
        with (
            patch.object(web, "os", windows),
            patch.object(
                web, "_sh_open_folder_and_select", side_effect=OSError("shell")
            ),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            self.assertEqual(
                popen.call_args.args[0], ["explorer.exe", str(path.parent)]
            )
        with (
            patch.object(web, "os", SimpleNamespace(name="posix")),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            web._show_path_in_folder(self.root / "missing.png")
            self.assertEqual(popen.call_count, 2)
        with (
            patch.object(web, "os", windows),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(self.root / "missing.png")
            popen.assert_called_once()

    def test_shell_api_releases_allocations_and_initialization(self):
        for parse_result, open_result, initialized, allocated in [
            (0, 0, 0, True),
            # RPC_E_CHANGED_MODE: selection may succeed using an existing COM
            # apartment, but this call must not uninitialize an apartment it
            # did not initialize itself.
            (0, 0, -2147417850, True),
            (1, 0, 1, False),
            (0, 1, -1, True),
        ]:
            with self.subTest(parse_result=parse_result, open_result=open_result):
                ole = Mock()
                shell = Mock()
                ole.CoInitialize.return_value = initialized

                def parse(path, unused, pidl, flags, sfgao):
                    if allocated:
                        pidl._obj.value = 123
                    return parse_result

                shell.SHParseDisplayName.side_effect = parse
                shell.SHOpenFolderAndSelectItems.return_value = open_result
                with (
                    patch.object(
                        web.ctypes, "OleDLL", side_effect=[ole, shell], create=True
                    ),
                    patch.object(
                        web.ctypes,
                        "WinError",
                        side_effect=lambda value: OSError(f"win error {value}"),
                        create=True,
                    ),
                ):
                    if parse_result or open_result:
                        with self.assertRaisesRegex(OSError, "win error"):
                            web._sh_open_folder_and_select(self.root)
                    else:
                        web._sh_open_folder_and_select(self.root)
                self.assertEqual(ole.CoTaskMemFree.call_count, int(allocated))
                self.assertEqual(
                    ole.CoUninitialize.call_count, int(initialized in (0, 1))
                )
