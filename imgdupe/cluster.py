from __future__ import annotations

import sqlite3
from dataclasses import dataclass

try:
    from tqdm import tqdm
except ImportError:  # Progress output is optional for library/test environments.
    def tqdm(iterable=None, **_kwargs):
        return iterable if iterable is not None else ()

from .bands import find_candidates
from .config import ScanConfig, Thresholds
from .db import fetch_hash_row, hash_row_to_dict, temporary_image_ids
from .match import PairScore, score_hashes
from .utils import utc_now_sql


ACCEPTED_CLUSTER_DECISIONS = {
    "exact_duplicate",
    "strong_duplicate",
    "probable_duplicate",
}


@dataclass
class ClusterStats:
    images: int = 0
    candidate_pairs: int = 0
    scored_pairs: int = 0
    stored_matches: int = 0
    clusters: int = 0
    clustered_images: int = 0


class UnionFind:
    def __init__(self, ids: list[int]) -> None:
        self.parent = {image_id: image_id for image_id in ids}
        self.rank = {image_id: 0 for image_id in ids}

    def find(self, image_id: int) -> int:
        parent = self.parent[image_id]
        if parent != image_id:
            self.parent[image_id] = self.find(parent)
        return self.parent[image_id]

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return
        rank_a = self.rank[root_a]
        rank_b = self.rank[root_b]
        if rank_a < rank_b:
            self.parent[root_a] = root_b
        elif rank_a > rank_b:
            self.parent[root_b] = root_a
        else:
            self.parent[root_b] = root_a
            self.rank[root_a] += 1


def build_clusters(
    conn: sqlite3.Connection,
    *,
    min_score: float = 70.0,
    config: ScanConfig | None = None,
    thresholds: Thresholds | None = None,
) -> ClusterStats:
    config = config or ScanConfig()
    thresholds = thresholds or Thresholds()
    rows = list(_iter_indexed_images(conn))
    stats = ClusterStats(images=len(rows))
    union_find = UnionFind([int(row["id"]) for row in rows])
    hashes_by_id = _load_hashes(conn, rows)
    sha_by_id = {int(row["id"]): row["sha256"] for row in rows}
    now = utc_now_sql()

    conn.execute("DELETE FROM matches")
    conn.execute("DELETE FROM clusters")

    for row in tqdm(rows, desc="Clustering images", unit="image"):
        image_id = int(row["id"])
        hashes = hashes_by_id.get(image_id)
        if hashes is None:
            continue
        candidates = find_candidates(
            conn,
            hashes,
            exclude_image_id=image_id,
            whole_band_size=config.whole_band_size,
            grid_band_size=config.grid_band_size,
        )
        stats.candidate_pairs += len(candidates)
        for candidate in candidates:
            other_id = candidate.image_id
            if other_id <= image_id:
                continue
            other_hashes = hashes_by_id.get(other_id)
            if other_hashes is None:
                continue
            stats.scored_pairs += 1
            pair_score = score_hashes(
                hashes,
                other_hashes,
                sha_equal=sha_by_id.get(image_id) == sha_by_id.get(other_id),
                thresholds=thresholds,
            )
            if pair_score.decision == "reject" or pair_score.score < min_score:
                continue
            _store_match(conn, image_id, other_id, pair_score, now)
            stats.stored_matches += 1
            if pair_score.decision in ACCEPTED_CLUSTER_DECISIONS:
                union_find.union(image_id, other_id)

    groups = _groups_from_union_find(union_find)
    cluster_id = 1
    for members in groups:
        if len(members) < 2:
            continue
        representative = _choose_representative(conn, members)
        for member_id in sorted(members):
            conn.execute(
                """
                INSERT INTO clusters (cluster_id, image_id, representative)
                VALUES (?, ?, ?)
                """,
                (cluster_id, member_id, 1 if member_id == representative else 0),
            )
            stats.clustered_images += 1
        stats.clusters += 1
        cluster_id += 1

    conn.commit()
    return stats


def _iter_indexed_images(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, sha256
        FROM images
        WHERE decode_error IS NULL
          AND missing_at IS NULL
          AND sha256 IS NOT NULL
          AND id IN (SELECT image_id FROM hashes)
        ORDER BY id
        """
    ).fetchall()


def _load_hashes(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[int, dict[str, bytes]]:
    hashes_by_id: dict[int, dict[str, bytes]] = {}
    for row in rows:
        image_id = int(row["id"])
        hash_row = fetch_hash_row(conn, image_id)
        if hash_row is not None:
            hashes_by_id[image_id] = hash_row_to_dict(hash_row)
    return hashes_by_id


def _store_match(
    conn: sqlite3.Connection,
    image_id_a: int,
    image_id_b: int,
    pair_score: PairScore,
    created_at: str,
) -> None:
    a, b = sorted((image_id_a, image_id_b))
    conn.execute(
        """
        INSERT INTO matches (
            image_id_a, image_id_b, dhash_dist, phash_dist, whash_dist,
            grid_match_count, grid_min_dist, score, decision, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(image_id_a, image_id_b) DO UPDATE SET
            dhash_dist = excluded.dhash_dist,
            phash_dist = excluded.phash_dist,
            whash_dist = excluded.whash_dist,
            grid_match_count = excluded.grid_match_count,
            grid_min_dist = excluded.grid_min_dist,
            score = excluded.score,
            decision = excluded.decision,
            created_at = excluded.created_at
        """,
        (
            a,
            b,
            pair_score.dhash_dist,
            pair_score.phash_dist,
            pair_score.whash_dist,
            pair_score.grid_match_count,
            pair_score.grid_min_dist,
            pair_score.score,
            pair_score.decision,
            created_at,
        ),
    )


def _groups_from_union_find(union_find: UnionFind) -> list[list[int]]:
    groups: dict[int, list[int]] = {}
    for image_id in union_find.parent:
        groups.setdefault(union_find.find(image_id), []).append(image_id)
    return list(groups.values())


def _choose_representative(conn: sqlite3.Connection, members: list[int]) -> int:
    with temporary_image_ids(conn, members) as membership:
        # IN drives primary-key probes; a JOIN can scan every image per cluster.
        rows = conn.execute(
            f"""
            SELECT i.id, i.width, i.height, i.size_bytes
            FROM images i
            WHERE i.id IN (SELECT image_id FROM {membership})
            """
        ).fetchall()
    best = max(
        rows,
        key=lambda row: (
            int(row["width"] or 0) * int(row["height"] or 0),
            int(row["size_bytes"] or 0),
            -int(row["id"]),
        ),
    )
    return int(best["id"])
