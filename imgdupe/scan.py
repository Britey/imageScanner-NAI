from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import sqlite3
from dataclasses import dataclass
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:  # Progress output is optional for library/test environments.
    def tqdm(iterable=None, **_kwargs):
        return iterable if iterable is not None else ()

from .bands import insert_bands
from .config import ScanConfig
from .db import (
    clear_hashes,
    crop_index_is_complete,
    delete_images,
    encode_prompt_metadata,
    get_crop_index_state,
    get_existing_image,
    has_prompt_status,
    replace_crop_hashes,
    replace_hashes,
    replace_prompt,
    set_crop_index_state,
    update_image_dates,
    update_image_path,
    update_image_record,
    upsert_image,
)
from .hashing import compute_image_hashes, sha256_file
from .model_type import ModelTypeResolution, ModelTypeResolver
from .nai_prompt import (
    PROMPT_PARSER_VERSION,
    PromptResult,
    extract_nai_prompt,
    normalize_prompt_tag,
)
from .utils import iter_image_paths, utc_now_sql


@dataclass
class ScanStats:
    seen: int = 0
    queued: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    moved: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class ScanTask:
    path: Path
    size_bytes: int
    mtime_ns: int
    created_ns: int | None
    modified_ns: int | None
    had_existing_hashes: bool
    prompt_only: bool
    date_only: bool
    existing_image_id: int | None
    classification_root: str
    marker_model_type: str | None
    model_marker_path: str | None
    classification_status: str
    classification_error: str | None
    moved: bool = False


