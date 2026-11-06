# Performance testing toolkit

The toolkit creates disposable SQLite collections, invokes the real application,
and retains timing samples, fixture manifests, query plans, profiles, and reports.
It never rewrites application code and needs no network service.
The process/RSS harness currently targets Linux/POSIX; the browser probe is
ordinary browser JavaScript. Linux is required for scoped file-cache eviction.

Use the project's normal Python environment with its dependencies installed:

```bash
python scripts/perf_test.py \
  --sizes 20000,200000 --repeats 3 --timeout 90 \
  --output performance-results/my-run
```

Both `.perf-work/` (disposable scratch) and `performance-results/` (retained raw
output) are Git-ignored. Commit concise findings rather than generated reports,
profiles or logs. The default scratch parent is `.perf-work/`. Use
`--work-dir /path/on/your/ssd` to choose a disk. Do not use a RAM-backed `/tmp`
when investigating disk performance. Allow roughly 12 GB of free scratch space
for the default large fixture, including temporary index construction overhead.
The finished 200,000-image fixture is around 2–3 GiB with this schema/profile.

The runner removes its generated databases, WAL files, images, and thumbnail
caches in `finally`, including after ordinary failures or interruption. Only a
directory with its exact run ownership marker and token can be cleaned. A hard
machine/process kill cannot execute cleanup; the token is written immediately
to `summary.json` so cleanup can be resumed:

```bash
python scripts/perf_test.py --cleanup /absolute/path/from/summary \
  --cleanup-token TOKEN_FROM_SUMMARY
```

`--keep-fixtures` deliberately retains them for browser checks or extra workloads.
Use that same cleanup command afterwards. Never point a benchmark worker at a
real user database: workers require the generator's fixture ownership marker.

## What the fixture represents

- 24 positive tags and one model-category tag per image. One tag matches every
  image, a rare tag matches exactly one, and a 10,000-name tail exercises indexes.
- Groups of ten images share prompts. The manifest records actual text lengths
  and compressed metadata widths. Metadata is valid compressed JSON with expanded
  synthetic generation history, not an empty database padded to a large file size.
- Three whole hashes and nine grid hashes produce **84 band rows per image**.
  Synthetic hashes are seeded random values, with a controlled 1% cohort sharing
  a grid tile. They model query cardinality, not a natural-image hash distribution.
- Sparse match graphs include a 10,000-member chain and smaller clusters.
- Up to 256 real 512×512 images allow actual image decoding, image upload search,
  and thumbnail generation. Remaining paths do not exist. The empty scan workload
  measures global scan preparation, **not** real filesystem indexing throughput.
- Production indexes are restored after bulk generation, then the normal
  one-time search-summary migration builds the production projections and
  installs their maintenance triggers. Fixture construction bypasses per-row
  summary maintenance only during bulk loading. `ANALYZE` is not run,
  matching the application's current setup. No crop index is generated.

## Measurements and interpretation

Backend samples run sequentially in fresh subprocesses using actual functions.
They record operation wall time, CPU time, whole-worker peak RSS, and result
counts. Source SHA-256 fingerprints accompany each worker result so uncommitted
changes can be identified. Subprocess startup and rollback time are reported separately. Lazy library
initialization inside an operation is included; a persistent Web UI can amortize
that cost. No timing samples run concurrently with diagnostics.

The normal policy does **not** flush OS caches. Fresh-process measurements are
not cold-disk measurements; generation and earlier queries warm filesystem pages.
`--cache-policy evict-db` uses `fsync` and `POSIX_FADV_DONTNEED` on owned DB/WAL
files before each backend sample. This is a best-effort Linux cache hint, not a
guarantee about storage/controller caches; it never drops global OS caches.
HTTP cases still run as a normal persistent server under either policy.
Linux workers also record `/proc/self/io` deltas where available; `read_bytes`
counts storage reads, while `rchar` includes reads satisfied from cache. These
snapshots are taken outside the measured wall-time interval.

