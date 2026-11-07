# Behavioral test audit — 2026-11-08

This pass reviewed both the interrupted draft rewrites and every previously
unchanged test module. Tests were judged by the behavior or bound they establish,
not by unique lines executed. Overlapping unit and integration tests are retained
when they protect different contracts or failure boundaries.

## Decisions

| Test module | Decision and observable contract |
| --- | --- |
| `test_cleanup` | Replace nonexistent-ID deletion with 1,205 real records under a 999-bind limit; assert cascades and unaffected images, clusters and matches. |
| `test_cli_coverage` → `test_cli_commands` | Replace mocked dispatch checks with real scan, prompt/query, report, cluster and failure commands; preserve option semantics, config safety, process lock ownership and real module entry point. |
| `test_platform_coverage` → `test_platform_files`, `test_platform_single_instance`, `test_settings_file` | Replace branch probes with recursive discovery, real contended process locks, simulated Windows handle ownership, and independent config value fallbacks. |
| `test_db_coverage` | Keep bounded metadata/replacement contracts; replace fake checkpoint responses with real WAL durability and active-transaction rollback checks. |
| `test_scan_coverage` | Keep incremental scan, failed-file recovery, crop readiness and bounded submission; remove impossible results and dead-helper probes; check stale crop cleanup and exact-file model rules. |
| `test_scan_moves` | Keep real move/copy, ambiguity, scan-scope and permission cases; verify prompt/tag identity survives moves and same-size different bytes never count as a move. |
| `test_scan_performance` | Keep actual spawn/WAL, VM-work, bounded path lookup, rule reconciliation and SQLite key-range checks; assert every expected batch path. |
| `test_match_signals` | Consolidate hash/band/scoring contracts; use asymmetric EXIF pixels and hash comparisons, close reencodings versus unrelated images, independently specified thresholds and distances. |
| `test_search_coverage` | Replace duplicated signals and mocked ranking with real exact/reencoded/changed/unrelated images, score cutoffs, ranking, transitive clusters and review artifacts. |
| `test_query_db_performance` | Keep maintained-projection, SQL selectivity, Unicode and legacy model regressions; use real retrieved candidates to prove batching, tie order and late best-result displacement. |
| `test_prompt_query_groups` | Keep AND/OR/negative/model behavior and replace parser-shape inspection with returned image IDs and match counts. |
| `test_crop_retrieval` | Retain directional whole/crop/cross-region isolation plus generated-image retrieval. Synthetic hashes intentionally isolate directions that whole-image tiles otherwise conflate. |
| `test_cluster_limits` | Retain low-bind-limit membership, representative tie rules, nested transactions, cleanup and indexed VM-work bounds. |
| `test_matching_performance` | Retain independent exhaustive scoring equivalence, pruning, streaming and atomic export failure checks. |
| `test_unique_tags_model` | Retain 10,000 concrete SQLite snapshots checked against an independent visibility specification. |
| `test_scaling_features` | Keep crop readiness/completeness, cache ordering, model labels and rendered settings/help; remove duplicate locks/settings, vacuous empty-candidate crop checks, fabricated pager tests and JavaScript/CSS string probes. |
| `test_prompt_parsing` | Retain actual PNG metadata extraction and public syntax cases across positive/negative captions, weights, malformed input, model identity and Unicode. |
| `test_stealth_prompt_metadata` | Keep real alpha/RGB image carriers, compression and malformed/bounded payloads; remove unused bit-helper probe. |
| `test_metadata_coverage` | Replace mocked decompressors/private helpers with real PNG text encodings, corruption recovery, truncation, expansion limits and non-PNG/missing-file behavior. |
| `test_model_paths` | Keep file-backed loading, persistence, inheritance, exact-file rules and legacy imports; remove duplicated resolver and contrived invalid-config cases. |
| `test_model_type` | Keep source/version/hash classification and consolidate unreadable/undecodable/cross-drive marker cases. |
| `test_prompt_scan_integration` | Use decodable images and isolated rules; assert fresh scan → stored hashes → positive-tag search, plus batch durability observed through another connection. |
| `test_prompt_schema` | Keep schema/metadata compatibility; assert migrated content stays searchable and replacement removes obsolete tags and raw metadata. |
| `test_prompt_highlighting` | Retain HTML escaping, Unicode, overlapping-tag and visible-text roundtrip checks. |
| `test_reference_prompt_manifest` | Retain reviewed golden-manifest completeness and optional local-corpus agreement. |
| `test_web_coverage` | Replace fabricated results and mocked scans with HTTP/form roundtrips over an actual index: classification, rescan, busy/failure recovery, positive/negative/model search, unique prompts, paging, escaping, upload bytes/results, thumbnails and folder actions. |
| `test_web_autocomplete` + `web_autocomplete.cjs` | Execute rendered production JS with actual markup bindings. Assert coalescing, keyboard/click selection, caret-local token replacement, both inputs, debounce invalidation, aborted/out-of-order fetches, delayed JSON, disabled/empty/error states. |
| `test_web_lifecycle` | Keep server interruption cleanup and Windows shell allocation/COM ownership at the platform boundary. |
| `test_web_security` | Retain malformed and foreign-source requests, unusable process tokens, body size/deadline limits, upload cleanup and real stalled-socket recovery. |
| `test_web_performance` | Keep real concurrent requests, lazy/closed per-request SQLite connections, LRU behavior, atomic publication and worker-slot recovery. Add broken-cache-link startup regression. |
| `test_web_startup_stats` | Retain existential visibility and repeated-tag SQLite VM-work comparison, isolated from unrelated page counters. |
| `test_perf_toolkit` | Retain generated-manifest/database agreement, ownership/symlink cleanup safety, actual process-tree timeouts and successful-sample-only reporting. |
| `test_web_zzscratch` | Remove print-only exploration that caught and printed a startup exception. Its useful finding is now an asserting cache-startup regression. |

