from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path


DELETE_BATCH_SIZE = 400
CROP_INDEX_STATE_KEY = "crop_index_state"
CROP_INDEX_VERSION_KEY = "crop_index_version"
CROP_INDEX_VERSION = "1"
PROMPT_SCHEMA_VERSION_KEY = "prompt_schema_version"
PROMPT_SCHEMA_VERSION = "2"
MAX_PROMPT_METADATA_BYTES = 4 * 1024 * 1024

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    created_ns INTEGER,
    modified_ns INTEGER,
    width INTEGER,
    height INTEGER,
    format TEXT,
    sha256 BLOB,
    indexed_at TEXT NOT NULL,
    missing_at TEXT,
    decode_error TEXT
);

CREATE TABLE IF NOT EXISTS hashes (
    image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    dhash256 BLOB,
    phash256 BLOB,
    whash256 BLOB,
    grid0 BLOB,
    grid1 BLOB,
    grid2 BLOB,
    grid3 BLOB,
    grid4 BLOB,
    grid5 BLOB,
    grid6 BLOB,
    grid7 BLOB,
    grid8 BLOB
);

CREATE TABLE IF NOT EXISTS hash_bands (
    hash_type TEXT NOT NULL,
    band_index INTEGER NOT NULL,
    band_value BLOB NOT NULL,
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    PRIMARY KEY (hash_type, band_index, band_value, image_id)
);

CREATE TABLE IF NOT EXISTS crop_hashes (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    phash256 BLOB NOT NULL,
    PRIMARY KEY (image_id, region)
);

CREATE TABLE IF NOT EXISTS matches (
    image_id_a INTEGER NOT NULL,
    image_id_b INTEGER NOT NULL,
    dhash_dist INTEGER,
    phash_dist INTEGER,
    whash_dist INTEGER,
    grid_match_count INTEGER,
    grid_min_dist INTEGER,
    score REAL NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (image_id_a, image_id_b)
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id INTEGER NOT NULL,
    image_id INTEGER NOT NULL,
    representative INTEGER DEFAULT 0,
    PRIMARY KEY (cluster_id, image_id)
);

CREATE TABLE IF NOT EXISTS image_prompts (
    image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    prompt TEXT,
    metadata_source TEXT,
    prompt_indexed_at TEXT NOT NULL,
    prompt_error TEXT,
    model_type TEXT,
    model_hash TEXT,
    model_version TEXT,
    prompt_format TEXT,
    raw_metadata BLOB,
    raw_metadata_codec TEXT,
    parser_version INTEGER,
    classification_root TEXT,
    model_marker_path TEXT,
    prompt_status TEXT,
    classification_status TEXT
);

