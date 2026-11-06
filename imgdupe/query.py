from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .bands import Candidate, find_candidates
from .config import ScanConfig
from .db import fetch_crop_hashes, fetch_hash_row, hash_row_to_dict, is_crop_index_ready
from .hashing import compute_image_hashes
from .hashing import load_normalized, tryhard_query_hashes
from .match import PairScore, score_hashes
from .nai_prompt import CJK_FULL_STOPS, COMMA_SEPARATORS, normalize_prompt_tag
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
    config = config or ScanConfig()
    crop_index_ready = is_crop_index_ready(conn)
    hashes, _ = compute_image_hashes(
        image_path,
        min_width=config.min_width,
        min_height=config.min_height,
        include_crop_regions=tryhard,
    )
    indexed_row = conn.execute(
        "SELECT id FROM images WHERE path = ?",
        (str(image_path.resolve()),),
    ).fetchone()
    lookup_hashes = dict(hashes)
    query_crop_hashes = _query_crop_hashes(image_path, config) if tryhard else {}
    crop_lookup_hashes = []
    if tryhard:
        for index, crop_hash in enumerate(query_crop_hashes.values()):
            lookup_hashes[f"phash256:query_crop:{index}"] = crop_hash
        if crop_index_ready:
            # Keep all three crop directions: whole -> crop, crop/tile -> whole
            # (the aliases above), and crop/tile -> any candidate crop region.
            crop_lookup_hashes.extend(query_crop_hashes.values())
            if hashes.get("phash256"):
                crop_lookup_hashes.append(hashes["phash256"])
    candidates = find_candidates(
        conn,
        lookup_hashes,
        exclude_image_id=int(indexed_row["id"]) if indexed_row is not None else None,
        crop_query_hashes=crop_lookup_hashes,
        whole_band_size=config.whole_band_size,
        grid_band_size=config.grid_band_size,
    )

    results: list[tuple[sqlite3.Row, Candidate, PairScore]] = []
    for candidate in candidates:
        hash_row = fetch_hash_row(conn, candidate.image_id)
        if hash_row is None:
            continue
        image_row = conn.execute(
            "SELECT * FROM images WHERE id = ?",
            (candidate.image_id,),
        ).fetchone()
        if image_row is None:
            continue
        candidate_hashes = hash_row_to_dict(hash_row)
        candidate_crop_hashes = (
            fetch_crop_hashes(conn, candidate.image_id) if crop_index_ready else {}
        )
        sha_equal = hashes.get("sha256") == image_row["sha256"]
        if sha_equal and not include_exact:
            continue
        pair_score = score_hashes(
            hashes,
            candidate_hashes,
            sha_equal=sha_equal,
            crop_hashes_b=candidate_crop_hashes,
            query_crop_hashes=query_crop_hashes,
        )
        if pair_score.decision != "reject" and pair_score.score >= min_score:
            results.append((image_row, candidate, pair_score))

    results.sort(key=lambda item: item[2].score, reverse=True)
    return results[:limit]


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
    tag_groups = _prompt_tag_groups(text_or_tags)
    negative_tag_terms = _negative_prompt_tag_terms(negative_text_or_tags)
    if not tag_groups and not negative_tag_terms:
        return []

    normalized_model_type = (model_type or "all").strip().casefold()

    mode = match_mode.lower()
    if mode not in {"all", "any"}:
        raise ValueError("match_mode must be 'all' or 'any'")
    ranked_order = _prompt_sort_order(sort_by, table_alias="i")
    final_order = _prompt_sort_order(sort_by, table_alias="")

    params: list[object] = []
    if tag_groups:
        wanted_rows = [
            (group_index, tag_norm, metadata_only)
            for group_index, group in enumerate(tag_groups)
            for tag_norm, metadata_only in group
        ]
        wanted_values = ", ".join("(?, ?, ?)" for _ in wanted_rows)
        having = "HAVING matched_count = ?" if mode == "all" else ""
        for group_index, tag_norm, metadata_only in wanted_rows:
            params.extend((group_index, tag_norm, int(metadata_only)))
        if mode == "all":
            params.append(len(tag_groups))
        base_ctes = f"""
            WITH wanted(group_id, tag_norm, metadata_only) AS (
                VALUES {wanted_values}
            ),
            matches AS (
                SELECT
                    prompt_tags.image_id,
                    COUNT(DISTINCT wanted.group_id) AS matched_count,
                    GROUP_CONCAT(DISTINCT prompt_tags.tag) AS matched_tags
                FROM prompt_tags
                JOIN image_prompts effective_prompt
                  ON effective_prompt.image_id = prompt_tags.image_id
                JOIN wanted ON wanted.tag_norm = prompt_tags.tag_norm
                    AND (
                        (wanted.metadata_only = 0 AND prompt_tags.tag_kind = 'prompt')
                        OR (
                            wanted.metadata_only = 1
                            AND prompt_tags.tag_kind <> 'prompt'
                            AND NOT (
                                prompt_tags.tag_kind = 'model_type'
                                AND effective_prompt.configured_model IS NOT NULL
                            )
                        )
                    )
                GROUP BY prompt_tags.image_id
                {having}
            )
        """
    else:
        base_ctes = """
            WITH matches AS (
                SELECT
                    image_id,
                    0 AS matched_count,
                    '' AS matched_tags
                FROM image_prompts
                WHERE prompt IS NOT NULL
            )
        """

    exclude_clause = ""
    if negative_tag_terms:
        negative_conditions = " OR ".join(
            "(excluded_tags.tag_norm = ? AND "
            "((? = 0 AND excluded_tags.tag_kind = 'prompt') OR "
            "(? = 1 AND excluded_tags.tag_kind <> 'prompt')))"
            for _ in negative_tag_terms
        )
        exclude_clause = f"""
              AND NOT EXISTS (
                  SELECT 1
                  FROM prompt_tags excluded_tags
                  WHERE excluded_tags.image_id = i.id
                    AND NOT (
                        excluded_tags.tag_kind = 'model_type'
                        AND ip.configured_model IS NOT NULL
                    )
                    AND ({negative_conditions})
              )
        """
        for tag_norm, metadata_only in negative_tag_terms:
            params.extend((tag_norm, int(metadata_only), int(metadata_only)))
    model_clause = ""
    if normalized_model_type != "all":
        model_clause = """
          AND EXISTS (
              SELECT 1
              FROM prompt_tags model_filter
              WHERE model_filter.image_id = i.id
                AND model_filter.tag_norm = ?
                AND (
                    model_filter.tag_kind = 'model_config'
                    OR (
                        model_filter.tag_kind = 'model_type'
                        AND ip.configured_model IS NULL
                    )
                )
          )
        """
        params.append(normalize_prompt_tag(f"@model:{normalized_model_type}"))
    params.extend([max(1, limit), max(0, offset)])
    if unique_prompts:
        sql = (
            base_ctes
            + f"""
        , ranked AS (
            SELECT
                i.*,
                ip.prompt,
                ip.model_type,
                ip.model_hash,
                ip.model_version,
                ip.configured_model,
                matches.matched_count,
                matches.matched_tags,
                ROW_NUMBER() OVER (
                    PARTITION BY CASE
                        WHEN ip.prompt IS NULL THEN 'image:' || i.id
                        ELSE 'prompt:' || ip.prompt
                    END
                    ORDER BY {ranked_order}
                ) AS prompt_rank
            FROM matches
            JOIN images i ON i.id = matches.image_id
            JOIN image_prompts ip ON ip.image_id = i.id
            WHERE i.missing_at IS NULL
              {exclude_clause}
              {model_clause}
        )
        SELECT *
        FROM ranked
        WHERE prompt_rank = 1
        ORDER BY {final_order}
        LIMIT ? OFFSET ?
        """
        )
    else:
        sql = (
            base_ctes
            + f"""
        SELECT
            i.*,
            ip.prompt,
            ip.model_type,
            ip.model_hash,
            ip.model_version,
            ip.configured_model,
            matches.matched_count,
            matches.matched_tags
        FROM matches
        JOIN images i ON i.id = matches.image_id
        LEFT JOIN image_prompts ip ON ip.image_id = i.id
        WHERE i.missing_at IS NULL
          {exclude_clause}
          {model_clause}
        ORDER BY {ranked_order}
        LIMIT ? OFFSET ?
        """
        )

    rows = conn.execute(
        sql,
        params,
    ).fetchall()

    return [
        PromptSearchResult(
            image_row=row,
            prompt=row["prompt"],
            matched_count=int(row["matched_count"]),
            matched_tags=tuple((row["matched_tags"] or "").split(",")),
        )
        for row in rows
    ]


