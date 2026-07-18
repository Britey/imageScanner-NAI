from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import sqlite3
from dataclasses import dataclass
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:  # Progress output is optional for library/test environments.
    class _ProgressFallback:
        def __init__(self, iterable=None):
            self.iterable = iterable if iterable is not None else ()

        def __iter__(self):
            return iter(self.iterable)

        def update(self, _amount: int = 1) -> None:
            pass

        def close(self) -> None:
            pass

    def tqdm(iterable=None, **_kwargs):
        return _ProgressFallback(iterable)

from .bands import insert_bands
from .config import ScanConfig
from .db import (
    clear_hashes,
    crop_index_is_complete,
    delete_images,
    encode_prompt_metadata,
    get_crop_index_state,
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
from .model_paths import (
    DEFAULT_MODEL_PATHS_PATH,
    ModelPathConfig,
    ModelPathRule,
    load_model_paths,
    path_is_within,
)
from .nai_prompt import (
    OTHER_MODEL_IDENTITY,
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
    configured_model: str | None
    model_config_root: str | None
    model_config_rule_id: str | None
    classification_status: str
    precomputed_sha256: bytes | None = None
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
    configured_model: str | None
    model_config_root: str | None
    model_config_rule_id: str | None
    classification_status: str


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Path],
    *,
    config: ScanConfig | None = None,
    model_paths: ModelPathConfig | None = None,
) -> ScanStats:
    config = config or ScanConfig()
    if config.crop_index:
        set_crop_index_state(conn, "building")
        conn.commit()
    workers = max(1, config.workers)
    stats = ScanStats()
    if model_paths is None:
        model_paths = load_model_paths(DEFAULT_MODEL_PATHS_PATH)
    if model_paths.issues:
        details = "; ".join(f"{issue.section}: {issue.message}" for issue in model_paths.issues)
        raise ValueError(f"invalid model_paths.ini: {details}")
    paths = list(iter_image_paths(roots))
    tasks: list[ScanTask] = []
    existing_rows = conn.execute("SELECT * FROM images").fetchall()
    _refresh_model_config_assignments(conn, existing_rows, model_paths)
    existing_by_path = {str(row["path"]): row for row in existing_rows}
    existing_by_id = {int(row["id"]): row for row in existing_rows}
    prompt_rows = {
        int(row["image_id"]): row
        for row in conn.execute(
            """
            SELECT image_id, model_type, parser_version, classification_root,
                   configured_model, model_config_root, model_config_rule_id,
                   classification_status
            FROM image_prompts
            """
        ).fetchall()
    }
    center_crop_ids = (
        {
            int(row["image_id"])
            for row in conn.execute(
                "SELECT image_id FROM crop_hashes WHERE region = 'center_50'"
            ).fetchall()
        }
        if config.crop_index
        else set()
    )
    moved_candidates = _missing_hash_candidates(
        conn,
        known_paths={str(path) for path in paths},
        roots=roots,
    )
    precomputed_hashes: dict[Path, bytes] = {}
    model_rule_cache: dict[Path, ModelPathRule | None] = {}

    for path in tqdm(paths, desc="Checking images", unit="image"):
        stats.seen += 1
        try:
            stat = path.stat()
        except OSError:
            stats.failed += 1
            continue
        created_ns = _created_ns_from_stat(stat)
        modified_ns = stat.st_mtime_ns

        existing = existing_by_path.get(str(path))
        moved_id = None
        if existing is None and moved_candidates:
            try:
                moved_id = _claim_moved_image(
                    path,
                    stat.st_size,
                    moved_candidates,
                    precomputed_hashes=precomputed_hashes,
                )
            except OSError:
                stats.failed += 1
                continue
            if moved_id is not None:
                existing = existing_by_id.get(moved_id)
        existing_id = int(existing["id"]) if existing is not None else None
        classification_root = _scan_root_for_path(path, roots)
        model_rule = model_rule_cache.get(path.parent)
        if path.parent not in model_rule_cache:
            model_rule = model_paths.resolve(path)
            model_rule_cache[path.parent] = model_rule
        classification_status = "configured" if model_rule is not None else "unconfigured"
        configured_model = model_rule.label if model_rule is not None else None
        model_config_root = str(model_rule.path) if model_rule is not None else None
        model_config_rule_id = model_rule.rule_id if model_rule is not None else None
        prompt_row = prompt_rows.get(existing_id) if existing_id is not None else None
        classification_changed = prompt_row is None or any(
            (
                prompt_row["classification_root"] != str(classification_root),
                prompt_row["configured_model"] != configured_model,
                prompt_row["model_config_root"] != model_config_root,
                prompt_row["model_config_rule_id"] != model_config_rule_id,
                prompt_row["classification_status"] != classification_status,
            )
        )
        needs_prompt = (
            config.prompt_index
            and existing_id is not None
            and (
                config.reindex_prompts
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
            and (
                not config.crop_index
                or (existing["width"] is not None and int(existing["width"]) < 64)
                or (existing["height"] is not None and int(existing["height"]) < 64)
                or int(existing["id"]) in center_crop_ids
            )
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
                configured_model=configured_model,
                model_config_root=model_config_root,
                model_config_rule_id=model_config_rule_id,
                classification_status=classification_status,
                precomputed_sha256=precomputed_hashes.get(path),
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
            task_iterator = iter(tasks)
            pending = set()

            def submit_next() -> bool:
                try:
                    task = next(task_iterator)
                except StopIteration:
                    return False
                pending.add(executor.submit(
                    _hash_task,
                    task,
                    config.min_width,
                    config.min_height,
                    config.crop_index,
                    config.prompt_index,
                    config.stealth_prompt,
                ))
                return True

            max_pending = max(workers, workers * 2)
            for _ in range(min(max_pending, len(tasks))):
                submit_next()
            progress = tqdm(total=len(tasks), desc="Hashing images", unit="image")
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    _store_result(conn, future.result(), config, stats)
                    progress.update(1)
                while len(pending) < max_pending and submit_next():
                    pass
            progress.close()

    conn.commit()
    if config.delete_missing:
        stats.deleted = _delete_missing_images(conn)
    _finish_crop_index_state(conn, config.crop_index)
    conn.commit()
    return stats


def _scan_root_for_path(path: Path, roots: list[Path]) -> Path:
    canonical = path.resolve(strict=False)
    candidates: list[Path] = []
    for supplied_root in roots:
        supplied = supplied_root.resolve(strict=False)
        scan_root = supplied if supplied.is_dir() else supplied.parent
        try:
            canonical.relative_to(scan_root)
        except ValueError:
            continue
        candidates.append(scan_root)
    return max(candidates, key=lambda item: len(item.parts)) if candidates else canonical.parent


def _refresh_model_config_assignments(
    conn: sqlite3.Connection,
    image_rows: list[sqlite3.Row],
    model_paths: ModelPathConfig,
) -> None:
    current = {
        int(row["image_id"]): row
        for row in conn.execute(
            """
            SELECT
                ip.image_id,
                ip.model_type,
                ip.configured_model,
                ip.model_config_root,
                ip.model_config_rule_id,
                COALESCE(category.tag_count, 0) AS category_tag_count,
                category.tag_norm AS category_tag_norm,
                category.tag_kind AS category_tag_kind
            FROM image_prompts ip
            LEFT JOIN (
                SELECT
                    image_id,
                    COUNT(*) AS tag_count,
                    MIN(tag_norm) AS tag_norm,
                    MIN(tag_kind) AS tag_kind
                FROM prompt_tags
                WHERE tag_kind IN ('model_type', 'model_config')
                GROUP BY image_id
            ) category ON category.image_id = ip.image_id
            """
        ).fetchall()
    }
    rule_cache: dict[Path, ModelPathRule | None] = {}
    for image in image_rows:
        image_id = int(image["id"])
        prompt_row = current.get(image_id)
        if prompt_row is None:
            continue
        image_path = Path(image["path"])
        rule = rule_cache.get(image_path.parent)
        if image_path.parent not in rule_cache:
            rule = model_paths.resolve(image_path)
            rule_cache[image_path.parent] = rule
        configured_model = rule.label if rule is not None else None
        config_root = str(rule.path) if rule is not None else None
        rule_id = rule.rule_id if rule is not None else None
        effective_model = configured_model or str(prompt_row["model_type"] or "sd")
        effective_kind = "model_config" if configured_model else "model_type"
        effective_tag = f"@model:{effective_model}"
        effective_tag_norm = normalize_prompt_tag(effective_tag)
        assignment_matches = (
            prompt_row["configured_model"] == configured_model
            and prompt_row["model_config_root"] == config_root
            and prompt_row["model_config_rule_id"] == rule_id
        )
        category_matches = (
            int(prompt_row["category_tag_count"] or 0) == 1
            and prompt_row["category_tag_norm"] == effective_tag_norm
            and prompt_row["category_tag_kind"] == effective_kind
        )
        if assignment_matches and category_matches:
            continue
        if not category_matches:
            conn.execute(
                """
                DELETE FROM prompt_tags
                WHERE image_id = ?
                  AND (tag_kind IN ('model_type', 'model_config') OR tag_norm = ?)
                """,
                (image_id, effective_tag_norm),
            )
            conn.execute(
                """
                INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind)
                VALUES (?, ?, ?, 1000000000, ?)
                """,
                (image_id, effective_tag, effective_tag_norm, effective_kind),
            )
        if not assignment_matches:
            conn.execute(
                """
                UPDATE image_prompts
                SET configured_model = ?, model_config_root = ?, model_config_rule_id = ?,
                    classification_status = ?
                WHERE image_id = ?
                """,
                (
                    configured_model,
                    config_root,
                    rule_id,
                    "configured" if rule is not None else "unconfigured",
                    image_id,
                ),
            )


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
            configured_model=task.configured_model,
            model_config_root=task.model_config_root,
            model_config_rule_id=task.model_config_rule_id,
            classification_status=task.classification_status,
        )

    if task.prompt_only:
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
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
            configured_model=task.configured_model,
            model_config_root=task.model_config_root,
            model_config_rule_id=task.model_config_rule_id,
            classification_status=task.classification_status,
        )

    try:
        hashes, metadata = compute_image_hashes(
            task.path,
            min_width=min_width,
            min_height=min_height,
            include_crop_regions=crop_index,
            precomputed_sha256=task.precomputed_sha256,
        )
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
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
            configured_model=task.configured_model,
            model_config_root=task.model_config_root,
            model_config_rule_id=task.model_config_rule_id,
            classification_status=task.classification_status,
        )
    except Exception as exc:
        prompt_result = _extract_prompt_fields(
            task.path,
            prompt_index=prompt_index,
            stealth_prompt=stealth_prompt,
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
            sha256=task.precomputed_sha256 or _sha256_or_none(task.path),
            hashes={},
            decode_error=f"{type(exc).__name__}: {exc}",
            had_existing_hashes=task.had_existing_hashes,
            prompt_only=False,
            date_only=False,
            existing_image_id=task.existing_image_id,
            moved=task.moved,
            prompt_result=prompt_result,
            classification_root=task.classification_root,
            configured_model=task.configured_model,
            model_config_root=task.model_config_root,
            model_config_rule_id=task.model_config_rule_id,
            classification_status=task.classification_status,
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


def _missing_hash_candidates(
    conn: sqlite3.Connection,
    *,
    known_paths: set[str] | None = None,
    roots: list[Path] | None = None,
) -> dict[int, dict[bytes, list[int]]]:
    rows = conn.execute(
        """
        SELECT id, path, size_bytes, sha256
        FROM images
        WHERE sha256 IS NOT NULL
        """
    ).fetchall()
    directory_roots = (
        [root.resolve(strict=False) for root in (roots or []) if root.is_dir()]
        if known_paths is not None
        else []
    )
    candidates: dict[int, dict[bytes, list[int]]] = {}
    for row in rows:
        row_path = Path(row["path"])
        if known_paths is None:
            if row_path.exists():
                continue
        else:
            if not any(path_is_within(row_path, root) for root in directory_roots):
                continue
            if str(row_path) in known_paths:
                continue
        hashes_by_size = candidates.setdefault(int(row["size_bytes"]), {})
        hashes_by_size.setdefault(row["sha256"], []).append(int(row["id"]))
    return candidates


def _claim_moved_image(
    path: Path,
    size_bytes: int,
    candidates: dict[int, dict[bytes, list[int]]],
    *,
    precomputed_hashes: dict[Path, bytes] | None = None,
) -> int | None:
    hashes_by_size = candidates.get(size_bytes)
    if not hashes_by_size:
        return None
    sha256 = sha256_file(path)
    if precomputed_hashes is not None:
        precomputed_hashes[path] = sha256
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


def _extract_prompt_fields(
    path: Path,
    *,
    prompt_index: bool,
    stealth_prompt: bool,
) -> PromptResult | None:
    if not prompt_index:
        return None
    return extract_nai_prompt(path, stealth_fallback=stealth_prompt)


def _store_prompt(
    conn: sqlite3.Connection,
    image_id: int,
    result: ScanResult,
    indexed_at: str,
) -> None:
    prompt_result = result.prompt_result or PromptResult(None, None, "no_metadata")
    no_metadata = prompt_result.error == "no_metadata"
    model_type = "no_metadata" if no_metadata else (prompt_result.model_type or "sd")
    model_hash = prompt_result.model_hash.casefold() if prompt_result.model_hash else None
    model_identity = prompt_result.model_identity
    if no_metadata:
        model_identity = "无元数据 no_metadata"
    elif prompt_result.error == "missing_model_hash":
        model_identity = OTHER_MODEL_IDENTITY
    elif model_type == "sd" and model_identity and model_hash and model_identity != OTHER_MODEL_IDENTITY:
        model_identity = f"{model_identity} {model_hash}"

    tag_rows: list[tuple[str, str, str]] = []
    seen_tags: set[str] = set()

    def add_tag(tag: str, kind: str) -> None:
        normalized = normalize_prompt_tag(tag)
        if not normalized:
            return
        if normalized in seen_tags:
            if kind != "prompt":
                for index, existing in enumerate(tag_rows):
                    if existing[1] == normalized and existing[2] == "prompt":
                        tag_rows[index] = (tag, normalized, kind)
                        break
            return
        seen_tags.add(normalized)
        tag_rows.append((tag, normalized, kind))

    for tag in prompt_result.tags:
        add_tag(tag, "prompt")
    effective_model = result.configured_model or model_type
    effective_kind = "model_config" if result.configured_model else "model_type"
    add_tag(f"@model:{effective_model}", effective_kind)
    if model_identity:
        add_tag(f"@model:{model_identity}", "model_identity")
    if model_hash:
        add_tag(f"@model:{model_hash}", "model_hash")
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
        model_type=model_type,
        model_hash=model_hash,
        model_identity=model_identity,
        model_version=prompt_result.model_version,
        configured_model=result.configured_model,
        model_config_root=result.model_config_root,
        model_config_rule_id=result.model_config_rule_id,
        prompt_format=prompt_result.prompt_format,
        raw_metadata=raw_metadata,
        raw_metadata_codec=raw_metadata_codec,
        parser_version=PROMPT_PARSER_VERSION,
        classification_root=result.classification_root,
        model_marker_path=None,
        prompt_status=prompt_status,
        classification_status=result.classification_status,
    )
