from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
try:
    from tqdm import tqdm
except ImportError:  # Progress output is optional for library/test environments.
    def tqdm(iterable=None, **_kwargs):
        return iterable if iterable is not None else ()

from .utils import human_size


@dataclass(frozen=True)
class ReviewStats:
    clusters: int
    images: int
    thumbnails: int
    out_dir: Path


def generate_review(
    conn: sqlite3.Connection,
    out_dir: Path,
    *,
    thumbnail_size: int = 256,
) -> ReviewStats:
    clusters = _fetch_clusters(conn)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = out_dir / "thumbnails"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    payload = []
    thumbnail_count = 0
    for cluster in tqdm(clusters, desc="Writing review", unit="cluster"):
        cluster_id = int(cluster["cluster_id"])
        images = _fetch_cluster_images(conn, cluster_id)
        matches = _fetch_cluster_matches(conn, [int(row["id"]) for row in images])
        image_payload = []
        for row in images:
            thumb_name = f"{int(row['id'])}.jpg"
            thumb_path = thumbs_dir / thumb_name
            if _write_thumbnail(Path(row["path"]), thumb_path, thumbnail_size):
                thumbnail_count += 1
            image_payload.append(_image_json(row, f"thumbnails/{thumb_name}"))

        cluster_payload = {
            "cluster_id": cluster_id,
            "count": len(images),
            "representative": _representative_path(images),
            "images": image_payload,
            "matches": [_match_json(row) for row in matches],
        }
        payload.append(cluster_payload)
        _write_cluster_page(out_dir / f"cluster_{cluster_id:06d}.html", cluster_payload)

    (out_dir / "clusters.json").write_text(
        json.dumps({"clusters": payload}, indent=2),
        encoding="utf-8",
    )
    _write_index_page(out_dir / "index.html", payload)
    return ReviewStats(
        clusters=len(payload),
        images=sum(cluster["count"] for cluster in payload),
        thumbnails=thumbnail_count,
        out_dir=out_dir,
    )


def _fetch_clusters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT cluster_id, COUNT(*) AS image_count
        FROM clusters
        GROUP BY cluster_id
        ORDER BY image_count DESC, cluster_id
        """
    ).fetchall()


def _fetch_cluster_images(conn: sqlite3.Connection, cluster_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            i.id, i.path, i.size_bytes, i.mtime_ns, i.width, i.height, i.format,
            c.representative
        FROM clusters c
        JOIN images i ON i.id = c.image_id
        WHERE c.cluster_id = ?
        ORDER BY c.representative DESC,
                 (COALESCE(i.width, 0) * COALESCE(i.height, 0)) DESC,
                 i.size_bytes DESC,
                 i.path
        """,
        (cluster_id,),
    ).fetchall()


def _fetch_cluster_matches(
    conn: sqlite3.Connection,
    image_ids: list[int],
) -> list[sqlite3.Row]:
    if len(image_ids) < 2:
        return []
    placeholders = ",".join("?" for _ in image_ids)
    return conn.execute(
        f"""
        SELECT *
        FROM matches
        WHERE image_id_a IN ({placeholders})
          AND image_id_b IN ({placeholders})
        ORDER BY score DESC, decision
        """,
        [*image_ids, *image_ids],
    ).fetchall()


