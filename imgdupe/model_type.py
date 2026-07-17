"""Resolve inherited ``.model_type`` markers for scanned images.

The resolver deliberately handles only explicit folder markers.  Metadata-based
fallback classification belongs to the prompt metadata layer, which can use a
``not_found`` result to select ``nai3``, ``nai4``, or the default ``sd``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MODEL_TYPE_FILENAME = ".model_type"
VALID_MODEL_TYPES = frozenset({"sd", "noob", "nai3", "nai4"})
MarkerStatus = Literal["found", "not_found", "invalid_model_marker"]


@dataclass(frozen=True, slots=True)
class ModelTypeResolution:
    """The effective explicit marker for one directory or image.

    ``marker_signature`` is a digest of the marker bytes.  Together with the
    canonical marker path and model type, it is suitable classification
    provenance for detecting marker changes between scans.
    """

    status: MarkerStatus
    model_type: str | None = None
    marker_path: Path | None = None
    marker_signature: str | None = None
    error: str | None = None

    @property
    def is_valid_marker(self) -> bool:
        return self.status == "found"


class ModelTypeResolver:
    """Resolve the closest marker at or below one canonical scan root.

    Directory results are cached for the lifetime of the resolver, making the
    object a per-scan snapshot.  Call :meth:`clear_cache` before reusing it for
    a later scan so marker additions, edits, and deletions are observed.
    """

    def __init__(self, scan_root: Path | str) -> None:
        self.scan_root = _canonical_path(scan_root)
        self._root_key = _path_key(self.scan_root)
        self._directory_cache: dict[str, ModelTypeResolution] = {}

    def resolve_for_image(self, image_path: Path | str) -> ModelTypeResolution:
        """Return the marker inherited by ``image_path``.

        The image need not exist, but its canonical parent must be contained by
        the configured scan root.
        """

        image = _canonical_path(image_path)
        return self.resolve_directory(image.parent)

    def resolve_directory(self, directory: Path | str) -> ModelTypeResolution:
        """Return the closest marker applying to ``directory``."""

        canonical_directory = _canonical_path(directory)
        self._require_within_root(canonical_directory)
        key = _path_key(canonical_directory)
        cached = self._directory_cache.get(key)
        if cached is not None:
            return cached

        if key == self._root_key:
            inherited = _NOT_FOUND
        else:
            inherited = self.resolve_directory(canonical_directory.parent)

        marker_path = canonical_directory / MODEL_TYPE_FILENAME
        if marker_path.exists():
            resolution = _read_marker(marker_path)
        else:
            resolution = inherited

        self._directory_cache[key] = resolution
        return resolution

    def clear_cache(self) -> None:
        """Discard the per-scan directory snapshot."""

        self._directory_cache.clear()

    @property
    def cached_directory_count(self) -> int:
        """Number of directory resolutions cached (primarily diagnostic)."""

        return len(self._directory_cache)

    def _require_within_root(self, path: Path) -> None:
        try:
            common = os.path.commonpath((str(self.scan_root), str(path)))
        except ValueError as exc:
            raise ValueError(f"path is outside scan root: {path}") from exc
        if _path_key(Path(common)) != self._root_key:
            raise ValueError(f"path is outside scan root: {path}")


_NOT_FOUND = ModelTypeResolution(status="not_found")


def _read_marker(marker_path: Path) -> ModelTypeResolution:
    canonical_marker = _canonical_path(marker_path)
    try:
        raw = canonical_marker.read_bytes()
    except OSError as exc:
        return ModelTypeResolution(
            status="invalid_model_marker",
            marker_path=canonical_marker,
            error=f"could not read model marker: {exc}",
        )

    signature = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return ModelTypeResolution(
            status="invalid_model_marker",
            marker_path=canonical_marker,
            marker_signature=signature,
            error=f"model marker is not valid UTF-8: {exc}",
        )

    value: str | None = None
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        value = candidate.casefold()
        break

    if value is None:
        return ModelTypeResolution(
            status="invalid_model_marker",
            marker_path=canonical_marker,
            marker_signature=signature,
            error="model marker contains no model type",
        )
    if value not in VALID_MODEL_TYPES:
        return ModelTypeResolution(
            status="invalid_model_marker",
            marker_path=canonical_marker,
            marker_signature=signature,
            error=f"unknown model type: {value}",
        )
    return ModelTypeResolution(
        status="found",
        model_type=value,
        marker_path=canonical_marker,
        marker_signature=signature,
    )


def _canonical_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))
