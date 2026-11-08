"""Sequential subprocess benchmark orchestration and owned-fixture cleanup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
OWNER_FILE = ".imgdupe-perf-run.json"
DEFAULT_CASES = (
    "init_db", "stats", "model_options", "suggest_common", "suggest_narrow",
    "query_rare", "query_common_rare", "query_common", "query_unique",
    "query_deep_offset", "image_query", "image_query_common_grid", "scan_empty",
    "review_cluster", "delete_400", "delete_4000",
)
DEFAULT_DIAGNOSTICS = (
    "stats", "model_options", "suggest_common", "query_common_rare", "query_unique",
    "image_query_common_grid", "scan_empty", "review_cluster",
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_output_directory(output: Path) -> Path:
    """Check a destination before creating output or disposable fixtures."""
    output = output.resolve()
    if output.exists():
        if not output.is_dir():
            raise ValueError("output path must be a directory")
        if any(output.iterdir()):
            raise ValueError("output directory must be new or empty")
    return output


def make_workspace(parent: Path) -> tuple[Path, str]:
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    workspace = parent.resolve() / ("run-" + token)
    workspace.mkdir()
    write_json(workspace / OWNER_FILE, {"token": token, "path": str(workspace)})
    return workspace, token


def cleanup_workspace(workspace: Path, token: str) -> None:
    """Delete only the exact directory created by this invocation, never a parent."""
    if workspace.is_symlink():
        raise ValueError("refusing to clean a symlink")
    marker = json.loads((workspace / OWNER_FILE).read_text(encoding="utf-8"))
    if marker != {"token": token, "path": str(workspace.resolve())}:
        raise ValueError("workspace ownership marker does not match")
    shutil.rmtree(workspace)


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        # A leader may exit before its server/worker children. Wait for the
        # group, not just the leader, before returning to fixture cleanup.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_command(command: list[str], log_path: Path, timeout: float) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    start = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=(os.name == "posix"))
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            return {"status": "timeout", "timeout_seconds": timeout,
                    "process_seconds": time.perf_counter() - start, "log": str(log_path)}
        except BaseException:
            stop_process(process)
            raise
    return {"status": "ok" if code == 0 else "error", "returncode": code,
            "process_seconds": time.perf_counter() - start, "log": str(log_path)}


def summarize_samples(samples: list[dict]) -> dict:
    valid = [s for s in samples if s.get("status") == "ok" and "wall_seconds" in s]
    result = {"samples": samples, "successful_samples": len(valid), "sample_count": len(samples)}
    if valid:
        values = [s["wall_seconds"] for s in valid]
        result.update(median_seconds=statistics.median(values), min_seconds=min(values),
                      max_seconds=max(values), first_seconds=values[0],
                      max_peak_rss_mib=max(s.get("peak_rss_mib", 0) for s in valid))
    return result


def evict_fixture_pages(manifest: dict) -> dict:
    """Best-effort Linux file-cache eviction, scoped to the generated DB only.

    This is a kernel hint, not proof of cold hardware caches. Never drops global
    OS caches or changes system settings. Call only with fixture users stopped.
    """
    from .webbench import load_fixture
    fixture = load_fixture(Path(manifest["work_dir"]) / "fixture.json")
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        raise RuntimeError("best-effort file-cache eviction is unavailable on this platform")
    files = []
    for path in (Path(fixture["db_path"]), Path(fixture["db_path"] + "-wal")):
        if not path.exists():
            continue
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
            os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        files.append(str(path))
    return {"method": "fsync + POSIX_FADV_DONTNEED on owned DB/WAL only", "files": files,
            "note": "Best-effort hint; not a guarantee of cold storage/controller caches."}


def source_fingerprints() -> dict[str, str]:
    return {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((REPO / "imgdupe").glob("*.py"))}


def environment_info() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO, text=True, capture_output=True, check=True).stdout.strip()
    packages = {}
    for name in ("pillow", "numpy", "imagehash", "scipy", "pywavelets", "tqdm"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    cpu = platform.processor()
    if Path("/proc/cpuinfo").exists():
        cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")), cpu)
    return {"created_at": datetime.now(timezone.utc).isoformat(), "commit": git("rev-parse", "HEAD"),
            "working_tree": git("status", "--short"), "source_sha256": source_fingerprints(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "cpu": cpu, "logical_cpus": os.cpu_count(),
            "packages": packages, "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="20000,200000", help="Comma-separated image counts; sequential fixtures")
    parser.add_argument("--output", type=Path, help="New retained results directory")
    parser.add_argument("--work-dir", type=Path, default=REPO / ".perf-work", help="Scratch parent, preferably on SSD")
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--metadata-bytes", type=int, default=2048)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--generation-timeout", type=float, default=3600)
    parser.add_argument("--cases", default=",".join(DEFAULT_CASES))
    parser.add_argument("--cache-policy", choices=("normal", "evict-db"), default="normal",
                        help="evict-db hints eviction of generated DB pages before each backend sample; no global cache flushing")
    parser.add_argument("--skip-web", action="store_true")
    parser.add_argument("--skip-diagnostics", action="store_true")
    parser.add_argument("--keep-fixtures", action="store_true", help="Keep owned scratch workspace for follow-up/browser tests; clean explicitly afterwards")
    parser.add_argument("--cleanup", type=Path, help="Clean a previously retained owned run, using --cleanup-token")
    parser.add_argument("--cleanup-token")
    args = parser.parse_args(argv)
    if args.cleanup:
        if not args.cleanup_token:
            parser.error("--cleanup requires --cleanup-token from summary.json")
        cleanup_workspace(args.cleanup, args.cleanup_token)
        return 0
    if not args.output:
        parser.error("--output is required for a benchmark run")
    sizes = [int(value) for value in args.sizes.split(",")]
    if not sizes or min(sizes) < 2 or len(set(sizes)) != len(sizes) or args.repeats < 1 or args.timeout <= 0:
        parser.error("sizes must be distinct and at least 2; repeats and timeout must be positive")
    cases = [name for name in args.cases.split(",") if name]
    from .workloads import CASES
    if not cases or any(name not in CASES for name in cases):
        parser.error("unknown/empty --cases; see scripts.perf_toolkit.workloads --help")
    try:
        output = validate_output_directory(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    output.mkdir(parents=True, exist_ok=True)
    workspace, token = make_workspace(args.work_dir)
    summary = {"environment": environment_info(), "configuration": vars(args) | {"output": str(output),
               "work_dir": str(args.work_dir), "cleanup": None},
               "methodology": ["Sequential actual application calls in fresh subprocesses; the toolkit does not modify application source.",
                   "Generation is excluded from measured operation wall time; process startup is recorded separately.",
                   "OS page cache is NOT flushed: first-process and repeated samples are not cold-disk claims.",
                   "Peak RSS is whole-worker high-water memory, including imports; wall/CPU time cover the operation.",
                   "cProfile/SQL diagnostics are separate from uninstrumented timing samples.",
                   "Synthetic distributions establish workload sensitivity, not production-corpus accuracy."],
               "workspace": str(workspace), "cleanup_token": token, "fixtures_removed": False, "runs": []}
    write_json(output / "summary.json", summary)
    failure = False
    try:
        for count in sizes:
            label = f"n{count}"
            if shutil.disk_usage(workspace).free < count * 60000 + 1024**3:
                raise RuntimeError("Insufficient scratch space for fixture and index creation (conservative 60kB/image + 1GiB estimate)")
            fixture_dir = workspace / label
            result_dir = output / label
            result_dir.mkdir()
            print(f"Generating {count:,} images at {fixture_dir}", flush=True)
            generation = run_command([sys.executable, "-m", "scripts.perf_toolkit.fixture", "--work-dir", str(fixture_dir),
                                      "--images", str(count), "--seed", str(args.seed), "--metadata-bytes", str(args.metadata_bytes)],
                                     result_dir / "generation.log", args.generation_timeout)
            if generation["status"] != "ok":
                raise RuntimeError(f"Fixture generation {generation['status']}: {generation['log']}")
            fixture_path = fixture_dir / "fixture.json"
            manifest = json.loads(fixture_path.read_text(encoding="utf-8"))
            write_json(result_dir / "fixture-manifest.json", manifest)
            run = {"label": label, "fixture": manifest, "generation": generation, "backend": {}, "diagnostics": {}}
            summary["runs"].append(run)
            write_json(output / "summary.json", summary)
            for case in cases:
                samples = []
                for index in range(args.repeats):
                    sample_path = result_dir / f"{case}-{index + 1}.json"
                    print(f"{label} {case} sample {index + 1}/{args.repeats}", flush=True)
                    eviction = evict_fixture_pages(manifest) if args.cache_policy == "evict-db" else None
                    execution = run_command([sys.executable, "-m", "scripts.perf_toolkit.workloads", "--fixture", str(fixture_path),
                                             "--case", case, "--output", str(sample_path)], sample_path.with_suffix(".log"), args.timeout)
                    sample = json.loads(sample_path.read_text()) if sample_path.exists() else {"case": case}
                    sample["process"] = execution
                    if eviction:
                        sample["cache_eviction"] = eviction
                    if execution["status"] != "ok":
                        sample["status"] = execution["status"]
                    samples.append(sample)
                    write_json(sample_path, sample)
                    if sample.get("status") != "ok":
                        failure = True
                        break  # Do not repeatedly run a known timeout/error.
                run["backend"][case] = summarize_samples(samples)
                write_json(output / "summary.json", summary)
            if not args.skip_web:
                print(f"{label} actual HTTP Web UI benchmarks", flush=True)
                web_output = result_dir / "web.json"
                execution = run_command([sys.executable, "-m", "scripts.perf_toolkit.webbench", "--fixture", str(fixture_path),
                                         "--output", str(web_output), "--repeats", str(args.repeats), "--timeout", str(args.timeout)],
                                        result_dir / "web.log", max(600, args.timeout * 20))
                run["web"] = json.loads(web_output.read_text()) if web_output.exists() else execution
                run["web_process"] = execution
                failure |= execution["status"] != "ok"
                write_json(output / "summary.json", summary)
            if not args.skip_diagnostics:
                for case in DEFAULT_DIAGNOSTICS:
                    if case not in cases or not run["backend"][case]["successful_samples"]:
                        continue
                    print(f"{label} separate diagnostic {case}", flush=True)
                    diagnostic_path = result_dir / "diagnostics" / f"{case}.json"
                    execution = run_command([sys.executable, "-m", "scripts.perf_toolkit.workloads", "--fixture", str(fixture_path),
                                             "--case", case, "--output", str(diagnostic_path), "--diagnostic",
                                             "--profile", str(diagnostic_path.with_suffix(".prof"))],
                                            diagnostic_path.with_suffix(".log"), args.timeout * 2)
                    run["diagnostics"][case] = {"file": str(diagnostic_path), "process": execution}
                    write_json(output / "summary.json", summary)
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if not args.keep_fixtures:
            cleanup_workspace(workspace, token)
            summary["fixtures_removed"] = True
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "summary.json", summary)
        from .report import write_reports
        write_reports(summary, output)
        print(f"Results retained in {output}; fixtures_removed={summary['fixtures_removed']}", flush=True)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
