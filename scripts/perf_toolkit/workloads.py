"""Isolated backend benchmark worker; invoked by the performance runner.

Each invocation measures one actual application operation. Imports, fixture
loading, and connection setup are outside the timed region. Diagnostics are a
separate run because tracing and profiling change performance. The parent
runner owns process deadlines; an interrupted destructive case is protected by
SQLite rollback, and successful deletion cases explicitly roll back too.
"""
from __future__ import annotations

import argparse
import cProfile
import dataclasses
import json
import os
import pstats
import re
import resource
import signal
import sqlite3
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from imgdupe.config import ScanConfig
from imgdupe.cluster import build_clusters
from imgdupe.db import connect, delete_images, init_db, temporary_image_ids
from imgdupe.model_paths import build_config
from imgdupe.nai_prompt import parse_novelai_prompt, read_stealth_metadata
from imgdupe.query import query_image, query_prompt_tags
from imgdupe.review import _fetch_cluster_matches
from imgdupe.scan import scan_roots
from imgdupe.web import WebState, _index_page, _model_filter_options, _stats, _tag_suggestions
from PIL import Image
from .runner import source_fingerprints


CASES: dict[str, dict[str, Any]] = {
    "init_db": {"description": "Open/migrate an already-current schema", "writable": True},
    "stats": {"description": "Home-page summary counters"},
    "model_options": {"description": "Home-page model filter options"},
    "index_page": {"description": "Complete home-page HTML generation"},
    "suggest_common": {"description": "Autocomplete broad prefix c"},
    "suggest_narrow": {"description": "Autocomplete narrow prefix rare"},
    "query_rare": {"description": "Rare positive tag; limit 100"},
    "query_common_rare": {"description": "Common AND rare tag; limit 100"},
    "query_common": {"description": "Common tag; duplicates allowed; limit 100"},
    "query_unique": {"description": "Common tag; unique prompts; limit 100"},
    "query_deep_offset": {"description": "Common tag; unique prompts; offset 10000, limit 100"},
    "image_query": {"description": "Actual image decoding/hash/candidate/scoring query; limit 20"},
    "image_query_common_grid": {"description": "Image query with a shared-grid candidate cohort; limit 20"},
    "image_query_tryhard": {"description": "Actual tryhard image query, including query crops and tiles"},
    "image_query_common_grid_tryhard": {"description": "Tryhard query against shared-grid cohort"},
    "scan_empty": {"description": "Incremental scan preparation with no requested roots", "writable": True},
    "cluster": {"description": "Full actual clustering; replaces the synthetic graph if completed", "writable": True},
    "review_cluster": {"description": "Fetch edges for the first min(10000, N) cluster members"},
    "review_cluster_index_walk": {"description": "Experimental SQL counterfactual: indexed edge walk with equivalent membership/order"},
    "delete_400": {"description": "Delete last 400 records then roll back", "writable": True},
    "delete_4000": {"description": "Delete last 4000 records then roll back", "writable": True},
    "parse_4k": {"description": "Parse valid weighted NovelAI prompt, about 4000 characters"},
    "parse_32k": {"description": "Parse valid weighted NovelAI prompt, about 32000 characters"},
    "stealth_2048": {"description": "Inspect opaque 2048-square image with no stealth payload"},
}

_TERMINATION_REQUESTED = False


def _diagnostic_termination(_signum, _frame) -> None:
    """Allow a bounded cluster diagnostic to save its partial profile.

    SQL callbacks may swallow Python exceptions, so the progress handler also
    observes this flag and interrupts SQLite at the next diagnostic tick.
    Uninstrumented timings never install this handler.
    """
    global _TERMINATION_REQUESTED
    _TERMINATION_REQUESTED = True
    raise TimeoutError("External SIGTERM interrupted the diagnostic; profile is partial")


def _fetch_cluster_matches_index_walk(conn: sqlite3.Connection, members: list[int]) -> list[sqlite3.Row]:
    """Toolkit-only SQL counterfactual; never used by the application itself."""
    if len(members) < 2:
        return []
    with temporary_image_ids(conn, members) as membership:
        return conn.execute(
            f"""
            SELECT m.*
            FROM {membership} member
            CROSS JOIN matches m
            WHERE m.image_id_a = member.image_id
              AND EXISTS (
                  SELECT 1 FROM {membership} other
                  WHERE other.image_id = m.image_id_b
              )
            ORDER BY m.score DESC, m.decision
            """
        ).fetchall()


