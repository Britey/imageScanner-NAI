"""Low-level handler boundary for malformed requests and resource-failure tests.

User workflows use WebTestCase in test_web_support, which parses raw HTTP bytes.
Here callers can inject otherwise inaccessible header/stream/deadline failures.
"""
from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from PIL import Image

from imgdupe import scan as scan_module, web
from imgdupe.db import init_db


class WebFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module in (web, scan_module):
            isolated = patch.object(module, "DEFAULT_MODEL_PATHS_PATH", self.root / "model_paths.ini")
            isolated.start()
            self.addCleanup(isolated.stop)
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
        self.addCleanup(self.state.wait_for_scan)
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
