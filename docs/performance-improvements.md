# Performance improvements and validation

Implemented against main `ce47e444f5d056bc3f1e117e7cda37ffa5998020`.
Raw benchmark results are retained locally under the ignored `performance-results/`
directory, outside Git. The [toolkit](performance-testing.md) reproduces the samples,
SQL plans, profiles, HTTP measurements and browser traces. This document records
the findings and tradeoffs.

## Measured results

The largest fixture contains 200,000 images, 5 million tags and 16.8 million hash-band rows. Its SQLite file grew from 2.504 to 2.538 GiB (about 35.7 MiB extra).
The same machine, seeded distributions and real application entry points were used.
Backend and ordinary HTTP rows below are medians of three samples with normal OS
page caching. Thumbnail batches and the overlapping-request check are individual
observations, not tail-latency estimates. Large and small fixtures both pass.

| Workload | Before | After |
|---|---:|---:|
| Home page (HTTP) | 1.129 s | 1.152 ms |
| Autocomplete common prefix (HTTP) | 324.531 ms | 1.082 ms |
| Common AND rare tags (HTTP) | 408.461 ms | 2.442 ms |
| Unique common-tag search (HTTP) | 1.424 s | 830.649 ms |
| Thumbnail while search runs (HTTP) | 1.400 s | 1.148 ms |
| 10,000-member sparse cluster review | 11.663 s | 21.994 ms |
| Repeated empty scan preparation | 3.759 s | 0.829 ms |
| Common-grid image query, fresh process | 386.725 ms | 191.189 ms |
| Delete 4,000 records, rolled back | 3.334 s | 4.177 s |
| 100 uncached thumbnails, 6 requests at once | 930.790 ms | 217.743 ms |
| 100 full-cache misses, 6 requests at once | 1.609 s | 255.160 ms |
| 100 cached thumbnails, 6 requests at once | 36.456 ms | 82.744 ms |

The scan median describes the repeated, unchanged-configuration path. The first
large-fixture scan still performs one configuration reconciliation (2.335 s); this work is skipped thereafter. Empty-root scans measure preparation, not filesystem indexing throughput.
Peak worker RSS for this workload fell from 440.7 to 56.1 MiB.

Fresh-process image timings include lazy library initialization. Persistent-server
image search was essentially unchanged (about 32 ms); this is not a claim of a
2× improvement for every upload. Tryhard queries were also measured, with exact
crop-score and retrieval equivalence covered separately by tests.

Browser observations at 1280×800: home first contentful paint was 112 ms (baseline
1,200 ms), result-page paint 548 ms (baseline 860 ms). Typing six characters sent
one autocomplete request and completed in 632 ms including typing/debounce
(baseline about 2,181 ms). All 100 lazy result images loaded, with zero failures
and no observed long tasks. Scrolling remains about one second because the probe
deliberately pauses between scroll steps. These are single browser observations.
Navigation/resource traces are retained locally with the benchmark results.

## Cold-cache follow-up

Best-effort `fsync` + `POSIX_FADV_DONTNEED` was scoped to the owned database;
this is not a guarantee about storage/controller caches. Three-sample medians:

| Workload | Before | After |
|---|---:|---:|
| HTTP home | 4.862 s | 2.347 ms |
| Model options | 5.746 s | 0.220 ms |
| Common-prefix autocomplete | 3.937 s | 0.455 ms |
| Unique common-tag search | 5.932 s | 3.230 s |

The ordinary 32K weighted prompt workload fell from 1.327 s to 19.6 ms;
header-only stealth inspection of a 2048-square image fell from 107.4 to 7.7 ms.

## Cost changes and limits

