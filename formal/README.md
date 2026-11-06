# Unique-tag counting model

`UniqueTags.tla` specifies the optimized query in
`imgdupe.web._count_unique_tags`. It enumerates distinct tag names, probes their
occurrences in any order, and stops at the first visible occurrence. An
independent set expression specifies the original JOIN + COUNT(DISTINCT).

The input is an immutable database snapshot, matching a single SQLite SELECT.
The `(image_id, tag_norm)` primary key becomes a function from image/tag pairs
to either a tag kind or `Absent`. A prompt-row set models the inner join;
configured-model presence models SQL `IS NOT NULL`, including empty strings.
Prompt text is immaterial: a NULL prompt still contributes tags. Model identity
and any other non-`model_type` kinds share the ordinary visibility rule.

The checked properties are:

- Types and a partition of pending/decided names remain valid.
- Accepted names equal the original query's visible names among decided names.
- Each accepted name contributes exactly one count.
- Discarding hidden occurrences never loses a visible witness.
- No occurrence is probed twice; probe count is bounded by the number of rows.
- Completion returns exactly the original distinct count.
- Under weak fairness of the query's next step, the query eventually completes.

`UniqueTags.cfg` exhaustively chooses **all 5,625 initial snapshots** for two
images, two tag names, four concrete tag kinds, absent prompt rows, and
NULL/non-NULL configured labels. Every choice of name and occurrence order is
explored. TLC checks 82,416 distinct reachable states, including liveness.
`UniqueTagsMutant.cfg` disables configured-model suppression. The runner requires
TLC to reject that negative control specifically on `CorrectDecisions`, with
exit status 12; syntax errors or an unexpectedly successful mutant fail the run.

`tests/test_unique_tags_model.py` connects the abstraction to the implementation:
it enumerates 10,000 corresponding concrete SQLite databases, distinguishing
empty from nonempty configured labels, and compares the optimized SQL, original
SQL, and an independent Python visibility oracle.

This is bounded model checking of the query algorithm, not an unbounded proof
of the Python program or SQLite query planner. It does not model concurrent
writes between multiple statements, wall-clock latency, or SQLite's VM cost;
the separate existing regression checks actual VM work on repeated tags.

## Run locally

Requires Java 11+ and Bash with `realpath` and `sha256sum` (Linux or WSL).
The official [TLA+ tools](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)
are pinned to v1.7.4; no binaries are committed. Download once outside the repo:

```bash
curl --fail --location --connect-timeout 60 --max-time 180 \
  --retry 3 --retry-delay 15 --retry-all-errors \
  -o /tmp/tla2tools.jar \
  https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
TLA2TOOLS_JAR=/tmp/tla2tools.jar bash scripts/check_formal.sh
```

The runner checks SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`
before executing the jar. State files, logs, and negative-control traces use a
private temporary directory that is removed on exit. The runner itself uses no
network. TLA+ CLI documentation is available in the
[official tools README](https://github.com/tlaplus/tlaplus/blob/master/README.md).
