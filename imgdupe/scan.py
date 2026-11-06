from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import sqlite3
import hashlib
import json
import multiprocessing
import re
from itertools import islice
from functools import lru_cache
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
    load_model_paths,
    path_is_within,
    lexical_path_is_within,
)
from .nai_prompt import (
    OTHER_MODEL_IDENTITY,
    PROMPT_PARSER_VERSION,
    PromptResult,
    extract_nai_prompt,
    normalize_prompt_tag,
    sanitize_model_name,
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
    roots = [root.resolve(strict=False) for root in roots]
    directory_roots = [root for root in roots if root.is_dir()]
    classification_roots = [root if root in directory_roots else root.parent for root in roots]
    revision = hashlib.sha256(json.dumps(sorted(
        (str(rule.path), rule.label, rule.rule_id) for rule in model_paths.rules
    ), separators=(",", ":")).encode()).hexdigest()
    previous = conn.execute("SELECT value FROM app_metadata WHERE key = 'model_config_revision'").fetchone()
    if previous is None or previous[0] != revision:
        _refresh_model_config_assignments(conn, _image_batches(conn), model_paths)
        conn.execute("INSERT OR REPLACE INTO app_metadata (key, value) VALUES ('model_config_revision', ?)",
                     (revision,))
    claimed_ids: set[int] = set()
    tasks = _scan_tasks(conn, roots, directory_roots, classification_roots, config,
                        model_paths, stats, claimed_ids)
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
        iterator = tqdm(results, desc="Hashing images", unit="image")
        for result in iterator:
            _store_result(conn, result, config, stats)
            claimed_ids.discard(result.existing_image_id)
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as executor:
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
            for _ in range(max_pending):
                if not submit_next():
                    break
            progress = tqdm(desc="Hashing images", unit="image")
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    result = future.result()
                    _store_result(conn, result, config, stats)
                    claimed_ids.discard(result.existing_image_id)
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


def _scan_tasks(conn, roots, directory_roots, classification_roots, config, model_paths,
                stats, claimed_ids):
    def listing_error(_error):
        stats.failed += 1

    resolve_rule = _model_rule_resolver(model_paths)
    for path, existing in _existing_paths(conn, iter_image_paths(roots, on_error=listing_error), model_paths):
        stats.seen += 1
        try:
            stat = path.stat()
        except OSError:
            stats.failed += 1
            continue
        created_ns = _created_ns_from_stat(stat)
        modified_ns = stat.st_mtime_ns

        precomputed_sha256 = None
        moved_id = None
        if existing is None and conn.execute(
            "SELECT 1 FROM images WHERE size_bytes=? AND sha256 IS NOT NULL LIMIT 1",
            (stat.st_size,),
        ).fetchone() is not None:
            try:
                precomputed_sha256 = sha256_file(path)
                existing = _find_moved_image(conn, precomputed_sha256, stat.st_size,
                                             directory_roots, claimed_ids)
            except OSError:
                stats.failed += 1
                continue
            if existing is not None:
                moved_id = int(existing["id"])
                claimed_ids.add(moved_id)
        existing_id = int(existing["id"]) if existing is not None else None
        classification_root = _scan_root_for_path(path, classification_roots, canonical=True)
        model_rule = resolve_rule(path)
        classification_status = "configured" if model_rule is not None else "unconfigured"
        configured_model = model_rule.label if model_rule is not None else None
        model_config_root = str(model_rule.path) if model_rule is not None else None
        model_config_rule_id = model_rule.rule_id if model_rule is not None else None
        prompt_row = existing if existing is not None and existing["prompt_image_id"] is not None else None
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
            and existing["missing_at"] is None
            and _reusable_decode_result(existing["decode_error"], config.min_width, config.min_height)
            and (
                existing["decode_error"] is not None
                or not config.crop_index
                or (existing["width"] is not None and int(existing["width"]) < 64)
                or (existing["height"] is not None and int(existing["height"]) < 64)
                or existing["has_center_crop"]
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

        stats.queued += 1
        yield ScanTask(
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
            precomputed_sha256=precomputed_sha256,
            moved=moved_id is not None,
        )


def _scan_root_for_path(path: Path, roots: list[Path], *, canonical: bool = False) -> Path:
    if not canonical:
        path = path.resolve(strict=False)
        roots = [root.resolve(strict=False) if root.is_dir() else root.resolve(strict=False).parent
                 for root in roots]
    candidates = [root for root in roots if lexical_path_is_within(path, root)]
    return max(candidates, key=lambda item: len(item.parts)) if candidates else path.parent


def _model_rule_resolver(model_paths):
    # File rules must not bleed into sibling files sharing a cached directory.
    exact_rules = {rule.path: rule for rule in model_paths.rules}
    resolve_directory = lru_cache(maxsize=4096)(
        lambda directory: model_paths.resolve(directory, canonical=True)
    )
    return lambda path: exact_rules.get(path) or resolve_directory(path.parent)


_SCAN_ROW_SQL = """
    SELECT i.*, ip.image_id AS prompt_image_id, ip.parser_version, ip.classification_root,
           ip.configured_model, ip.model_config_root, ip.model_config_rule_id,
           ip.classification_status,
           EXISTS(SELECT 1 FROM crop_hashes ch WHERE ch.image_id=i.id AND ch.region='center_50')
               AS has_center_crop
    FROM images i LEFT JOIN image_prompts ip ON ip.image_id=i.id
"""


def _existing_paths(conn, paths, model_paths=None):
    iterator = iter(paths)
    while batch := list(islice(iterator, 400)):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(_SCAN_ROW_SQL + f" WHERE i.path IN ({placeholders})",
                            [str(path) for path in batch])
        by_path = {row["path"]: row for row in rows}
        if model_paths is not None and by_path:
            # Repair externally inserted or legacy category tags for this scan
            # without revisiting the rest of an unchanged collection.
            _refresh_model_batch(conn, list(by_path.values()), model_paths)
        for path in batch:
            yield path, by_path.get(str(path))


def _image_batches(conn):
    rows = conn.execute("SELECT id, path FROM images ORDER BY id LIMIT 400").fetchall()
    while rows:
        yield from rows
        rows = conn.execute("SELECT id, path FROM images WHERE id > ? ORDER BY id LIMIT 400",
                            (rows[-1]["id"],)).fetchall()


def _path_is_missing(path):
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        # An inaccessible/offline path is not evidence of a move or deletion.
        pass
    return False


def _reusable_decode_result(error: str | None, min_width: int, min_height: int) -> bool:
    if error is None or error.startswith("UnidentifiedImageError:"):
        return True
    dimensions = re.fullmatch(r"ImageTooSmallError: image is too small: (\d+)x(\d+)", error)
    if dimensions is not None:
        width, height = map(int, dimensions.groups())
        return width < min_width or height < min_height
    # Filesystem access and other environmental failures can recover without a
    # change to the file's size or mtime. Retry those instead of caching them.
    return False


def _find_moved_image(conn, sha256, size_bytes, roots, claimed_ids):
    found = None
    for row in conn.execute(_SCAN_ROW_SQL + " WHERE i.sha256=? AND i.size_bytes=?",
                            (sha256, size_bytes)):
        if row["id"] in claimed_ids:
            continue
        old_path = Path(row["path"])
        if any(lexical_path_is_within(old_path, root) for root in roots) and _path_is_missing(old_path):
            if found is not None:
                return None  # More than one missing copy: preserve the ambiguity.
            found = row
    return found


def _refresh_model_config_assignments(
    conn: sqlite3.Connection,
    image_rows: list[sqlite3.Row],
    model_paths: ModelPathConfig,
) -> None:
    iterator = iter(image_rows)
    while batch := list(islice(iterator, 400)):
        _refresh_model_batch(conn, batch, model_paths)


def _refresh_model_batch(conn, image_rows, model_paths):
    placeholders = ",".join("?" for _ in image_rows)
    current = {
        int(row["image_id"]): row
        for row in conn.execute(
            f"""
            SELECT ip.image_id, ip.model_type, ip.configured_model,
                   ip.model_config_root, ip.model_config_rule_id,
                   COUNT(pt.image_id) AS category_tag_count,
                   MIN(pt.tag_norm) AS category_tag_norm, MIN(pt.tag_kind) AS category_tag_kind
            FROM image_prompts ip
            LEFT JOIN prompt_tags pt ON pt.image_id=ip.image_id
                AND pt.tag_kind IN ('model_type', 'model_config')
            WHERE ip.image_id IN ({placeholders})
            GROUP BY ip.image_id
            """, [row["id"] for row in image_rows]
        )
    }
    resolve_rule = _model_rule_resolver(model_paths)
    for image in image_rows:
        image_id = int(image["id"])
        prompt_row = current.get(image_id)
        if prompt_row is None:
            continue
        image_path = Path(image["path"])
        rule = resolve_rule(image_path)
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
            if not _path_is_missing(row_path):
                continue
        else:
            if not any(path_is_within(row_path, root) for root in directory_roots):
                continue
            if str(row_path) in known_paths or not _path_is_missing(row_path):
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
    deleted = 0
    iterator = iter(_image_batches(conn))
    while rows := list(islice(iterator, 400)):
        image_ids = [int(row["id"]) for row in rows if _path_is_missing(Path(row["path"]))]
        deleted += delete_images(conn, image_ids)
    return deleted


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
    if model_identity:
        model_identity = sanitize_model_name(model_identity)

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
