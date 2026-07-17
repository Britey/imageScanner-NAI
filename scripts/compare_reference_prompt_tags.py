"""Compare the local 30-image corpus with the reviewed prompt-tag manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from imgdupe.nai_prompt import extract_nai_prompt


DEFAULT_MANIFEST = PROJECT_ROOT / "tests" / "fixtures" / "reference_prompt_tags.json"
DEFAULT_REFERENCE_ROOT = PROJECT_ROOT / "参考图示例"


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        manifest = json.load(file)
    if manifest.get("schema_version") != 2 or not isinstance(manifest.get("images"), list):
        raise ValueError("unsupported or malformed reference manifest")
    return manifest


def compare_manifest(manifest_path: Path, reference_root: Path) -> list[str]:
    manifest = load_manifest(manifest_path)
    mismatches: list[str] = []
    expected_paths = {entry["path"] for entry in manifest["images"]}
    actual_paths = {
        path.relative_to(reference_root).as_posix()
        for path in reference_root.rglob("*.png")
    }
    for missing in sorted(expected_paths - actual_paths):
        mismatches.append(f"{missing}: reference image is missing")
    for unexpected in sorted(actual_paths - expected_paths):
        mismatches.append(f"{unexpected}: image is absent from the manifest")

    for entry in manifest["images"]:
        relative_path = entry["path"]
        image_path = reference_root / Path(relative_path)
        if not image_path.is_file():
            continue
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            mismatches.append(
                f"{relative_path}: SHA-256 changed "
                f"(expected {entry['sha256']}, got {digest})"
            )
            continue

        result = extract_nai_prompt(image_path)
        scalar_fields = {
            "prompt_format": result.prompt_format,
            "detected_model_type": result.model_type,
            "model_version": result.model_version,
            "model_hash": result.model_hash,
        }
        for field, actual in scalar_fields.items():
            if actual != entry[field]:
                mismatches.append(
                    f"{relative_path}: {field} expected {entry[field]!r}, got {actual!r}"
                )
        actual_tags = list(result.tags)
        if actual_tags != entry["tags"]:
            expected_tags = entry["tags"]
            missing_tags = [tag for tag in expected_tags if tag not in actual_tags]
            extra_tags = [tag for tag in actual_tags if tag not in expected_tags]
            order_only = not missing_tags and not extra_tags
            detail = (
                "same tags in a different order"
                if order_only
                else f"missing={missing_tags!r}, extra={extra_tags!r}"
            )
            mismatches.append(f"{relative_path}: tag mismatch ({detail})")
    return mismatches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.reference_root.is_dir():
        print(f"Reference folder not found: {args.reference_root}")
        return 2
    try:
        mismatches = compare_manifest(args.manifest, args.reference_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not compare reference prompts: {exc}")
        return 2
    if mismatches:
        print(f"Reference comparison failed with {len(mismatches)} mismatch(es):")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    count = len(load_manifest(args.manifest)["images"])
    print(f"Reference comparison passed: {count} images matched exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