@dataclass(frozen=True)
class ScanResult:
    path: Path
    size_bytes: int
    mtime_ns: int
    created_ns: int | None
    modified_ns: int | None
    width: int | None
    height: int | None
    image_format: str | None
    sha256: bytes | None
    hashes: dict[str, bytes]
    decode_error: str | None
    had_existing_hashes: bool
    prompt_only: bool
    date_only: bool
    existing_image_id: int | None
    moved: bool
    prompt_result: PromptResult | None
    classification_root: str
    marker_model_type: str | None
    model_marker_path: str | None
    classification_status: str
    classification_error: str | None


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Path],
    *,
    config: ScanConfig | None = None,
) -> ScanStats:
    config = config or ScanConfig()
    if config.crop_index:
        set_crop_index_state(conn, "building")
        conn.commit()
    workers = max(1, config.workers)
    stats = ScanStats()
    paths = list(iter_image_paths(roots))
    model_resolvers = _build_model_resolvers(roots)
    tasks: list[ScanTask] = []
    moved_candidates = _missing_hash_candidates(conn)

    for path in tqdm(paths, desc="Checking images", unit="image"):
        stats.seen += 1
        try:
            stat = path.stat()
        except OSError:
            stats.failed += 1
            continue
        created_ns = _created_ns_from_stat(stat)
        modified_ns = stat.st_mtime_ns

        existing = get_existing_image(conn, path)
        moved_id = None
        if existing is None and moved_candidates:
            try:
                moved_id = _claim_moved_image(path, stat.st_size, moved_candidates)
            except OSError:
                stats.failed += 1
                continue
            if moved_id is not None:
                existing = conn.execute("SELECT * FROM images WHERE id = ?", (moved_id,)).fetchone()
        existing_id = int(existing["id"]) if existing is not None else None
        classification_root, marker = _resolve_model_marker(path, model_resolvers)
        classification_status = _classification_status(marker)
        marker_path = str(marker.marker_path) if marker.marker_path else None
        prompt_row = (
            conn.execute(
                """
                SELECT model_type, parser_version, classification_root,
                       model_marker_path, classification_status
                FROM image_prompts
                WHERE image_id = ?
                """,
                (existing_id,),
            ).fetchone()
            if existing_id is not None
            else None
        )
        classification_changed = prompt_row is None or any(
            (
                prompt_row["classification_root"] != str(classification_root),
                prompt_row["model_marker_path"] != marker_path,
                prompt_row["classification_status"] != classification_status,
                marker.model_type is not None and prompt_row["model_type"] != marker.model_type,
            )
        )
        needs_prompt = (
            config.prompt_index
            and existing_id is not None
            and (
                config.reindex_prompts
                or not has_prompt_status(conn, existing_id)
                or prompt_row is None
                or prompt_row["parser_version"] != PROMPT_PARSER_VERSION
                or classification_changed
            )
        )
        unchanged_hashes = (
            existing is not None
            and (moved_id is not None or int(existing["size_bytes"]) == stat.st_size)
            and (moved_id is not None or int(existing["mtime_ns"]) == stat.st_mtime_ns)
            and existing["decode_error"] is None
            and existing["missing_at"] is None
            and (not config.crop_index or _has_crop_hashes(conn, int(existing["id"])))
        )
        needs_date_update = (
            unchanged_hashes
            and existing_id is not None
            and (
                existing["created_ns"] is None
                or existing["modified_ns"] is None
                or _optional_int(existing["created_ns"]) != created_ns
                or _optional_int(existing["modified_ns"]) != modified_ns
            )
        )
        if unchanged_hashes and moved_id is None and not needs_prompt and not needs_date_update:
            stats.skipped += 1
            continue

        tasks.append(
            ScanTask(
                path=path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                created_ns=created_ns,
                modified_ns=modified_ns,
                had_existing_hashes=existing is not None,
                prompt_only=unchanged_hashes and needs_prompt,
                date_only=unchanged_hashes and (needs_date_update or moved_id is not None) and not needs_prompt,
                existing_image_id=existing_id,
                classification_root=str(classification_root),
                marker_model_type=marker.model_type,
                model_marker_path=marker_path,
                classification_status=classification_status,
                classification_error=marker.error,
                moved=moved_id is not None,
            )
        )
        stats.queued += 1

    if not tasks:
        if config.delete_missing:
            stats.deleted = _delete_missing_images(conn)
        _finish_crop_index_state(conn, config.crop_index)
        conn.commit()
        return stats

    if workers <= 1:
        results = (
            _hash_task(
                task,
                config.min_width,
                config.min_height,
                config.crop_index,
                config.prompt_index,
                config.stealth_prompt,
            )
            for task in tasks
        )
        iterator = tqdm(results, total=len(tasks), desc="Hashing images", unit="image")
        for result in iterator:
            _store_result(conn, result, config, stats)
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _hash_task,
                    task,
                    config.min_width,
                    config.min_height,
                    config.crop_index,
                    config.prompt_index,
                    config.stealth_prompt,
                )
                for task in tasks
            ]
            for future in tqdm(as_completed(futures), total=len(futures), desc="Hashing images", unit="image"):
                _store_result(conn, future.result(), config, stats)

    conn.commit()
    if config.delete_missing:
        stats.deleted = _delete_missing_images(conn)
    _finish_crop_index_state(conn, config.crop_index)
    conn.commit()
    return stats


def _build_model_resolvers(roots: list[Path]) -> list[tuple[Path, ModelTypeResolver]]:
    resolved: dict[str, tuple[Path, ModelTypeResolver]] = {}
    for root in roots:
        canonical = root.resolve(strict=False)
        scan_root = canonical if canonical.is_dir() else canonical.parent
        key = str(scan_root).casefold()
        resolved.setdefault(key, (scan_root, ModelTypeResolver(scan_root)))
    return sorted(resolved.values(), key=lambda item: len(item[0].parts), reverse=True)


