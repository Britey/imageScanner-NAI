"""Shared fixtures for the web UI tests.

Requests are encoded as raw HTTP/1.1 bytes and handled by the application's
real request handler class, so request-line, header and body parsing all run
exactly as they do behind a socket. Responses are decoded with http.client.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import random
import tempfile
import threading
import unittest
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from imgdupe import scan as scan_module, web
from imgdupe.config import ScanConfig
from imgdupe.db import connect, init_db
from imgdupe.model_paths import build_config
from imgdupe.scan import scan_roots


_DEFAULT = object()
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list = []

    def get(self, name: str, default=None):
        return self.attrs.get(name, default)

    def raw_text(self) -> str:
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.raw_text())
        return "".join(parts)

    def text(self) -> str:
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag not in {"script", "style"}:
                    walk(child)

        walk(self)
        return " ".join("".join(parts).split())

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag=None, class_=None, **attrs):
        found = []
        for node in self.iter():
            if tag is not None and node.tag != tag:
                continue
            if class_ is not None and class_ not in node.attrs.get("class", "").split():
                continue
            if any(node.attrs.get(key) != value for key, value in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, tag=None, class_=None, **attrs):
        found = self.find_all(tag, class_, **attrs)
        return found[0] if found else None


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self.current = self.root

    def _node(self, tag, attrs):
        node = Node(tag, {key: "" if value is None else value for key, value in attrs}, self.current)
        self.current.children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._node(tag, attrs)
        if tag not in _VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self._node(tag, attrs)

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


class Form:
    def __init__(self, node: Node):
        self.node = node
        self.action = node.get("action")
        self.method = node.get("method")
        self.enctype = node.get("enctype")

    def control(self, name: str) -> Node | None:
        for node in self.node.iter():
            if node.tag in {"input", "select", "textarea"} and node.get("name") == name:
                return node
        return None

    def fields(self) -> dict[str, str]:
        """Return the values a browser would submit for this form."""
        values: dict[str, str] = {}
        for node in self.node.iter():
            name = node.get("name")
            if not name or "disabled" in node.attrs:
                continue
            if node.tag == "input":
                kind = node.get("type", "text")
                if kind == "file":
                    continue
                if kind == "checkbox" and "checked" not in node.attrs:
                    continue
                values[name] = node.get("value", "on" if kind == "checkbox" else "")
            elif node.tag == "textarea":
                values[name] = node.raw_text().removeprefix("\n")
            elif node.tag == "select":
                options = node.find_all("option")
                chosen = next((option for option in options if "selected" in option.attrs), options[0])
                values[name] = chosen.get("value", chosen.text())
        return values

    def options(self, name: str) -> list[str]:
        return [option.get("value") for option in self.control(name).find_all("option")]


class Page:
    def __init__(self, document: str):
        builder = _TreeBuilder()
        builder.feed(document)
        builder.close()
        self.document = document
        self.root = builder.root

    @property
    def text(self) -> str:
        return self.root.find("body").text()

    @property
    def title(self) -> str:
        return self.root.find("title").text()

    @property
    def lang(self) -> str:
        return self.root.find("html").get("lang")

    @property
    def forms(self) -> list[Form]:
        return [Form(node) for node in self.root.find_all("form")]

    def form(self, action: str) -> Form:
        for form in self.forms:
            if form.action == action:
                return form
        raise AssertionError(f"no form posts to {action}")

    @property
    def cards(self) -> list[Node]:
        return self.root.find_all("article", class_="result")

    def stats(self) -> dict[str, str]:
        section = self.root.find("section", class_="stats")
        return {cell.find("span").text(): cell.find("strong").text() for cell in section.find_all("div")}

    def scripts(self) -> list[str]:
        return [node.raw_text() for node in self.root.find_all("script")]


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: bytes
    request_bytes: int
    request_bytes_read: int

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    @property
    def page(self) -> Page:
        return Page(self.text)

    @property
    def json(self):
        return json.loads(self.text)

    def header(self, name: str) -> str | None:
        return self.headers.get(name)


class RequestReader(io.BytesIO):
    """Request stream that remembers how far the server read before closing."""

    position_at_close = None

    def close(self):
        if not self.closed:
            self.position_at_close = self.tell()
        super().close()


class FakeConnection:
    """The socket boundary seen by the handler: one buffered request in, bytes out."""

    def __init__(self, reader, sockname, shutdown_error=None):
        self.reader = reader
        self.sockname = sockname
        self.output = bytearray()
        self.timeouts: list[float | None] = []
        self.shutdown_error = shutdown_error
        self.shutdowns: list[int] = []

    def makefile(self, mode, *args, **kwargs):
        if "r" not in mode:
            raise AssertionError("responses are written through sendall")
        return self.reader

    def settimeout(self, value):
        self.timeouts.append(value)

    def getsockname(self):
        return self.sockname

    def sendall(self, data):
        self.output += data

    def shutdown(self, how):
        if self.shutdown_error is not None:
            raise self.shutdown_error
        self.shutdowns.append(how)


class _ResponseSource:
    def __init__(self, data: bytes):
        self.data = data

    def makefile(self, mode, *args, **kwargs):
        return io.BytesIO(self.data)


def pattern_image(path: Path, seed: int, size=(160, 120), *, prompt: str | None = None, fmt="PNG") -> Path:
    """Write a deterministic, visually distinctive image (solid colours all hash alike)."""
    generator = random.Random(seed)
    image = Image.new("RGB", (160, 120), tuple(generator.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        left, top = generator.randrange(150), generator.randrange(110)
        draw.rectangle(
            (left, top, left + generator.randrange(10, 80), top + generator.randrange(10, 60)),
            fill=tuple(generator.randrange(256) for _ in range(3)),
        )
    if size != (160, 120):
        image = image.resize(size, Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    if prompt is not None:
        metadata = PngInfo()
        metadata.add_text("parameters", prompt)
        image.save(path, fmt, pnginfo=metadata)
    else:
        image.save(path, fmt)
    return path


def sd_parameters(positive: str, *, negative: str = "lowres", model_hash: str = "ABCDEF12") -> str:
    return f"{positive}\nNegative prompt: {negative}\nSteps: 20, Seed: 1, Model hash: {model_hash}"


class WebTestCase(unittest.TestCase):
    """A WebState configured like ``web.serve`` over a temporary on-disk index."""

    thumbnail_cache_limit = 10
    disable_advanced_options = False
    concurrent_requests = True

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "index.sqlite"
        self.model_paths_path = self.root / "model_paths.ini"
        # Isolate the folder-label configuration from the user's real file.
        for module in (web, scan_module):
            patcher = patch.object(module, "DEFAULT_MODEL_PATHS_PATH", self.model_paths_path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.log))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.conn.commit()
        self.state = self.make_state()

    def make_state(self, **overrides) -> web.WebState:
        options = dict(
            conn=self.conn,
            db_path=self.db_path,
            limit=10,
            min_score=55,
            thumbnail_size=32,
            work_dir=self.root / "work",
            thumbs_dir=self.root / "thumbs",
            thumbnail_cache_limit=self.thumbnail_cache_limit,
            disable_advanced_options=self.disable_advanced_options,
            concurrent_requests=self.concurrent_requests,
        )
        options.update(overrides)
        state = web.WebState(**options)
        self.addCleanup(state.wait_for_scan)
        return state

    # Index helpers -------------------------------------------------------

    def index(self, *roots: Path, model_paths=None, **config):
        stats = scan_roots(
            self.conn,
            list(roots or (self.library,)),
            config=ScanConfig(workers=1, **config),
            model_paths=model_paths or build_config(()),
        )
        self.conn.commit()
        return stats

    def image_id(self, path: Path) -> int:
        return self.conn.execute("SELECT id FROM images WHERE path=?", (str(path.resolve()),)).fetchone()[0]

    # HTTP helpers --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers=(),
        host=_DEFAULT,
        reader=None,
        sockname=None,
        state: web.WebState | None = None,
        shutdown_error=None,
    ) -> Response:
        state = state or self.state
        header_lines = []
        if host is not None:
            header_lines.append(("Host", f"127.0.0.1:{state.server_port}" if host is _DEFAULT else host))
        header_lines.extend(headers)
        if body is not None and not any(name.lower() == "content-length" for name, _ in headers):
            header_lines.append(("Content-Length", str(len(body))))
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in header_lines)
        raw = head.encode("latin-1") + b"\r\n" + (body or b"")
        reader = reader or RequestReader(raw)
        connection = FakeConnection(reader, sockname or ("127.0.0.1", state.server_port), shutdown_error)
        self.last_handler = web._make_handler(state)(connection, ("127.0.0.1", 50000), None)
        response = http.client.HTTPResponse(_ResponseSource(bytes(connection.output)), method=method)
        response.begin()
        payload = response.read()
        return Response(
            response.status,
            response.headers,
            payload,
            len(raw),
            getattr(reader, "position_at_close", None) or 0,
        )

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, fields=None, *, token=_DEFAULT, content_type="application/x-www-form-urlencoded",
             headers=(), state: web.WebState | None = None, **kwargs) -> Response:
        state = state or self.state
        values = dict(fields or {})
        if token is not None:
            values["csrf_token"] = state.csrf_token if token is _DEFAULT else token
        return self.request(
            "POST", path, body=urlencode(values).encode("utf-8"),
            headers=[("Content-Type", content_type), *headers], state=state, **kwargs,
        )

    def submit(self, form: Form, **changes) -> Response:
        self.assertEqual(form.method, "post")
        return self.post(form.action, {**form.fields(), **changes}, token=None)

    def multipart(self, fields: dict[str, str], *, image: bytes | None = None, filename: str | None = "query.png",
                  boundary="----imgdupe-test-boundary") -> tuple[bytes, str]:
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b"\r\n"
            )
        if image is not None:
            disposition = 'form-data; name="image"' + ("" if filename is None else f'; filename="{filename}"')
            # Browsers put the part's Content-Type on its own header line.
            parts.append(
                f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: image/png\r\n\r\n".encode()
                + image + b"\r\n"
            )
        return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"

    def upload(self, image: bytes | None, fields=None, *, filename: str | None = "query.png", token=_DEFAULT) -> Response:
        values = {"lang": "en", "include_exact": "1", **(fields or {})}
        if token is not None:
            values["csrf_token"] = self.state.csrf_token if token is _DEFAULT else token
        body, content_type = self.multipart(values, image=image, filename=filename)
        return self.request("POST", "/search", body=body, headers=[("Content-Type", content_type)])

    # Real socket server --------------------------------------------------

    def start_server(self, state: web.WebState | None = None) -> web.HTTPServer:
        state = state or self.state
        server = web.HTTPServer(("127.0.0.1", 0), web._make_handler(state))
        state.server_port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "test server did not stop")

        self.addCleanup(stop)
        return server

    def http(self, server: web.HTTPServer, method: str, path: str, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            self.fail(f"local HTTP request did not complete: {exc!r}")
        finally:
            client.close()