`image_fixtures`, `test_web_support` and `web_handler_fixture` are shared fixtures,
not additional tests. The HTTP workflow fixture parses real request bytes;
the lower-level handler fixture permits controlled malformed-stream/deadline
injection. Both isolate folder-label configuration from personal settings.

## Source and verification policy

Removed unused move helpers `_missing_hash_candidates`, `_claim_moved_image`
and the unused noncanonical root-resolution branch. The ambiguity mutation now
targets `_find_moved_image`, which the scanner actually calls.

The broken-cache-link regression failed against the existing startup code, then
passed with a shared cache-listing helper that skips unreadable entries while
still enforcing the cache limit.

Additional source cleanup removes uncalled private query wrappers and the unused
handler file sender, and simplifies checks guaranteed by upstream contracts:
argparse command choices, candidate-ID SQL bounds, successful SQLite upserts,
checkpoint result shape, internally constructed scan task IDs, and normalized,
deduplicated parser output. Optional progress fallbacks accept the iterable their
callers always pass. Bounded decompression retains early unconsumed-input
rejection and the final decoded-size ceiling, with real compressed boundary tests.
The scan-configuration handler retains its early running guard under the shared
mutation lock; a second running check immediately before starting was redundant.

The manual workflow requires passing tests and curated semantic mutations.
The 100% statement/branch coverage gate remains mandatory, with no exclusions.
Private dead branches and simulated impossibilities are removed or simplified;
remaining gaps require behavior assertions, rather than execution alone.
Five additional mutations cover
legacy model parentheses, lossless comma-containing matched tags, autocomplete
debounce invalidation, rendered input bindings and delayed JSON completion.
Mutation runs reject runtime errors as evidence; expected cardinalities are
asserted before inspecting a returned result.

## Limits

The Node harness is a controlled event/DOM boundary, not a real browser. It does
not validate layout, clipboard integration or native focus behavior. Windows APIs
are simulated when running on Linux. Two reference-image tests require a local,
uncommitted corpus. Performance checks use bounded SQL work or live-resource
counts rather than fragile machine-specific wall-clock thresholds; process/socket
timeouts remain safety bounds. The mutation set is curated, not exhaustive.

Final execution results are recorded in [verification.md](verification.md).