def _resolve_model_marker(
    path: Path,
    resolvers: list[tuple[Path, ModelTypeResolver]],
) -> tuple[Path, ModelTypeResolution]:
    canonical = path.resolve(strict=False)
    for scan_root, resolver in resolvers:
        try:
            canonical.relative_to(scan_root)
        except ValueError:
            continue
        return scan_root, resolver.resolve_for_image(canonical)
    # iter_image_paths only yields files below a requested root. This fallback
    # keeps direct/internal callers deterministic if that invariant changes.
    scan_root = canonical.parent
    return scan_root, ModelTypeResolver(scan_root).resolve_for_image(canonical)


def _classification_status(resolution: ModelTypeResolution) -> str:
    if resolution.status != "invalid_model_marker":
        return resolution.status
    signature = resolution.marker_signature or "unknown"
    return f"invalid_model_marker:{signature}"


def _finish_crop_index_state(conn: sqlite3.Connection, crop_index_requested: bool) -> None:
    if crop_index_requested:
        set_crop_index_state(conn, "ready" if crop_index_is_complete(conn) else "incomplete")
        return
    state = get_crop_index_state(conn)
    if state == "building":
        set_crop_index_state(conn, "incomplete")
    elif state == "ready" and not crop_index_is_complete(conn):
        set_crop_index_state(conn, "incomplete")


def _store_result(
    conn: sqlite3.Connection,
    result: ScanResult,
    config: ScanConfig,
    stats: ScanStats,
) -> None:
    indexed_at = utc_now_sql()
    if result.date_only:
        if result.existing_image_id is None:
            stats.failed += 1
            _commit_batch_if_needed(conn, config, stats)
            return
        if result.moved:
            update_image_path(
                conn,
                result.existing_image_id,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
            )
            stats.moved += 1
        else:
            update_image_dates(
                conn,
                result.existing_image_id,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
            )
        stats.indexed += 1
        _commit_batch_if_needed(conn, config, stats)
        return

    if result.prompt_only:
        if result.existing_image_id is None:
            stats.failed += 1
            _commit_batch_if_needed(conn, config, stats)
            return
        if result.moved:
            update_image_path(
                conn,
                result.existing_image_id,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
            )
            stats.moved += 1
        else:
            update_image_dates(
                conn,
                result.existing_image_id,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
            )
        _store_prompt(conn, result.existing_image_id, result, indexed_at)
        stats.indexed += 1
        _commit_batch_if_needed(conn, config, stats)
        return

    if result.decode_error is None:
        if result.moved and result.existing_image_id is not None:
            image_id = result.existing_image_id
            update_image_record(
                conn,
                image_id,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
                width=result.width,
                height=result.height,
                image_format=result.image_format,
                sha256=result.sha256,
                decode_error=None,
            )
            stats.moved += 1
        else:
            image_id = upsert_image(
                conn,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
                width=result.width,
                height=result.height,
                image_format=result.image_format,
                sha256=result.sha256,
                decode_error=None,
            )
        replace_hashes(
            conn,
            image_id,
            result.hashes,
            clear_existing=result.had_existing_hashes,
        )
        replace_crop_hashes(
            conn,
            image_id,
            result.hashes,
            clear_existing=result.had_existing_hashes,
        )
        insert_bands(
            conn,
            image_id,
            result.hashes,
            whole_band_size=config.whole_band_size,
            grid_band_size=config.grid_band_size,
        )
        if config.prompt_index:
            _store_prompt(conn, image_id, result, indexed_at)
        stats.indexed += 1
    else:
        if result.moved and result.existing_image_id is not None:
            image_id = result.existing_image_id
            update_image_record(
                conn,
                image_id,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
                sha256=result.sha256,
                decode_error=result.decode_error,
            )
            stats.moved += 1
        else:
            image_id = upsert_image(
                conn,
                path=result.path,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
                created_ns=result.created_ns,
                modified_ns=result.modified_ns,
                indexed_at=indexed_at,
                sha256=result.sha256,
                decode_error=result.decode_error,
            )
        if result.had_existing_hashes:
            clear_hashes(conn, image_id)
        if config.prompt_index:
            _store_prompt(conn, image_id, result, indexed_at)
        stats.failed += 1

    _commit_batch_if_needed(conn, config, stats)


