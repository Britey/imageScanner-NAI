"""Manually check that regression tests reject curated, realistic semantic faults.

Run with the same interpreter/environment used for the normal test suite:
    python scripts/check_mutations.py

This is a selected fault-injection check, not an exhaustive mutation score. The
runner itself uses only the standard library; the copied tests need the project's
normal runtime dependencies. Every mutant changes exactly one anchored site in
a temporary copy. Repository source and tests are never rewritten by this script.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True)
class Mutation:
    name: str
    module: str
    before: str
    after: str
    regression: str


MUTATIONS = (
    Mutation(
        "unique-tags-deduplication", "web",
        "FROM (SELECT DISTINCT tag_norm FROM prompt_tags) tags",
        "FROM (SELECT tag_norm FROM prompt_tags) tags",
        "Repeated occurrences must contribute only one distinct visible tag.",
    ),
    Mutation(
        "unique-tags-model-visibility", "web",
        "WHERE pt.tag_norm = tags.tag_norm\n"
        "              AND NOT (pt.tag_kind = 'model_type' AND ip.configured_model IS NOT NULL)",
        "WHERE pt.tag_norm = tags.tag_norm\n"
        "              AND NOT (pt.tag_kind = 'model_type' AND ip.configured_model IS NULL)",
        "Configured labels suppress detected model tags, including empty non-NULL labels.",
    ),
    Mutation(
        "unique-tags-existential-witness", "web",
        "FROM (SELECT DISTINCT tag_norm FROM prompt_tags) tags\n        WHERE EXISTS (",
        "FROM (SELECT DISTINCT tag_norm FROM prompt_tags) tags\n        WHERE NOT EXISTS (",
        "A name counts when at least one occurrence has a visible prompt-row witness.",
    ),
    Mutation(
        "web-upload-trailing-crlf", "web",
        'content = content[:-2]', 'content = content',
        "Multipart framing CRLF must be removed without altering uploaded image bytes.",
    ),
    Mutation(
        "web-upload-filename-header-regression", "web",
        'header.split(marker, 1)[1].partition("\\n")[0].split(";", 1)[0]',
        'header.split(marker, 1)[1].split(";", 1)[0]',
        "A multipart filename must stop at the header line, preserving its real image suffix.",
    ),
    Mutation(
        "parser-legacy-prompt-source-regression", "nai_prompt",
        'if result.metadata_source == "text:parameters":',
        'if result.prompt_format == "a1111":',
        "Legacy plain-prompt metadata must not be mistaken for a parameters field.",
    ),
    Mutation(
        "web-host-authority-guard", "web",
        'if authority[0] not in allowed_hosts or authority[1] != state.server_port:',
        'if authority[0] not in allowed_hosts and authority[1] != state.server_port:',
        "A request with an untrusted Host must be rejected even when its port matches.",
    ),
    Mutation(
        "web-csrf-token-guard", "web",
        'if not secrets.compare_digest(token, state.csrf_token.encode("ascii")):',
        'if False and not secrets.compare_digest(token, state.csrf_token.encode("ascii")):',
        "State-changing POST requests must require the server's valid CSRF token.",
    ),
    Mutation(
        "web-request-body-limit", "web",
        'if len(lengths[0]) > 20 or int(lengths[0]) > limit:',
        'if len(lengths[0]) > 20 and int(lengths[0]) > limit:',
        "Oversized request bodies must be rejected before reading or allocating their contents.",
    ),
    Mutation(
        "web-query-upload-cleanup", "web",
        'finally:\n                if upload_path is not None:\n                    upload_path.unlink(missing_ok=True)',
        'finally:\n                if False and upload_path is not None:\n                    upload_path.unlink(missing_ok=True)',
        "Uploaded query files must be removed after successful or failed searches.",
    ),
    Mutation(
        "scan-created-time-fallback", "scan",
        'return int(ctime_ns) if ctime_ns is not None else None',
        'return int(ctime_ns) + 1 if ctime_ns is not None else None',
        "Fallback creation timestamps must retain the exact filesystem nanoseconds.",
    ),
    Mutation(
        "scan-ambiguous-move", "scan",
        'if len(image_ids) != 1:', 'if len(image_ids) < 1:',
        "Identical missing files are ambiguous and must not be claimed as one moved image.",
    ),
    Mutation(
        "database-metadata-byte-ceiling", "db",
        'if len(data) > MAX_PROMPT_METADATA_BYTES:',
        'if len(data) > MAX_PROMPT_METADATA_BYTES + 1:',
        "Raw metadata one byte above the storage ceiling must be rejected.",
    ),
    Mutation(
        "database-crop-version", "db",
        'and get_metadata(conn, CROP_INDEX_VERSION_KEY) == CROP_INDEX_VERSION',
        'or get_metadata(conn, CROP_INDEX_VERSION_KEY) == CROP_INDEX_VERSION',
        "Crop readiness requires both the ready state and the current index version.",
    ),
    Mutation(
        "parser-case-normalization", "nai_prompt",
        'return SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", cleaned)).casefold()',
        'return SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", cleaned))',
        "Prompt matching must use case-insensitive normalized tags.",
    ),
    Mutation(
        "parser-zero-mix-weight", "nai_prompt",
        'if float(weight) <= 0:', 'if float(weight) < 0:',
        "Zero-weight NovelAI segments must not enter the searchable positive prompt.",
    ),
    Mutation(
        "hash-hamming-xor", "hashing",
        '(int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()',
        '(int.from_bytes(a, "big") | int.from_bytes(b, "big")).bit_count()',
        "Hamming distance counts differing bits, including identical nonzero hashes.",
    ),
    Mutation(
        "hash-minimum-dimensions", "hashing",
        'if img.width < min_width or img.height < min_height:',
        'if img.width < min_width and img.height < min_height:',
        "An image that is too narrow or too short must be rejected independently.",
    ),
    Mutation(
        "score-exact-duplicate", "match",
        'return PairScore(None, None, None, 9, 0, 0, 100.0, "exact_duplicate")',
        'return PairScore(None, None, None, 9, 0, 0, 90.0, "exact_duplicate")',
        "An exact byte duplicate must retain the full 100-point score.",
    ),
    Mutation(
        "score-inclusive-review-threshold", "match",
        'or score >= thresholds.score_review', 'or score > thresholds.score_review',
        "A score exactly on the review threshold qualifies for review.",
    ),
    Mutation(
        "score-query-tile-direction", "match",
        'candidate_phash = candidate_hashes.get("phash256")',
        'candidate_phash = candidate_hashes.get("dhash256")',
        "A query tile must compare to the candidate whole-image pHash.",
    ),
    Mutation(
        "candidate-whole-band-minimum", "bands",
        'if total_hits >= 2 or whole_hit or grid_hit or crop_hit:',
        'if total_hits >= 1 or whole_hit or grid_hit or crop_hit:',
        "One whole-image band hit alone is insufficient; grid and crop hits differ.",
    ),
    Mutation(
        "query-hide-exact", "query",
        'if sha_equal and not include_exact:', 'if not sha_equal and not include_exact:',
        "Hiding exact duplicates must exclude equal digests rather than other candidates.",
    ),
    Mutation(
        "query-inclusive-minimum-score", "query",
        'pair_score.score >= min_score:', 'pair_score.score > min_score:',
        "An image exactly at the requested minimum score must remain in the results.",
    ),
    Mutation(
        "query-cross-region-crop-retrieval", "query",
        'crop_lookup_hashes.extend(query_crop_hashes.values())',
        'crop_lookup_hashes.extend(())',
        "Query crops and tiles must retrieve candidates through other indexed crop regions.",
    ),
    Mutation(
        "query-whole-to-crop-retrieval", "query",
        'crop_lookup_hashes.append(hashes["phash256"])',
        'crop_lookup_hashes.append(b"")',
        "Cross-region crop lookup must also retain the whole-query to candidate-crop path.",
    ),
    Mutation(
        "query-crop-to-whole-retrieval", "query",
        'lookup_hashes[f"phash256:query_crop:{index}"] = crop_hash',
        'lookup_hashes[f"ignored:query_crop:{index}"] = crop_hash',
        "Query crops and tiles must still retrieve whole-image candidates without a ready crop index.",
    ),
    Mutation(
        "candidate-lookup-index-probes", "bands",
        'CROSS JOIN hash_bands hb', 'JOIN hash_bands hb',
        "Batched crop lookup must probe the covering index rather than scan all stored crop bands.",
    ),
    Mutation(
        "web-highlight-before-escaping", "web",
        'raw_prompt = prompt.strip()', 'raw_prompt = html.escape(prompt.strip())',
        "Prompt highlighting must match raw tags before HTML escaping visible segments.",
    ),
    Mutation(
        "cluster-representative-resolution", "cluster",
        'best = max(\n        rows,', 'best = min(\n        rows,',
        "Cluster representatives prefer the largest resolution, then size and stable ID.",
    ),
    Mutation(
        "cluster-representative-stable-id", "cluster",
        '-int(row["id"]),', 'int(row["id"]),',
        "Equal-quality cluster representatives must retain the smallest stable image ID.",
    ),
    Mutation(
        "review-match-membership-both-endpoints", "review",
        'JOIN {membership} b ON b.image_id = m.image_id_b',
        'JOIN {membership} b ON b.image_id = m.image_id_a',
        "Cluster review must exclude matches with either endpoint outside the selected cluster.",
    ),
    Mutation(
        "review-path-html-escaping", "review",
        "html.escape(image['path'])", "image['path']",
        "Generated review pages must HTML-escape image paths.",
    ),
    Mutation(
        "cli-disable-prompt-index", "cli",
        'prompt_index=not args.no_prompt_index,', 'prompt_index=args.no_prompt_index,',
        "The CLI indexes prompts by default and honors --no-prompt-index.",
    ),
    Mutation(
        "settings-cache-minimum", "settings",
        'thumbnail_cache_limit=max(1, cache_limit),', 'thumbnail_cache_limit=max(0, cache_limit),',
        "Invalid nonpositive cache settings must clamp to at least one thumbnail.",
    ),
    Mutation(
        "platform-contended-file-lock", "single_instance",
        'except BlockingIOError:\n            lock_file.close()\n            return False',
        'except BlockingIOError:\n            lock_file.close()\n            return True',
        "A second application instance must fail to acquire a contended file lock.",
    ),
    Mutation(
        "utility-binary-size-units", "utils",
        'value /= 1024', 'value /= 1000',
        "Displayed KB/MB/GB sizes must preserve the documented binary conversion.",
    ),
    Mutation(
        "model-path-nearest-rule", "model_paths",
        'return max(matches, key=lambda rule: (_path_depth(rule.path), rule.rule_id))',
        'return min(matches, key=lambda rule: (_path_depth(rule.path), rule.rule_id))',
        "The deepest matching model-path rule overrides its ancestor rules.",
    ),
)


TEST_COMMAND = ("-m", "unittest", "discover", "-s", "tests")
FAILURE_SUMMARY = re.compile(r"^FAILED \(([^\n]+)\)$", re.MULTILINE)
TEST_COUNT = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)


def run_tests(workspace: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    # Force this copy ahead of installed/editable checkouts, and prevent a
    # previous mutant's cached bytecode from hiding a later same-size rewrite.
    environment["PYTHONPATH"] = str(workspace)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONPYCACHEPREFIX", None)
    return subprocess.run(
        [sys.executable, *TEST_COMMAND], cwd=workspace, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace", timeout=timeout, check=False,
    )


def assertion_failures(output: str) -> tuple[int, int]:
    summary = FAILURE_SUMMARY.search(output)
    if summary is None:
        return 0, 0
    counts = dict(re.findall(r"(failures|errors)=(\d+)", summary.group(1)))
    return int(counts.get("failures", 0)), int(counts.get("errors", 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120,
                        help="Maximum seconds per full test run (default: 120).")
    parser.add_argument("--list", action="store_true", help="List the curated semantic regressions.")
    parser.add_argument("--mutant", action="append", choices=[m.name for m in MUTATIONS],
                        help="Run only this mutant (repeat to select several).")
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    selected = [m for m in MUTATIONS if not arguments.mutant or m.name in arguments.mutant]
    if arguments.list:
        for mutation in selected:
            print(f"{mutation.name}: {mutation.regression}")
        return 0

    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="imgdupe-mutations-") as temporary:
        workspace = Path(temporary)
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        for directory in ("imgdupe", "tests"):
            shutil.copytree(repository / directory, workspace / directory, ignore=ignored)
        (workspace / "scripts").mkdir()
        shutil.copy2(repository / "scripts" / "compare_reference_prompt_tags.py", workspace / "scripts")
        shutil.copy2(repository / "settings.ini", workspace / "settings.ini")

        # Verify the package location in the same isolated environment used by
        # tests, so an editable installation cannot make mutants silently inert.
        environment = os.environ.copy()
        environment.update(PYTHONPATH=str(workspace), PYTHONDONTWRITEBYTECODE="1")
        probe = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; import imgdupe; "
             "assert Path(imgdupe.__file__).resolve().parent == Path.cwd() / 'imgdupe'"],
            cwd=workspace, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=arguments.timeout, check=False,
        )
        if probe.returncode:
            print("INVALID: temporary package import could not be verified.\n" + probe.stdout)
            return 1

        # Validate every anchored site before starting, including unselected
        # ones, so stale curated mutations never quietly become no-ops.
        originals = {}
        for mutation in MUTATIONS:
            path = workspace / "imgdupe" / f"{mutation.module}.py"
            original = originals.setdefault(path, path.read_text(encoding="utf-8"))
            sites = original.count(mutation.before)
            if sites != 1 or mutation.before == mutation.after:
                print(f"INVALID {mutation.name}: expected one unique mutation site, found {sites}.")
                return 1
            try:
                ast.parse(original.replace(mutation.before, mutation.after, 1))
            except SyntaxError as error:
                print(f"INVALID {mutation.name}: syntax error in curated mutation: {error}")
                return 1

        print("Baseline: running full unittest suite in the temporary copy...", flush=True)
        try:
            baseline = run_tests(workspace, arguments.timeout)
        except subprocess.TimeoutExpired:
            print("INVALID: baseline timed out; no mutants were evaluated.")
            return 1
        test_count = TEST_COUNT.search(baseline.stdout)
        if baseline.returncode != 0 or test_count is None or int(test_count.group(1)) == 0:
            print("INVALID: baseline must pass a nonempty test suite.\n" + baseline.stdout)
            return 1
        print(f"Baseline passed: {test_count.group(1)} tests.\n", flush=True)

        killed = 0
        for mutation in selected:
            path = workspace / "imgdupe" / f"{mutation.module}.py"
            original = originals[path]
            path.write_text(original.replace(mutation.before, mutation.after, 1), encoding="utf-8")
            try:
                result = run_tests(workspace, arguments.timeout)
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT {mutation.name}: not counted as an assertion kill.", flush=True)
                continue
            finally:
                path.write_text(original, encoding="utf-8")
            failures, errors = assertion_failures(result.stdout)
            count = TEST_COUNT.search(result.stdout)
            if (result.returncode != 0 and failures > 0 and errors == 0
                    and count is not None and count.group(1) == test_count.group(1)):
                killed += 1
                tests = re.findall(r"^FAIL: ([^\n]+)", result.stdout, re.MULTILINE)
                print(f"KILLED {mutation.name}: {mutation.regression}", flush=True)
                print("  Assertion regression: " + "; ".join(tests), flush=True)
            elif result.returncode == 0:
                print(f"SURVIVED {mutation.name}: {mutation.regression}", flush=True)
            else:
                # Syntax/import/runtime errors and missing test discovery are
                # invalid experiments, never evidence of a useful test kill.
                print(f"INVALID {mutation.name}: expected assertion failures without errors.\n"
                      + result.stdout, flush=True)
        print(f"\nAssertion kills: {killed}/{len(selected)} curated semantic mutants.")
        return 0 if killed == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
