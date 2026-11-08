from __future__ import annotations

import html
import json
import ctypes
import os
import re
import secrets
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from email.message import Message
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer as BaseHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from PIL import Image, ImageOps

from .config import ScanConfig
from . import db as database
from .db import checkpoint_database, connect, init_db
from .model_paths import (
    DEFAULT_MODEL_PATHS_PATH,
    build_config,
    load_model_paths,
    make_rule,
    save_model_paths,
)
from .query import PromptSearchResult, query_image, query_prompt_tags
from .scan import ScanStats, scan_roots
from .settings import AppSettings, load_settings
from .utils import human_size


@dataclass(frozen=True)
class SearchOptions:
    limit: int
    min_score: float
    include_exact: bool
    tryhard: bool
    quality: str
    lang: str


@dataclass(frozen=True)
class PromptSearchOptions:
    tags: str
    negative_tags: str
    match_mode: str
    sort_by: str
    limit: int
    offset: int
    lang: str
    unique_prompts: bool
    model_type: str = "all"


@dataclass(frozen=True)
class ScanRequest:
    roots: tuple[Path, ...]
    workers: int
    crop_index: bool
    prompt_index: bool
    stealth_prompt: bool
    reindex_prompts: bool
    delete_missing: bool
    lang: str


@dataclass(frozen=True)
class WebScanStatus:
    state: str = "idle"
    roots: tuple[str, ...] = ()
    started_at: str | None = None
    finished_at: str | None = None
    stats: ScanStats | None = None
    error: str | None = None


MAX_FORM_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
BODY_TIMEOUT_SECONDS = 30.0