| Path | Resulting work |
|---|---|
| Home counters | Read one transactionally maintained summary row; no collection scan. |
| Autocomplete/model filters | Probe distinct visible tag spellings/categories, rather than millions of image/tag occurrences. Cost still grows with the matching vocabulary. |
| Prompt search | Select a rare positive group first; use indexed membership and temporary wanted-term tables. Sort narrow IDs/keys and fetch prompt text/metadata only for the selected page. Common searches and deep offsets still scale with the matching rows and sort. |
| Image query | One decode; batches of 400 candidate images/hashes/crops; a stable bounded top-k heap. Exact crop comparison remains O(Q×C) in the worst case, with deduplication and bounded integer conversion caches. |
| Scan | Stream 400-path/row batches, at most 2×workers pending tasks, bounded rule cache. No whole-database preparation on each unchanged scan. SHA/size index probes still depend on the number of identical digest candidates. |
| Clustering | Load hashes once, fetch only greater-ID candidates, avoid candidate sorting, and skip full scores using an exact upper bound. Hash storage remains O(N); dense candidate/output graphs still have O(N²) worst cases. |
| Review | Walk indexed edges with indexed membership. Stream edges into JSON/HTML; memory no longer grows with every edge. SQL ordering and the output itself still cost work proportional to edges (including sort cost). Largest-cluster image metadata remains resident. |
| Web UI | Eight bounded request threads, request-local SQLite connections, 180 ms autocomplete debounce/cancellation, lightweight scan polling, and indexed in-memory thumbnail LRU. Cache indexing/sorting occurs at startup; explicit clearing still walks the cache. |

No candidate caps or approximate hot-bucket dropping were introduced. Score
rounding, tie order, exclusions, unique-prompt grouping and all crop retrieval
directions have regression tests.

Tradeoffs: maintaining summary tables adds transactional write work. Deleting
4,000 rows was 25% slower here despite reverse indexes. An isolated rare-tag query rose from about 0.36 to 1.13 ms due to bounded temporary-term setup. Cached-thumbnail batches also have more request/thread/connection overhead; uncached batches and responsiveness during searches improved substantially.
The first open of an older database performs a transactional one-time projection
backfill and needs disk space for the new summaries/indexes. Interrupted backfill
rolls back its version marker and data. Parser version 6 causes prompt parsing to
be refreshed on the next applicable scan.

## Completed clustering run

The final 200,000-image clustering run completed in **153.53 seconds**
with 402.3 MiB peak worker RSS. It enumerated
12,975,635 pairs; the exact upper bound rejected all before
full grid scoring. This synthetic random-hash fixture produced no accepted
matches, so it does **not** represent the cost of writing a dense duplicate graph.
Within the comparable 90-second cap, progress increased from about 43,100 to
80,800 images. The baseline timed out, so no complete-runtime speedup ratio is claimed.
Dense candidate sets remain quadratic; this PR does not remove that fundamental
limitation. Both timed-out runs rolled back, and the completed run passed SQLite
quick-check, match/cluster foreign-key checks, summary-counter comparison and a
successful WAL checkpoint.

## Correctness fixes from the independent review

- Spawn worker processes explicitly, including scans launched by the Web UI.
- Count traversal errors; only confirmed absent paths qualify as moved/deleted. Permission and other I/O failures do not prove absence.
- Preserve literal NovelAI `::` and separate tags after closing weighted scopes; make scope scanning linear. Read stealth headers before allocating payload arrays.
- Publish thumbnails atomically and remove temporary failures; publish streamed review files atomically, closing handles before cleanup on Windows.
- Sanitize new model identities containing separators and recognize legacy model query terms, including exclusions and explicit OR groups.
- Reuse unchanged deterministic decode failures, retry transient I/O failures, and retry too-small images when minimum dimensions permit them.
- Use platform-specific folder launchers and report launcher errors normally.
- Reject nonpositive query/tag limits and avoid SQLite variable-limit failures for large term lists.
- Commit temporary-table transactions only when the helper opened them, preserving caller transactions and allowing final WAL checkpoints.

## Verification and cleanup

290 tests pass (two optional reference-corpus skips); all 3,554 application statements
and 1,120 branches are covered, with no exclusions. All 38 curated mutations fail
with assertions; both review mutations also pass a final rerun. The TLA+ visibility
model and its negative control pass. Independent cross-reviews covered each subsystem.
Verification logs are retained locally with the benchmark results, outside Git.
The reusable [toolkit](performance-testing.md) remains. Generated databases, WALs,
images, thumbnail caches and the temporary test environment are removed after measurement.
