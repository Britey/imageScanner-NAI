"""Opt-in cache, SQL-counterfactual and bounded clustering follow-up experiments.

Requires a retained owned fixture. Experimental SQL/indexes live only in the
toolkit/disposable database; application source is never changed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .runner import evict_fixture_pages, run_command, summarize_samples, validate_output_directory, write_json
from .webbench import Server, load_fixture, request


def run(fixture_path: Path, output: Path, timeout: float = 90, repeats: int = 3) -> dict:
    output = validate_output_directory(output)
    manifest = load_fixture(fixture_path)
    output.mkdir(parents=True, exist_ok=True)
    results = {"fixture": manifest, "experiments": {}, "notes": [
        "All samples run sequentially after the main benchmark and browser server have stopped.",
        "Cache eviction is best effort and scoped to owned SQLite files, never global OS caches.",
        "Counterfactual SQL and indexes are benchmark-only hypotheses, not application fixes.",
        "Cluster diagnostic runtime is instrumented and capped separately from the normal timeout run.",
    ]}

    def save():
        write_json(output / "experiments.json", results)

    def measure(case: str, label: str, *, cold=False, count=repeats, diagnostic=False, budget=timeout):
        samples = []
        for index in range(count):
            print(f"experiment {label} {index + 1}/{count}", flush=True)
            advice = evict_fixture_pages(manifest) if cold else None
            destination = output / f"{label}-{index+1}.json"
            command = [sys.executable, "-m", "scripts.perf_toolkit.workloads", "--fixture", str(fixture_path.resolve()),
                       "--case", case, "--output", str(destination)]
            if diagnostic:
                command.extend(["--diagnostic", "--profile", str(destination.with_suffix(".prof"))])
            execution = run_command(command, destination.with_suffix(".log"), budget)
            sample = json.loads(destination.read_text()) if destination.exists() else {"case": case}
            sample["process"] = execution
            if advice:
                sample["cache_eviction"] = advice
            if execution["status"] != "ok":
                sample["status"] = execution["status"]
            write_json(destination, sample)
            samples.append(sample)
            if sample.get("status") != "ok":
                break
        results["experiments"][label] = summarize_samples(samples)
        save()
        return samples

    save()
    for case in ("stats", "model_options", "suggest_common", "query_rare", "query_unique"):
        measure(case, "evict_db_" + case, cold=True)

    cold_http = []
    for index in range(repeats):
        print(f"experiment evict_db_http_home {index + 1}/{repeats}", flush=True)
        advice = evict_fixture_pages(manifest)
        server = Server(fixture_path, output / f"cold-home-{index+1}.log", timeout)
        try:
            server.start()
            row = request(server.port, "/?lang=en", timeout=timeout)
            row["cache_eviction"] = advice
            row["server"] = server.stats()
            cold_http.append(row)
        finally:
            server.stop()
    results["cold_http_home"] = cold_http
    save()

    measure("review_cluster_index_walk", "review_index_walk")
    # Verify full ordered-row equality on this large fixture, outside timing.
    from imgdupe.review import _fetch_cluster_matches
    from .workloads import _operation
    with sqlite3.connect(manifest["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        members = list(range(1, min(10000, manifest["image_count"]) + 1))
        old = _fetch_cluster_matches(conn, members)
        new = _operation("review_cluster_index_walk", conn, manifest, output)()
        equivalent = [tuple(row) for row in old] == [tuple(row) for row in new]
        results["review_equivalence"] = {"equal_full_ordered_rows": equivalent, "rows": len(old)}
        if not equivalent:
            raise AssertionError("experimental review SQL did not preserve ordered rows")
    conn.close()
    save()

    for case in ("parse_4k", "parse_32k", "stealth_2048"):
        measure(case, case)

    # Do not add duplicate indexes after the production schema gains them.
    conn = sqlite3.connect(manifest["db_path"])
    names = ("perf_experiment_clusters_image", "perf_experiment_matches_b")
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if {"idx_clusters_image", "idx_matches_reverse"} <= existing:
        results["reverse_index_experiment"] = "skipped: both indexes are already in the production schema"
        conn.close()
    else:
        if existing.intersection(names):
            conn.close()
            raise ValueError("reserved experimental indexes already exist")
        measure("delete_4000", "delete_warmup", count=1)
        measure("delete_4000", "delete_original")
        try:
            conn.execute(f"CREATE INDEX {names[0]} ON clusters(image_id, cluster_id)")
            conn.execute(f"CREATE INDEX {names[1]} ON matches(image_id_b)")
            conn.commit()
            measure("delete_4000", "delete_reverse_indexes_warmup", count=1)
            measure("delete_4000", "delete_reverse_indexes")
        finally:
            for name in names:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.commit()
            conn.close()
        results["experimental_indexes_removed"] = True
    save()

    measure("cluster", "cluster_bounded", count=1)
    # Preserve a partial profile when the normal large clustering run times out.
    measure("cluster", "cluster_partial_profile", count=1, diagnostic=True, budget=min(timeout, 30))
    conn = sqlite3.connect(manifest["db_path"])
    try:
        results["final_match_count"] = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        results["sqlite_quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    save()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.repeats < 1:
        parser.error("timeout and repeats must be positive")
    try:
        output = validate_output_directory(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    run(args.fixture, output, args.timeout, args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
