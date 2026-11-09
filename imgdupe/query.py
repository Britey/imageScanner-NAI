from __future__ import annotations

import html
import heapq
import json
from contextlib import contextmanager
from uuid import uuid4
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .bands import Candidate, find_candidates
from .config import ScanConfig
from .db import hash_row_to_dict, is_crop_index_ready, temporary_image_ids
from .hashing import compute_image_hashes
from .match import PairScore, score_hashes
from .nai_prompt import CJK_FULL_STOPS, COMMA_SEPARATORS, normalize_prompt_tag, sanitize_model_name
from .utils import human_size


@dataclass(frozen=True)
class PromptSearchResult:
    image_row: sqlite3.Row
    prompt: str | None
    matched_count: int
    matched_tags: tuple[str, ...]


def query_image(
    conn: sqlite3.Connection,
    image_path: Path,
    *,
    config: ScanConfig | None = None,
    limit: int = 50,
    min_score: float = 0.0,
    include_exact: bool = True,
    tryhard: bool = False,
) -> list[tuple[sqlite3.Row, Candidate, PairScore]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    config = config or ScanConfig()
    crop_index_ready = is_crop_index_ready(conn)
    hashes, _ = compute_image_hashes(
        image_path,
        min_width=config.min_width,
        min_height=config.min_height,
        include_query_tiles=tryhard,
    )
    indexed_row = conn.execute(
        "SELECT id FROM images WHERE path = ?",
        (str(image_path.resolve()),),
    ).fetchone()
    lookup_hashes = dict(hashes)
    query_crop_hashes = {key: value for key, value in hashes.items() if key.startswith(("crop:", "tile:"))}
    crop_lookup_hashes = []
    if tryhard:
        for index, crop_hash in enumerate(query_crop_hashes.values()):
            lookup_hashes[f"phash256:query_crop:{index}"] = crop_hash
        if crop_index_ready:
            # Keep all three crop directions: whole -> crop, crop/tile -> whole
            # (the aliases above), and crop/tile -> any candidate crop region.
            crop_lookup_hashes.extend(query_crop_hashes.values())
            crop_lookup_hashes.append(hashes["phash256"])
    candidates = find_candidates(
        conn,
        lookup_hashes,
        exclude_image_id=int(indexed_row["id"]) if indexed_row is not None else None,
        crop_query_hashes=crop_lookup_hashes,
        whole_band_size=config.whole_band_size,
        grid_band_size=config.grid_band_size,
    )

    # Bound SQLite round trips and resident payloads by batches, not candidates.
    # A bounded heap preserves stable score ordering without sorting every hit.
    results: list[tuple[float, int, sqlite3.Row, Candidate, PairScore]] = []
    for start in range(0, len(candidates), 400):
        batch = candidates[start:start + 400]
        with temporary_image_ids(conn, (candidate.image_id for candidate in batch)) as ids:
            image_rows = {row["id"]: row for row in conn.execute(
                f"SELECT i.* FROM {ids} ids CROSS JOIN images i ON i.id=ids.image_id"
            )}
            hash_rows = {row["image_id"]: hash_row_to_dict(row) for row in conn.execute(
                f"SELECT h.* FROM {ids} ids CROSS JOIN hashes h ON h.image_id=ids.image_id"
            )}
            crop_rows: dict[int, dict[str, bytes]] = {}
            if crop_index_ready:
                for row in conn.execute(f"SELECT c.* FROM {ids} ids CROSS JOIN crop_hashes c ON c.image_id=ids.image_id"):
                    crop_rows.setdefault(row["image_id"], {})[row["region"]] = row["phash256"]
        for index, candidate in enumerate(batch, start):
            image_row = image_rows.get(candidate.image_id)
            candidate_hashes = hash_rows.get(candidate.image_id)
            if image_row is None or candidate_hashes is None:
                continue
            sha_equal = hashes.get("sha256") == image_row["sha256"]
            if sha_equal and not include_exact:
                continue
            pair_score = score_hashes(
                hashes, candidate_hashes, sha_equal=sha_equal,
                crop_hashes_b=crop_rows.get(candidate.image_id, {}),
                query_crop_hashes=query_crop_hashes,
            )
            if pair_score.decision != "reject" and pair_score.score >= min_score:
                item = (pair_score.score, -index, image_row, candidate, pair_score)
                if len(results) < limit:
                    heapq.heappush(results, item)
                else:
                    heapq.heappushpop(results, item)
    return [(row, candidate, score) for _, _, row, candidate, score in sorted(results, reverse=True)]


def query_prompt_tags(
    conn: sqlite3.Connection,
    text_or_tags: str | list[str],
    *,
    negative_text_or_tags: str | list[str] | None = None,
    match_mode: str = "all",
    limit: int = 100,
    offset: int = 0,
    unique_prompts: bool = False,
    sort_by: str = "created_desc",
    model_type: str | None = None,
) -> list[PromptSearchResult]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    tag_groups = _database_tag_groups(conn, text_or_tags)
    negative_groups = _database_tag_groups(conn, negative_text_or_tags)
    negative_tag_terms = list(dict.fromkeys(term for group in negative_groups for term in group))
    if not tag_groups and not negative_tag_terms:
        return []
    mode = match_mode.lower()
    if mode not in {"all", "any"}:
        raise ValueError("match_mode must be 'all' or 'any'")
    normalized_model_type = (model_type or "all").strip().casefold()
    wanted_rows = [(group_id, norm, int(metadata)) for group_id, group in enumerate(tag_groups) for norm, metadata in group]
    negative_rows = [(0, norm, int(metadata)) for norm, metadata in negative_tag_terms]
    with _temporary_terms(conn, wanted_rows) as wanted, _temporary_terms(conn, negative_rows) as unwanted:
        visibility = """((w.metadata_only=0 AND pt.tag_kind='prompt') OR
            (w.metadata_only=1 AND pt.tag_kind<>'prompt' AND NOT(pt.tag_kind='model_type' AND s.configured_model IS NOT NULL)))"""
        if len(wanted_rows) == 1:
            metadata_only = wanted_rows[0][2]
            index = "idx_prompt_tags_norm_image" if metadata_only else "idx_prompt_tags_kind_norm_image"
            term_visibility = visibility if metadata_only else "pt.tag_kind='prompt'"
            base = f"""WITH matches AS (
                SELECT pt.image_id,1 AS matched_count FROM {wanted} w
                CROSS JOIN prompt_tags pt INDEXED BY {index} ON pt.tag_norm=w.tag_norm
                JOIN prompt_search s ON s.image_id=pt.image_id
                WHERE s.has_prompt_row AND NOT s.missing AND {term_visibility}
            )"""
        elif tag_groups:
            seed_clause = ""
            if mode == "all":
                # Frequency estimates use the compact vocabulary, not all tag
                # occurrences. An absent group stops immediately at its index.
                seed = conn.execute(f"""SELECT w.group_id FROM {wanted} w
                    LEFT JOIN tag_stats t ON t.tag_norm=w.tag_norm
                      AND ((w.metadata_only=0 AND t.tag_kind='prompt') OR (w.metadata_only=1 AND t.tag_kind<>'prompt'))
                    GROUP BY w.group_id ORDER BY COALESCE(SUM(t.count),0),w.group_id LIMIT 1""").fetchone()[0]
                seed_clause = f"AND w.group_id={int(seed)}"
            having = f"HAVING COUNT(DISTINCT w.group_id)={len(tag_groups)}" if mode == "all" else ""
            base = f"""WITH candidates AS MATERIALIZED (
                SELECT DISTINCT pt.image_id FROM {wanted} w
                CROSS JOIN prompt_tags pt INDEXED BY idx_prompt_tags_norm_image ON pt.tag_norm=w.tag_norm
                JOIN prompt_search s ON s.image_id=pt.image_id
                WHERE s.has_prompt_row AND NOT s.missing AND {visibility} {seed_clause}
            ), matches AS (
                SELECT pt.image_id, COUNT(DISTINCT w.group_id) AS matched_count
                FROM candidates c CROSS JOIN {wanted} w
                JOIN prompt_tags pt ON pt.image_id=c.image_id AND pt.tag_norm=w.tag_norm
                JOIN prompt_search s ON s.image_id=c.image_id
                WHERE {visibility} GROUP BY pt.image_id {having}
            )"""
        else:
            base = "WITH matches AS (SELECT image_id,0 AS matched_count FROM prompt_search WHERE prompt_id IS NOT NULL)"
        params: list[object] = []
        exclude = f"""AND NOT EXISTS(SELECT 1 FROM {unwanted} w
            JOIN prompt_tags pt ON pt.image_id=i.id AND pt.tag_norm=w.tag_norm WHERE {visibility})"""
        model_clause = ""
        if normalized_model_type != "all":
            model_clause = """AND EXISTS(SELECT 1 FROM prompt_tags m WHERE m.image_id=i.id AND m.tag_norm=?
                AND (m.tag_kind='model_config' OR (m.tag_kind='model_type' AND s.configured_model IS NULL)))"""
            params.append(normalize_prompt_tag(f"@model:{normalized_model_type}"))
        order = _prompt_sort_order(sort_by, table_alias="i")
        final_order = _prompt_sort_order(sort_by, table_alias="")
        # Only narrow sort keys enter the window and pagination. Metadata and
        # prompt text are fetched once, for the selected page.
        rank = f", ROW_NUMBER() OVER(PARTITION BY s.prompt_id,CASE WHEN s.prompt_id IS NULL THEN i.id END ORDER BY {order}) AS prompt_rank" if unique_prompts else ""
        rank_filter = "WHERE prompt_rank=1" if unique_prompts else ""
        sql = base + f""", ranked AS (
            SELECT i.id,i.path,i.mtime_ns,i.created_ns,i.modified_ns,matches.matched_count {rank}
            FROM matches JOIN images i ON i.id=matches.image_id
            JOIN prompt_search s ON s.image_id=i.id
            WHERE NOT s.missing {exclude} {model_clause}
        ), page AS MATERIALIZED (
            SELECT * FROM ranked {rank_filter} ORDER BY {final_order} LIMIT ? OFFSET ?
        )
        SELECT i.*,ip.prompt,ip.model_type,ip.model_hash,ip.model_version,ip.configured_model,
            page.matched_count,
            (SELECT json_group_array(DISTINCT pt.tag) FROM {wanted} w
             JOIN prompt_tags pt ON pt.image_id=i.id AND pt.tag_norm=w.tag_norm
             JOIN prompt_search s ON s.image_id=i.id WHERE {visibility}) AS matched_tags
        FROM page JOIN images i ON i.id=page.id LEFT JOIN image_prompts ip ON ip.image_id=i.id
        ORDER BY {_prompt_sort_order(sort_by, table_alias='i')}
        """
        rows = conn.execute(sql, [*params, limit, max(0, offset)]).fetchall()
    return [PromptSearchResult(row, row["prompt"], int(row["matched_count"]),
                               tuple(json.loads(row["matched_tags"] or "[]"))) for row in rows]


@contextmanager
def _temporary_terms(conn: sqlite3.Connection, terms: list[tuple[int, str, int]]):
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    table = f"temp.terms_{uuid4().hex}"
    conn.execute(f"CREATE TEMP TABLE {table}(group_id INTEGER,tag_norm TEXT,metadata_only INTEGER,PRIMARY KEY(group_id,tag_norm,metadata_only)) WITHOUT ROWID")
    try:
        conn.executemany(f"INSERT INTO {table} VALUES(?,?,?)", terms)
        yield table
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        if owns_transaction:
            conn.commit()


def _database_tag_groups(conn: sqlite3.Connection, text_or_tags: str | list[str] | None) -> list[tuple[tuple[str, bool], ...]]:
    """Recognize stored legacy model names before interpreting commas as groups.

    Longest existing metadata names win; ordinary ``@model:sd, cat`` still
    separates normally. New normalized names are also accepted with commas.
    """
    if text_or_tags is None:
        return []
    inputs = [text_or_tags] if isinstance(text_or_tags, str) else text_or_tags
    groups: list[tuple[tuple[str, bool], ...]] = []
    seen_groups: set[frozenset[tuple[str, bool]]] = set()
    separator_chars = re.escape(COMMA_SEPARATORS + CJK_FULL_STOPS + "|｜()")
    boundaries = re.compile(f"[{separator_chars}]")
    for item in inputs:
        replacements: dict[str, tuple[tuple[str, bool], ...]] = {}
        literal_models: dict[str, str] = {}
        fragments: list[str] = []
        consumed = 0
        for match in re.finditer(r"(?i)@model:", item):
            start = match.start()
            if start < consumed or (start and item[start - 1] not in " (|｜" + COMMA_SEPARATORS + CJK_FULL_STOPS):
                continue
            first_boundary = boundaries.search(item, start)
            prefix_end = first_boundary.start() if first_boundary else len(item)
            prefix = normalize_prompt_tag(item[start:prefix_end])
            known = [row[0] for row in conn.execute(
                "SELECT DISTINCT tag_norm FROM tag_stats WHERE tag_norm>=? AND tag_norm<? AND tag_kind<>'prompt'",
                (prefix, prefix + "\U0010ffff"),
            )]
            normalized = {name: normalize_prompt_tag(sanitize_model_name(name)) for name in known}
            continuation_prefixes = tuple(
                _model_continuation_key(name) for name in normalized.values() if len(name) > len(prefix)
            )
            longest = max(map(len, normalized.values()), default=0)
            # Parentheses opened inside the name belong to it, so their commas
            # do not split the term. A candidate span may only end where the
            # remainder starts with a term terminator (separator, OR operator,
            # an enclosing group's ")" or the end), so nothing sticks to the
            # placeholder and an enclosing ")" stays in the remainder.
            # Protect even an unknown model's full lexical term. Otherwise a
            # comma inside its parentheses becomes a separate prompt query.
            best_end = _model_token_end(item, start, boundaries)
            atom_end = best_end
            best_names = [normalize_prompt_tag(item[start:best_end])]
            depth = 0
            boundary_iter = boundaries.finditer(item, start)
            while True:
                boundary = next(boundary_iter, None)
                end = boundary.start() if boundary else len(item)
                character = boundary.group() if boundary else ""
                stopping = not boundary or character in "|｜" or (character == ")" and not depth)
                # EOF and OR finish the scan even inside an unfinished name.
                # Resolve that final span before falling back to a short model
                # prefix and exposing its comma-separated continuation as tags.
                if stopping or (not depth and character != "("):
                    sanitized = normalize_prompt_tag(sanitize_model_name(item[start:end]))
                    raw = normalize_prompt_tag(item[start:end])
                    names = [name for name in known if name == raw or normalized[name] == sanitized]
                    if names:
                        best_end, best_names = end, names
                    elif (atom_end == prefix_end and best_end == atom_end and end > atom_end
                          and _model_continuation_key(sanitized).startswith(continuation_prefixes)):
                        # An overlong continuation of a legacy comma name is
                        # one unmatched metadata term, including extra content
                        # inserted before a stored qualifier's closing ")" or
                        # extra nested opening parentheses around its text.
                        # Do not search its short prefix plus unrelated prompt
                        # terms. A name already finished
                        # beyond the first comma retains subsequent AND groups.
                        best_end, best_names = end, [raw]
                    if len(sanitized) > longest:
                        break
                if stopping:
                    break
                depth += 1 if character == "(" else -1 if character == ")" else 0
            placeholder = f"@model:legacy-placeholder-{len(replacements)}-{uuid4().hex}"
            replacements[placeholder] = tuple((name, True) for name in best_names)
            literal_models[placeholder] = normalize_prompt_tag(item[start:best_end])
            fragments.extend((item[consumed:start], placeholder))
            consumed = best_end
        fragments.append(item[consumed:])
        protected = re.compile("|".join(map(re.escape, replacements))) if replacements else None
        model_depths = {marker: name.count("(") - name.count(")") for marker, name in literal_models.items()}
        for group in _prompt_tag_groups("".join(fragments), protected=protected, model_depths=model_depths):
            expanded_terms: list[tuple[str, bool]] = []
            for normalized_term, metadata_only in group:
                if normalized_term in replacements:
                    expanded_terms.extend(replacements[normalized_term])
                else:
                    # Only complete tokens expand as metadata alternatives.
                    # A marker embedded in a literal qualifier is restored to
                    # the user's text, so internal markers never reach SQL.
                    if protected is not None:
                        normalized_term = protected.sub(lambda match: literal_models[match.group()], normalized_term)
                    expanded_terms.append((normalized_term, metadata_only))
            expanded = tuple(dict.fromkeys(expanded_terms))
            key = frozenset(expanded)
            if key not in seen_groups:
                groups.append(expanded)
                seen_groups.add(key)
    return groups


def _model_continuation_key(name: str) -> str:
    """Compare unfinished qualifiers without counting redundant nested openers."""
    return re.sub(r"(?:\(\s*)+", "(", name.rstrip(") "))


def _model_token_end(text: str, start: int, boundaries: re.Pattern[str]) -> int:
    """Keep qualifier commas inside the name; OR ends it at any depth."""
    depth = 0
    for boundary in boundaries.finditer(text, start):
        character = boundary.group()
        if character in "|｜":
            return boundary.start()
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif not depth:
            return boundary.start()
    return len(text)


def _prompt_tag_groups(
    text_or_tags: str | list[str], *, protected: re.Pattern[str] | None = None,
    model_depths: dict[str, int] | None = None,
) -> list[tuple[tuple[str, bool], ...]]:
    inputs = [text_or_tags] if isinstance(text_or_tags, str) else text_or_tags
    groups: list[tuple[tuple[str, bool], ...]] = []
    seen_groups: set[frozenset[tuple[str, bool]]] = set()
    separators = str.maketrans({character: "," for character in COMMA_SEPARATORS + CJK_FULL_STOPS})
    for item in inputs:
        # Remove balanced structural wrappers before comma splitting separates
        # their opening and closing characters (including embedded AND groups).
        item = _strip_query_group_parentheses(item.translate(separators), protected, model_depths)
        for raw_group in item.split(","):
            group_text = raw_group.strip()
            terms: list[tuple[str, bool]] = []
            seen_terms: set[tuple[str, bool]] = set()
            for raw_term in re.split(r"[|｜]", group_text):
                tag_norm = normalize_prompt_tag(raw_term)
                metadata_only = raw_term.strip().casefold().startswith("@model:")
                term = (tag_norm, metadata_only)
                if not tag_norm or term in seen_terms:
                    continue
                seen_terms.add(term)
                terms.append(term)
            group_key = frozenset(terms)
            if not terms or group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            groups.append(tuple(terms))
    return groups


def _strip_query_group_parentheses(
    text: str, protected: re.Pattern[str] | None, model_depths: dict[str, int] | None = None,
) -> str:
    """Unwrap structural groups and finish them before following plain terms."""
    openings: list[tuple[int, bool]] = []
    wrappers: set[int] = set()
    separators: set[int] = set()
    model_ends = {
        match.end() - 1: (model_depths or {}).get(match.group(), 0)
        for match in protected.finditer(text)
    } if protected is not None else {}
    previous = ""
    model_tail = False
    for index, character in enumerate(text):
        if character == "(":
            structural = previous in {"", ",", "|", "｜", "("} and (not openings or openings[-1][1])
            openings.append((index, structural))
        elif character == ")":
            if openings:
                start, structural = openings.pop()
                if structural:
                    wrappers.update((start, index))
                else:
                    model_tail = False
            elif model_tail:
                wrappers.add(index)
            if index in wrappers:
                following = index + 1
                while following < len(text) and text[following].isspace():
                    following += 1
                if following < len(text) and text[following] not in ",|｜)":
                    separators.add(index)
        if index in model_ends:
            # A model opener hidden by its marker still owns its closing ")"
            # after an inner pipe. Keep those closers literal instead of letting
            # them close structural groups surrounding the model alternatives.
            openings.extend((-1, False) for _ in range(model_ends[index]))
            model_tail = True
        elif character != ")" and not character.isspace():
            model_tail = False
        if not character.isspace():
            previous = "," if index in separators else character
    return "".join(
        "," if index in separators else "" if index in wrappers else character
        for index, character in enumerate(text)
    )


def _prompt_sort_order(sort_by: str, *, table_alias: str) -> str:
    prefix = f"{table_alias}." if table_alias else ""
    created = f"COALESCE({prefix}created_ns, {prefix}mtime_ns)"
    modified = f"COALESCE({prefix}modified_ns, {prefix}mtime_ns)"
    path = f"LOWER({prefix}path)"
    sort_orders = {
        "created_desc": f"matched_count DESC, {created} DESC, {path} ASC, {prefix}id ASC",
        "created_asc": f"matched_count DESC, {created} ASC, {path} ASC, {prefix}id ASC",
        "modified_desc": f"matched_count DESC, {modified} DESC, {path} ASC, {prefix}id ASC",
        "modified_asc": f"matched_count DESC, {modified} ASC, {path} ASC, {prefix}id ASC",
        "name_asc": f"matched_count DESC, {path} ASC, {prefix}id ASC",
        "name_desc": f"matched_count DESC, {path} DESC, {prefix}id ASC",
    }
    return sort_orders.get(sort_by, sort_orders["created_desc"])


def write_query_html(
    out_path: Path,
    query_path: Path,
    results: list[tuple[sqlite3.Row, Candidate, PairScore]],
) -> None:
    rows = []
    for image_row, candidate, score in results:
        path = str(image_row["path"])
        rows.append(
            f"""
            <tr>
              <td><img src="{html.escape(Path(path).as_uri())}" loading="lazy"></td>
              <td class="path">{html.escape(path)}</td>
              <td>{score.score:.2f}</td>
              <td>{html.escape(score.decision)}</td>
              <td>{score.phash_dist}</td>
              <td>{score.whash_dist}</td>
              <td>{score.dhash_dist}</td>
              <td>{score.grid_match_count}</td>
              <td>{candidate.total_hits}</td>
              <td>{image_row["width"]}x{image_row["height"]}</td>
              <td>{human_size(image_row["size_bytes"])}</td>
            </tr>
            """
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>imgdupe query</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2933; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d8dee9; padding: 8px; vertical-align: top; }}
    th {{ text-align: left; background: #f3f6f8; position: sticky; top: 0; }}
    img {{ width: 160px; height: 160px; object-fit: contain; background: #f7f7f7; }}
    .path {{ word-break: break-all; max-width: 520px; }}
  </style>
</head>
<body>
  <h1>Query Results</h1>
  <p>{html.escape(str(query_path))}</p>
  <table>
    <thead>
      <tr>
        <th>Preview</th><th>Path</th><th>Score</th><th>Decision</th>
        <th>pHash</th><th>wHash</th><th>dHash</th><th>Grid</th>
        <th>Bands</th><th>Dimensions</th><th>Size</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
