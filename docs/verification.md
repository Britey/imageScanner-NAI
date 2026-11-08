# Verification results

## Unfinished legacy model query follow-up, 2026-11-08

Fixed the re-review blocker against PR #5 head `9ad5190`. The legacy model-name
scan now checks its final span at EOF or an explicit OR operator even while
parentheses remain open. An extended comma-containing name cannot fall back to
a short model prefix and leak its remaining comma fragments into prompt terms.
Explicit OR alternatives and fully stored unfinished names retain exact matches.

The regression fails before the fix and passes afterward for the reported cases,
nested parentheses and fullwidth separators, including negative and match-any
searches. The full suite runs **361 tests**, with two optional corpus skips, at
**100% statements and branches**: 3,533 statements and 1,098 branches, without
exclusions. A new curated mutation restores the faulty stopping-point check;
all **8/8 relevant model-query mutations** are rejected by assertions on the
361-test baseline, with no survivors, test errors or timeouts. The mutation set
now contains 53 faults; the previous full 52-fault run remains recorded below.

Validation logs are retained locally under the ignored
`performance-results/model-stop-review-2026-11-08/` directory.

## Independent-review fixes, 2026-11-08

Fixed the follow-up model-query blockers and autocomplete dismissal race, plus
the four validated findings from the SWE-2 Flash review experiment. Extended
legacy comma names remain one metadata term; structural closing parentheses
finish model terms before following text or groups. Internal markers expand only
as complete tokens and are restored inside literal prompt qualifiers. Escape and
outside clicks cancel debounce, abort fetches and invalidate late JSON responses.

NAI3 mix weights recognize Unicode minus and fullwidth number/decimal syntax
without changing literal tag text. Moved images reconcile existing path-derived
labels during the first scan with prompt extraction disabled, preserving prompt
content, other tags and hashes. Benchmark charts define all six cycled colors;
both benchmark CLIs reject file output paths with usage error 2 before fixture
or output changes.

The full suite runs **360 tests**, with the same two optional reference-corpus
skips, and reaches **100% statements and branches**: 3,532 statements and 1,098
branches, without exclusions. Regression cases failed before their fixes, then
passed. The curated mutation set now contains 52 faults, including the new parser,
dismissal, move-label and Unicode-weight regressions. The manual workflow keeps
the 100% gate and has a 30-minute budget for the expanded full-suite mutation run.
No GitHub workflow was launched.

Logs are retained locally under the ignored
`performance-results/independent-review-fixes-2026-11-08/` directory. Historical
review and experiment reports describe the snapshots they originally tested.

## Model query review follow-up, 2026-11-08

Fixed the two model-query blockers reported against PR #4 head
`7417664fcca59abb92067b243962e34a608e5fef`. Unknown or longer parenthesized model
names now remain single metadata terms instead of leaking inner comma fragments
into ordinary prompt searches. Balanced structural wrappers are removed before
comma splitting, including nested and embedded groups. Exact model-name matching
still requires explicit separators before separate tags.

The regressions reproduced failures before the fix. The final suite runs
**349 tests**, with the same two optional reference-corpus skips, at **100%
statements and branches**: 3,517 statements and 1,088 branches, without exclusions.
A bounded-work regression also checks that a model term containing 1,000 inner
comma fragments is not repeatedly normalized at every fragment.

Logs are retained locally under the ignored
`performance-results/model-query-review-2026-11-08/` directory.

## Behavioral test audit, 2026-11-08

Reviewed the interrupted draft tests and every unchanged test module against the
intended app behavior. The [per-module audit](test-audit.md) records replacements,
retained contracts, source simplifications and the limits of the test boundaries.

The final suite runs **344 tests**, with two optional reference-corpus skips, and
reaches **100% statements and branches**: 3,489 statements and 1,072 branches,
with no exclusions, missing statements or partial branches. The existing 100%
coverage gate remains in `pyproject.toml` and the manual verification workflow.
Validation used Linux, Python 3.14.4, SQLite 3.46.1, Pillow 13.0.0, coverage.py
7.17.2 and Node.js 24.21.0. The workflow provisions Node.js 22 for rendered
JavaScript behavior checks; local tests require Node.js 18 or newer.

All **43/43 curated semantic mutations** are rejected by assertion failures,
with no survivors, test errors or timeouts on the final 344-test baseline. This
includes the original 38 faults (with move ambiguity retargeted to the active
scanner) plus five additional model-name/autocomplete faults. Cardinality
assertions prevent missing results from becoming accidental runtime-error kills.
This is a curated check, not an exhaustive mutation score.

The TLA+ checks also pass, including the intentionally invalid negative control:
82,416 distinct states, 111,145 generated states, depth 9. The model and its
bounded scope are unchanged. No GitHub workflow was launched.

A broken thumbnail-cache link was converted from a print-only scratch finding
into a regression: startup failed before the cache enumeration fix and passed
afterward. The test also checks pruning and restored eviction order.

Final test, coverage, mutation and formal logs are retained locally under the
ignored `performance-results/test-audit-2026-11-08/` directory. Earlier sections
below describe their corresponding historical suites and source revisions.

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