CREATE TABLE IF NOT EXISTS prompt_tags (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    tag_norm TEXT NOT NULL,
    position INTEGER NOT NULL,
    tag_kind TEXT NOT NULL DEFAULT 'prompt',
    PRIMARY KEY (image_id, tag_norm)
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_images_sha256 ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_path ON images(path);
CREATE INDEX IF NOT EXISTS idx_hash_bands_image_id ON hash_bands(image_id);
CREATE INDEX IF NOT EXISTS idx_crop_hashes_image_id ON crop_hashes(image_id);
CREATE INDEX IF NOT EXISTS idx_matches_score ON matches(score);
CREATE INDEX IF NOT EXISTS idx_clusters_cluster ON clusters(cluster_id);
CREATE INDEX IF NOT EXISTS idx_prompt_tags_norm_image ON prompt_tags(tag_norm, image_id);
"""


MIGRATIONS = """
DROP INDEX IF EXISTS idx_hash_bands_lookup;
CREATE INDEX IF NOT EXISTS idx_hash_bands_image_id ON hash_bands(image_id);
CREATE INDEX IF NOT EXISTS idx_crop_hashes_image_id ON crop_hashes(image_id);
CREATE INDEX IF NOT EXISTS idx_prompt_tags_norm_image ON prompt_tags(tag_norm, image_id);
CREATE INDEX IF NOT EXISTS idx_images_created_ns ON images(created_ns);
CREATE INDEX IF NOT EXISTS idx_images_modified_ns ON images(modified_ns);
"""


PROMPT_COLUMNS = {
    "model_type": "TEXT",
    "model_hash": "TEXT",
    "model_version": "TEXT",
    "prompt_format": "TEXT",
    "raw_metadata": "BLOB",
    "raw_metadata_codec": "TEXT",
    "parser_version": "INTEGER",
    "classification_root": "TEXT",
    "model_marker_path": "TEXT",
    "prompt_status": "TEXT",
    "classification_status": "TEXT",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with conn:
        _ensure_column(conn, "images", "created_ns", "INTEGER")
        _ensure_column(conn, "images", "modified_ns", "INTEGER")
    conn.executescript(MIGRATIONS)
    # Keep additive upgrades in one transaction. The version marker is written
    # last, so it never advertises a partially applied prompt-schema migration.
    with conn:
        for column, column_type in PROMPT_COLUMNS.items():
            _ensure_column(conn, "image_prompts", column, column_type)
        _ensure_column(conn, "prompt_tags", "tag_kind", "TEXT NOT NULL DEFAULT 'prompt'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_prompts_model_type_image "
            "ON image_prompts(model_type, image_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_tags_kind_norm_image "
            "ON prompt_tags(tag_kind, tag_norm, image_id)"
        )
        set_metadata(conn, PROMPT_SCHEMA_VERSION_KEY, PROMPT_SCHEMA_VERSION)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def encode_prompt_metadata(raw_metadata: str | bytes | None) -> tuple[bytes | None, str | None]:
    """Encode bounded prompt metadata for image_prompts raw storage.

    Callers should record ``prompt_metadata_too_large`` when this raises
    ValueError. The limit applies to decoded UTF-8 bytes, not compressed size.
    """
    if raw_metadata is None:
        return None, None
    raw = raw_metadata.encode("utf-8") if isinstance(raw_metadata, str) else bytes(raw_metadata)
    if len(raw) > MAX_PROMPT_METADATA_BYTES:
        raise ValueError("prompt_metadata_too_large")
    compressed = zlib.compress(raw)
    if len(compressed) < len(raw):
        return compressed, "zlib+utf8"
    return raw, "utf8"


def decode_prompt_metadata(raw_metadata: bytes | None, codec: str | None) -> str | None:
    """Decode raw prompt metadata while enforcing the decoded-size ceiling."""
    if raw_metadata is None:
        return None
    data = bytes(raw_metadata)
    if codec == "zlib+utf8":
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(data, MAX_PROMPT_METADATA_BYTES + 1)
        if decompressor.unconsumed_tail or len(decoded) > MAX_PROMPT_METADATA_BYTES:
            raise ValueError("prompt_metadata_too_large")
        decoded += decompressor.flush()
        if len(decoded) > MAX_PROMPT_METADATA_BYTES:
            raise ValueError("prompt_metadata_too_large")
        data = decoded
    elif codec != "utf8":
        raise ValueError(f"unsupported raw metadata codec: {codec}")
    if len(data) > MAX_PROMPT_METADATA_BYTES:
        raise ValueError("prompt_metadata_too_large")
    return data.decode("utf-8")


def get_metadata(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
    return str(row["value"] if isinstance(row, sqlite3.Row) else row[0]) if row else default


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_crop_index_state(conn: sqlite3.Connection) -> str:
    return get_metadata(conn, CROP_INDEX_STATE_KEY, "disabled") or "disabled"


def set_crop_index_state(conn: sqlite3.Connection, state: str) -> None:
    if state not in {"disabled", "building", "incomplete", "ready"}:
        raise ValueError(f"invalid crop index state: {state}")
    set_metadata(conn, CROP_INDEX_STATE_KEY, state)
    set_metadata(conn, CROP_INDEX_VERSION_KEY, CROP_INDEX_VERSION)


def is_crop_index_ready(conn: sqlite3.Connection) -> bool:
    return (
        get_crop_index_state(conn) == "ready"
        and get_metadata(conn, CROP_INDEX_VERSION_KEY) == CROP_INDEX_VERSION
    )


def crop_index_is_complete(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT NOT EXISTS (
            SELECT 1
            FROM hashes h
            JOIN images i ON i.id = h.image_id
            WHERE i.missing_at IS NULL
              AND i.decode_error IS NULL
              AND i.width >= 64
              AND i.height >= 64
              AND NOT EXISTS (
                  SELECT 1
                  FROM crop_hashes ch
                  WHERE ch.image_id = h.image_id
                    AND ch.region = 'center_50'
              )
        )
        """
    ).fetchone()
    return bool(row[0])


def checkpoint_database(conn: sqlite3.Connection, mode: str = "PASSIVE") -> tuple[int, int, int] | None:
    normalized = mode.upper()
    if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError(f"invalid WAL checkpoint mode: {mode}")
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def get_existing_image(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM images WHERE path = ?",
        (str(path),),
    ).fetchone()


