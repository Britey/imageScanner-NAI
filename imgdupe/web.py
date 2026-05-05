from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image, ImageOps

from .query import PromptSearchResult, query_image, query_prompt_tags
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
    match_mode: str
    limit: int
    offset: int
    lang: str


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
        "match_mode": "Match mode",
        "all_tags": "All tags",
        "any_tag": "Any tag",
        "autocomplete": "Autocomplete",
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
        "tryhard_hint": "Requires scanning with --crop-index.",
        "tryhard_limited": "No crop index found. Tryhard will split the query image into many temporary crop/tile variants and compare them against the normal index.",
        "search": "Search",
        "new_search": "New search",
        "results": "Results",
        "prompt_results": "Prompt Results",
        "prompt_showing": "Tags {mode} · max {limit} results · offset {offset}",
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
        "match_mode": "匹配模式",
        "all_tags": "全部标签",
        "any_tag": "任一标签",
        "autocomplete": "自动补全",
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
        "tryhard_hint": "需要使用 --crop-index 扫描。",
        "tryhard_limited": "未找到裁剪索引。深度搜索会把查询图片临时切成许多裁剪/分块变体，并与普通索引匹配。",
        "search": "搜索",
        "new_search": "重新搜索",
        "results": "搜索结果",
        "prompt_results": "提示词搜索结果",
        "prompt_showing": "标签{mode} · 最多 {limit} 个结果 · 偏移 {offset}",
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
) -> str:
    state = WebState(
        conn=conn,
        db_path=db_path,
        limit=limit,
        min_score=min_score,
        thumbnail_size=thumbnail_size,
        work_dir=Path(tempfile.mkdtemp(prefix="imgdupe-web-")),
    )
    handler = _make_handler(state)
    server = HTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}"
    try:
        print(f"Serving imgdupe web UI at {url}")
        server.serve_forever()
    finally:
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
    ) -> None:
        self.conn = conn
        self.db_path = db_path
        self.limit = limit
        self.min_score = min_score
        self.thumbnail_size = thumbnail_size
        self.work_dir = work_dir
        self.uploads_dir = work_dir / "uploads"
        self.thumbs_dir = work_dir / "thumbs"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)


