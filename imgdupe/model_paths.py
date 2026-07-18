r"""Persistent model-label rules for configured filesystem paths.

The on-disk format uses numbered INI sections while storing an independent,
stable rule ID in each section::

    [rule.1]
    id = 8f39...
    path = D:\Images\Noob
    label = Noob XL

Using only ``=`` as the INI delimiter keeps Windows drive-letter colons safe.
Missing paths are retained: a temporarily offline drive must not erase a user's
configuration.
"""

from __future__ import annotations

import configparser
import os
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_CONFIG_FILENAME = "model_paths.ini"
DEFAULT_MODEL_PATHS_PATH = Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME
LEGACY_MARKER_FILENAME = ".model_type"
_COMMA_LIKE = frozenset(",，、､︐︑﹐﹑")
_PIPE_LIKE = frozenset("|｜")
_LEGACY_NAMESPACE = uuid.UUID("b52fca54-640a-47d2-a02a-d3fd89ac07f7")
_PATH_RULE_NAMESPACE = uuid.UUID("510ef268-e2c6-48c0-8c85-b7b13bb680da")


@dataclass(frozen=True, slots=True)
class ModelPathRule:
    rule_id: str
    path: Path
    label: str


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    section: str
    message: str


@dataclass(frozen=True, slots=True)
class ModelPathConfig:
    rules: tuple[ModelPathRule, ...] = ()
    issues: tuple[ConfigIssue, ...] = ()

    def resolve(self, candidate: Path | str) -> ModelPathRule | None:
        """Return the rule at the closest configured ancestor of ``candidate``."""

        path = canonical_path(candidate)
        matches = [rule for rule in self.rules if path_is_within(path, rule.path)]
        if not matches:
            return None
        # Canonical duplicate paths are removed while loading/constructing.  The
        # ID tie-breaker nevertheless makes manually constructed configs stable.
        return max(matches, key=lambda rule: (_path_depth(rule.path), rule.rule_id))

    def scan_roots(self) -> tuple[Path, ...]:
        """Enumerate unique configured paths, including missing/offline paths."""

        seen: set[str] = set()
        roots: list[Path] = []
        for rule in self.rules:
            key = path_key(rule.path)
            if key not in seen:
                seen.add(key)
                roots.append(rule.path)
        return tuple(roots)

    def deduplicated_rescan_roots(self) -> tuple[Path, ...]:
        """Return the minimal roots needed to rescan all configured paths.

        When one configured root contains another, only the outer root is
        returned.  Existence is deliberately not checked.
        """

        selected: list[Path] = []
        for root in sorted(self.scan_roots(), key=lambda item: (_path_depth(item), path_key(item))):
            if not any(path_is_within(root, outer) for outer in selected):
                selected.append(root)
        return tuple(selected)


def make_rule(path: Path | str, label: str, rule_id: str | None = None) -> ModelPathRule:
    """Validate and construct a rule with a stable UUID identifier."""

    normalized_label = validate_label(label)
    normalized_id = _validate_rule_id(rule_id or str(uuid.uuid4()))
    return ModelPathRule(normalized_id, canonical_path(path), normalized_label)


def build_config(rules: Iterable[ModelPathRule]) -> ModelPathConfig:
    """Build a validated config, rejecting duplicate paths or stable IDs."""

    result: list[ModelPathRule] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for supplied in rules:
        rule = make_rule(supplied.path, supplied.label, supplied.rule_id)
        key = path_key(rule.path)
        if rule.rule_id in ids:
            raise ValueError(f"duplicate model-path rule ID: {rule.rule_id}")
        if key in paths:
            raise ValueError(f"duplicate model-path rule path: {rule.path}")
        ids.add(rule.rule_id)
        paths.add(key)
        result.append(rule)
    return ModelPathConfig(tuple(result))


def load_model_paths(path: Path | str) -> ModelPathConfig:
    """Load valid rules and report invalid/duplicate sections non-fatally."""

    config_path = Path(path)
    if not config_path.exists():
        return ModelPathConfig()

    parser = _new_parser()
    try:
        with config_path.open("r", encoding="utf-8-sig") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error, UnicodeError) as exc:
        return ModelPathConfig(issues=(ConfigIssue("<file>", str(exc)),))

    rules: list[ModelPathRule] = []
    issues: list[ConfigIssue] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for section in parser.sections():
        if not section.casefold().startswith("rule."):
            continue
        try:
            configured_path = parser.get(section, "path")
            configured_id = parser.get(section, "id", fallback="").strip()
            rule = make_rule(
                configured_path,
                parser.get(section, "label"),
                configured_id or str(uuid.uuid5(_PATH_RULE_NAMESPACE, path_key(configured_path))),
            )
            key = path_key(rule.path)
            if rule.rule_id in seen_ids:
                raise ValueError(f"duplicate rule ID {rule.rule_id}")
            if key in seen_paths:
                raise ValueError(f"duplicate configured path {rule.path}")
        except (configparser.Error, ValueError) as exc:
            issues.append(ConfigIssue(section, str(exc)))
            continue
        seen_ids.add(rule.rule_id)
        seen_paths.add(key)
        rules.append(rule)
    return ModelPathConfig(tuple(rules), tuple(issues))