class HTTPServer(ThreadingMixIn, BaseHTTPServer):
    """Bound request concurrency; each request owns its SQLite connection."""

    max_request_workers = 8

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(self.max_request_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class RequestError(ValueError):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def _request_authority(value: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit("//" + value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or any(char.isspace() for char in value)):
        raise ValueError("invalid request authority")
    return parsed.hostname.casefold(), parsed.port if parsed.port is not None else 80


def _csrf_forms(document: str, token: str) -> str:
    field = f'<input type="hidden" name="csrf_token" value="{html.escape(token, quote=True)}">'
    return re.sub(
        r"""(<form\b(?=[^>]*\bmethod=["']post["'])(?=[^>]*\baction=["']/(?!/)[^"']*["'])[^>]*>)""",
        lambda match: match.group(0) + field,
        document,
        flags=re.IGNORECASE,
    )


PROMPT_SEARCH_DEFAULT_LIMIT = 100
PROMPT_SEARCH_MAX_LIMIT = 500
TRANSLATIONS = {
    "en": {
        "app_title": "Image Search",
        "failures": "Failures",
        "image_search": "Image Search",
        "prompt_search": "Prompt Search",
        "indexed": "Indexed",
        "searchable": "Searchable",
        "groups": "Groups",
        "matches": "Matches",
        "image": "Image",
        "prompt_tags": "Prompt Tags",
        "prompt_placeholder": "1girl, blue eyes, smile",
        "boolean_search_help": "Comma means AND; | means OR within a group. Example: a | b, c",
        "model_type": "Model category",
        "model_all": "All models",
        "negative_prompt_tags": "Negative Tags",
        "negative_prompt_placeholder": "lowres, blurry, text",
        "match_mode": "Match mode",
        "sort_by": "Sort by",
        "sort_created_desc": "Creation date, newest first",
        "sort_created_asc": "Creation date, oldest first",
        "sort_modified_desc": "Modified date, newest first",
        "sort_modified_asc": "Modified date, oldest first",
        "sort_name_asc": "Name, A to Z",
        "sort_name_desc": "Name, Z to A",
        "all_tags": "All tags",
        "any_tag": "Any tag",
        "autocomplete": "Autocomplete",
        "unique_prompts": "Unique prompts only",
        "excluding_tags": "excluding {tags}",
        "show_in_folder": "Show in folder",
        "folder_opened": "Folder Opened",
        "expand_prompt": "Expand prompt",
        "collapse_prompt": "Collapse prompt",
        "language": "Language",
        "english": "English",
        "chinese": "Chinese",
        "quality": "Quality",
        "strict": "Strict",
        "balanced": "Balanced",
        "loose": "Loose",
        "min_score": "Minimum score",
        "max_results": "Max results",
        "include_exact": "Include exact file matches",
        "tryhard": "Tryhard crop search",
        "crop_index": "Crop index",
        "crop_ready": "ready",
        "crop_missing": "not built",
        "advanced_features": "Advanced features",
        "previous_page": "Previous",
        "next_page": "Next",
        "page_number": "Page {page}",
        "clear_thumbnail_cache": "Clear thumbnail cache",
        "thumbnail_cache_cleared": "Cleared {count} cached thumbnails.",
        "search": "Search",
        "new_search": "New search",
        "results": "Results",
        "prompt_results": "Prompt Results",
        "prompt_showing": "Tags {mode} · max {limit} results · offset {offset}",
        "prompt_sorted": "sorted by {sort}",
        "showing": "Quality {quality} · score at least {score:.1f} · max {limit} results · exact matches {exact} · tryhard {tryhard}",
        "shown": "shown",
        "hidden": "hidden",
        "on": "on",
        "off": "off",
        "no_results": "No results met the current score threshold.",
        "no_prompt_results": "No indexed prompts matched those tags.",
        "prompts": "Prompts",
        "unique_tags": "Unique tags",
        "indexing_failures": "Indexing Failures",
        "no_failures": "No indexing failures recorded.",
        "path": "Path",
        "error": "Error",
        "search_error": "Search Error",
        "exact_match": "Exact visual match",
        "strong_match": "Strong visual match",
        "probable_match": "Probable visual match",
        "possible_match": "Possible visual match",
        "scan_index": "Index / Update index",
        "scan_roots": "Folders or images to index",
        "scan_placeholder": "E:\\Images\nF:\\Downloads",
        "scan_idle": "Index updates only run after you confirm them here. Starting the web UI does not scan folders automatically.",
        "scan_running": "Index update running since {started_at}. This page will refresh automatically.",
        "scan_complete": "Last update finished at {finished_at}: seen {seen}, queued {queued}, indexed {indexed}, moved {moved}, deleted {deleted}, skipped {skipped}, failed {failed}.",
        "scan_failed": "Last update failed at {finished_at}: {error}",
        "workers": "Workers",
        "index_prompt_tags": "Index prompt tags",
        "build_crop_index": "Build crop index",
        "reindex_prompts": "Reindex prompts",
        "read_stealth_prompts": "Read stealth prompts",
        "delete_missing": "Delete records for missing files",
        "confirm_update_index": "Confirm update index",
        "rescan_configured": "Rescan all configured folders",
        "classify_folders": "Classify folders",
        "classify_help": "These folders have no configured model label. Enter any label to store in model_paths.ini before scanning.",
        "model_label": "Model label",
        "save_and_scan": "Save labels and scan",
    },
    "zh": {
        "app_title": "图片搜索",
        "failures": "失败记录",
        "image_search": "以图搜图",
        "prompt_search": "提示词搜索",
        "indexed": "已索引",
        "searchable": "可搜索",
        "groups": "分组",
        "matches": "匹配",
        "image": "图片",
        "prompt_tags": "提示词标签",
        "prompt_placeholder": "1girl, blue eyes, smile",
        "boolean_search_help": "逗号表示 AND；| 表示同一组内的 OR。例如：a | b, c",
        "model_type": "模型分类",
        "model_all": "所有模型",
        "negative_prompt_tags": "排除标签",
        "negative_prompt_placeholder": "lowres, blurry, text",
        "match_mode": "匹配模式",
        "sort_by": "排序",
        "sort_created_desc": "创建时间，新到旧",
        "sort_created_asc": "创建时间，旧到新",
        "sort_modified_desc": "修改时间，新到旧",
        "sort_modified_asc": "修改时间，旧到新",
        "sort_name_asc": "名称，A 到 Z",
        "sort_name_desc": "名称，Z 到 A",
        "all_tags": "全部标签",
        "any_tag": "任一标签",
        "autocomplete": "自动补全",
        "unique_prompts": "同一提示词仅显示一次",
        "excluding_tags": "排除 {tags}",
        "show_in_folder": "在文件夹中显示",
        "folder_opened": "已打开文件夹",
        "expand_prompt": "展开提示词",
        "collapse_prompt": "收起提示词",
        "language": "语言",
        "english": "英语",
        "chinese": "中文",
        "quality": "匹配强度",
        "strict": "严格",
        "balanced": "平衡",
        "loose": "宽松",
        "min_score": "最低分数",
        "max_results": "最多结果",
        "include_exact": "包含完全相同的文件",
        "tryhard": "深度裁剪搜索",
        "crop_index": "裁剪索引",
        "crop_ready": "已建立",
        "crop_missing": "未建立",
        "advanced_features": "高级功能",
        "previous_page": "上一页",
        "next_page": "下一页",
        "page_number": "第 {page} 页",
        "clear_thumbnail_cache": "清除缩略图缓存",
        "thumbnail_cache_cleared": "已清除 {count} 张缓存缩略图。",
        "search": "搜索",
        "new_search": "重新搜索",
        "results": "搜索结果",
        "prompt_results": "提示词搜索结果",
        "prompt_showing": "标签{mode} · 最多 {limit} 个结果 · 偏移 {offset}",
        "prompt_sorted": "按{sort}排序",
        "showing": "匹配强度 {quality} · 最低分数 {score:.1f} · 最多 {limit} 个结果 · 完全匹配{exact} · 深度搜索{tryhard}",
        "shown": "显示",
        "hidden": "隐藏",
        "on": "开启",
        "off": "关闭",
        "no_results": "没有结果达到当前分数阈值。",
        "no_prompt_results": "没有已索引提示词匹配这些标签。",
        "prompts": "提示词",
        "unique_tags": "唯一标签",
        "indexing_failures": "索引失败记录",
        "no_failures": "没有记录到索引失败。",
        "path": "路径",
        "error": "错误",
        "search_error": "搜索错误",
        "exact_match": "完全视觉匹配",
        "strong_match": "强视觉匹配",
        "probable_match": "可能视觉匹配",
        "possible_match": "疑似视觉匹配",
        "scan_index": "索引 / 更新索引",
        "scan_roots": "要索引的文件夹或图片",
        "scan_placeholder": "E:\\图片\nF:\\下载",
        "scan_idle": "索引更新只会在你手动确认后运行。启动网页界面不会自动扫描文件夹。",
        "scan_running": "正在更新索引，开始时间：{started_at}。页面会自动刷新。",
        "scan_complete": "上次更新完成于 {finished_at}：发现 {seen}，待处理 {queued}，已索引 {indexed}，已移动 {moved}，已删除 {deleted}，已跳过 {skipped}，失败 {failed}。",
        "scan_failed": "上次更新失败于 {finished_at}：{error}",
        "workers": "并行数",
        "index_prompt_tags": "索引提示词标签",
        "build_crop_index": "建立裁剪索引",
        "reindex_prompts": "重新索引提示词",
        "read_stealth_prompts": "读取隐写提示词",
        "delete_missing": "删除已不存在的记录",
        "confirm_update_index": "确认更新索引",
        "rescan_configured": "一键重扫所有已配置目录",
        "classify_folders": "分类目录",
        "classify_help": "以下目录尚未配置模型标签。输入任意标签后会保存到 model_paths.ini，再开始扫描。",
        "model_label": "模型标签",
        "save_and_scan": "保存标签并扫描",
    },
}


def serve(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    limit: int = 100,
    min_score: float = 55.0,
    thumbnail_size: int = 256,
    open_browser: bool = False,
    settings: AppSettings | None = None,
) -> str:
    settings = settings or load_settings()
    work_dir = Path(tempfile.mkdtemp(prefix="imgdupe-web-"))
    thumbs_dir = db_path.resolve().with_suffix(db_path.suffix + ".thumb-cache")
    state = WebState(
        conn=conn,
        db_path=db_path,
        limit=limit,
        min_score=min_score,
        thumbnail_size=thumbnail_size,
        work_dir=work_dir,
        thumbs_dir=thumbs_dir,
        thumbnail_cache_limit=settings.thumbnail_cache_limit,
        disable_advanced_options=settings.disable_advanced_options,
        concurrent_requests=True,
    )
    handler = _make_handler(state)
    server = HTTPServer((host, port), handler)
    state.server_port = server.server_port
    state.allowed_hosts = frozenset({host.casefold()})
    if host.casefold() in {"localhost", "127.0.0.1", "::1"}:
        state.allowed_hosts = frozenset({"localhost", "127.0.0.1", "::1"})
    url = f"http://{host}:{server.server_port}"
    try:
        print(f"Serving imgdupe web UI at {url}")
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    finally:
        server.server_close()
        state.wait_for_scan()
        checkpoint = checkpoint_database(state.conn, "TRUNCATE")
        if checkpoint and checkpoint[0]:
            print(f"WAL checkpoint remained busy: {checkpoint}")
        shutil.rmtree(state.work_dir, ignore_errors=True)
    return url


class WebState:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        db_path: Path,
        limit: int,
        min_score: float,
        thumbnail_size: int,
        work_dir: Path,
        thumbs_dir: Path,
        thumbnail_cache_limit: int,
        disable_advanced_options: bool,
        concurrent_requests: bool = False,
    ) -> None:
        self._conn = conn
        self._request_state = threading.local()
        self.concurrent_requests = concurrent_requests
        self.csrf_token = secrets.token_urlsafe(32)
        self.allowed_hosts = frozenset({"localhost", "127.0.0.1", "::1"})
        self.server_port = 8765
        self.db_path = db_path
        self.limit = limit
        self.min_score = min_score
        self.thumbnail_size = thumbnail_size
        self.work_dir = work_dir
        self.uploads_dir = work_dir / "uploads"
        self.thumbs_dir = thumbs_dir
        self.thumbnail_cache_limit = thumbnail_cache_limit
        self.disable_advanced_options = disable_advanced_options
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.scan_lock = threading.Lock()
        self.mutation_lock = threading.Lock()
        self.thumbnail_lock = threading.RLock()
        self.thumbnail_workers = [threading.Lock() for _ in range(64)]
        self.scan_status = WebScanStatus()
        self.scan_thread: threading.Thread | None = None
        _prune_thumbnail_cache(self.thumbs_dir, self.thumbnail_cache_limit)
        self._thumbnails: OrderedDict[Path, None] = OrderedDict()
        self._thumbnail_versions: dict[str, set[Path]] = {}
        for path in _cached_thumbnail_paths(self.thumbs_dir):
            self._remember_thumbnail(path)
        self.thumbnail_cache_count = len(self._thumbnails)

    @property
    def conn(self) -> sqlite3.Connection:
        if getattr(self._request_state, "active", False):
            if not hasattr(self._request_state, "connection"):
                self._request_state.connection = connect(self.db_path)
            return self._request_state.connection
        return self._conn

    @conn.setter
    def conn(self, value: sqlite3.Connection) -> None:
        self._conn = value

    @contextmanager
    def request_context(self):
        if not self.concurrent_requests:
            yield
            return
        self._request_state.active = True
        try:
            yield
        finally:
            connection = getattr(self._request_state, "connection", None)
            if connection is not None:
                connection.close()
                del self._request_state.connection
            self._request_state.active = False

    def _remember_thumbnail(self, path: Path) -> None:
        self._thumbnails[path] = None
        self._thumbnails.move_to_end(path)
        self._thumbnail_versions.setdefault(path.name.split("-", 1)[0], set()).add(path)

    def start_scan(self, request: ScanRequest) -> bool:
        with self.scan_lock:
            if self.scan_status.state == "running":
                return False
            self.scan_status = WebScanStatus(
                state="running",
                roots=tuple(str(root) for root in request.roots),
                started_at=_local_timestamp(),
            )
        thread = threading.Thread(
            target=_run_scan_job,
            args=(self, request),
            name="imgdupe-web-scan",
            daemon=False,
        )
        with self.scan_lock:
            self.scan_thread = thread
        thread.start()
        return True

    def wait_for_scan(self) -> None:
        with self.scan_lock:
            thread = self.scan_thread
        if thread is not None and thread.is_alive():
            print("Waiting for the active index update to finish...")
            thread.join()

    def clear_thumbnail_cache(self) -> int:
        with self.thumbnail_lock:
            removed = 0
            for path in self.thumbs_dir.glob("*.jpg"):
                try:
                    path.unlink()
                    self.thumbnail_removed(path)
                    removed += 1
                except OSError:
                    pass
            return removed

    def thumbnail_removed(self, path: Path) -> None:
        with self.thumbnail_lock:
            self._thumbnails.pop(path, None)
            key = path.name.split("-", 1)[0]
            versions = self._thumbnail_versions.get(key)
            if versions is not None:
                versions.discard(path)
                if not versions:
                    del self._thumbnail_versions[key]
            self.thumbnail_cache_count = len(self._thumbnails)

    def thumbnail_created(self, path: Path) -> None:
        with self.thumbnail_lock:
            self._remember_thumbnail(path)
            # Evict only known oldest entries; no directory scan/stat/sort on a
            # miss. An undeletable file stays accounted for and is retried later.
            for victim in list(islice(self._thumbnails, max(0, len(self._thumbnails) - max(1, self.thumbnail_cache_limit)))):
                try:
                    victim.unlink(missing_ok=True)
                    self.thumbnail_removed(victim)
                except OSError:
                    pass
            self.thumbnail_cache_count = len(self._thumbnails)

    def get_scan_status(self) -> WebScanStatus:
        with self.scan_lock:
            return self.scan_status

    def finish_scan(self, status: WebScanStatus) -> None:
        with self.scan_lock:
            self.scan_status = status


def _run_scan_job(state: WebState, request: ScanRequest) -> None:
    started_at = state.get_scan_status().started_at or _local_timestamp()
    try:
        conn = connect(state.db_path)
        try:
            init_db(conn)
            stats = scan_roots(
                conn,
                list(request.roots),
                config=ScanConfig(
                    workers=request.workers,
                    crop_index=request.crop_index,
                    prompt_index=request.prompt_index,
                    stealth_prompt=request.stealth_prompt,
                    reindex_prompts=request.reindex_prompts,
                    delete_missing=request.delete_missing,
                ),
            )
        finally:
            conn.close()
        state.finish_scan(
            WebScanStatus(
                state="completed",
                roots=tuple(str(root) for root in request.roots),
                started_at=started_at,
                finished_at=_local_timestamp(),
                stats=stats,
            )
        )
    except Exception as exc:
        state.finish_scan(
            WebScanStatus(
                state="failed",
                roots=tuple(str(root) for root in request.roots),
                started_at=started_at,
                finished_at=_local_timestamp(),
                error=f"{type(exc).__name__}: {exc}",
            )
        )


def _local_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _make_handler(state: WebState):
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            self._header_timer = threading.Timer(REQUEST_TIMEOUT_SECONDS, self._expire_headers)
            self._header_timer.daemon = True
            self._header_timer.start()

        def _expire_headers(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

        def finish(self) -> None:
            self._header_timer.cancel()
            super().finish()

        def end_headers(self) -> None:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            super().end_headers()

        def _check_request_source(self) -> None:
            self._header_timer.cancel()
            hosts = self.headers.get_all("Host", [])
            try:
                if len(hosts) != 1:
                    raise ValueError("exactly one Host header is required")
                authority = _request_authority(hosts[0])
                local_host = self.connection.getsockname()[0].casefold()
                allowed_hosts = state.allowed_hosts | {local_host}
                if authority[0] not in allowed_hosts or authority[1] != state.server_port:
                    raise ValueError("untrusted Host header")
                if self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
                    raise ValueError("cross-site request rejected")
                for name in ("Origin", "Referer"):
                    value = self.headers.get(name)
                    if value is not None:
                        origin = urllib.parse.urlsplit(value)
                        if origin.scheme != "http" or _request_authority(origin.netloc) != authority:
                            raise ValueError("foreign request origin")
            except ValueError as exc:
                raise RequestError(HTTPStatus.FORBIDDEN, str(exc)) from exc

        def _read_request_body(self, limit: int) -> bytes:
            self.close_connection = True
            if self.headers.get("Transfer-Encoding"):
                raise RequestError(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is unsupported")
            if self.headers.get("Content-Encoding", "identity").casefold() != "identity":
                raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Encoding is unsupported")
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
                raise RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            if len(lengths[0]) > 20 or int(lengths[0]) > limit:
                raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            remaining = int(lengths[0])
            deadline = time.monotonic() + BODY_TIMEOUT_SECONDS
            chunks = []
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise RequestError(HTTPStatus.REQUEST_TIMEOUT, "request body deadline exceeded")
                self.connection.settimeout(timeout)
                try:
                    chunk = self.rfile.read1(min(64 * 1024, remaining))
                except OSError as exc:
                    raise RequestError(HTTPStatus.REQUEST_TIMEOUT, "request body timed out") from exc
                if not chunk:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "incomplete request body")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _prepare_post(self) -> None:
            self._check_request_source()
            is_upload = urllib.parse.urlsplit(self.path).path == "/search"
            body = self._read_request_body(MAX_UPLOAD_BYTES if is_upload else MAX_FORM_BYTES)
            content_type = self.headers.get("Content-Type", "")
            media = Message()
            media["Content-Type"] = content_type
            if media.get_content_charset("utf-8") not in {"utf-8", "utf8"}:
                raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "only UTF-8 form encoding is supported")
            try:
                if is_upload:
                    if media.get_content_type() != "multipart/form-data":
                        raise ValueError("expected multipart/form-data upload")
                    self._multipart = _extract_multipart(body, _boundary_from_content_type(content_type))
                    self._fields = self._multipart[0]
                else:
                    if media.get_content_type() != "application/x-www-form-urlencoded":
                        raise ValueError("expected form-urlencoded request")
                    self._fields = {
                        key: values[-1]
                        for key, values in urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4096).items()
                    }
            except ValueError as exc:
                raise RequestError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            token = self._fields.get("csrf_token", "").encode("utf-8")
            if not secrets.compare_digest(token, state.csrf_token.encode("ascii")):
                raise RequestError(HTTPStatus.FORBIDDEN, "invalid CSRF token")

        def do_GET(self) -> None:
            with state.request_context():
                self._do_GET()

        def _do_GET(self) -> None:
            try:
                self._check_request_source()
            except RequestError as exc:
                self.send_error(exc.status, str(exc))
                return
            parsed = urllib.parse.urlparse(self.path)
            lang = _lang_from_query(parsed.query)
            if parsed.path == "/":
                self._send_html(_index_page(state, lang, parsed.query))
                return
            if parsed.path == "/scan-status":
                self._send_json({"state": state.get_scan_status().state})
                return
            if parsed.path == "/failures":
                self._send_html(_failures_page(state, lang))
                return
            if parsed.path == "/suggest-tags":
                self._send_json(_tag_suggestions(state, parsed.query))
                return
            if parsed.path == "/show-in-folder":
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "use the authenticated POST form")
                return
            if parsed.path.startswith("/thumb/"):
                self._send_thumbnail(parsed.path.removeprefix("/thumb/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            with state.request_context():
                try:
                    self._prepare_post()
                except RequestError as exc:
                    self.send_error(exc.status, str(exc))
                    return
                if urllib.parse.urlparse(self.path).path in {"/scan", "/scan-configure", "/scan-configured", "/clear-thumbnail-cache"}:
                    with state.mutation_lock:
                        self._do_POST()
                else:
                    self._do_POST()

        def _do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/show-in-folder":
                self._show_in_folder(self._fields, _normalize_lang(self._fields.get("lang")))
                return
            if parsed.path == "/clear-thumbnail-cache":
                lang = _normalize_lang(self._fields.get("lang"))
                removed = state.clear_thumbnail_cache()
                self._redirect(
                    f"/?lang={urllib.parse.quote(lang)}&thumbnail_cache_cleared={removed}"
                )
                return
            if parsed.path == "/scan":
                try:
                    request = self._save_scan_request()
                    unconfigured = _unconfigured_scan_roots(request.roots)
                    if unconfigured:
                        self._send_html(_model_path_prompt_page(request, unconfigured))
                        return
                    if not state.start_scan(request):
                        raise RuntimeError("an index update is already running")
                    self._redirect(f"/?lang={request.lang}#scan-index")
                except Exception as exc:
                    self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/scan-configure":
                try:
                    fields = self._read_form_fields()
                    request = _scan_request_from_fields(fields)
                    if state.get_scan_status().state == "running":
                        raise RuntimeError("an index update is already running")
                    unconfigured = _unconfigured_scan_roots(request.roots)
                    config = load_model_paths(DEFAULT_MODEL_PATHS_PATH)
                    if config.issues:
                        raise ValueError("model_paths.ini contains invalid rules; fix them before adding labels")
                    additions = []
                    expected = {str(root).casefold(): root for root in unconfigured}
                    submitted: set[str] = set()
                    target_keys = sorted(
                        (key for key in fields if key.startswith("model_target_")),
                        key=lambda key: int(key.removeprefix("model_target_")),
                    )
                    for target_key in target_keys:
                        index = target_key.removeprefix("model_target_")
                        target = _configured_target(Path(fields[target_key]))
                        canonical_key = str(target).casefold()
                        if canonical_key not in expected or canonical_key in submitted:
                            raise ValueError("the folder classification request is stale or invalid")
                        submitted.add(canonical_key)
                        additions.append(make_rule(target, fields.get(f"model_label_{index}", "")))
                    if submitted != set(expected):
                        raise ValueError("the folder classification request is incomplete or stale")
                    if additions:
                        config = build_config((*config.rules, *additions))
                        save_model_paths(DEFAULT_MODEL_PATHS_PATH, config)
                    state.start_scan(request)
                    self._redirect(f"/?lang={request.lang}#scan-index")
                except Exception as exc:
                    self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/scan-configured":
                try:
                    fields = self._read_form_fields()
                    lang = _normalize_lang(fields.get("lang"))
                    config = load_model_paths(DEFAULT_MODEL_PATHS_PATH)
                    if config.issues:
                        raise ValueError("model_paths.ini contains invalid rules; fix them before rescanning")
                    roots = tuple(root for root in config.deduplicated_rescan_roots() if root.exists())
                    if not roots:
                        raise ValueError("no configured folders are currently available")
                    defaults = ScanConfig()
                    request = ScanRequest(
                        roots=roots,
                        workers=defaults.workers,
                        crop_index=False,
                        prompt_index=True,
                        stealth_prompt=False,
                        reindex_prompts=False,
                        delete_missing=False,
                        lang=lang,
                    )
                    if not state.start_scan(request):
                        raise RuntimeError("an index update is already running")
                    self._redirect(f"/?lang={lang}#scan-index")
                except Exception as exc:
                    self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/prompt-search":
                try:
                    options = self._save_prompt_search()
                    results = query_prompt_tags(
                        state.conn,
                        options.tags,
                        negative_text_or_tags=options.negative_tags,
                        match_mode=options.match_mode,
                        limit=options.limit + 1,
                        offset=options.offset,
                        unique_prompts=options.unique_prompts,
                        sort_by=options.sort_by,
                        model_type=options.model_type,
                    )
                    has_next = len(results) > options.limit
                    self._send_html(_prompt_results_page(results[: options.limit], options, has_next))
                except Exception as exc:
                    self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path != "/search":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            upload_path = None
            try:
                upload_path, options = self._save_upload()
                results = query_image(
                    state.conn,
                    upload_path,
                    limit=options.limit,
                    min_score=options.min_score,
                    include_exact=options.include_exact,
                    tryhard=options.tryhard,
                )
                self._send_html(_results_page(results, options))
            except Exception as exc:
                self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
            finally:
                if upload_path is not None:
                    upload_path.unlink(missing_ok=True)

        def _save_scan_request(self) -> ScanRequest:
            return _scan_request_from_fields(self._read_form_fields())

        def _read_form_fields(self) -> dict[str, str]:
            return self._fields

        def _save_prompt_search(self) -> PromptSearchOptions:
            fields = self._read_form_fields()
            tags = fields.get("tags", "").strip()
            negative_tags = fields.get("negative_tags", "").strip()
            if not tags and not negative_tags:
                raise ValueError("enter at least one prompt tag or negative tag")
            return PromptSearchOptions(
                tags=tags,
                negative_tags=negative_tags,
                match_mode=fields.get("match_mode") if fields.get("match_mode") in {"all", "any"} else "all",
                sort_by=_normalize_prompt_sort(fields.get("sort_by")),
                limit=max(1, min(_int_field(fields, "limit", PROMPT_SEARCH_DEFAULT_LIMIT), PROMPT_SEARCH_MAX_LIMIT)),
                offset=max(0, _int_field(fields, "offset", 0)),
                lang=_normalize_lang(fields.get("lang")),
                unique_prompts=fields.get("unique_prompts") == "1",
                model_type=fields.get("model_type", "all").strip()[:500] or "all",
            )

        def _show_in_folder(self, fields: dict[str, str], lang: str) -> None:
            try:
                image_id = int(fields.get("id", ""))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid image id")
                return
            row = state.conn.execute(
                "SELECT path FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if row is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = Path(row["path"]).resolve()
            try:
                _show_path_in_folder(path)
            except OSError as exc:
                self._send_html(_error_page(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            text = _text(lang)
            self._send_html(
                _page(
                    text["folder_opened"],
                    f"""
                    <p class="muted">{text['folder_opened']}</p>
                    <script>
                      if (history.length > 1) {{
                        history.back();
                      }} else {{
                        window.close();
                        setTimeout(() => {{
                          window.location.href = "/";
                        }}, 200);
                      }}
                    </script>
                    """,
                    lang=lang,
                )
            )

        def _save_upload(self) -> tuple[Path, SearchOptions]:
            fields, file_bytes, filename = self._multipart
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                suffix = ".upload"
            options = _options_from_fields(fields, state)
            upload_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=state.uploads_dir, prefix="query-", suffix=suffix, delete=False) as upload:
                    upload_path = Path(upload.name)
                upload_path.write_bytes(file_bytes)
            except OSError:
                if upload_path is not None:
                    upload_path.unlink(missing_ok=True)
                raise
            return upload_path, options

        def _send_thumbnail(self, raw_image_id: str) -> None:
            try:
                image_id = int(raw_image_id.removesuffix(".jpg"))
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            row = state.conn.execute(
                "SELECT path, size_bytes, mtime_ns FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if row is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            thumb_path = state.thumbs_dir / (
                f"{image_id}-{int(row['mtime_ns'])}-{int(row['size_bytes'])}-{state.thumbnail_size}-v2.jpg"
            )
            with state.thumbnail_workers[image_id % len(state.thumbnail_workers)]:
                with state.thumbnail_lock:
                    cached = thumb_path.exists()
                    if cached:
                        state._remember_thumbnail(thumb_path)
                        try:
                            os.utime(thumb_path, None)
                        except OSError:
                            pass
                        data = thumb_path.read_bytes()
                    else:
                        for stale_path in tuple(state._thumbnail_versions.get(str(image_id), ())):
                            try:
                                stale_path.unlink()
                                state.thumbnail_removed(stale_path)
                            except OSError:
                                pass
                if not cached:
                    pending = thumb_path.with_suffix(".pending")
                    if not _write_thumbnail(Path(row["path"]), pending, state.thumbnail_size):
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    try:
                        with state.thumbnail_lock:
                            os.replace(pending, thumb_path)
                            # Read before eviction so even a one-entry cache is safe.
                            data = thumb_path.read_bytes()
                            state.thumbnail_created(thumb_path)
                    except OSError:
                        _discard_thumbnail(pending)
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
            self._send_bytes(data, "image/jpeg")

        def _send_html(self, document: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = _csrf_forms(document, state.csrf_token).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, value: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, data: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

    return Handler


def _scan_request_from_fields(fields: dict[str, str]) -> ScanRequest:
    roots = tuple(
        Path(line.strip()).expanduser()
        for line in fields.get("roots", "").splitlines()
        if line.strip()
    )
    if not roots:
        raise ValueError("enter at least one folder or image path to index")
    missing_roots = [str(root) for root in roots if not root.exists()]
    if missing_roots:
        raise ValueError(f"path does not exist: {missing_roots[0]}")
    default_config = ScanConfig()
    workers = max(1, min(_int_field(fields, "workers", default_config.workers), 64))
    return ScanRequest(
        roots=roots,
        workers=workers,
        crop_index=fields.get("crop_index") == "1",
        prompt_index=fields.get("prompt_index") == "1",
        stealth_prompt=fields.get("stealth_prompt") == "1",
        reindex_prompts=fields.get("reindex_prompts") == "1",
        delete_missing=fields.get("delete_missing") == "1",
        lang=_normalize_lang(fields.get("lang")),
    )


def _configured_target(root: Path) -> Path:
    canonical = root.resolve(strict=False)
    return canonical if canonical.is_dir() else canonical.parent


def _unconfigured_scan_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    config = load_model_paths(DEFAULT_MODEL_PATHS_PATH)
    if config.issues:
        details = "; ".join(f"{issue.section}: {issue.message}" for issue in config.issues)
        raise ValueError(f"invalid model_paths.ini: {details}")
    missing: list[Path] = []
    seen: set[str] = set()
    for supplied in roots:
        target = _configured_target(supplied)
        key = str(target).casefold()
        if key in seen or config.resolve(target) is not None:
            continue
        seen.add(key)
        missing.append(target)
    return tuple(missing)


def _scan_request_hidden_fields(request: ScanRequest) -> str:
    values = {
        "roots": "\n".join(str(root) for root in request.roots),
        "workers": str(request.workers),
        "lang": request.lang,
    }
    for name in ("crop_index", "prompt_index", "stealth_prompt", "reindex_prompts", "delete_missing"):
        if getattr(request, name):
            values[name] = "1"
    return "".join(
        f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
        for name, value in values.items()
    )


def _model_path_prompt_page(request: ScanRequest, roots: tuple[Path, ...]) -> str:
    text = _text(request.lang)
    rows = []
    for index, root in enumerate(roots):
        rows.append(
            f"""
            <label class="prompt-input-label">
              <span class="path">{html.escape(str(root))}</span>
              <span>{text['model_label']}</span>
              <input type="hidden" name="model_target_{index}" value="{html.escape(str(root), quote=True)}">
              <input type="text" name="model_label_{index}" required autocomplete="off">
            </label>
            """
        )
    body = f"""
    <nav><a href="/?lang={request.lang}#scan-index">{text['new_search']}</a></nav>
    <section class="scan-panel">
      <h1>{text['classify_folders']}</h1>
      <p>{text['classify_help']}</p>
      <form method="post" action="/scan-configure">
        {_scan_request_hidden_fields(request)}
        {''.join(rows)}
        <button type="submit">{text['save_and_scan']}</button>
      </form>
    </section>
    """
    return _page(text["classify_folders"], body, lang=request.lang)


def _model_filter_options(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return database.fetch_model_options(conn)


def _scan_panel(state: WebState, lang: str) -> str:
    text = _text(lang)
    status = state.get_scan_status()
    default_config = ScanConfig()
    roots_value = "\n".join(status.roots)
    disabled = "disabled" if status.state == "running" else ""
    status_html = ""
    refresh_script = ""
    advanced_options = "" if state.disable_advanced_options else f"""
        <details class="advanced">
          <summary>{text["advanced_features"]}</summary>
          <label class="checkbox">
            <input type="checkbox" name="crop_index" value="1" {disabled}>
            {text["build_crop_index"]}
          </label>
        </details>
    """
    if status.state == "running":
        status_html = f"""
        <p class="scan-status is-running">
          {text["scan_running"].format(started_at=html.escape(status.started_at or ""))}
        </p>
        """
        refresh_script = """<script>
        const pollScan = async () => {
          try {
            const response = await fetch('/scan-status');
            if (response.ok && (await response.json()).state !== 'running') {
              window.location.reload();
              return;
            }
          } catch (error) { /* Retry after a temporary connection failure. */ }
          setTimeout(pollScan, 3000);
        };
        setTimeout(pollScan, 3000);
        </script>"""
    elif status.state == "completed" and status.stats is not None:
        stats = status.stats
        status_html = f"""
        <p class="scan-status is-complete">
          {text["scan_complete"].format(
              finished_at=html.escape(status.finished_at or ""),
              seen=stats.seen,
              queued=stats.queued,
              indexed=stats.indexed,
              moved=stats.moved,
              deleted=stats.deleted,
              skipped=stats.skipped,
              failed=stats.failed,
          )}
        </p>
        """
    elif status.state == "failed":
        status_html = f"""
        <p class="scan-status is-failed">
          {text["scan_failed"].format(
              finished_at=html.escape(status.finished_at or ""),
              error=html.escape(status.error or "unknown error"),
          )}
        </p>
        """
    else:
        status_html = f"""
        <p class="scan-status">
          {text["scan_idle"]}
        </p>
        """

    return f"""
    <section id="scan-index" class="scan-panel">
      <form method="post" action="/scan">
        <input type="hidden" name="lang" value="{html.escape(lang)}">
        <h2>{text["scan_index"]}</h2>
        {status_html}
        <label class="prompt-input-label" for="scan-roots">{text["scan_roots"]}</label>
        <textarea id="scan-roots" name="roots" rows="4" placeholder="{html.escape(text["scan_placeholder"])}" required {disabled}>{html.escape(roots_value)}</textarea>
        <div class="controls scan-controls">
          <label>
            {text["workers"]}
            <input type="number" name="workers" min="1" max="64" step="1" value="{default_config.workers}" {disabled}>
          </label>
          <label class="checkbox">
            <input type="checkbox" name="prompt_index" value="1" checked {disabled}>
            {text["index_prompt_tags"]}
          </label>
          <label class="checkbox">
            <input type="checkbox" name="reindex_prompts" value="1" {disabled}>
            {text["reindex_prompts"]}
          </label>
          <label class="checkbox">
            <input type="checkbox" name="stealth_prompt" value="1" {disabled}>
            {text["read_stealth_prompts"]}
          </label>
          <label class="checkbox">
            <input type="checkbox" name="delete_missing" value="1" {disabled}>
            {text["delete_missing"]}
          </label>
        </div>
        {advanced_options}
        <button type="submit" {disabled}>{text["confirm_update_index"]}</button>
      </form>
      <form method="post" action="/scan-configured" class="configured-rescan-form">
        <input type="hidden" name="lang" value="{html.escape(lang, quote=True)}">
        <button type="submit" {disabled}>{text["rescan_configured"]}</button>
      </form>
    </section>
    {refresh_script}
    """


def _index_page(state: WebState, lang: str, query: str = "") -> str:
    text = _text(lang)
    stats = _stats(state.conn)
    query_values = urllib.parse.parse_qs(query)
    model_filter_options = _model_filter_options(state.conn)
    model_filter_html = "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(label)}</option>'
        for value, label in model_filter_options
    )
    cache_notice = ""
    if "thumbnail_cache_cleared" in query_values:
        try:
            removed = max(0, int(query_values["thumbnail_cache_cleared"][-1]))
        except ValueError:
            removed = 0
        cache_notice = (
            f'<p class="notice">{text["thumbnail_cache_cleared"].format(count=removed)}</p>'
        )
    image_advanced_options = "" if state.disable_advanced_options else f"""
        <details class="advanced">
          <summary>{text['advanced_features']}</summary>
          <label class="checkbox">
            <input type="checkbox" name="tryhard" value="1">
            {text['tryhard']}
          </label>
          <span class="muted">{text['crop_index']}: {text['crop_ready'] if stats['crop_ready'] else text['crop_missing']}</span>
        </details>
    """
    body = f"""
    <header class="topbar">
      <div>
        <h1>{text['app_title']}</h1>
        <p class="muted">{html.escape(str(state.db_path))}</p>
      </div>
      <nav class="toplinks">
        <a href="#image-search">{text['image_search']}</a>
        <a href="#prompt-search">{text['prompt_search']}</a>
        <a href="/failures?lang={lang}">{text['failures']}</a>
        <form method="post" action="/clear-thumbnail-cache" class="inline-form">
          <input type="hidden" name="lang" value="{html.escape(lang, quote=True)}">
          <button type="submit" class="link-button">{text['clear_thumbnail_cache']}</button>
        </form>
      </nav>
    </header>
    {cache_notice}
    <section class="stats">
      <div><strong>{stats['indexed']}</strong><span>{text['indexed']}</span></div>
      <div><strong>{stats['hashed']}</strong><span>{text['searchable']}</span></div>
      <div><strong>{stats['failed']}</strong><span>{text['failures']}</span></div>
      <div><strong>{stats['groups']}</strong><span>{text['groups']}</span></div>
      <div><strong>{stats['matches']}</strong><span>{text['matches']}</span></div>
      <div><strong>{stats['prompts']}</strong><span>{text['prompts']}</span></div>
      <div><strong>{stats['unique_tags']}</strong><span>{text['unique_tags']}</span></div>
    </section>
    <section class="search">
      <form id="image-search" method="post" action="/search" enctype="multipart/form-data">
        <h2>{text['image_search']}</h2>
        <label class="dropzone" for="image-input">
          <input id="image-input" type="file" name="image" accept="image/*" required>
          <span>{text['image']}</span>
        </label>
        <div class="controls">
          <label>
            {text['language']}
            <select name="lang">
              <option value="en" {_selected(lang, 'en')}>{text['english']}</option>
              <option value="zh" {_selected(lang, 'zh')}>{text['chinese']}</option>
            </select>
          </label>
          <label>
            {text['quality']}
            <select name="quality">
              <option value="balanced" selected>{text['balanced']}</option>
              <option value="strict">{text['strict']}</option>
              <option value="loose">{text['loose']}</option>
            </select>
          </label>
          <label>
            {text['min_score']}
            <input type="number" name="min_score" min="0" max="100" step="1" value="{state.min_score:.0f}">
          </label>
          <label>
            {text['max_results']}
            <input type="number" name="limit" min="1" max="500" step="1" value="{state.limit}">
          </label>
          <label class="checkbox">
            <input type="checkbox" name="include_exact" value="1" checked>
            {text['include_exact']}
          </label>
        </div>
        {image_advanced_options}
        <button type="submit">{text['search']}</button>
      </form>
      <form id="prompt-search" method="post" action="/prompt-search" class="prompt-form">
        <h2>{text['prompt_search']}</h2>
        <label class="prompt-input-label" for="prompt-tags-input">{text['prompt_tags']}</label>
        <p class="muted">{text['boolean_search_help']}</p>
        <div class="prompt-input-wrap">
          <textarea id="prompt-tags-input" name="tags" rows="5" placeholder="{text['prompt_placeholder']}" autocomplete="off" spellcheck="false"></textarea>
          <div id="tag-suggestions" class="suggestions" hidden></div>
        </div>
        <label class="prompt-input-label" for="negative-prompt-tags-input">{text['negative_prompt_tags']}</label>
        <div class="prompt-input-wrap">
          <textarea id="negative-prompt-tags-input" name="negative_tags" rows="3" placeholder="{text['negative_prompt_placeholder']}" autocomplete="off" spellcheck="false"></textarea>
          <div id="negative-tag-suggestions" class="suggestions" hidden></div>
        </div>
        <label class="autocomplete-toggle">
          <input id="autocomplete-enabled" type="checkbox" checked>
          {text['autocomplete']}
        </label>
        <label class="autocomplete-toggle">
          <input name="unique_prompts" type="checkbox" value="1" checked>
          {text['unique_prompts']}
        </label>
        <div class="controls prompt-controls">
          <input type="hidden" name="lang" value="{lang}">
          <input type="hidden" name="match_mode" value="all">
          <label>
            {text['model_type']}
            <select name="model_type">
              <option value="all" selected>{text['model_all']}</option>
              {model_filter_html}
            </select>
          </label>
          <label>
            {text['sort_by']}
            <select name="sort_by">
              <option value="created_desc" selected>{text['sort_created_desc']}</option>
              <option value="created_asc">{text['sort_created_asc']}</option>
              <option value="modified_desc">{text['sort_modified_desc']}</option>
              <option value="modified_asc">{text['sort_modified_asc']}</option>
              <option value="name_asc">{text['sort_name_asc']}</option>
              <option value="name_desc">{text['sort_name_desc']}</option>
            </select>
          </label>
          <label>
            {text['max_results']}
            <input type="number" name="limit" min="1" max="{PROMPT_SEARCH_MAX_LIMIT}" step="1" value="{PROMPT_SEARCH_DEFAULT_LIMIT}">
          </label>
          <input type="hidden" name="offset" value="0">
        </div>
        <button type="submit">{text['search']}</button>
      </form>
    </section>
    {_scan_panel(state, lang)}
    <script>
      const input = document.querySelector("#image-input");
      const language = document.querySelector("select[name='lang']");
      const quality = document.querySelector("select[name='quality']");
      const minScore = document.querySelector("input[name='min_score']");
      const tryhard = document.querySelector("input[name='tryhard']");
      const promptInput = document.querySelector("#prompt-tags-input");
      const negativePromptInput = document.querySelector("#negative-prompt-tags-input");
      const suggestions = document.querySelector("#tag-suggestions");
      const negativeSuggestions = document.querySelector("#negative-tag-suggestions");
      const autocomplete = document.querySelector("#autocomplete-enabled");
      const scores = {{ strict: "75", balanced: "{state.min_score:.0f}", loose: "40" }};
      let activeSuggestion = -1;
      let suggestionRequest = 0;
      let activePromptInput = promptInput;
      language.addEventListener("change", () => {{
        window.location.href = "/?lang=" + encodeURIComponent(language.value);
      }});
      quality.addEventListener("change", () => {{
        minScore.value = scores[quality.value] || scores.balanced;
      }});
      if (tryhard) {{
        tryhard.addEventListener("change", () => {{
          if (tryhard.checked && Number(minScore.value) > 1) minScore.value = "1";
        }});
      }}
      window.addEventListener("paste", event => {{
        const item = [...event.clipboardData.items].find(i => i.type.startsWith("image/"));
        if (!item) return;
        const file = item.getAsFile();
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      }});
      const suggestionsForInput = inputElement => (
        inputElement === negativePromptInput ? negativeSuggestions : suggestions
      );
      const tokenInfo = inputElement => {{
        const value = inputElement.value;
        const caret = inputElement.selectionStart ?? value.length;
        const left = value.slice(0, caret);
        const operators = [",", "，", "、", ";", "；", "|", "｜", "\\n"];
        const start = Math.max(...operators.map(operator => left.lastIndexOf(operator))) + 1;
        return {{ start, end: caret, token: value.slice(start, caret).trimStart() }};
      }};
      const hideSuggestions = suggestionsElement => {{
        suggestionsElement.hidden = true;
        suggestionsElement.innerHTML = "";
        delete suggestionsElement.dataset.request;
        delete suggestionsElement.dataset.query;
        activeSuggestion = -1;
      }};
      const hideAllSuggestions = () => {{
        hideSuggestions(suggestions);
        hideSuggestions(negativeSuggestions);
      }};
      const suggestionsCurrent = (inputElement, suggestionsElement) => (
        !suggestionsElement.hidden
        && suggestionsElement.dataset.request === String(suggestionRequest)
        && suggestionsElement.dataset.query === tokenInfo(inputElement).token.trim()
      );
      const suggestionButtons = suggestionsElement => [...suggestionsElement.querySelectorAll("button[data-tag]")];
      const setActiveSuggestion = (suggestionsElement, index) => {{
        const buttons = suggestionButtons(suggestionsElement);
        if (!buttons.length) {{
          activeSuggestion = -1;
          return;
        }}
        activeSuggestion = (index + buttons.length) % buttons.length;
        buttons.forEach((button, buttonIndex) => {{
          button.classList.toggle("is-active", buttonIndex === activeSuggestion);
        }});
      }};
      const applySuggestion = (inputElement, suggestionsElement, tag) => {{
        const info = tokenInfo(inputElement);
        const value = inputElement.value;
        const leading = value.slice(info.start, info.end).match(/^\\s*/)?.[0] || "";
        const after = value.slice(info.end);
        const separator = /^[,，、;；|｜]/.test(after) ? "" : ", ";
        const insert = leading + tag + separator;
        inputElement.value = value.slice(0, info.start) + insert + value.slice(info.end);
        const caret = info.start + insert.length;
        inputElement.focus();
        inputElement.setSelectionRange(caret, caret);
        hideSuggestions(suggestionsElement);
      }};
      let suggestionTimer;
      let suggestionController;
      const dismissSuggestions = () => {{
        clearTimeout(suggestionTimer);
        suggestionController?.abort();
        ++suggestionRequest;
        hideAllSuggestions();
      }};
      const scheduleSuggestions = inputElement => {{
        clearTimeout(suggestionTimer);
        suggestionController?.abort();
        ++suggestionRequest;
        hideAllSuggestions();
        suggestionTimer = setTimeout(() => refreshSuggestions(inputElement), 180);
      }};
      const refreshSuggestions = async inputElement => {{
        clearTimeout(suggestionTimer);
        suggestionController?.abort();
        suggestionController = new AbortController();
        const signal = suggestionController.signal;
        const suggestionsElement = suggestionsForInput(inputElement);
        const request = ++suggestionRequest;
        activePromptInput = inputElement;
        const otherSuggestions = suggestionsElement === suggestions ? negativeSuggestions : suggestions;
        hideSuggestions(otherSuggestions);
        if (!autocomplete.checked) return hideSuggestions(suggestionsElement);
        const info = tokenInfo(inputElement);
        const query = info.token.trim();
        if (query.length < 1) return hideSuggestions(suggestionsElement);
        try {{
        const response = await fetch("/suggest-tags?q=" + encodeURIComponent(query), {{signal}});
        if (request !== suggestionRequest || activePromptInput !== inputElement) return false;
        if (!response.ok) return hideSuggestions(suggestionsElement);
        const tags = await response.json();
        if (request !== suggestionRequest || activePromptInput !== inputElement) return false;
        if (!tags.length) return hideSuggestions(suggestionsElement);
        suggestionsElement.innerHTML = tags.map(item => (
          `<button type="button" data-tag="${{item.tag.replaceAll("&", "&amp;").replaceAll('"', "&quot;")}}">
             <span>${{item.tag.replaceAll("&", "&amp;").replaceAll("<", "&lt;")}}</span>
             <small>${{item.count}}</small>
           </button>`
        )).join("");
        suggestionsElement.dataset.request = String(request);
        suggestionsElement.dataset.query = query;
        suggestionsElement.hidden = false;
        activeSuggestion = -1;
        return true;
        }} catch (error) {{
          if (request === suggestionRequest) hideSuggestions(suggestionsElement);
          return false;
        }}
      }};
      const bindPromptAutocomplete = inputElement => {{
        const suggestionsElement = suggestionsForInput(inputElement);
        inputElement.addEventListener("input", () => scheduleSuggestions(inputElement));
        inputElement.addEventListener("click", () => scheduleSuggestions(inputElement));
        inputElement.addEventListener("focus", () => scheduleSuggestions(inputElement));
        inputElement.addEventListener("keydown", async event => {{
          if (event.key === "ArrowDown") {{
            event.preventDefault();
            if (!suggestionsCurrent(inputElement, suggestionsElement)) {{
              await refreshSuggestions(inputElement);
            }}
            if (suggestionsCurrent(inputElement, suggestionsElement)) {{
              setActiveSuggestion(suggestionsElement, activeSuggestion + 1);
            }}
          }} else if (event.key === "ArrowUp") {{
            event.preventDefault();
            if (!suggestionsCurrent(inputElement, suggestionsElement)) {{
              await refreshSuggestions(inputElement);
            }}
            const buttons = suggestionButtons(suggestionsElement);
            if (suggestionsCurrent(inputElement, suggestionsElement) && buttons.length) {{
              setActiveSuggestion(
                suggestionsElement,
                activeSuggestion < 0 ? buttons.length - 1 : activeSuggestion - 1
              );
            }}
          }} else if (event.key === "Enter") {{
            if (!suggestionsCurrent(inputElement, suggestionsElement)) return;
            const buttons = suggestionButtons(suggestionsElement);
            if (!buttons.length) return;
            event.preventDefault();
            const button = buttons[activeSuggestion >= 0 ? activeSuggestion : 0];
            if (button) applySuggestion(inputElement, suggestionsElement, button.dataset.tag);
          }} else if (event.key === "Escape") {{
            dismissSuggestions();
          }}
        }});
      }};
      bindPromptAutocomplete(promptInput);
      bindPromptAutocomplete(negativePromptInput);
      autocomplete.addEventListener("change", () => refreshSuggestions(activePromptInput));
      [suggestions, negativeSuggestions].forEach(suggestionsElement => {{
        suggestionsElement.addEventListener("click", event => {{
          const button = event.target.closest("button[data-tag]");
          if (button) {{
            const inputElement = suggestionsElement === negativeSuggestions ? negativePromptInput : promptInput;
            if (suggestionsCurrent(inputElement, suggestionsElement)) applySuggestion(inputElement, suggestionsElement, button.dataset.tag);
          }}
        }});
      }});
      document.addEventListener("click", event => {{
        if (!event.target.closest(".prompt-input-wrap") && event.target !== autocomplete) dismissSuggestions();
      }});
    </script>
    """
    return _page(text["app_title"], body, lang=lang)


def _results_page(results, options: SearchOptions) -> str:
    text = _text(options.lang)
    cards = []
    for image_row, candidate, score in results:
        cards.append(
            f"""
            <article class="result">
              <a href="{html.escape(Path(image_row['path']).as_uri())}">
                <img src="/thumb/{int(image_row['id'])}.jpg?v={int(image_row['mtime_ns'])}-{int(image_row['size_bytes'])}" alt="" loading="lazy">
              </a>
              <div class="score">{score.score:.2f}</div>
              <div>{html.escape(_label(score.decision, options.lang))}</div>
              <div>{image_row['width']}x{image_row['height']} &middot; {human_size(image_row['size_bytes'])}</div>
              <form method="post" action="/show-in-folder" class="folder-form">
                <input type="hidden" name="id" value="{int(image_row['id'])}">
                <input type="hidden" name="lang" value="{options.lang}">
                <button class="folder-link" type="submit">{text['show_in_folder']}</button>
              </form>
              <div class="details">pHash {score.phash_dist} &middot; wHash {score.whash_dist} &middot; dHash {score.dhash_dist} &middot; grid {score.grid_match_count} &middot; crop {score.crop_min_dist} &middot; bands {candidate.total_hits}</div>
              <p class="path">{html.escape(image_row['path'])}</p>
            </article>
            """
        )
    empty = f"<p>{text['no_results']}</p>" if not cards else ""
    summary = text["showing"].format(
        quality=html.escape(_quality_label(options.quality, options.lang)),
        score=options.min_score,
        limit=options.limit,
        exact=text["shown"] if options.include_exact else text["hidden"],
        tryhard=text["on"] if options.tryhard else text["off"],
    )
    body = f"""
    <nav><a href="/?lang={options.lang}">{text['new_search']}</a></nav>
    <header>
      <h1>{text['results']}</h1>
      <p>{summary}</p>
    </header>
    <section class="grid">{''.join(cards)}</section>
    {empty}
    """
    return _page(text["results"], body, lang=options.lang)


def _prompt_results_page(
    results: list[PromptSearchResult],
    options: PromptSearchOptions,
    has_next: bool,
) -> str:
    text = _text(options.lang)
    cards = []
    for result in results:
        image_row = result.image_row
        visible_matched_tags = tuple(
            tag for tag in result.matched_tags
            if not tag.strip().casefold().startswith("@model:")
        )
        prompt_html = _highlight_prompt(result.prompt or "", visible_matched_tags)
        prompt_button = (
            f"""
              <button class="prompt-toggle" type="button" data-expanded="false">
                {text['expand_prompt']}
              </button>
            """
            if result.prompt
            else ""
        )
        cards.append(
            f"""
            <article class="result">
              <a href="{html.escape(Path(image_row['path']).as_uri())}">
                <img src="/thumb/{int(image_row['id'])}.jpg?v={int(image_row['mtime_ns'])}-{int(image_row['size_bytes'])}" alt="" loading="lazy">
              </a>
              <div class="score">{result.matched_count}</div>
              <div>{html.escape(', '.join(visible_matched_tags))}</div>
              <div>{image_row['width']}x{image_row['height']} &middot; {human_size(image_row['size_bytes'])}</div>
              <div>{html.escape(_prompt_model_label(image_row))}</div>
              <form method="post" action="/show-in-folder" class="folder-form">
                <input type="hidden" name="id" value="{int(image_row['id'])}">
                <input type="hidden" name="lang" value="{options.lang}">
                <button class="folder-link" type="submit">{text['show_in_folder']}</button>
              </form>
              <p class="prompt is-collapsed">{prompt_html}</p>
              {prompt_button}
              <p class="path">{html.escape(image_row['path'])}</p>
            </article>
            """
        )
    mode_label = text["all_tags"] if options.match_mode == "all" else text["any_tag"]
    summary = text["prompt_showing"].format(
        mode=html.escape(mode_label),
        limit=options.limit,
        offset=options.offset,
    )
    summary = f"{summary} · {text['prompt_sorted'].format(sort=html.escape(_prompt_sort_label(options.sort_by, options.lang)))}"
    if options.unique_prompts:
        summary = f"{summary} · {text['unique_prompts']}"
    if options.model_type != "all":
        summary = f"{summary} · {text['model_type']}: {html.escape(options.model_type.upper())}"
    if options.negative_tags:
        summary = f"{summary} · {text['excluding_tags'].format(tags=html.escape(options.negative_tags))}"
    empty = f"<p>{text['no_prompt_results']}</p>" if not cards else ""
    query_lines = [html.escape(options.tags)] if options.tags else []
    if options.negative_tags:
        query_lines.append(text["excluding_tags"].format(tags=html.escape(options.negative_tags)))
    query_summary = "<br>".join(query_lines)
    pager = _prompt_pager(options, has_next)
    body = f"""
    <nav><a href="/?lang={options.lang}">{text['new_search']}</a></nav>
    <header>
      <h1>{text['prompt_results']}</h1>
      <p>{summary}</p>
      <p class="muted">{query_summary}</p>
    </header>
    {pager}
    <section class="grid">{''.join(cards)}</section>
    {empty}
    {pager}
    <script>
      const expandLabel = {json.dumps(text['expand_prompt'])};
      const collapseLabel = {json.dumps(text['collapse_prompt'])};
      document.querySelectorAll(".prompt-toggle").forEach(button => {{
        button.addEventListener("click", () => {{
          const prompt = button.previousElementSibling;
          const expanded = button.dataset.expanded === "true";
          prompt.classList.toggle("is-collapsed", expanded);
          button.dataset.expanded = expanded ? "false" : "true";
          button.textContent = expanded ? expandLabel : collapseLabel;
        }});
      }});
    </script>
    """
    return _page(text["prompt_results"], body, lang=options.lang)


def _prompt_pager(options: PromptSearchOptions, has_next: bool) -> str:
    text = _text(options.lang)
    previous = (
        _prompt_page_form(
            options,
            max(0, options.offset - options.limit),
            text["previous_page"],
        )
        if options.offset > 0
        else ""
    )
    following = (
        _prompt_page_form(
            options,
            options.offset + options.limit,
            text["next_page"],
        )
        if has_next
        else ""
    )
    page = options.offset // options.limit + 1
    return f"""
    <nav class="pager">
      {previous}
      <span>{text['page_number'].format(page=page)}</span>
      {following}
    </nav>
    """


def _prompt_model_label(image_row: sqlite3.Row) -> str:
    configured_model = str(image_row["configured_model"] or "").strip()
    model_type = (configured_model or str(image_row["model_type"] or "sd")).upper()
    details: list[str] = []
    if model_type == "NAI4" and image_row["model_version"] == "4.5":
        details.append("4.5")
    model_hash = str(image_row["model_hash"] or "").strip()
    if model_hash:
        details.append(model_hash)
    return f"{model_type} ({', '.join(details)})" if details else model_type


def _prompt_page_form(options: PromptSearchOptions, offset: int, label: str) -> str:
    unique_field = '<input type="hidden" name="unique_prompts" value="1">' if options.unique_prompts else ""
    return f"""
    <form method="post" action="/prompt-search" class="pager-form">
      <input type="hidden" name="tags" value="{html.escape(options.tags, quote=True)}">
      <input type="hidden" name="negative_tags" value="{html.escape(options.negative_tags, quote=True)}">
      <input type="hidden" name="match_mode" value="{html.escape(options.match_mode, quote=True)}">
      <input type="hidden" name="sort_by" value="{html.escape(options.sort_by, quote=True)}">
      <input type="hidden" name="model_type" value="{html.escape(options.model_type, quote=True)}">
      <input type="hidden" name="limit" value="{options.limit}">
      <input type="hidden" name="offset" value="{offset}">
      <input type="hidden" name="lang" value="{html.escape(options.lang, quote=True)}">
      {unique_field}
      <button type="submit">{html.escape(label)}</button>
    </form>
    """


def _highlight_prompt(prompt: str, matched_tags: tuple[str, ...]) -> str:
    raw_prompt = prompt.strip()
    tags = sorted(
        {tag.strip() for tag in matched_tags if tag.strip()},
        key=len,
        reverse=True,
    )
    if not raw_prompt or not tags:
        return html.escape(raw_prompt)

    pattern = re.compile(
        "|".join(re.escape(tag) for tag in tags),
        flags=re.IGNORECASE,
    )
    fragments = []
    start = 0
    for match in pattern.finditer(raw_prompt):
        fragments.append(html.escape(raw_prompt[start:match.start()]))
        fragments.append(f"<mark>{html.escape(match.group(0))}</mark>")
        start = match.end()
    fragments.append(html.escape(raw_prompt[start:]))
    return "".join(fragments)


def _failures_page(state: WebState, lang: str) -> str:
    text = _text(lang)
    rows = state.conn.execute(
        """
        SELECT i.path,
               CASE
                   WHEN i.decode_error IS NOT NULL AND ip.prompt_error IS NOT NULL
                       THEN i.decode_error || '; prompt: ' || ip.prompt_error
                   WHEN i.decode_error IS NOT NULL THEN i.decode_error
                   ELSE 'prompt: ' || ip.prompt_error
               END AS error
        FROM images i
        LEFT JOIN image_prompts ip ON ip.image_id = i.id
        WHERE i.decode_error IS NOT NULL
           OR (ip.prompt_error IS NOT NULL AND ip.prompt_error NOT IN ('no_metadata', 'no_prompt'))
        ORDER BY i.indexed_at DESC, i.path
        LIMIT 500
        """
    ).fetchall()
    items = []
    for row in rows:
        items.append(
            f"""
            <tr>
              <td class="path">{html.escape(row['path'])}</td>
              <td>{html.escape(row['error'] or '')}</td>
            </tr>
            """
        )
    body = f"""
    <nav><a href="/?lang={lang}">{text['search']}</a></nav>
    <h1>{text['indexing_failures']}</h1>
    {f"<p>{text['no_failures']}</p>" if not items else ""}
    <p><a href="/failures?lang=en">{text['english']}</a> · <a href="/failures?lang=zh">{text['chinese']}</a></p>
    <table>
      <thead><tr><th>{text['path']}</th><th>{text['error']}</th></tr></thead>
      <tbody>{''.join(items)}</tbody>
    </table>
    """
    return _page(text["failures"], body, lang=lang)


def _tag_suggestions(state: WebState, query: str) -> list[dict[str, object]]:
    values = urllib.parse.parse_qs(query)
    prefix = (values.get("q", [""])[0] or "").strip().casefold()
    if not prefix:
        return []
    return database.fetch_tag_suggestions(state.conn, prefix)


def _error_page(exc: Exception) -> str:
    text = _text("en")
    return _page(
        text["search_error"],
        f"""
        <nav><a href="/">New search</a></nav>
        <h1>{text['search_error']}</h1>
        <p>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</p>
        """,
    )


def _page(title: str, body: str, *, lang: str = "zh") -> str:
    return f"""<!doctype html>
<html lang="{html.escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #20262d; background: #f7f9fb; }}
    main {{ max-width: 1200px; margin: 0 auto; }}
    a {{ color: #245b9f; }}
    button {{ border: 0; border-radius: 6px; background: #245b9f; color: #fff; padding: 10px 16px; font-weight: 700; }}
    .topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; margin-bottom: 16px; }}
    .toplinks {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: flex-end; }}
    .inline-form {{ display: inline; width: auto; }}
    .configured-rescan-form {{ display: block; width: fit-content; margin-top: 12px; }}
    .link-button {{ background: transparent; color: #245b9f; padding: 0; text-decoration: underline; cursor: pointer; }}
    .notice {{ border: 1px solid #b7dfc2; border-radius: 6px; background: #effaf2; color: #236c3b; padding: 10px 12px; }}
    .muted {{ color: #52606d; word-break: break-all; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .stats div {{ background: #fff; border: 1px solid #d9e1e8; border-radius: 6px; padding: 12px; }}
    .stats strong {{ display: block; font-size: 24px; }}
    .stats span {{ color: #52606d; }}
    .scan-panel {{ margin: 0 0 24px; padding: 16px 0 24px; border-top: 1px solid #d9e1e8; border-bottom: 1px solid #d9e1e8; }}
    .scan-panel h2 {{ margin: 0; font-size: 20px; }}
    .scan-status {{ color: #52606d; margin: 0; }}
    .scan-status.is-running {{ color: #245b9f; font-weight: 700; }}
    .scan-status.is-complete {{ color: #236c3b; font-weight: 700; }}
    .scan-status.is-failed {{ color: #9b1c1c; font-weight: 700; }}
    .scan-controls {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .search {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 24px; align-items: start; }}
    form {{ display: grid; gap: 16px; width: 100%; }}
    form h2 {{ margin: 0; font-size: 20px; }}
    .prompt-form {{ border-left: 1px solid #d9e1e8; padding-left: 24px; }}
    @media (max-width: 760px) {{ .prompt-form {{ border-left: 0; border-top: 1px solid #d9e1e8; padding-left: 0; padding-top: 24px; }} }}
    .prompt-input-label {{ display: block; color: #3e4c59; font-size: 13px; font-weight: 700; }}
    .prompt-input-wrap {{ position: relative; }}
    textarea {{ box-sizing: border-box; width: 100%; min-width: 0; min-height: 160px; padding: 12px; border: 1px solid #bcccdc; border-radius: 6px; background: #fff; font: inherit; line-height: 1.45; resize: vertical; }}
    textarea:focus, .controls input:focus, .controls select:focus {{ outline: 2px solid #9fb3c8; outline-offset: 1px; }}
    .autocomplete-toggle {{ display: flex; align-items: center; gap: 8px; color: #3e4c59; font-size: 13px; }}
    .suggestions {{ position: absolute; z-index: 10; left: 0; right: 0; top: calc(100% + 6px); border: 1px solid #bcccdc; border-radius: 6px; background: #fff; box-shadow: 0 8px 20px rgba(15, 23, 42, 0.14); overflow: hidden; }}
    .suggestions button {{ display: flex; justify-content: space-between; gap: 12px; width: 100%; border: 0; border-radius: 0; background: #fff; color: #20262d; padding: 9px 12px; font-weight: 500; text-align: left; }}
    .suggestions button:hover, .suggestions button:focus, .suggestions button.is-active {{ background: #eef2f6; outline: 0; }}
    .suggestions small {{ color: #52606d; }}
    .prompt-controls {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .dropzone {{ display: grid; place-items: center; min-height: 220px; border: 2px dashed #9fb3c8; border-radius: 8px; background: #fff; cursor: pointer; }}
    .dropzone input {{ max-width: 90%; }}
    .dropzone span {{ color: #52606d; font-weight: 700; }}
    .controls {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .controls label {{ display: grid; gap: 6px; font-size: 13px; color: #52606d; }}
    .controls input, .controls select {{ min-width: 0; padding: 8px; border: 1px solid #bcccdc; border-radius: 6px; background: #fff; }}
    .controls .checkbox {{ display: flex; align-items: center; gap: 8px; }}
    .controls .checkbox input {{ width: auto; }}
    .advanced {{ border: 1px solid #d9e1e8; border-radius: 6px; background: #fff; padding: 10px 12px; }}
    .advanced summary {{ cursor: pointer; color: #3e4c59; font-weight: 700; }}
    .advanced[open] {{ display: grid; gap: 12px; }}
    .advanced .checkbox {{ display: flex; align-items: center; gap: 8px; color: #52606d; font-size: 13px; }}
    .pager {{ display: flex; align-items: center; justify-content: center; gap: 12px; margin: 16px 0; }}
    .pager-form {{ display: block; width: auto; }}
    button:disabled, input:disabled, textarea:disabled {{ opacity: 0.65; cursor: not-allowed; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
    .result {{ border: 1px solid #d9e1e8; border-radius: 6px; background: #fff; padding: 12px; }}
    img {{ width: 100%; aspect-ratio: 1; object-fit: contain; background: #eef2f6; }}
    .score {{ font-size: 24px; font-weight: 800; margin-top: 8px; }}
    .details {{ color: #52606d; font-size: 13px; margin-top: 4px; }}
    .prompt {{ color: #52606d; font-size: 13px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .prompt.is-collapsed {{ max-height: 4.35em; overflow: hidden; position: relative; }}
    .prompt.is-collapsed::after {{ content: ""; position: absolute; inset: auto 0 0 0; height: 2em; background: linear-gradient(to bottom, rgba(255,255,255,0), #fff); }}
    .prompt mark {{ background: #ffe58f; color: inherit; padding: 0 2px; border-radius: 3px; }}
    .prompt-toggle {{ justify-self: start; background: transparent; color: #245b9f; padding: 0; font-weight: 700; }}
    .folder-link {{ display: inline-block; justify-self: start; margin-top: 4px; border: 1px solid #bcccdc; border-radius: 6px; background: #fff; color: #245b9f; padding: 7px 10px; font-size: 13px; font-weight: 700; text-decoration: none; }}
    .folder-link:hover, .folder-link:focus {{ background: #eef2f6; outline: 0; }}
    .path {{ word-break: break-all; color: #3e4c59; font-size: 13px; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff; }}
    th, td {{ border-bottom: 1px solid #d9e1e8; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f6f8; }}
  </style>
</head>
<body>
  <main>{body}</main>
</body>
</html>
"""


def _write_thumbnail(source: Path, target: Path, thumbnail_size: int) -> bool:
    temporary = None
    try:
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
            img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (thumbnail_size, thumbnail_size), (245, 247, 250))
            left = (thumbnail_size - img.width) // 2
            top = (thumbnail_size - img.height) // 2
            canvas.paste(img, (left, top))
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                canvas.save(output, "JPEG", quality=82, optimize=True)
            os.replace(temporary, target)
        return True
    except Exception:
        return False
    finally:
        if temporary is not None:
            _discard_thumbnail(temporary)


def _discard_thumbnail(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _cached_thumbnail_paths(cache_dir: Path) -> list[Path]:
    entries = []
    for path in cache_dir.glob("*.jpg"):
        try:
            entries.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue  # A stale cache entry must not prevent the UI from starting.
    return [path for _, path in sorted(entries, key=lambda entry: entry[0])]


def _prune_thumbnail_cache(cache_dir: Path, limit: int) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    thumbnails = list(reversed(_cached_thumbnail_paths(cache_dir)))
    for path in thumbnails[max(1, limit) :]:
        try:
            path.unlink()
        except OSError:
            pass


def _show_path_in_folder(path: Path) -> None:
    if os.name == "nt" and path.exists():
        try:
            _sh_open_folder_and_select(path)
            return
        except OSError:
            pass
    command = "explorer.exe" if os.name == "nt" else ("open" if sys.platform == "darwin" else "xdg-open")
    subprocess.Popen(
        [command, str(path.parent)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sh_open_folder_and_select(path: Path) -> None:
    ole32 = ctypes.OleDLL("ole32")
    shell32 = ctypes.OleDLL("shell32")
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell32.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

    pidl = ctypes.c_void_p()
    sfgao = wintypes.DWORD()
    initialized = ole32.CoInitialize(None)
    try:
        result = shell32.SHParseDisplayName(
            str(path),
            None,
            ctypes.byref(pidl),
            0,
            ctypes.byref(sfgao),
        )
        if result != 0:
            raise ctypes.WinError(result)
        result = shell32.SHOpenFolderAndSelectItems(
            pidl,
            0,
            None,
            0,
        )
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        if pidl:
            ole32.CoTaskMemFree(pidl)
        if initialized in (0, 1):
            ole32.CoUninitialize()


def _boundary_from_content_type(content_type: str) -> bytes:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            return ("--" + part.removeprefix("boundary=").strip('"')).encode()
    raise ValueError("missing multipart boundary")


def _extract_multipart(body: bytes, boundary: bytes) -> tuple[dict[str, str], bytes, str]:
    fields: dict[str, str] = {}
    file_bytes: bytes | None = None
    filename = "query.upload"
    for part in body.split(boundary):
        header, _, content = part.partition(b"\r\n\r\n")
        if not header or not content:
            continue
        header_text = header.decode("utf-8", errors="replace")
        content = content.removesuffix(b"\r\n")
        name = _field_name_from_header(header_text)
        if not name:
            continue
        if name == "image":
            filename = _filename_from_header(header_text)
            file_bytes = content
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    if file_bytes is not None:
        return fields, file_bytes, filename
    raise ValueError("no image file was uploaded")


def _field_name_from_header(header: str) -> str | None:
    marker = 'name="'
    if marker not in header:
        return None
    return header.split(marker, 1)[1].split('"', 1)[0]


def _filename_from_header(header: str) -> str:
    marker = "filename="
    if marker not in header:
        return "query.upload"
    value = header.split(marker, 1)[1].partition("\n")[0].split(";", 1)[0].strip().strip('"')
    return value or "query.upload"


def _options_from_fields(fields: dict[str, str], state: WebState) -> SearchOptions:
    quality = fields.get("quality", "balanced")
    quality_scores = {
        "strict": 75.0,
        "balanced": state.min_score,
        "loose": 40.0,
    }
    min_score = _float_field(fields, "min_score", quality_scores.get(quality, state.min_score))
    limit = _int_field(fields, "limit", state.limit)
    tryhard = fields.get("tryhard") == "1"
    if tryhard:
        min_score = min(min_score, 1.0)
    return SearchOptions(
        limit=max(1, min(limit, 500)),
        min_score=max(0.0, min(min_score, 100.0)),
        include_exact=fields.get("include_exact") == "1",
        tryhard=tryhard,
        quality=quality if quality in quality_scores else "balanced",
        lang=_normalize_lang(fields.get("lang")),
    )


def _int_field(fields: dict[str, str], key: str, default: int) -> int:
    try:
        return int(fields.get(key, ""))
    except ValueError:
        return default


def _float_field(fields: dict[str, str], key: str, default: float) -> float:
    try:
        return float(fields.get(key, ""))
    except ValueError:
        return default


def _stats(conn: sqlite3.Connection) -> dict[str, int]:
    return database.fetch_stats(conn)


def _count_unique_tags(conn: sqlite3.Connection) -> int:
    return database.count_unique_tags(conn)


def _lang_from_query(query: str) -> str:
    values = urllib.parse.parse_qs(query)
    return _normalize_lang(values.get("lang", ["zh"])[0])


def _normalize_lang(lang: str | None) -> str:
    return lang if lang in TRANSLATIONS else "zh"


def _normalize_prompt_sort(sort_by: str | None) -> str:
    return sort_by if sort_by in _PROMPT_SORT_LABEL_KEYS else "created_desc"


_PROMPT_SORT_LABEL_KEYS = {
    "created_desc": "sort_created_desc",
    "created_asc": "sort_created_asc",
    "modified_desc": "sort_modified_desc",
    "modified_asc": "sort_modified_asc",
    "name_asc": "sort_name_asc",
    "name_desc": "sort_name_desc",
}


def _prompt_sort_label(sort_by: str, lang: str) -> str:
    text = _text(lang)
    return text[_PROMPT_SORT_LABEL_KEYS[_normalize_prompt_sort(sort_by)]]


def _text(lang: str) -> dict[str, str]:
    return TRANSLATIONS[_normalize_lang(lang)]


def _selected(current: str, value: str) -> str:
    return "selected" if current == value else ""


def _quality_label(quality: str, lang: str) -> str:
    text = _text(lang)
    return text.get(quality, quality)


def _label(decision: str, lang: str) -> str:
    text = _text(lang)
    labels = {
        "exact_duplicate": text["exact_match"],
        "strong_duplicate": text["strong_match"],
        "probable_duplicate": text["probable_match"],
        "review": text["possible_match"],
    }
    return labels.get(decision, decision.replace("_", " "))
