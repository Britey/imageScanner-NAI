# Verification results

## Performance changes, 2026-11-07

The performance PR starts from main `ce47e444f5d056bc3f1e117e7cda37ffa5998020`.
Local validation uses Python 3.13.15, SQLite 3.53.1 and coverage.py 7.17.2.
The complete suite runs **290 tests**, with the same two optional corpus skips,
and reaches **100% application statements and branches** (3,554 statements,
1,120 branches), without exclusions.
All **38/38 curated semantic mutations** are rejected by assertions (288-test
isolated baseline); both review mutations were also rerun against the final
290-test baseline. No mutant survived or failed merely with an import/runtime
error. Validation logs and raw measurements are retained locally under the ignored
`performance-results/2026-11-07-final/` directory, outside Git. The measured findings
are summarized in [performance improvements](performance-improvements.md).

New regressions exercise an actual spawned process pool from a background thread
with a WAL connection open; inaccessible directories and move safety; failed-file
recovery; bounded scan batches; low SQLite parameter limits; summary updates,
rollback and migration; score equivalence; linear prompt parsing; atomic export
and thumbnail failures; and concurrent HTTP requests with separate connections.

The finite TLA+ visibility model still passes with its negative control. Its
scope is now explicitly the reference visibility semantics; incremental trigger
transitions are validated by SQLite tests, not claimed to be formally modeled.

## Earlier verification baseline

Validated locally on 2026-11-07, starting from PR #2 head
`193e75c2ffc98c4ff30658b5d65db4ea45878e38`. Latest `origin/main` was fetched and
confirmed to be an ancestor before implementation.

Environment: Linux, Python 3.13.15, SQLite 3.53.1, coverage.py 7.17.2,
Pillow 13.0.0, ImageHash 4.3.2, NumPy 2.5.4; OpenJDK 21 with TLA+ tools v1.7.4.
The same full suite and 100% coverage gate also pass on Python 3.14.4 with SQLite
3.46.1, including the batched lookup query-plan and large-cluster regressions.
The 38-mutation run used the primary Python 3.13/SQLite 3.53.1 environment.

- **236 tests run**, with two existing optional-reference-corpus skips and no
  failures or errors. Generated image/database fixtures run without that corpus.
- **100% package statements and branches**: 3,162/3,162 statements and 992/992
  branches; zero exclusions, partial branches, or missing lines. The configuration
  enforces this threshold for every module under `imgdupe`.
- **38/38 selected semantic mutations rejected by assertions**, with zero
  survivors, test errors, or timeouts. The runner also requires a passing baseline
  and unchanged test-discovery count in each isolated copy. This is a curated
  set of faults, not an exhaustive mutation score.
- **10,000 concrete SQLite snapshots** compare the optimized distinct-tag SQL,
  the original SQL, and an independent visibility oracle.
- **TLA+ safety and termination checks pass**: 5,625 initial snapshots,
  82,416 distinct reachable states, 111,145 generated states, search depth 9.
  An intentionally incorrect visibility rule fails `CorrectDecisions` as expected.
  See [model scope and assumptions](../formal/README.md); this is bounded model
  checking, not an unbounded proof of the Python implementation.
- The workflow configuration was parsed and confirmed to have exactly one
  trigger, `workflow_dispatch`. No GitHub workflow was launched.

Adversarial review and mutations strengthened assertions for real background
scan persistence, complete option forwarding, exceptional server cleanup,
thumbnail recency/pixels, crop-match direction, inclusive score thresholds, and
zero-weight NovelAI mix segments. Two regressions exposed and now prevent actual
bugs: multiline multipart headers losing the upload extension, and legacy
metadata helpers discarding plain `Description`/`prompt` values. Reverting either
fix is one of the rejected mutations.

The post-review fixes cover all five confirmed findings:

- The HTTP server validates Host and browser origin metadata, requires a
  per-process token on POSTs, and makes show-in-folder a POST action. Tests assert
  rejected requests cannot save configuration, start scans, clear caches, or
  launch a folder. Rendered forms and real local HTTP requests exercise valid
  token roundtrips.
- Body lengths are mandatory, validated and capped (256 KiB forms, 64 MiB uploads).
  Absolute header/body deadlines bound stalled requests. Temporary uploads are
  uniquely named and removed after successful or failed queries. Tests cover
  malformed/oversized/short bodies, write/close failures and a stalled socket
  followed by a successful request to the same single-threaded server.
- Batched, deduplicated band probes preserve crop-to-whole, whole-to-crop, and
  cross-region crop/tile-to-crop retrieval. Real SQLite fixtures isolate every
  direction; generated images share an identical quadrant at different positions.
  Query-plan assertions require all three covering-index lookup keys and a
  separate assertion bounds the number of batched SQL statements.
- Highlighting finds tags in raw text, then escapes each segment. Entity names
  no longer match encoded punctuation; exact HTML and visible-text roundtrip
  assertions cover punctuation, markup, overlapping tags and Unicode.
- Cluster membership uses a deduplicated connection-local temporary table rather
  than one SQL placeholder per member. Real SQLite tests reduce the variable
  limit to one, prove the old query fails, and check all matches, global ordering,
  representative tie rules, nested contexts, failure cleanup and caller rollback.
  Membership subqueries preserve indexed lookups; a VM-work regression adds
  5,001 unrelated images and 5,000 matches and confirms that a two-image cluster
  avoids full-table scans. It fails for both former JOIN queries and passes on
  both verified Python/SQLite runtimes while allowing only one bind variable.

Other production changes remove unreachable PNG branches and a return after
`ArgumentParser.error`, and express the existing GB size fallback with a reachable
loop exit. No coverage pragmas or omitted production modules are used.

Reproduce the Python and mutation checks with the commands in
[README.md](../README.md#verification), and the TLA+ checks with
[formal/README.md](../formal/README.md#run-locally).
