from __future__ import annotations

from pathlib import Path

from .config import IMAGE_EXTENSIONS


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def iter_image_paths(roots: list[Path]):
    for root in roots:
        root = root.resolve()
        if root.is_file():
            if is_image_path(root):
                yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and is_image_path(path):
                yield path.resolve()


def utc_now_sql() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
