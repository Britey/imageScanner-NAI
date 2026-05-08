from __future__ import annotations

import sqlite3
from pathlib import Path


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
    prompt_error TEXT
);

CREATE TABLE IF NOT EXISTS prompt_tags (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    tag_norm TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (image_id, tag_norm)
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
    _ensure_column(conn, "images", "created_ns", "INTEGER")
    _ensure_column(conn, "images", "modified_ns", "INTEGER")
    conn.executescript(MIGRATIONS)
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


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
    tags: list[tuple[str, str]],
) -> None:
    conn.execute("DELETE FROM prompt_tags WHERE image_id = ?", (image_id,))
    conn.execute(
        """
        INSERT INTO image_prompts (
            image_id, prompt, metadata_source, prompt_indexed_at, prompt_error
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(image_id) DO UPDATE SET
            prompt = excluded.prompt,
            metadata_source = excluded.metadata_source,
            prompt_indexed_at = excluded.prompt_indexed_at,
            prompt_error = excluded.prompt_error
        """,
        (image_id, prompt, metadata_source, indexed_at, prompt_error),
    )
    if not tags:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO prompt_tags (
            image_id, tag, tag_norm, position
        )
        VALUES (?, ?, ?, ?)
        """,
        [
            (image_id, tag, tag_norm, position)
            for position, (tag, tag_norm) in enumerate(tags)
        ],
    )
