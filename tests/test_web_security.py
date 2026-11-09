from __future__ import annotations

import contextlib
import http.client
import io
import socket
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from imgdupe import web
from web_handler_fixture import WebFixture


class Forms(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {
                "action": attrs.get("action"),
                "method": attrs.get("method"),
                "fields": {},
            }
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and "name" in attrs:
            self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class WebSecurityTests(WebFixture):
    def replace_header(self, handler, name, value):
        del handler.headers[name]
        if value is not None:
            handler.headers[name] = value

    def test_foreign_sources_and_invalid_tokens_cannot_mutate(self):
        self.image()
        cases = [
            ("Host", "evil.example:8765"),
            ("Host", "127.0.0.1:9999"),
            ("Origin", "https://evil.example"),
            ("Origin", "null"),
            ("Referer", "http://evil.example/attack"),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Site", "same-site"),
            ("token", ""),
            ("token", "wrong"),
            ("token", "非ASCII"),
        ]
        with (
            patch.object(self.state, "start_scan") as start,
            patch.object(web, "save_model_paths") as save,
            patch.object(self.state, "clear_thumbnail_cache") as clear,
            patch.object(web, "_show_path_in_folder") as show,
        ):
            for route in (
                "/scan",
                "/scan-configure",
                "/scan-configured",
                "/clear-thumbnail-cache",
                "/show-in-folder",
            ):
                for name, value in cases:
                    with self.subTest(route=route, header=name, value=value):
                        fields = self.scan_fields(
                            id="1",
                            model_target_0=str(self.root),
                            model_label_0="unsafe",
                        )
                        if name == "token":
                            fields["csrf_token"] = value
                        handler = self.request("POST", route, fields)
                        if name != "token":
                            self.replace_header(handler, name, value)
                        self.assertEqual(self.response(handler)[0], 403)
            for operation in (start, save, clear, show):
                operation.assert_not_called()

    def test_host_syntax_duplicates_and_get_source_gate(self):
        for value in (
            None,
            "",
            "attacker.example",
            "localhost:9999",
            "localhost:bad",
            "user@localhost:8765",
            "localhost:8765/path",
            "localhost:8765?query",
            "localhost:8765#fragment",
            "local host:8765",
            "[broken",
        ):
            with self.subTest(host=value):
                handler = self.request(path="/")
                self.replace_header(handler, "Host", value)
                self.assertEqual(self.response(handler)[0], 403)
        handler = self.request()
        handler.headers["Host"] = "localhost:8765"
        self.assertEqual(self.response(handler)[0], 403)
        for name, value in [
            ("Origin", "http://localhost:8765"),
            ("Origin", "https://127.0.0.1:8765"),
            ("Referer", "file:///tmp/attack"),
        ]:
            handler = self.request()
            self.replace_header(handler, name, value)
            self.assertEqual(self.response(handler)[0], 403)
        self.assertEqual(web._request_authority("localhost"), ("localhost", 80))
        self.assertEqual(web._request_authority("[::1]:8765"), ("::1", 8765))

    def test_own_origins_bind_hostname_and_actual_local_address_are_usable(self):
        for host, endpoint in [("localhost:8765", "127.0.0.1"), ("[::1]:8765", "::1")]:
            handler = self.request()
            self.replace_header(handler, "Host", host)
            handler.headers["Origin"] = "http://" + host
            handler.headers["Referer"] = "http://" + host + "/page?lang=en"
            handler.headers["Sec-Fetch-Site"] = "same-origin"
            handler.connection.getsockname.return_value = (endpoint, 8765)
            self.assertEqual(self.response(handler)[0], 200)
        self.state.allowed_hosts = frozenset({"images.local"})
        for host in ("images.local", "192.168.1.25"):
            handler = self.request()
            self.replace_header(handler, "Host", host + ":8765")
            handler.connection.getsockname.return_value = ("192.168.1.25", 8765)
            self.assertEqual(self.response(handler)[0], 200)

    def test_every_rendered_form_has_a_usable_process_token(self):
        status, headers, document = self.response(self.request(path="/?lang=en"))
        self.assertEqual(status, 200)
        self.assertIn("X-Frame-Options: DENY", headers)
        self.assertIn("frame-ancestors 'none'", headers)
        forms = Forms(document).forms
        self.assertGreaterEqual(len(forms), 5)
        for form in forms:
            self.assertEqual(form["method"], "post")
            self.assertEqual(form["fields"]["csrf_token"], self.state.csrf_token)
        clear = next(
            form for form in forms if form["action"] == "/clear-thumbnail-cache"
        )
        # Submit exactly the hidden values parsed from the rendered form.
        body = urlencode(clear["fields"]).encode()
        self.assertEqual(
            self.response(self.request("POST", clear["action"], body=body))[0], 303
        )
        with patch.object(web, "_unconfigured_scan_roots", return_value=(self.root,)):
            status, _, document = self.response(
                self.request("POST", "/scan", self.scan_fields())
            )
        self.assertEqual(status, 200)
        classification = Forms(document).forms[0]
        self.assertEqual(classification["fields"]["csrf_token"], self.state.csrf_token)
        self.assertEqual(classification["fields"]["model_target_0"], str(self.root))
        self.assertEqual(
            web._csrf_forms(
                '<form method="get" action="/search"></form>', self.state.csrf_token
            ),
            '<form method="get" action="/search"></form>',
        )
        self.assertEqual(
            web._csrf_forms(
                '<form method="post" action="https://foreign.example"></form>',
                self.state.csrf_token,
            ),
            '<form method="post" action="https://foreign.example"></form>',
        )
        self.assertEqual(
            web._csrf_forms(
                '<form method="post" action="//foreign.example"></form>',
                self.state.csrf_token,
            ),
            '<form method="post" action="//foreign.example"></form>',
        )

    def test_show_folder_requires_post_and_token_in_results(self):
        path = self.image(prompt="cat")
        with patch.object(web, "_show_path_in_folder") as show:
            self.assertEqual(
                self.response(self.request(path="/show-in-folder?id=1"))[0], 405
            )
            show.assert_not_called()
            self.assertEqual(
                self.response(
                    self.request("POST", "/show-in-folder", {"id": "1", "lang": "en"})
                )[0],
                200,
            )
            show.assert_called_once_with(path)
        row = self.conn.execute(
            "SELECT i.*,ip.* FROM images i JOIN image_prompts ip ON i.id=ip.image_id"
        ).fetchone()
        options = web.PromptSearchOptions(
            "cat", "", "all", "created_desc", 10, 0, "en", False
        )
        result = web.PromptSearchResult(row, "cat", 1, ("cat",))
        document = web._prompt_results_page([result], options, True)
        handler = self.request()
        handler._send_html(document)
        body = handler.wfile.getvalue().partition(b"\r\n\r\n")[2].decode()
        forms = Forms(body).forms
        self.assertTrue(any(form["action"] == "/show-in-folder" for form in forms))
        self.assertNotIn('href="/show-in-folder', body)
        for form in forms:
            self.assertEqual(form["fields"]["csrf_token"], self.state.csrf_token)

    def test_invalid_lengths_and_encodings_are_rejected_without_reading(self):
        cases = [
            ("Content-Length", None, 411),
            ("Content-Length", "-1", 400),
            ("Content-Length", "+5", 400),
            ("Content-Length", "oops", 400),
            ("Content-Length", str(web.MAX_FORM_BYTES + 1), 413),
            ("Content-Length", "9" * 100, 413),
            ("Transfer-Encoding", "chunked", 400),
            ("Content-Encoding", "gzip", 415),
        ]
        for name, value, status in cases:
            with self.subTest(name=name, value=value):
                handler = self.request("POST", "/clear-thumbnail-cache")
                self.replace_header(handler, name, value)
                handler.rfile = Mock()
                handler.rfile.read1.side_effect = AssertionError(
                    "invalid or oversized request bodies must not be read"
                )
                self.assertEqual(self.response(handler)[0], status)
                handler.rfile.read1.assert_not_called()
        handler = self.request("POST", "/clear-thumbnail-cache")
        handler.headers["Content-Length"] = "0"
        handler.rfile = Mock()
        handler.rfile.read1.side_effect = AssertionError(
            "duplicate Content-Length must be rejected before reading"
        )
        self.assertEqual(self.response(handler)[0], 400)
        handler.rfile.read1.assert_not_called()
        handler = self.request("POST", "/search")
        self.replace_header(handler, "Content-Length", str(web.MAX_UPLOAD_BYTES + 1))
        handler.rfile = Mock()
        handler.rfile.read1.side_effect = AssertionError(
            "oversized uploads must be rejected before reading"
        )
        self.assertEqual(self.response(handler)[0], 413)
        handler.rfile.read1.assert_not_called()

    def test_short_timeout_expired_and_empty_bodies_are_bounded(self):
        handler = self.request("POST", "/clear-thumbnail-cache", body=b"short")
        self.replace_header(handler, "Content-Length", "100")
        self.assertEqual(self.response(handler)[0], 400)
        handler = self.request("POST", "/clear-thumbnail-cache")
        handler.rfile = Mock()
        handler.rfile.read1.side_effect = TimeoutError("stalled")
        self.assertEqual(self.response(handler)[0], 408)
        handler = self.request("POST", "/clear-thumbnail-cache")
        with patch.object(
            web.time, "monotonic", side_effect=[0, web.BODY_TIMEOUT_SECONDS + 1]
        ):
            self.assertEqual(self.response(handler)[0], 408)
        handler = self.request("POST", "/clear-thumbnail-cache")
        handler.rfile = Mock()
        handler.rfile.read1.return_value = b"a"
        with patch.object(
            web.time, "monotonic", side_effect=[0, 1, web.BODY_TIMEOUT_SECONDS + 1]
        ):
            self.assertEqual(self.response(handler)[0], 408)
        self.assertEqual(
            self.response(self.request("POST", "/clear-thumbnail-cache", body=b""))[0],
            403,
        )

    def test_malformed_form_encoding_and_excessive_fields(self):
        self.assertEqual(
            self.response(self.request("POST", "/clear-thumbnail-cache", body=b"\xff"))[
                0
            ],
            400,
        )
        self.assertEqual(
            self.response(
                self.request(
                    "POST",
                    "/clear-thumbnail-cache",
                    content_type="application/x-www-form-urlencoded; charset=iso-8859-1",
                )
            )[0],
            415,
        )
        self.assertEqual(
            self.response(
                self.request(
                    "POST",
                    "/clear-thumbnail-cache",
                    content_type="application/x-www-form-urlencoded-evil",
                )
            )[0],
            400,
        )
        self.assertEqual(
            self.response(
                self.request("POST", "/clear-thumbnail-cache", body=b"a=&" * 4097)
            )[0],
            400,
        )

    def test_upload_write_failure_cleans_up_and_create_failure_is_reported(self):
        fields = {"csrf_token": self.state.csrf_token}
        handler = self.request("POST", "/search")
        handler._multipart = (fields, b"image", "test.png")
        with (
            patch.object(Path, "write_bytes", side_effect=OSError("full")),
            self.assertRaises(OSError),
        ):
            handler._save_upload()
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        with (
            patch.object(
                web.tempfile, "NamedTemporaryFile", side_effect=OSError("full")
            ),
            self.assertRaises(OSError),
        ):
            handler._save_upload()
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        real_temporary_file = web.tempfile.NamedTemporaryFile

        class CloseFailure:
            def __enter__(inner):
                inner.file = real_temporary_file(
                    dir=self.state.uploads_dir, delete=False
                )
                return inner.file

            def __exit__(inner, *args):
                inner.file.close()
                raise OSError("close failed")

        with (
            patch.object(
                web.tempfile,
                "NamedTemporaryFile",
                side_effect=lambda **kwargs: CloseFailure(),
            ),
            self.assertRaisesRegex(OSError, "close failed"),
        ):
            handler._save_upload()
        self.assertEqual(list(self.state.uploads_dir.iterdir()), [])
        # Exercise the route-level finally path when no upload was created.
        with (
            patch.object(self.handler_type, "_prepare_post"),
            patch.object(
                self.handler_type, "_save_upload", side_effect=OSError("full")
            ),
        ):
            self.assertEqual(self.response(self.request("POST", "/search"))[0], 400)

    def test_wildcard_serve_binding_and_header_expiration_cleanup(self):
        server = Mock(server_port=8765)
        with (
            patch.object(web, "HTTPServer", return_value=server),
            patch.object(web, "checkpoint_database", return_value=None),
        ):
            self.assertEqual(
                web.serve(self.conn, self.state.db_path, host="0.0.0.0"),
                "http://0.0.0.0:8765",
            )
        handler = self.request()
        handler._expire_headers()
        handler.connection.shutdown.assert_called_once_with(socket.SHUT_RD)
        handler.connection.shutdown.side_effect = OSError("closed")
        handler._expire_headers()


class WebSocketSecurityTests(WebFixture):
    def test_real_socket_host_csrf_and_stalled_bodies_leave_server_responsive(self):
        # This single server uses a private in-memory SQLite connection owned by
        # the test; no shared app instance, network service, or user data is used.
        import sqlite3
        from imgdupe.db import init_db

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        original = self.state.conn
        self.state.conn = conn
        server = web.HTTPServer(("127.0.0.1", 0), self.handler_type)
        self.state.server_port = server.server_port
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        with (
            patch.object(web, "REQUEST_TIMEOUT_SECONDS", 0.25),
            patch.object(web, "BODY_TIMEOUT_SECONDS", 0.25),
        ):
            thread.start()
            try:

                def request(method, path, body=None, headers=None):
                    client = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=2
                    )
                    try:
                        client.request(method, path, body=body, headers=headers or {})
                        response = client.getresponse()
                        return response.status, response.read()
                    except (OSError, http.client.HTTPException) as exc:
                        self.fail(f"local HTTP request did not complete safely: {exc}")
                    finally:
                        client.close()

                status, document = request("GET", "/?lang=en")
                self.assertEqual(status, 200)
                forms = Forms(document.decode()).forms
                clear = next(
                    form for form in forms if form["action"] == "/clear-thumbnail-cache"
                )
                status, _ = request(
                    "POST",
                    clear["action"],
                    urlencode(clear["fields"]),
                    {"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(status, 303)
                self.assertEqual(
                    request("GET", "/", headers={"Host": "attacker.example"})[0], 403
                )
                with socket.create_connection(
                    server.server_address, timeout=2
                ) as stalled:
                    stalled.settimeout(2)
                    stalled.sendall(
                        (
                            f"POST /clear-thumbnail-cache HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 100\r\n\r\nx"
                        ).encode()
                    )
                    started = time.monotonic()
                    try:
                        response = stalled.recv(8192)
                    except OSError as exc:
                        self.fail(
                            f"body deadline did not produce a bounded response: {exc}"
                        )
                    status_line = response.split(b"\r\n", 1)[0].split(b" ", 2)
                    self.assertGreaterEqual(len(status_line), 2, response)
                    self.assertEqual(status_line[1], b"408")
                    self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(request("GET", "/suggest-tags?q=")[0], 200)
                # Keep bytes flowing faster than the idle socket timeout. Only
                # the absolute header deadline can free the synchronous server
                # while this unfinished header continues arriving.
                with socket.create_connection(
                    server.server_address, timeout=2
                ) as stalled:
                    stalled.settimeout(2)
                    stalled.sendall(b"GET / HTTP/1.1\r\nX-Incomplete: ")
                    stop = threading.Event()

                    def trickle_header():
                        while not stop.wait(0.02):
                            try:
                                stalled.sendall(b"x")
                            except OSError:
                                return

                    sender = threading.Thread(target=trickle_header, daemon=True)
                    sender.start()
                    started = time.monotonic()
                    try:
                        try:
                            response = stalled.recv(8192)
                        except OSError as exc:
                            self.fail(
                                f"absolute header deadline did not close or reject the request: {exc}"
                            )
                        self.assertLess(time.monotonic() - started, 2)
                        # EOF and explicit rejection are both safe outcomes;
                        # the timer and native socket timeout may race.
                        if response:
                            status_line = response.split(b"\r\n", 1)[0].split(b" ", 2)
                            self.assertGreaterEqual(len(status_line), 2, response)
                            self.assertIn(status_line[1], {b"400", b"403", b"408"})
                    finally:
                        stop.set()
                        sender.join(timeout=1)
                        self.assertFalse(
                            sender.is_alive(), "header sender did not stop"
                        )
                self.assertEqual(request("GET", "/suggest-tags?q=")[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                self.state.conn = original
                conn.close()
                self.assertFalse(thread.is_alive())