def _prompt_tag_norms(text_or_tags: str | list[str] | None) -> list[str]:
    return [tag_norm for group in _prompt_tag_groups(text_or_tags) for tag_norm, _ in group]


def _prompt_tag_groups(
    text_or_tags: str | list[str] | None,
) -> list[tuple[tuple[str, bool], ...]]:
    if text_or_tags is None:
        return []
    inputs = [text_or_tags] if isinstance(text_or_tags, str) else text_or_tags
    groups: list[tuple[tuple[str, bool], ...]] = []
    seen_groups: set[frozenset[tuple[str, bool]]] = set()
    separators = str.maketrans({character: "," for character in COMMA_SEPARATORS + CJK_FULL_STOPS})
    for item in inputs:
        for raw_group in item.translate(separators).split(","):
            group_text = _strip_whole_group_parentheses(raw_group.strip())
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


def _negative_prompt_tag_terms(
    text_or_tags: str | list[str] | None,
) -> list[tuple[str, bool]]:
    terms: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for group in _prompt_tag_groups(text_or_tags):
        for term in group:
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return terms


def _strip_whole_group_parentheses(text: str) -> str:
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        depth = 0
        encloses_whole_group = True
        for index, character in enumerate(text):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0 or (depth == 0 and index != len(text) - 1):
                    encloses_whole_group = False
                    break
        if not encloses_whole_group or depth != 0:
            break
        text = text[1:-1].strip()
    return text


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


def _query_crop_hashes(image_path: Path, config: ScanConfig) -> dict[str, bytes]:
    img = load_normalized(
        image_path,
        min_width=config.min_width,
        min_height=config.min_height,
    )
    return tryhard_query_hashes(img)


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