def _commit_batch_if_needed(
    conn: sqlite3.Connection, config: ScanConfig, stats: ScanStats
) -> None:
    if config.batch_size > 0 and (stats.indexed + stats.failed) % config.batch_size == 0:
        conn.commit()


def _hash_task(
    task: ScanTask,
    min_width: int,
    min_height: int,
    crop_index: bool,
    prompt_index: bool,
    stealth_prompt: bool,
) -> ScanResult:
    if task.date_only:
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            created_ns=task.created_ns,
            modified_ns=task.modified_ns,
            width=None,
            height=None,
            image_format=None,
            sha256=None,
            hashes={},
            decode_error=None,
            had_existing_hashes=task.had_existing_hashes,
            prompt_only=False,
            date_only=True,
            existing_image_id=task.existing_image_id,
            moved=task.moved,
            prompt_result=None,
            classification_root=task.classification_root,
            marker_model_type=task.marker_model_type,
            model_marker_path=task.model_marker_path,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )

    if task.prompt_only:
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            created_ns=task.created_ns,
            modified_ns=task.modified_ns,
            width=None,
            height=None,
            image_format=None,
            sha256=None,
            hashes={},
            decode_error=None,
            had_existing_hashes=task.had_existing_hashes,
            prompt_only=True,
            date_only=False,
            existing_image_id=task.existing_image_id,
            moved=task.moved,
            prompt_result=prompt_result,
            classification_root=task.classification_root,
            marker_model_type=task.marker_model_type,
            model_marker_path=task.model_marker_path,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )

    try:
        hashes, metadata = compute_image_hashes(
            task.path,
            min_width=min_width,
            min_height=min_height,
            include_crop_regions=crop_index,
        )
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            created_ns=task.created_ns,
            modified_ns=task.modified_ns,
            width=int(metadata["width"]),
            height=int(metadata["height"]),
            image_format=str(metadata["format"]),
            sha256=hashes["sha256"],
            hashes=hashes,
            decode_error=None,
            had_existing_hashes=task.had_existing_hashes,
            prompt_only=False,
            date_only=False,
            existing_image_id=task.existing_image_id,
            moved=task.moved,
            prompt_result=prompt_result,
            classification_root=task.classification_root,
            marker_model_type=task.marker_model_type,
            model_marker_path=task.model_marker_path,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )
    except Exception as exc:
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            created_ns=task.created_ns,
            modified_ns=task.modified_ns,
            width=None,
            height=None,
            image_format=None,
            sha256=_sha256_or_none(task.path),
            hashes={},
            decode_error=f"{type(exc).__name__}: {exc}",
            had_existing_hashes=task.had_existing_hashes,
            prompt_only=False,
            date_only=False,
            existing_image_id=task.existing_image_id,
            moved=task.moved,
            prompt_result=prompt_result,
            classification_root=task.classification_root,
            marker_model_type=task.marker_model_type,
            model_marker_path=task.model_marker_path,
            classification_status=task.classification_status,
            classification_error=task.classification_error,
        )


def _sha256_or_none(path: Path) -> bytes | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _created_ns_from_stat(stat) -> int | None:
    birthtime_ns = getattr(stat, "st_birthtime_ns", None)
    if birthtime_ns is not None:
        return int(birthtime_ns)
    ctime_ns = getattr(stat, "st_ctime_ns", None)
    return int(ctime_ns) if ctime_ns is not None else None


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def _missing_hash_candidates(conn: sqlite3.Connection) -> dict[int, dict[bytes, list[int]]]:
    rows = conn.execute(
        """
        SELECT id, path, size_bytes, sha256
        FROM images
        WHERE sha256 IS NOT NULL
        """
    ).fetchall()
    candidates: dict[int, dict[bytes, list[int]]] = {}
    for row in rows:
        if Path(row["path"]).exists():
            continue
        hashes_by_size = candidates.setdefault(int(row["size_bytes"]), {})
        hashes_by_size.setdefault(row["sha256"], []).append(int(row["id"]))
    return candidates


