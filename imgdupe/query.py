from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .bands import CROP_HASH_TYPES, Candidate, find_candidates
from .config import ScanConfig
from .db import fetch_crop_hashes, fetch_hash_row, hash_row_to_dict
from .hashing import compute_image_hashes
from .hashing import load_normalized, tryhard_query_hashes
from .match import PairScore, score_hashes
from .nai_prompt import normalize_prompt_tag, split_prompt_tags
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
    lookup_hashes.update(query_crop_hashes)
    if tryhard and hashes.get("phash256"):
        for crop_hash_type in CROP_HASH_TYPES:
            lookup_hashes[crop_hash_type] = hashes["phash256"]
    if tryhard:
        for index, crop_hash in enumerate(query_crop_hashes.values()):
            lookup_hashes[f"phash256:query_crop:{index}"] = crop_hash
    candidates = find_candidates(
        conn,
        lookup_hashes,
        exclude_image_id=int(indexed_row["id"]) if indexed_row is not None else None,
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
        candidate_crop_hashes = fetch_crop_hashes(conn, candidate.image_id)
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
    match_mode: str = "all",
    limit: int = 100,
    offset: int = 0,
) -> list[PromptSearchResult]:
    raw_tags = (
        split_prompt_tags(text_or_tags)
        if isinstance(text_or_tags, str)
        else [tag for item in text_or_tags for tag in split_prompt_tags(item)]
    )
    tag_norms = []
    seen = set()
    for tag in raw_tags:
        tag_norm = normalize_prompt_tag(tag)
        if not tag_norm or tag_norm in seen:
            continue
        seen.add(tag_norm)
        tag_norms.append(tag_norm)
    if not tag_norms:
        return []

    mode = match_mode.lower()
    if mode not in {"all", "any"}:
        raise ValueError("match_mode must be 'all' or 'any'")

    placeholders = ", ".join("?" for _ in tag_norms)
    having = "HAVING matched_count = ?" if mode == "all" else ""
    params: list[object] = [*tag_norms]
    if mode == "all":
        params.append(len(tag_norms))
    params.extend([max(1, limit), max(0, offset)])

    rows = conn.execute(
        f"""
        WITH matches AS (
            SELECT
                image_id,
                COUNT(DISTINCT tag_norm) AS matched_count,
                GROUP_CONCAT(DISTINCT tag) AS matched_tags
            FROM prompt_tags
            WHERE tag_norm IN ({placeholders})
            GROUP BY image_id
            {having}
        )
        SELECT
            i.*,
            ip.prompt,
            matches.matched_count,
            matches.matched_tags
        FROM matches
        JOIN images i ON i.id = matches.image_id
        LEFT JOIN image_prompts ip ON ip.image_id = i.id
        WHERE i.missing_at IS NULL
        ORDER BY matches.matched_count DESC, i.indexed_at DESC, i.path
        LIMIT ? OFFSET ?
        """,
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