def upsert_image(
    conn: sqlite3.Connection,
    *,
    path: Path,
    size_bytes: int,
    mtime_ns: int,
    created_ns: int | None = None,
    modified_ns: int | None = None,
    indexed_at: str,
    width: int | None = None,
    height: int | None = None,
    image_format: str | None = None,
    sha256: bytes | None = None,
    decode_error: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO images (
            path, size_bytes, mtime_ns, created_ns, modified_ns, width, height, format, sha256,
            indexed_at, missing_at, decode_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(path) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            created_ns = excluded.created_ns,
            modified_ns = excluded.modified_ns,
            width = excluded.width,
            height = excluded.height,
            format = excluded.format,
            sha256 = excluded.sha256,
            indexed_at = excluded.indexed_at,
            missing_at = NULL,
            decode_error = excluded.decode_error
        """,
        (
            str(path),
            size_bytes,
            mtime_ns,
            created_ns,
            modified_ns,
            width,
            height,
            image_format,
            sha256,
            indexed_at,
            decode_error,
        ),
    )
    row = get_existing_image(conn, path)
    if row is None:
        raise RuntimeError(f"Failed to upsert image row for {path}")
    return int(row["id"])


def update_image_dates(
    conn: sqlite3.Connection,
    image_id: int,
    *,
    created_ns: int | None,
    modified_ns: int | None,
) -> None:
    conn.execute(
        """
        UPDATE images
        SET created_ns = ?, modified_ns = ?
        WHERE id = ?
        """,
        (created_ns, modified_ns, image_id),
    )


def update_image_record(
    conn: sqlite3.Connection,
    image_id: int,
    *,
    path: Path,
    size_bytes: int,
    mtime_ns: int,
    created_ns: int | None = None,
    modified_ns: int | None = None,
    indexed_at: str,
    width: int | None = None,
    height: int | None = None,
    image_format: str | None = None,
    sha256: bytes | None = None,
    decode_error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE images
        SET path = ?,
            size_bytes = ?,
            mtime_ns = ?,
            created_ns = ?,
            modified_ns = ?,
            width = ?,
            height = ?,
            format = ?,
            sha256 = ?,
            indexed_at = ?,
            missing_at = NULL,
            decode_error = ?
        WHERE id = ?
        """,
        (
            str(path),
            size_bytes,
            mtime_ns,
            created_ns,
            modified_ns,
            width,
            height,
            image_format,
            sha256,
            indexed_at,
            decode_error,
            image_id,
        ),
    )


def update_image_path(
    conn: sqlite3.Connection,
    image_id: int,
    *,
    path: Path,
    size_bytes: int,
    mtime_ns: int,
    created_ns: int | None = None,
    modified_ns: int | None = None,
    indexed_at: str,
) -> None:
    conn.execute(
        """
        UPDATE images
        SET path = ?,
            size_bytes = ?,
            mtime_ns = ?,
            created_ns = ?,
            modified_ns = ?,
            indexed_at = ?,
            missing_at = NULL
        WHERE id = ?
        """,
        (str(path), size_bytes, mtime_ns, created_ns, modified_ns, indexed_at, image_id),
    )


def delete_images(conn: sqlite3.Connection, image_ids: list[int]) -> int:
    unique_ids = list(dict.fromkeys(image_ids))
    if not unique_ids:
        return 0

    affected_cluster_ids: set[int] = set()
    for batch in _batches(unique_ids):
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT DISTINCT cluster_id FROM clusters WHERE image_id IN ({placeholders})",
            batch,
        ).fetchall()
        affected_cluster_ids.update(int(row["cluster_id"]) for row in rows)

    for batch in _batches(unique_ids):
        placeholders = ", ".join("?" for _ in batch)
        conn.execute(
            f"DELETE FROM matches WHERE image_id_a IN ({placeholders}) OR image_id_b IN ({placeholders})",
            (*batch, *batch),
        )

    # Cluster membership is derived from the complete match graph. Removing even
    # one member can split a cluster or remove its representative, so invalidate
    # every affected cluster rather than leaving stale membership behind.
    for batch in _batches(sorted(affected_cluster_ids)):
        placeholders = ", ".join("?" for _ in batch)
        conn.execute(f"DELETE FROM clusters WHERE cluster_id IN ({placeholders})", batch)

    for batch in _batches(unique_ids):
        placeholders = ", ".join("?" for _ in batch)
        conn.execute(f"DELETE FROM images WHERE id IN ({placeholders})", batch)
    return len(unique_ids)