def _claim_moved_image(
    path: Path,
    size_bytes: int,
    candidates: dict[int, dict[bytes, list[int]]],
) -> int | None:
    hashes_by_size = candidates.get(size_bytes)
    if not hashes_by_size:
        return None
    sha256 = sha256_file(path)
    image_ids = hashes_by_size.get(sha256, [])
    if len(image_ids) != 1:
        return None
    image_id = image_ids[0]
    del hashes_by_size[sha256]
    if not hashes_by_size:
        del candidates[size_bytes]
    return image_id


def _delete_missing_images(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, path FROM images").fetchall()
    image_ids = [int(row["id"]) for row in rows if not Path(row["path"]).exists()]
    return delete_images(conn, image_ids)


def _has_crop_hashes(conn: sqlite3.Connection, image_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM images i
        WHERE i.id = ?
          AND (
              i.width < 64
              OR i.height < 64
              OR EXISTS (
                  SELECT 1
                  FROM crop_hashes ch
                  WHERE ch.image_id = i.id
                    AND ch.region = 'center_50'
              )
          )
        LIMIT 1
        """,
        (image_id,),
    ).fetchone()
    return row is not None


def _extract_prompt_fields(
    path: Path,
    *,
    prompt_index: bool,
    stealth_prompt: bool,
    classification_status: str,
    classification_error: str | None,
) -> PromptResult | None:
    if not prompt_index:
        return None
    if classification_status.startswith("invalid_model_marker"):
        return PromptResult(
            prompt=None,
            metadata_source=None,
            error=classification_error or "invalid_model_marker",
            model_type="sd",
        )
    return extract_nai_prompt(path, stealth_fallback=stealth_prompt)


def _store_prompt(
    conn: sqlite3.Connection,
    image_id: int,
    result: ScanResult,
    indexed_at: str,
) -> None:
    prompt_result = result.prompt_result or PromptResult(None, None, "no_metadata")
    model_type = result.marker_model_type or prompt_result.model_type or "sd"
    tag_rows: list[tuple[str, str, str]] = [
        (tag, normalize_prompt_tag(tag), "prompt")
        for tag in prompt_result.tags
        if normalize_prompt_tag(tag)
    ]
    if prompt_result.model_hash:
        model_tag = f"@model:{prompt_result.model_hash.casefold()}"
        tag_rows.append((model_tag, normalize_prompt_tag(model_tag), "model_hash"))
    prompt_error = prompt_result.error
    prompt_status = "ok" if prompt_error is None else prompt_error.split(":", 1)[0]
    try:
        raw_metadata, raw_metadata_codec = encode_prompt_metadata(prompt_result.raw_metadata)
    except ValueError:
        raw_metadata = None
        raw_metadata_codec = None
        prompt_status = "prompt_metadata_too_large"
        prompt_error = "prompt_metadata_too_large"
    replace_prompt(
        conn,
        image_id,
        prompt=prompt_result.prompt,
        metadata_source=prompt_result.metadata_source,
        prompt_error=prompt_error,
        indexed_at=indexed_at,
        tags=tag_rows,
        model_type=model_type if not result.classification_status.startswith("invalid_model_marker") else None,
        model_hash=prompt_result.model_hash,
        model_version=prompt_result.model_version,
        prompt_format=prompt_result.prompt_format,
        raw_metadata=raw_metadata,
        raw_metadata_codec=raw_metadata_codec,
        parser_version=PROMPT_PARSER_VERSION,
        classification_root=result.classification_root,
        model_marker_path=result.model_marker_path,
        prompt_status=prompt_status,
        classification_status=result.classification_status,
    )