def save_model_paths(path: Path | str, config: ModelPathConfig | Sequence[ModelPathRule]) -> None:
    """Atomically write path rules without checking whether their paths exist."""

    rules = config.rules if isinstance(config, ModelPathConfig) else tuple(config)
    validated = build_config(rules)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parser = _new_parser()
    for index, rule in enumerate(validated.rules, 1):
        section = f"rule.{index}"
        parser.add_section(section)
        parser.set(section, "id", rule.rule_id)
        parser.set(section, "path", str(rule.path))
        parser.set(section, "label", rule.label)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            parser.write(stream, space_around_delimiters=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def discover_legacy_markers(scan_roots: Iterable[Path | str]) -> tuple[ModelPathRule, ...]:
    """Discover legacy markers below existing roots for a one-time import.

    Missing/offline roots are skipped.  Invalid and unreadable markers are left
    untouched and omitted; this helper never deletes or edits marker files.
    """

    discovered: dict[str, ModelPathRule] = {}
    for supplied_root in scan_roots:
        root = canonical_path(supplied_root)
        if not root.is_dir():
            continue
        candidates = [root / LEGACY_MARKER_FILENAME]
        try:
            candidates.extend(root.rglob(LEGACY_MARKER_FILENAME))
            for marker in candidates:
                label = _read_legacy_label(marker)
                if label is None:
                    continue
                directory = canonical_path(marker.parent)
                key = path_key(directory)
                stable_id = str(uuid.uuid5(_LEGACY_NAMESPACE, f"{key}\0{label}"))
                discovered.setdefault(key, make_rule(directory, label, stable_id))
        except OSError:
            # An offline subtree or permissions failure must not invalidate
            # discoveries from other roots.
            continue
    return tuple(discovered[key] for key in sorted(discovered))


def import_legacy_markers(
    config: ModelPathConfig,
    scan_roots: Iterable[Path | str],
) -> tuple[ModelPathConfig, tuple[ModelPathRule, ...]]:
    """Add non-duplicate legacy discoveries and return what was imported."""

    existing_paths = {path_key(rule.path) for rule in config.rules}
    existing_ids = {rule.rule_id for rule in config.rules}
    imported: list[ModelPathRule] = []
    for rule in discover_legacy_markers(scan_roots):
        if path_key(rule.path) in existing_paths or rule.rule_id in existing_ids:
            continue
        imported.append(rule)
        existing_paths.add(path_key(rule.path))
        existing_ids.add(rule.rule_id)
    combined = build_config((*config.rules, *imported))
    return ModelPathConfig(combined.rules, config.issues), tuple(imported)


def validate_label(label: str) -> str:
    value = label.strip()
    if not value:
        raise ValueError("model label must not be empty")
    for character in value:
        if character in _COMMA_LIKE:
            raise ValueError("model label must not contain comma-like query separators")
        if character in _PIPE_LIKE:
            raise ValueError("model label must not contain pipe operators")
        category = unicodedata.category(character)
        if character in "\r\n" or category in {"Cc", "Cs", "Zl", "Zp"}:
            raise ValueError("model label must not contain newline or control characters")
    return value


def canonical_path(path: Path | str) -> Path:
    value = str(path).strip()
    if not value:
        raise ValueError("configured path must not be empty")
    return Path(value).expanduser().resolve(strict=False)


def path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(canonical_path(path))))


def path_is_within(path: Path | str, ancestor: Path | str) -> bool:
    candidate_key = path_key(path)
    ancestor_key = path_key(ancestor)
    try:
        return os.path.normcase(os.path.commonpath((candidate_key, ancestor_key))) == ancestor_key
    except ValueError:
        return False


def _path_depth(path: Path) -> int:
    return len(path.parts)


def _validate_rule_id(rule_id: str) -> str:
    value = rule_id.strip()
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid model-path rule ID: {rule_id!r}") from exc


def _new_parser() -> configparser.ConfigParser:
    return configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=True)


def _read_legacy_label(marker: Path) -> str | None:
    try:
        text = marker.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        try:
            return validate_label(candidate)
        except ValueError:
            return None
    return None