def _batches(values: list[int], size: int = DELETE_BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def replace_hashes(
    conn: sqlite3.Connection,
    image_id: int,
    hashes: dict[str, bytes],
    *,
    clear_existing: bool = True,
) -> None:
    grids = [hashes.get(f"grid{i}") for i in range(9)]
    if clear_existing:
        conn.execute("DELETE FROM hash_bands WHERE image_id = ?", (image_id,))
    conn.execute(
        """
        INSERT INTO hashes (
            image_id, dhash256, phash256, whash256,
            grid0, grid1, grid2, grid3, grid4, grid5, grid6, grid7, grid8
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(image_id) DO UPDATE SET
            dhash256 = excluded.dhash256,
            phash256 = excluded.phash256,
            whash256 = excluded.whash256,
            grid0 = excluded.grid0,
            grid1 = excluded.grid1,
            grid2 = excluded.grid2,
            grid3 = excluded.grid3,
            grid4 = excluded.grid4,
            grid5 = excluded.grid5,
            grid6 = excluded.grid6,
            grid7 = excluded.grid7,
            grid8 = excluded.grid8
        """,
        (
            image_id,
            hashes.get("dhash256"),
            hashes.get("phash256"),
            hashes.get("whash256"),
            *grids,
        ),
    )


def clear_hashes(conn: sqlite3.Connection, image_id: int) -> None:
    conn.execute("DELETE FROM hash_bands WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM hashes WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM crop_hashes WHERE image_id = ?", (image_id,))


def fetch_hash_row(conn: sqlite3.Connection, image_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hashes WHERE image_id = ?", (image_id,)).fetchone()


def hash_row_to_dict(row: sqlite3.Row) -> dict[str, bytes]:
    keys = ["dhash256", "phash256", "whash256"] + [f"grid{i}" for i in range(9)]
    return {key: row[key] for key in keys if row[key] is not None}


def replace_crop_hashes(
    conn: sqlite3.Connection,
    image_id: int,
    hashes: dict[str, bytes],
    *,
    clear_existing: bool = True,
) -> None:
    crop_rows = [
        (image_id, key.removeprefix("crop:"), value)
        for key, value in hashes.items()
        if key.startswith("crop:")
    ]
    if clear_existing:
        conn.execute("DELETE FROM crop_hashes WHERE image_id = ?", (image_id,))
    if not crop_rows:
        return
    conn.executemany(
        """
        INSERT INTO crop_hashes (image_id, region, phash256)
        VALUES (?, ?, ?)
        ON CONFLICT(image_id, region) DO UPDATE SET
            phash256 = excluded.phash256
        """,
        crop_rows,
    )


def fetch_crop_hashes(conn: sqlite3.Connection, image_id: int) -> dict[str, bytes]:
    rows = conn.execute(
        "SELECT region, phash256 FROM crop_hashes WHERE image_id = ?",
        (image_id,),
    ).fetchall()
    return {f"crop:{row['region']}": row["phash256"] for row in rows}


def has_prompt_status(conn: sqlite3.Connection, image_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM image_prompts WHERE image_id = ? LIMIT 1",
        (image_id,),
    ).fetchone()
    return row is not None


def replace_prompt(
    conn: sqlite3.Connection,
    image_id: int,
    *,
    prompt: str | None,
    metadata_source: str | None,
    prompt_error: str | None,
    indexed_at: str,
    tags: list[tuple[str, str] | tuple[str, str, str]],
    model_type: str | None = None,
    model_hash: str | None = None,
    model_version: str | None = None,
    prompt_format: str | None = None,
    raw_metadata: bytes | None = None,
    raw_metadata_codec: str | None = None,
    parser_version: int | None = None,
    classification_root: str | None = None,
    model_marker_path: str | None = None,
    prompt_status: str | None = None,
    classification_status: str | None = None,
) -> None:
    conn.execute("DELETE FROM prompt_tags WHERE image_id = ?", (image_id,))
    conn.execute(
        """
        INSERT INTO image_prompts (
            image_id, prompt, metadata_source, prompt_indexed_at, prompt_error,
            model_type, model_hash, model_version, prompt_format, raw_metadata, raw_metadata_codec,
            parser_version, classification_root, model_marker_path, prompt_status,
            classification_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(image_id) DO UPDATE SET
            prompt = excluded.prompt,
            metadata_source = excluded.metadata_source,
            prompt_indexed_at = excluded.prompt_indexed_at,
            prompt_error = excluded.prompt_error,
            model_type = excluded.model_type,
            model_hash = excluded.model_hash,
            model_version = excluded.model_version,
            prompt_format = excluded.prompt_format,
            raw_metadata = excluded.raw_metadata,
            raw_metadata_codec = excluded.raw_metadata_codec,
            parser_version = excluded.parser_version,
            classification_root = excluded.classification_root,
            model_marker_path = excluded.model_marker_path,
            prompt_status = excluded.prompt_status,
            classification_status = excluded.classification_status
        """,
        (
            image_id,
            prompt,
            metadata_source,
            indexed_at,
            prompt_error,
            model_type,
            model_hash,
            model_version,
            prompt_format,
            raw_metadata,
            raw_metadata_codec,
            parser_version,
            classification_root,
            model_marker_path,
            prompt_status,
            classification_status,
        ),
    )
    if not tags:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO prompt_tags (
            image_id, tag, tag_norm, position, tag_kind
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                image_id,
                tag[0],
                tag[1],
                position,
                tag[2] if len(tag) == 3 else "prompt",
            )
            for position, tag in enumerate(tags)
        ],
    )