Each backend call has a process-group timeout. Timeouts are retained as censored
results, never counted as fast successes. A timed-out case is not repeated.
Three samples support medians and observed ranges, not confident tail estimates.
The HTTP harness's empirical p95 with three samples is simply the maximum.

Separate diagnostic runs record cProfile, bounded SQL summaries, SQLite VM work,
and `EXPLAIN QUERY PLAN`. These times are **not** mixed into benchmark medians.
Image diagnostics also report candidate counts and band hits. In particular,
inspect SQL time versus Python processing before attributing slow image queries
to candidate scoring or slow home pages to rendering.

The real HTTP suite starts the production Web UI with its bounded request threads and CSRF
checks, measures home/autocomplete/prompt/image search, loads 100 thumbnails with
six concurrent connections, and repeats with a warm and full thumbnail cache.
It also measures a typing burst and a cached thumbnail requested during a slow
search, to expose request queueing. HTTP timing excludes browser layout/paint.
The contention test records observed interval overlap; client scheduling does not
prove the exact server execution ordering.

## Browser measurements

Keep the fixture temporarily and start the actual UI:

```bash
python -m scripts.perf_toolkit.webbench \
  --fixture /owned/run/n200000/fixture.json --serve-worker --port 8766
```

Open `http://127.0.0.1:8766/?lang=en`. Paste
`scripts/perf_toolkit/browser_probe.js` into DevTools (or use a browser evaluation
tool). It exposes:

```javascript
imgdupePerf.capture()                     // navigation, paint, resource timing
await imgdupePerf.autocompleteBurst()     // ordinary input events, 80 ms apart
await imgdupePerf.scrollResults()         // normal scrolling to load lazy images
```

For a 100-image result page with real assets, search `common`, uncheck unique
prompts, choose creation date oldest first, and keep the limit at 100. Reinstall
the probe after navigation. Capture the initial load and then the scroll result;
save returned JSON beside the HTTP results. Browser cache state and viewport
must be recorded. Stop the server before deleting its fixture.

## Focused and expensive cases

```bash
# Short smoke run, all normal cases
python scripts/perf_test.py --sizes 50 --repeats 1 \
  --output performance-results/smoke

# Focus on prompt searching, without HTTP or profiler overhead
python scripts/perf_test.py --sizes 20000,200000 \
  --cases query_rare,query_common_rare,query_unique \
  --skip-web --skip-diagnostics --output performance-results/prompts

# One additional operation on a retained fixture
python -m scripts.perf_toolkit.workloads --fixture /owned/run/fixture.json \
  --case parse_32k --output performance-results/parser.json
```

`cluster` is opt-in because it can take a long time and replaces the generated
graph if successful. Run it last, through the timeout-enforcing runner (for
example `--cases cluster --skip-web`). Killing an incomplete cluster run rolls
back its transaction. `delete_400` and `delete_4000` always roll back; their timing
includes index/cascade work but excludes rollback. `parse_4k`, `parse_32k`, and
`stealth_2048` are additional optional worker cases.

For an owned fixture retained with `--keep-fixtures`, the follow-up suite runs
scoped cache-eviction samples, actual HTTP home requests after eviction, a
benchmark-only alternative review query, a reverse-index deletion experiment,
parser/stealth measurements, and bounded full clustering:

```bash
python -m scripts.perf_toolkit.experiments \
  --fixture /owned/run/n200000/fixture.json \
  --output performance-results/my-run/experiments --timeout 90
```

It verifies ordered-row equality for the alternative review SQL and removes its
experimental indexes afterwards. Normal application SQL is unchanged. Its final
clustering profile has a separate 30-second cap and preserves partial evidence on
SIGTERM. Run the owned-workspace cleanup command after all follow-up work.

Reports: `report.html` provides interactive backend/HTTP charts; `report.md`
contains tables; `summary.json` and per-case JSON are the authoritative raw
evidence. Synthetic timings on one machine are workload-specific, not a universal
capacity limit or a prediction for another user's disk and image collection.

Toolkit checks:

```bash
python -m unittest discover -s tests -p test_perf_toolkit.py
```
