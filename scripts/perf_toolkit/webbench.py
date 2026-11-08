"""HTTP benchmarks against the real imgdupe Web UI.

Run with ``python -m scripts.perf_toolkit.webbench --fixture ... --output ...``.
The separate ``--serve-worker --port ...`` mode is useful for browser profiling.
Only owned synthetic fixtures are accepted; application code is never patched.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import cProfile
import html
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

from .runner import source_fingerprints


REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def load_fixture(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    db = Path(manifest["db_path"]).resolve()
    if db.parent != path.resolve().parent or not db.is_file():
        raise ValueError("fixture DB must exist beside its manifest")
    # The generator owns this directory. Never point this at a user's database.
    marker = db.parent / ".imgdupe-perf-fixture.json"
    if not marker.is_file():
        raise ValueError(f"synthetic fixture ownership marker is missing: {marker}")
    owner = json.loads(marker.read_text(encoding="utf-8"))
    if (owner.get("kind") != "imgdupe-performance-fixture-v1"
            or owner.get("work_dir") != str(db.parent)
            or manifest.get("kind") != owner["kind"]):
        raise ValueError("fixture ownership marker does not identify this directory")
    return manifest


def _process_stats(pid: int) -> dict:
    """Best-effort Linux observations; no psutil dependency or machine changes."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        memory = {}
        for key in ("VmRSS", "VmHWM"):
            match = re.search(rf"^{key}:\s+(\d+) kB$", status, re.MULTILINE)
            if match:
                memory[key + "_mib"] = int(match.group(1)) / 1024
        return {**memory, "cpu_seconds": (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")}
    except (OSError, ValueError, IndexError):
        return {}


def request(port: int, path: str, *, timeout: float, method: str = "GET",
            body: bytes | None = None, content_type: str | None = None,
            return_body: bool = False, started_event: threading.Event | None = None) -> dict:
    """Time complete HTTP responses. TTFB is measured at response-header receipt."""
    start = time.perf_counter()
    row = {"method": method, "path": path, "started_unix_ns": time.time_ns(),
           "started_monotonic": start}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Connection": "close"}
        if content_type:
            headers["Content-Type"] = content_type
        conn.request(method, path, body=body, headers=headers)
        if started_event:
            started_event.set()
        response = conn.getresponse()
        row["ttfb_seconds"] = time.perf_counter() - start
        row["status"] = response.status
        chunks = []
        size = 0
        while True:
            remaining = timeout - (time.perf_counter() - start)
            if remaining <= 0:
                raise TimeoutError("absolute response deadline exceeded")
            if conn.sock:
                conn.sock.settimeout(remaining)
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded benchmark's 64 MiB safety limit")
            if return_body:
                chunks.append(chunk)
        row["body_bytes"] = size
        if return_body:
            row["body"] = b"".join(chunks)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if started_event:
            started_event.set()
        row["duration_seconds"] = time.perf_counter() - start
        row["finished_monotonic"] = time.perf_counter()
        conn.close()
    return row


def summarize(rows: list[dict]) -> dict:
    durations = sorted(row["duration_seconds"] for row in rows if "error" not in row)
    result = {"samples": len(rows), "successful_samples": len(durations),
              "errors": sum("error" in row for row in rows),
              "status_counts": {str(code): sum(row.get("status") == code for row in rows)
                                for code in sorted({row["status"] for row in rows if "status" in row})}}
    if durations:
        result.update(median_seconds=statistics.median(durations),
                      min_seconds=durations[0], max_seconds=durations[-1],
                      p95_seconds=durations[max(0, math.ceil(.95 * len(durations)) - 1)],
                      p95_method="nearest-rank; with small samples this is the observed maximum")
    return result


class Server:
    def __init__(self, fixture_path: Path, log_path: Path, timeout: float,
                 profile_path: Path | None = None):
        self.fixture_path = fixture_path.resolve()
        self.log_path = log_path
        self.timeout = timeout
        self.profile_path = profile_path
        self.process: subprocess.Popen | None = None
        self.log = None
        self.port = 0
        self.startup_seconds = 0.0
        self.final_stats: dict = {}

    def start(self) -> "Server":
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        args = [sys.executable, "-m", "scripts.perf_toolkit.webbench", "--serve-worker",
                "--fixture", str(self.fixture_path), "--port", str(self.port)]
        if self.profile_path:
            args += ["--server-profile", str(self.profile_path.resolve())]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8")
        start = time.perf_counter()
        self.process = subprocess.Popen(args, cwd=REPO_ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        deadline = start + self.timeout
        while time.perf_counter() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise RuntimeError(f"server exited during startup; see {self.log_path}")
            # TCP connect does not execute the expensive home-page statistics.
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                    self.startup_seconds = time.perf_counter() - start
                    return self
            except OSError:
                time.sleep(.05)
        self.stop()
        raise TimeoutError(f"server did not become ready in {self.timeout}s")

    def stats(self) -> dict:
        return _process_stats(self.process.pid) if self.process else self.final_stats

    def stop(self) -> None:
        if self.process:
            self.final_stats = self.stats()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None


def _post(port: int, fields: dict, timeout: float, *, event=None) -> dict:
    return request(port, "/prompt-search", timeout=timeout, method="POST",
                   body=urllib.parse.urlencode(fields).encode(),
                   content_type="application/x-www-form-urlencoded", started_event=event)


def _get_csrf(home: dict) -> str:
    if home.get("status") != 200:
        raise RuntimeError(f"home page failed: {home.get('error', home.get('status'))}")
    body = home.pop("body").decode("utf-8")
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    if not match:
        raise ValueError("actual home page did not contain a CSRF token")
    return html.unescape(match.group(1))


def _multipart(fields: dict, path: Path) -> tuple[bytes, str]:
    boundary = "perf-" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="query.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
    chunks.extend([path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _parallel_requests(server: Server, paths: list[str], timeout: float, concurrency: int) -> tuple[list[dict], float]:
    begin = time.perf_counter()
    rows = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
    futures = [executor.submit(request, server.port, path, timeout=timeout) for path in paths]
    try:
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            if "error" in row:
                # Stop CPU-bound server work as soon as a client times out.
                server.stop()
                for pending in futures:
                    pending.cancel()
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return rows, time.perf_counter() - begin


def validate_output_file(output: Path) -> Path:
    output = output.resolve()
    if output.exists() and not output.is_file():
        raise ValueError("output path must be a file")
    return output


def run_benchmark(fixture_path: Path, output: Path, *, repeats: int = 3,
                  timeout: float = 90, server_profile: Path | None = None) -> dict:
    output = validate_output_file(output)
    manifest = load_fixture(fixture_path)
    db = Path(manifest["db_path"])
    cache = db.with_suffix(db.suffix + ".thumb-cache")
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "kind": "real_http_webui", "fixture": str(fixture_path.resolve()),
              "image_count": manifest["image_count"], "repeats": repeats, "request_timeout_seconds": timeout,
              "source_sha256": source_fingerprints(),
              "profiled": server_profile is not None, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "notes": ["Actual imgdupe.web.serve, HTTPServer, request handlers, SQL, CSRF, and thumbnail generation.",
                        "HTTP timings exclude browser parsing/rendering; browser measurements are separate.",
                        "TTFB is measured when response headers are received, not first network byte.",
                        "Fresh server process does not imply cold OS filesystem cache. No OS cache flushing.",
                        "Thumbnails use six concurrent requests, approximating a page fetching visible assets.",
                        "Percentiles are empirical nearest-rank values; small repeat counts do not estimate tail latency."],
              "cases": {}, "server_runs": []}
    server = None

    def save():
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    def record(name: str, rows: list[dict], **extra):
        result["cases"][name] = {"summary": summarize(rows), "requests": rows, **extra}
        print(json.dumps({"case": name, **result["cases"][name]["summary"]}), flush=True)
        save()
        if any("error" in row for row in rows):
            raise RuntimeError(f"HTTP case {name} failed; stopping rather than accumulating timeout work")
        if any(row.get("status") != 200 for row in rows):
            raise RuntimeError(f"HTTP case {name} returned a non-200 status; inspect recorded responses")

    def close_server():
        if server:
            server.stop()
            result["server_runs"].append({"startup_seconds": server.startup_seconds,
                                          "process": server.final_stats, "log": str(server.log_path)})

    try:
        if cache.exists():
            shutil.rmtree(cache)
        server = Server(fixture_path, output.with_suffix(".server.log"), timeout, server_profile).start()
        first_home = request(server.port, "/?lang=en", timeout=timeout, return_body=True)
        token = _get_csrf(first_home)
        home_rows = [first_home] + [request(server.port, "/?lang=en", timeout=timeout) for _ in range(repeats - 1)]
        record("home", home_rows)
        for name, prefix in (("autocomplete_common", "c"), ("autocomplete_narrow", "rare")):
            record(name, [request(server.port, "/suggest-tags?q=" + prefix, timeout=timeout) for _ in range(repeats)])
        fields = {"csrf_token": token, "tags": "common", "match_mode": "all", "limit": "100",
                  "sort_by": "created_desc", "unique_prompts": "1", "lang": "en"}
        record("prompt_selective", [_post(server.port, {**fields, "tags": "common, rare"}, timeout) for _ in range(repeats)])
        record("prompt_common_unique", [_post(server.port, fields, timeout) for _ in range(repeats)])
        record("prompt_common_nonunique", [_post(server.port, {**fields, "unique_prompts": "0"}, timeout) for _ in range(repeats)])
        upload, content_type = _multipart({"csrf_token": token, "lang": "en", "limit": "20",
                                           "min_score": "55", "include_exact": "1"}, Path(manifest["query_image"]))
        record("image_search", [request(server.port, "/search", method="POST", body=upload,
                                         content_type=content_type, timeout=timeout) for _ in range(repeats)])
        thumbnail_ids = list(range(1, min(100, int(manifest["real_image_count"])) + 1))
        paths = [f"/thumb/{image_id}.jpg" for image_id in thumbnail_ids]
        if paths:
            rows, duration = _parallel_requests(server, paths, timeout, 6)
            record("thumbnails_uncached", rows, page_asset_seconds=duration, concurrency=6)
            rows, duration = _parallel_requests(server, paths, timeout, 6)
            record("thumbnails_cached", rows, page_asset_seconds=duration, concurrency=6)
        rows, duration = _parallel_requests(server, ["/suggest-tags?q=" + q for q in ("c", "co", "com", "comm", "commo", "common")], timeout, 6)
        record("autocomplete_burst", rows, batch_seconds=duration, concurrency=6)

        fast_path = paths[0] if paths else "/benchmark-missing"
        isolated = request(server.port, fast_path, timeout=timeout)
        started = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(_post, server.port, fields, timeout, event=started)
            started.wait(timeout=min(timeout, 5))
            time.sleep(.05)
            contended = request(server.port, fast_path, timeout=timeout)
            heavy = slow.result()
        overlap = heavy["started_monotonic"] <= contended["started_monotonic"] < heavy["finished_monotonic"]
        record("head_of_line", [isolated, heavy, contended], isolated_fast=isolated,
               heavy_request=heavy, contended_fast=contended, request_intervals_overlap=overlap,
               note="Heavy request sent first; 50ms later a cached thumbnail/404 is requested. Interval overlap is observed, not a server execution trace.")

        # Seed a full cache before a fresh server counts it, so production's
        # eviction branch really executes. Never manipulate WebState counters.
        if paths:
            close_server()
            server = None
            sample_jpg = next(cache.glob("*.jpg")).read_bytes()
            shutil.rmtree(cache)
            cache.mkdir()
            for index in range(1000):
                (cache / f"benchmark-prefill-{index}.jpg").write_bytes(sample_jpg)
            server = Server(fixture_path, output.with_suffix(".full-cache.server.log"), timeout).start()
            rows, duration = _parallel_requests(server, paths, timeout, 6)
            record("thumbnails_full_cache_misses", rows, page_asset_seconds=duration, concurrency=6,
                   prefilled_thumbnails=1000, note="Valid generated JPEG entries preseed the default 1000-entry cache before server startup.")
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        close_server()
        if cache.exists():
            shutil.rmtree(cache)
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save()
    return result


def serve_worker(fixture_path: Path, port: int, profile_path: Path | None = None) -> None:
    manifest = load_fixture(fixture_path)
    db = Path(manifest["db_path"])
    from imgdupe.db import connect, init_db
    from imgdupe.settings import AppSettings
    from imgdupe.web import serve

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    profile = cProfile.Profile() if profile_path else None
    old_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="web-worker-", dir=db.parent) as runtime:
        tempfile.tempdir = runtime
        conn = connect(db)
        try:
            init_db(conn)
            if profile:
                profile.enable()
            serve(conn, db, host="127.0.0.1", port=port, open_browser=False,
                  settings=AppSettings(thumbnail_cache_limit=1000, disable_advanced_options=True))
        except KeyboardInterrupt:
            pass
        finally:
            if profile:
                profile.disable()
                profile_path.parent.mkdir(parents=True, exist_ok=True)
                profile.dump_stats(str(profile_path))
            conn.close()
            tempfile.tempdir = old_tempdir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--serve-worker", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--server-profile", type=Path)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.timeout <= 0:
        parser.error("repeats and timeout must be positive")
    if args.serve_worker:
        serve_worker(args.fixture, args.port, args.server_profile)
    else:
        if not args.output:
            parser.error("--output is required unless --serve-worker is selected")
        try:
            args.output = validate_output_file(args.output)
        except ValueError as error:
            parser.error(str(error))
        run_benchmark(args.fixture, args.output, repeats=args.repeats, timeout=args.timeout,
                      server_profile=args.server_profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
