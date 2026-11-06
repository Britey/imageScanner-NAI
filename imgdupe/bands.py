from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .hashing import CENTER_CROP_FRACTIONS, EDGE_CROP_FRACTIONS


WHOLE_HASH_TYPES = ("dhash256", "phash256", "whash256")
GRID_HASH_TYPES = tuple(f"grid{i}" for i in range(9))
CROP_REGION_NAMES = tuple(
    [f"{side}_{int(round(fraction * 100))}" for fraction in EDGE_CROP_FRACTIONS for side in ("top", "bottom", "left", "right")]
    + [f"center_{int(round(fraction * 100))}" for fraction in CENTER_CROP_FRACTIONS]
    + [
        "top_left_quarter",
        "top_right_quarter",
        "bottom_left_quarter",
        "bottom_right_quarter",
    ]
)
CROP_HASH_TYPES = tuple(f"crop:{name}" for name in CROP_REGION_NAMES)
# Three parameters per ordinary probe fit SQLite's minimum 999-variable limit.
BAND_LOOKUP_BATCH_SIZE = 200


@dataclass(frozen=True)
class Candidate:
    image_id: int
    total_hits: int
    hits_by_type: dict[str, int]


def split_bands(hash_bytes: bytes, band_size_bytes: int = 2):
    for index in range(0, len(hash_bytes), band_size_bytes):
        band = hash_bytes[index : index + band_size_bytes]
        if len(band) == band_size_bytes:
            yield index // band_size_bytes, band


def insert_bands(
    conn: sqlite3.Connection,
    image_id: int,
    hashes: dict[str, bytes],
    *,
    whole_band_size: int = 2,
    grid_band_size: int = 2,
) -> None:
    rows = []
    for hash_type in WHOLE_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        for band_index, band_value in split_bands(hash_bytes, whole_band_size):
            rows.append((hash_type, band_index, band_value, image_id))

    for hash_type in GRID_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        for band_index, band_value in split_bands(hash_bytes, grid_band_size):
            rows.append((hash_type, band_index, band_value, image_id))

    for hash_type in CROP_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        for band_index, band_value in split_bands(hash_bytes, whole_band_size):
            rows.append((hash_type, band_index, band_value, image_id))

    conn.executemany(
        """
        INSERT OR IGNORE INTO hash_bands
            (hash_type, band_index, band_value, image_id)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def find_candidates(
    conn: sqlite3.Connection,
    hashes: dict[str, bytes],
    *,
    exclude_image_id: int | None = None,
    min_image_id: int | None = None,
    sort: bool = True,
    crop_query_hashes: Iterable[bytes] = (),
    whole_band_size: int = 2,
    grid_band_size: int = 2,
) -> list[Candidate]:
    """Retrieve candidates using distinct indexed bands, including cross-region crops.

    ``crop_query_hashes`` compares each value against every indexed crop region.
    It accepts query crops, query tiles and the query's whole-image pHash. Each
    stored band contributes at most one hit, even when several query regions or
    aliases contain the same band. Batching avoids one SQL call per region/band.
    ``min_image_id`` restricts the indexed lookup itself to later images, so
    clustering visits each pair once. ``sort=False`` avoids unused rank sorting.
    """
    probes: set[tuple[str, int, bytes]] = set()
    for hash_key, hash_bytes in hashes.items():
        if not hash_bytes:
            continue
        hash_type = _lookup_hash_type(hash_key)
        if hash_type not in WHOLE_HASH_TYPES + GRID_HASH_TYPES + CROP_HASH_TYPES:
            continue
        band_size = grid_band_size if hash_type.startswith("grid") else whole_band_size
        for band_index, band_value in split_bands(hash_bytes, band_size):
            probes.add((hash_type, band_index, band_value))

    crop_probes = {
        (band_index, band_value)
        for hash_bytes in crop_query_hashes
        for band_index, band_value in split_bands(hash_bytes, whole_band_size)
    }
    # A wildcard crop probe already covers this named-region probe. Removing
    # overlap before SQL avoids retaining a potentially huge set of result rows.
    probes = {
        probe for probe in probes
        if probe[0] not in CROP_HASH_TYPES or (probe[1], probe[2]) not in crop_probes
    }
    hit_counts: Counter[int] = Counter()
    type_counts: dict[int, Counter[str]] = defaultdict(Counter)
    for rows in (
        _matching_band_rows(conn, probes, min_image_id=min_image_id),
        _matching_band_rows(conn, crop_probes, crop=True, min_image_id=min_image_id),
    ):
        for row in rows:
            image_id = int(row["image_id"])
            if exclude_image_id is not None and image_id == exclude_image_id:
                continue
            hash_type = row["hash_type"]
            hit_counts[image_id] += 1
            type_counts[image_id][hash_type] += 1

    candidates: list[Candidate] = []
    for image_id, total_hits in hit_counts.items():
        by_type = dict(type_counts[image_id])
        whole_hit = any(by_type.get(name, 0) >= 2 for name in WHOLE_HASH_TYPES)
        grid_hit = any(name.startswith("grid") for name in by_type)
        crop_hit = any(name.startswith("crop:") for name in by_type)
        if total_hits >= 2 or whole_hit or grid_hit or crop_hit:
            candidates.append(Candidate(image_id, total_hits, by_type))
    if sort:
        candidates.sort(key=lambda item: (-item.total_hits, item.image_id))
    return candidates


def _matching_band_rows(
    conn: sqlite3.Connection, probes: set[tuple], *, crop: bool = False,
    min_image_id: int | None = None,
):
    ordered = sorted(probes)
    columns = "band_index, band_value" if crop else "hash_type, band_index, band_value"
    placeholders = "(?, ?)" if crop else "(?, ?, ?)"
    type_condition = (
        "hb.hash_type IN (" + ",".join("?" for _ in CROP_HASH_TYPES) + ")"
        if crop else "hb.hash_type = wanted.hash_type"
    )
    for start in range(0, len(ordered), BAND_LOOKUP_BATCH_SIZE):
        batch = ordered[start:start + BAND_LOOKUP_BATCH_SIZE]
        values = ",".join(placeholders for _ in batch)
        parameters = [value for probe in batch for value in probe]
        if crop:
            parameters.extend(CROP_HASH_TYPES)
        id_condition = ""
        if min_image_id is not None:
            id_condition = "AND hb.image_id > ?"
            parameters.append(min_image_id)
        # Keep the small wanted relation outermost. Otherwise SQLite can scan
        # every stored crop band and build an automatic index on wanted.
        yield from conn.execute(
            f"""
            WITH wanted({columns}) AS (VALUES {values})
            SELECT hb.image_id, hb.hash_type
            FROM wanted
            CROSS JOIN hash_bands hb
            WHERE hb.band_index = wanted.band_index
             AND hb.band_value = wanted.band_value
             AND {type_condition}
             {id_condition}
            """,
            parameters,
        )


def _lookup_hash_type(hash_key: str) -> str:
    if hash_key.startswith("phash256:"):
        return "phash256"
    return hash_key
