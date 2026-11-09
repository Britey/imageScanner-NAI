from __future__ import annotations

from pathlib import Path
import os

from .config import IMAGE_EXTENSIONS


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def iter_image_paths(roots: list[Path], *, on_error=None):
    for root in roots:
        root = root.resolve()
        if root.is_file():
            if is_image_path(root):
                yield root
            continue
        for directory, _subdirectories, filenames in os.walk(root, onerror=on_error):
            for filename in filenames:
                path = Path(directory) / filename
                if is_image_path(path) and path.is_file():
                    # Ordinary files are already below a canonical root. Only
                    # symlinks need the filesystem resolution used by the index.
                    yield path.resolve() if path.is_symlink() else path


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