def _rss_mib() -> float:
    """Current RSS where available, falling back to the process high-water mark."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return _peak_rss_mib()


def _peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _io_counters() -> dict[str, int]:
    """Linux per-process actual storage I/O; snapshots stay outside timing."""
    try:
        return {key: int(value) for line in Path("/proc/self/io").read_text().splitlines()
                for key, value in [line.split(":", 1)] if key in {"read_bytes", "write_bytes", "rchar", "wchar"}}
    except (OSError, ValueError):
        return {}


def _connect_fixture(path: Path, *, writable: bool) -> sqlite3.Connection:
    if writable:
        return connect(path)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # These are the production connection settings. The fixture is already WAL.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _summarize(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value):
        return {"result_count": None, "result": dataclasses.asdict(value)}
    if isinstance(value, str):
        return {"result_count": 1, "html_bytes": len(value.encode("utf-8"))}
    if isinstance(value, dict):
        return {"result_count": len(value), "result": value}
    if isinstance(value, (tuple, list)):
        return {"result_count": len(value)}
    if value is None:
        return {"result_count": 0}
    if isinstance(value, int):
        return {"result_count": value}
    return {"result_count": None, "result_type": type(value).__name__}


def _operation(name: str, conn: sqlite3.Connection, manifest: dict[str, Any], scratch: Path):
    """Prepare an operation without executing it; setup stays outside timing."""
    state = SimpleNamespace(conn=conn)
    if name == "init_db":
        return lambda: init_db(conn)
    if name == "stats":
        return lambda: _stats(conn)
    if name == "model_options":
        return lambda: _model_filter_options(conn)
    if name == "suggest_common":
        return lambda: _tag_suggestions(state, "q=c")
    if name == "suggest_narrow":
        return lambda: _tag_suggestions(state, "q=rare")
    if name == "index_page":
        web_state = WebState(
            conn=conn, db_path=Path(manifest["db_path"]), limit=100, min_score=55,
            thumbnail_size=256, work_dir=scratch / "web", thumbs_dir=scratch / "thumbs",
            thumbnail_cache_limit=1000, disable_advanced_options=True,
        )
        return lambda: _index_page(web_state, "en")
    if name.startswith("query_"):
        tags = "rare" if name == "query_rare" else "common, rare" if name == "query_common_rare" else "common"
        return lambda: query_prompt_tags(
            conn, tags, match_mode="all", limit=100,
            offset=10000 if name == "query_deep_offset" else 0,
            unique_prompts=name in {"query_unique", "query_deep_offset"},
        )
    if name.startswith("image_query"):
        tryhard = name.endswith("tryhard")
        key = "common_grid_query_image" if "common_grid" in name else "query_image"
        return lambda: query_image(conn, Path(manifest[key]), limit=20, tryhard=tryhard)
    if name == "scan_empty":
        return lambda: scan_roots(conn, [], config=ScanConfig(workers=1), model_paths=build_config([]))
    if name == "cluster":
        return lambda: build_clusters(conn)
    if name in {"review_cluster", "review_cluster_index_walk"}:
        members = list(range(1, min(10000, int(manifest["image_count"])) + 1))
        fetch = _fetch_cluster_matches_index_walk if name == "review_cluster_index_walk" else _fetch_cluster_matches
        return lambda: fetch(conn, members)
    if name.startswith("delete_"):
        count = min(int(name.split("_")[1]), int(manifest["image_count"]))
        last_id = int(manifest["image_count"])
        ids = list(range(last_id - count + 1, last_id + 1))
        conn.execute("BEGIN")
        return lambda: delete_images(conn, ids)
    if name.startswith("parse_"):
        length = 4000 if name == "parse_4k" else 32000
        prompt = ("tag, " * (length // 5)) + "1.2::weighted::"
        return lambda: parse_novelai_prompt(prompt)
    if name == "stealth_2048":
        image = Image.new("RGBA", (2048, 2048), (127, 127, 127, 255))
        return lambda: read_stealth_metadata(image)
    raise ValueError(f"unknown case: {name}")


class SQLDiagnostics:
    """Bounded statement summaries and query plans, collected only when requested."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.total_statements = 0
        self.vm_ticks = 0
        self.groups: dict[str, dict[str, Any]] = {}
        self._in_plan = False

    def progress(self):
        self.vm_ticks += 1
        return int(_TERMINATION_REQUESTED)

    def trace(self, sql: str):
        if self._in_plan:
            return
        self.total_statements += 1
        # Bound input before normalizing: scan updates and query VALUES can grow.
        normalized = re.sub(r"'[^']*(?:''[^']*)*'", "?", sql[:12000])
        normalized = re.sub(r"\b\d+\b", "?", normalized)
        normalized = re.sub(r"image_ids_[0-9a-f]+", "image_ids_TEMP", normalized)
        normalized = " ".join(normalized.split())[:2000]
        if normalized in self.groups:
            self.groups[normalized]["count"] += 1
            return
        if len(self.groups) >= 50:
            return
        group: dict[str, Any] = {"sql": normalized, "count": 1}
        self.groups[normalized] = group
        if sql.lstrip().upper().startswith(("SELECT", "WITH", "DELETE", "UPDATE")) and len(sql) <= 30000:
            self._in_plan = True
            # Nested EXPLAIN lets us inspect temporary membership tables while
            # they still exist. It does not execute the statement being explained.
            try:
                group["query_plan"] = [list(row) for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql)]
            except sqlite3.Error as exc:
                group["plan_error"] = str(exc)
            finally:
                self._in_plan = False

    def result(self):
        return {
            "statement_count": self.total_statements,
            "vm_instructions_approx": self.vm_ticks * 1000,
            "vm_note": "Includes small EXPLAIN diagnostic overhead; not a latency sample.",
            "statements": sorted(self.groups.values(), key=lambda item: item["count"], reverse=True),
            "statement_groups_capped_at": 50,
        }