def _make_handler(state: WebState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            lang = _lang_from_query(parsed.query)
            if parsed.path == "/":
                self._send_html(_index_page(state, lang))
                return
            if parsed.path == "/failures":
                self._send_html(_failures_page(state, lang))
                return
            if parsed.path == "/suggest-tags":
                self._send_json(_tag_suggestions(state, parsed.query))
                return
            if parsed.path.startswith("/thumb/"):
                self._send_thumbnail(parsed.path.removeprefix("/thumb/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/prompt-search":
                try:
                    options = self._save_prompt_search()
                    results = query_prompt_tags(
                        state.conn,
                        options.tags,
                        match_mode=options.match_mode,
                        limit=options.limit,
                        offset=options.offset,
                    )
                    self._send_html(_prompt_results_page(results, options))
                except Exception as exc:
                    self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path != "/search":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
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

        def _save_prompt_search(self) -> PromptSearchOptions:
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            if not content_type.startswith("application/x-www-form-urlencoded"):
                raise ValueError("expected form-urlencoded prompt search")
            fields = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(body, keep_blank_values=True).items()
            }
            tags = fields.get("tags", "").strip()
            if not tags:
                raise ValueError("enter at least one prompt tag")
            return PromptSearchOptions(
                tags=tags,
                match_mode=fields.get("match_mode") if fields.get("match_mode") in {"all", "any"} else "all",
                limit=max(1, min(_int_field(fields, "limit", state.limit), 500)),
                offset=max(0, _int_field(fields, "offset", 0)),
                lang=_normalize_lang(fields.get("lang")),
            )

        def _save_upload(self) -> tuple[Path, SearchOptions]:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("expected multipart/form-data upload")
            boundary = _boundary_from_content_type(content_type)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            fields, file_bytes, filename = _extract_multipart(body, boundary)
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                suffix = ".upload"
            upload_path = state.uploads_dir / f"query-{len(list(state.uploads_dir.iterdir())) + 1}{suffix}"
            upload_path.write_bytes(file_bytes)
            return upload_path, _options_from_fields(fields, state)

        def _send_thumbnail(self, raw_image_id: str) -> None:
            try:
                image_id = int(raw_image_id.removesuffix(".jpg"))
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            thumb_path = state.thumbs_dir / f"{image_id}.jpg"
            if not thumb_path.exists():
                row = state.conn.execute(
                    "SELECT path FROM images WHERE id = ?",
                    (image_id,),
                ).fetchone()
                if row is None or not _write_thumbnail(
                    Path(row["path"]),
                    thumb_path,
                    state.thumbnail_size,
                ):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            self._send_file(thumb_path, "image/jpeg")

        def _send_html(self, document: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = document.encode("utf-8")
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

        def _send_file(self, path: Path, content_type: str | None = None) -> None:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

    return Handler


def _index_page(state: WebState, lang: str) -> str:
    text = _text(lang)
    stats = _stats(state.conn)
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
      </nav>
    </header>
    <section class="stats">
      <div><strong>{stats['indexed']}</strong><span>{text['indexed']}</span></div>
      <div><strong>{stats['hashed']}</strong><span>{text['searchable']}</span></div>
      <div><strong>{stats['failed']}</strong><span>{text['failures']}</span></div>
      <div><strong>{stats['groups']}</strong><span>{text['groups']}</span></div>
      <div><strong>{stats['matches']}</strong><span>{text['matches']}</span></div>
      <div><strong>{text['crop_ready'] if stats['crop_images'] else text['crop_missing']}</strong><span>{text['crop_index']}</span></div>
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
          <label class="checkbox">
            <input type="checkbox" name="tryhard" value="1">
            {text['tryhard']}
          </label>
          {f"<p class='hint'>{text['tryhard_limited']}</p>" if not stats['crop_images'] else ""}
        </div>
        <button type="submit">{text['search']}</button>
      </form>
      <form id="prompt-search" method="post" action="/prompt-search" class="prompt-form">
        <h2>{text['prompt_search']}</h2>
        <label class="prompt-input-label" for="prompt-tags-input">{text['prompt_tags']}</label>
        <div class="prompt-input-wrap">
          <textarea id="prompt-tags-input" name="tags" rows="5" placeholder="{text['prompt_placeholder']}" autocomplete="off" spellcheck="false" required></textarea>
          <div id="tag-suggestions" class="suggestions" hidden></div>
        </div>
        <label class="autocomplete-toggle">
          <input id="autocomplete-enabled" type="checkbox" checked>
          {text['autocomplete']}
        </label>
        <div class="controls prompt-controls">
          <input type="hidden" name="lang" value="{lang}">
          <label>
            {text['match_mode']}
            <select name="match_mode">
              <option value="all" selected>{text['all_tags']}</option>
              <option value="any">{text['any_tag']}</option>
            </select>
          </label>
          <label>
            {text['max_results']}
            <input type="number" name="limit" min="1" max="500" step="1" value="{state.limit}">
          </label>
          <label>
            Offset
            <input type="number" name="offset" min="0" step="1" value="0">
          </label>
        </div>
        <button type="submit">{text['search']}</button>
      </form>
    </section>
    <script>
      const input = document.querySelector("#image-input");
      const language = document.querySelector("select[name='lang']");
      const quality = document.querySelector("select[name='quality']");
      const minScore = document.querySelector("input[name='min_score']");
      const tryhard = document.querySelector("input[name='tryhard']");
      const promptInput = document.querySelector("#prompt-tags-input");
      const suggestions = document.querySelector("#tag-suggestions");
      const autocomplete = document.querySelector("#autocomplete-enabled");
      const scores = {{ strict: "75", balanced: "{state.min_score:.0f}", loose: "40" }};
      language.addEventListener("change", () => {{
        window.location.href = "/?lang=" + encodeURIComponent(language.value);
      }});
      quality.addEventListener("change", () => {{
        minScore.value = scores[quality.value] || scores.balanced;
      }});
      tryhard.addEventListener("change", () => {{
        if (tryhard.checked && Number(minScore.value) > 1) minScore.value = "1";
      }});
      window.addEventListener("paste", event => {{
        const item = [...event.clipboardData.items].find(i => i.type.startsWith("image/"));
        if (!item) return;
        const file = item.getAsFile();
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      }});
      const tokenInfo = () => {{
        const value = promptInput.value;
        const caret = promptInput.selectionStart ?? value.length;
        const left = value.slice(0, caret);
        const start = Math.max(left.lastIndexOf(","), left.lastIndexOf("，"), left.lastIndexOf("\\n")) + 1;
        return {{ start, end: caret, token: value.slice(start, caret).trimStart() }};
      }};
      const hideSuggestions = () => {{
        suggestions.hidden = true;
        suggestions.innerHTML = "";
      }};
      const applySuggestion = tag => {{
        const info = tokenInfo();
        const value = promptInput.value;
        const leading = value.slice(info.start, info.end).match(/^\\s*/)?.[0] || "";
        const insert = leading + tag;
        promptInput.value = value.slice(0, info.start) + insert + value.slice(info.end);
        const caret = info.start + insert.length;
        promptInput.focus();
        promptInput.setSelectionRange(caret, caret);
        hideSuggestions();
      }};
      const refreshSuggestions = async () => {{
        if (!autocomplete.checked) return hideSuggestions();
        const info = tokenInfo();
        const query = info.token.trim();
        if (query.length < 1) return hideSuggestions();
        const response = await fetch("/suggest-tags?q=" + encodeURIComponent(query));
        if (!response.ok) return hideSuggestions();
        const tags = await response.json();
        if (!tags.length) return hideSuggestions();
        suggestions.innerHTML = tags.map(item => (
          `<button type="button" data-tag="${{item.tag.replaceAll("&", "&amp;").replaceAll('"', "&quot;")}}">
             <span>${{item.tag.replaceAll("&", "&amp;").replaceAll("<", "&lt;")}}</span>
             <small>${{item.count}}</small>
           </button>`
        )).join("");
        suggestions.hidden = false;
      }};
      promptInput.addEventListener("input", refreshSuggestions);
      promptInput.addEventListener("click", refreshSuggestions);
      autocomplete.addEventListener("change", refreshSuggestions);
      suggestions.addEventListener("click", event => {{
        const button = event.target.closest("button[data-tag]");
        if (button) applySuggestion(button.dataset.tag);
      }});
      document.addEventListener("click", event => {{
        if (!event.target.closest(".prompt-input-wrap") && event.target !== autocomplete) hideSuggestions();
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
                <img src="/thumb/{int(image_row['id'])}.jpg" alt="">
              </a>
              <div class="score">{score.score:.2f}</div>
              <div>{html.escape(_label(score.decision, options.lang))}</div>
              <div>{image_row['width']}x{image_row['height']} &middot; {human_size(image_row['size_bytes'])}</div>
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
) -> str:
    text = _text(options.lang)
    cards = []
    for result in results:
        image_row = result.image_row
        prompt_html = _highlight_prompt(result.prompt or "", result.matched_tags)
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
                <img src="/thumb/{int(image_row['id'])}.jpg" alt="">
              </a>
              <div class="score">{result.matched_count}</div>
              <div>{html.escape(', '.join(result.matched_tags))}</div>
              <div>{image_row['width']}x{image_row['height']} &middot; {human_size(image_row['size_bytes'])}</div>
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
    empty = f"<p>{text['no_prompt_results']}</p>" if not cards else ""
    body = f"""
    <nav><a href="/?lang={options.lang}">{text['new_search']}</a></nav>
    <header>
      <h1>{text['prompt_results']}</h1>
      <p>{summary}</p>
      <p class="muted">{html.escape(options.tags)}</p>
    </header>
    <section class="grid">{''.join(cards)}</section>
    {empty}
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


def _highlight_prompt(prompt: str, matched_tags: tuple[str, ...]) -> str:
    escaped_prompt = html.escape(prompt.strip())
    tags = sorted(
        {tag.strip() for tag in matched_tags if tag.strip()},
        key=len,
        reverse=True,
    )
    if not escaped_prompt or not tags:
        return escaped_prompt

    pattern = re.compile(
        "|".join(re.escape(html.escape(tag)) for tag in tags),
        flags=re.IGNORECASE,
    )
    return pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped_prompt)


def _failures_page(state: WebState, lang: str) -> str:
    text = _text(lang)
    rows = state.conn.execute(
        """
        SELECT path, decode_error
        FROM images
        WHERE decode_error IS NOT NULL
        ORDER BY indexed_at DESC, path
        LIMIT 500
        """
    ).fetchall()
    items = []
    for row in rows:
        items.append(
            f"""
            <tr>
              <td class="path">{html.escape(row['path'])}</td>
              <td>{html.escape(row['decode_error'] or '')}</td>
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
    rows = state.conn.execute(
        """
        SELECT
            MIN(tag) AS tag,
            COUNT(*) AS count
        FROM prompt_tags
        WHERE tag_norm >= ?
          AND tag_norm < ?
        GROUP BY tag_norm
        ORDER BY count DESC, tag_norm
        LIMIT 5
        """,
        (prefix, _prefix_upper_bound(prefix)),
    ).fetchall()
    return [
        {"tag": row["tag"], "count": int(row["count"])}
        for row in rows
        if row["tag"]
    ]


def _prefix_upper_bound(prefix: str) -> str:
    return prefix + "\U0010ffff"


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


def _page(title: str, body: str, *, lang: str = "en") -> str:
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
    .muted {{ color: #52606d; word-break: break-all; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .stats div {{ background: #fff; border: 1px solid #d9e1e8; border-radius: 6px; padding: 12px; }}
    .stats strong {{ display: block; font-size: 24px; }}
    .stats span {{ color: #52606d; }}
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
    .suggestions button:hover, .suggestions button:focus {{ background: #eef2f6; outline: 0; }}
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
    .hint {{ color: #9b4d18; font-size: 13px; margin: 0; }}
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
    try:
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
            img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (thumbnail_size, thumbnail_size), (245, 247, 250))
            left = (thumbnail_size - img.width) // 2
            top = (thumbnail_size - img.height) // 2
            canvas.paste(img, (left, top))
            canvas.save(target, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


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
        if content.endswith(b"\r\n"):
            content = content[:-2]
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
    value = header.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
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
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS indexed,
            SUM(CASE WHEN decode_error IS NULL AND missing_at IS NULL THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN decode_error IS NOT NULL THEN 1 ELSE 0 END) AS failed
        FROM images
        """
    ).fetchone()
    hashed = conn.execute("SELECT COUNT(*) AS count FROM hashes").fetchone()["count"]
    groups = conn.execute("SELECT COUNT(DISTINCT cluster_id) AS count FROM clusters").fetchone()["count"]
    matches = conn.execute("SELECT COUNT(*) AS count FROM matches").fetchone()["count"]
    crop_images = conn.execute("SELECT COUNT(DISTINCT image_id) AS count FROM crop_hashes").fetchone()["count"]
    prompts = conn.execute(
        "SELECT COUNT(*) AS count FROM image_prompts WHERE prompt IS NOT NULL"
    ).fetchone()["count"]
    unique_tags = conn.execute(
        "SELECT COUNT(DISTINCT tag_norm) AS count FROM prompt_tags"
    ).fetchone()["count"]
    return {
        "indexed": int(row["indexed"] or 0),
        "ok": int(row["ok"] or 0),
        "failed": int(row["failed"] or 0),
        "hashed": int(hashed or 0),
        "groups": int(groups or 0),
        "matches": int(matches or 0),
        "crop_images": int(crop_images or 0),
        "prompts": int(prompts or 0),
        "unique_tags": int(unique_tags or 0),
    }


def _lang_from_query(query: str) -> str:
    values = urllib.parse.parse_qs(query)
    return _normalize_lang(values.get("lang", ["en"])[0])


def _normalize_lang(lang: str | None) -> str:
    return lang if lang in TRANSLATIONS else "en"


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