def _write_thumbnail(source: Path, target: Path, thumbnail_size: int) -> bool:
    try:
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
            img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (thumbnail_size, thumbnail_size), (245, 247, 250))
            left = (thumbnail_size - img.width) // 2
            top = (thumbnail_size - img.height) // 2
            canvas.paste(img, (left, top))
            canvas.save(target, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def _write_index_page(path: Path, clusters: list[dict]) -> None:
    rows = []
    for cluster in clusters:
        first_image = cluster["images"][0] if cluster["images"] else {}
        thumb = html.escape(first_image.get("thumbnail", ""))
        representative = html.escape(cluster.get("representative") or "")
        rows.append(
            f"""
            <a class="cluster" href="cluster_{cluster['cluster_id']:06d}.html">
              <img src="{thumb}" alt="">
              <span class="cluster-title">Cluster {cluster['cluster_id']}</span>
              <span>{cluster['count']} images</span>
              <span class="path">{representative}</span>
            </a>
            """
        )
    path.write_text(_page("imgdupe review", "".join(rows), index=True), encoding="utf-8")


def _write_cluster_page(path: Path, cluster: dict) -> None:
    cards = []
    for image in cluster["images"]:
        badge = "KEEP" if image["representative"] else ""
        cards.append(
            f"""
            <article class="card">
              <img src="{html.escape(image['thumbnail'])}" alt="">
              <div class="meta">
                <strong>{badge}</strong>
                <span>{html.escape(image['dimensions'])}</span>
                <span>{html.escape(image['size'])}</span>
                <span>{html.escape(image['format'] or '')}</span>
              </div>
              <p class="path">{html.escape(image['path'])}</p>
            </article>
            """
        )
    match_rows = []
    for match in cluster["matches"]:
        match_rows.append(
            f"""
            <tr>
              <td>{match['image_id_a']}</td>
              <td>{match['image_id_b']}</td>
              <td>{match['score']:.2f}</td>
              <td>{html.escape(match['decision'])}</td>
              <td>{match['phash_dist']}</td>
              <td>{match['whash_dist']}</td>
              <td>{match['dhash_dist']}</td>
              <td>{match['grid_match_count']}</td>
            </tr>
            """
        )
    body = f"""
    <nav><a href="index.html">Review index</a></nav>
    <h1>Cluster {cluster['cluster_id']}</h1>
    <section class="grid">{''.join(cards)}</section>
    <h2>Pair Scores</h2>
    <table>
      <thead>
        <tr>
          <th>A</th><th>B</th><th>Score</th><th>Decision</th>
          <th>pHash</th><th>wHash</th><th>dHash</th><th>Grid</th>
        </tr>
      </thead>
      <tbody>{''.join(match_rows)}</tbody>
    </table>
    """
    path.write_text(_page(f"Cluster {cluster['cluster_id']}", body), encoding="utf-8")


def _page(title: str, body: str, *, index: bool = False) -> str:
    layout_class = "clusters" if index else "content"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #20262d; }}
    a {{ color: #245b9f; }}
    .clusters {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
    .cluster, .card {{ border: 1px solid #d9e1e8; border-radius: 6px; padding: 12px; background: #fff; }}
    .cluster {{ display: grid; gap: 8px; text-decoration: none; color: inherit; }}
    .cluster-title {{ font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
    img {{ width: 100%; max-width: 256px; aspect-ratio: 1; object-fit: contain; background: #f5f7fa; }}
    .meta {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; color: #52606d; }}
    .path {{ word-break: break-all; color: #3e4c59; font-size: 13px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border-bottom: 1px solid #d9e1e8; padding: 8px; text-align: left; }}
    th {{ background: #f3f6f8; }}
  </style>
</head>
<body>
  <main class="{layout_class}">
    {body}
  </main>
</body>
</html>
"""


def _image_json(row: sqlite3.Row, thumbnail: str) -> dict:
    width = row["width"]
    height = row["height"]
    return {
        "id": int(row["id"]),
        "path": row["path"],
        "thumbnail": thumbnail,
        "representative": bool(row["representative"]),
        "width": width,
        "height": height,
        "dimensions": f"{width}x{height}" if width and height else "",
        "size_bytes": row["size_bytes"],
        "size": human_size(row["size_bytes"]),
        "format": row["format"],
        "mtime_ns": row["mtime_ns"],
    }


def _match_json(row: sqlite3.Row) -> dict:
    return {
        "image_id_a": int(row["image_id_a"]),
        "image_id_b": int(row["image_id_b"]),
        "score": float(row["score"]),
        "decision": row["decision"],
        "dhash_dist": row["dhash_dist"],
        "phash_dist": row["phash_dist"],
        "whash_dist": row["whash_dist"],
        "grid_match_count": row["grid_match_count"],
        "grid_min_dist": row["grid_min_dist"],
    }


def _representative_path(images: list[sqlite3.Row]) -> str | None:
    for row in images:
        if row["representative"]:
            return row["path"]
    return images[0]["path"] if images else None