def _profile_summary(profiler: cProfile.Profile) -> list[dict[str, Any]]:
    stats = pstats.Stats(profiler)
    rows = []
    for (filename, line, function), (primitive, calls, total, cumulative, _callers) in sorted(
        stats.stats.items(), key=lambda entry: entry[1][3], reverse=True,
    )[:40]:
        rows.append({
            "file": filename, "line": line, "function": function,
            "primitive_calls": primitive, "calls": calls,
            "self_seconds": total, "cumulative_seconds": cumulative,
        })
    return rows


def run_case(name: str, manifest: dict[str, Any], *, diagnostic: bool = False, profile: Path | None = None) -> dict[str, Any]:
    """Run one case on a generated fixture, returning a JSON-safe result.

    ``scan_empty`` intentionally executes the real commit path. Generated
    classification state should already be current; ``sqlite_changes`` exposes
    unexpected reconciliation writes. Destructive delete cases always roll back.
    """
    if name not in CASES:
        raise ValueError(f"unknown case {name!r}")
    db_path = Path(manifest["db_path"])
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    owner_path = db_path.parent / ".imgdupe-perf-fixture.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    if (
        manifest.get("kind") != "imgdupe-performance-fixture-v1"
        or owner.get("kind") != manifest["kind"]
        or Path(owner.get("work_dir", "")).resolve() != db_path.parent.resolve()
        or Path(manifest.get("work_dir", "")).resolve() != db_path.parent.resolve()
        or db_path.is_symlink()
    ):
        raise ValueError("Refusing a database without a matching generated-fixture ownership marker")
    if profile is not None:
        diagnostic = True
    setup_start = time.perf_counter()
    conn = _connect_fixture(db_path, writable=CASES[name].get("writable", False))
    result: dict[str, Any] = {
        "case": name, "description": CASES[name]["description"], "source_sha256": source_fingerprints(),
        "mode": "diagnostic" if diagnostic else "timing", "status": "ok",
        "image_count": int(manifest["image_count"]), "sqlite_version": sqlite3.sqlite_version,
        "python_version": sys.version.split()[0], "pid": os.getpid(),
        "cache_state": "Fresh process/SQLite connection; OS page cache is not flushed",
        "notes": [],
    }
    profiler = cProfile.Profile() if diagnostic else None
    sql_diagnostics = SQLDiagnostics(conn) if diagnostic else None
    try:
        with tempfile.TemporaryDirectory(prefix="imgdupe-perf-worker-") as directory:
            operation = _operation(name, conn, manifest, Path(directory))
            result["setup_seconds"] = time.perf_counter() - setup_start
            result["baseline_rss_mib"] = _rss_mib()
            before_changes = conn.total_changes
            io_before = _io_counters()
            if sql_diagnostics is not None:
                conn.set_trace_callback(sql_diagnostics.trace)
                conn.set_progress_handler(sql_diagnostics.progress, 1000)
            wall_start = time.perf_counter()
            cpu_start = time.process_time()
            if profiler:
                profiler.enable()
            try:
                if diagnostic and name in {"image_query", "image_query_common_grid"}:
                    from imgdupe.bands import find_candidates

                    def measured_candidates(*args, **kwargs):
                        candidates = find_candidates(*args, **kwargs)
                        result["candidate_count"] = len(candidates)
                        result["candidate_band_hits"] = sum(item.total_hits for item in candidates)
                        return candidates

                    # Observation only, in a dedicated diagnostic process. The
                    # returned candidate list is unchanged and consumed normally.
                    with patch("imgdupe.query.find_candidates", measured_candidates):
                        value = operation()
                    result["notes"].append("Candidate counters instrumented only in diagnostic mode")
                else:
                    value = operation()
            except Exception as exc:
                result.update(status="error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            else:
                result.update(_summarize(value))
            finally:
                if profiler:
                    profiler.disable()
                result["cpu_seconds"] = time.process_time() - cpu_start
                result["wall_seconds"] = time.perf_counter() - wall_start
                result["io_bytes"] = {key: value - io_before.get(key, value)
                                      for key, value in _io_counters().items()}
                result["peak_rss_mib"] = _peak_rss_mib()
                result["peak_increase_over_baseline_mib"] = max(0.0, result["peak_rss_mib"] - result["baseline_rss_mib"])
                result["sqlite_changes"] = conn.total_changes - before_changes
                conn.set_trace_callback(None)
                conn.set_progress_handler(None, 0)
            if sql_diagnostics is not None:
                result["sql_diagnostics"] = sql_diagnostics.result()
            if profiler is not None:
                result["profile_top"] = _profile_summary(profiler)
                if profile is not None:
                    profile.parent.mkdir(parents=True, exist_ok=True)
                    profiler.dump_stats(str(profile))
                    result["profile_path"] = str(profile.resolve())
    finally:
        rollback_start = time.perf_counter()
        conn.rollback()
        result["rollback_seconds"] = time.perf_counter() - rollback_start
        conn.close()
    if name.startswith("delete_"):
        result["mutation_policy"] = "Timed DELETE includes cascades; rollback is outside timing; nothing committed"
    elif name == "scan_empty":
        result["mutation_policy"] = "Actual application commits on disposable generated fixture; sqlite_changes should be zero"
    elif name == "cluster":
        result["mutation_policy"] = "Actual application replaces matches/clusters and commits on success; interrupted transaction rolls back"
        result["notes"].append("Opt-in destructive fixture benchmark: run after all baseline measurements; successful completion changes graph-dependent results")
    elif name == "review_cluster_index_walk":
        result["implementation"] = "experimental_sql_counterfactual"
        result["notes"].append("Toolkit-only alternative SQL; production review.py is unchanged; full rows and ORDER BY match the baseline query")
    if _TERMINATION_REQUESTED:
        result["interrupted"] = True
        result["notes"].append("Partial diagnostic stopped by external SIGTERM; do not treat elapsed time as completed-work latency")
    return result


def main(argv: list[str] | None = None) -> int:
    global _TERMINATION_REQUESTED
    _TERMINATION_REQUESTED = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args(argv)
    manifest = json.loads(args.fixture.read_text(encoding="utf-8"))
    previous_sigterm = None
    if args.case == "cluster" and (args.diagnostic or args.profile):
        previous_sigterm = signal.signal(signal.SIGTERM, _diagnostic_termination)
    try:
        result = run_case(args.case, manifest, diagnostic=args.diagnostic, profile=args.profile)
    except Exception as exc:
        result = {"case": args.case, "status": "error", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("case", "status", "wall_seconds", "peak_rss_mib", "result_count") if key in result}), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
