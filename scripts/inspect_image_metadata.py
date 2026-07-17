"""Print a bounded, prompt-focused summary of image metadata via ExifTool.

This helper intentionally keeps large arrays and unrelated generation payloads out
of terminal/chat output. It requests only a few container fields from ExifTool,
then emits prompt-like JSON leaves and a compact A1111 parameters summary.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROMPT_FIELD_NAMES = {
    "actual_prompt",
    "base_caption",
    "caption",
    "char_caption",
    "negative_prompt",
    "prompt",
    "uc",
}
SETTINGS_FIELD_PATTERN = re.compile(
    r"(?:^|,\s*)(Model hash|Model|Steps|Seed):\s*([^,\r\n]+)",
    re.IGNORECASE,
)
NEGATIVE_MARKER = re.compile(r"(?im)^\s*Negative prompt:\s*")
SETTINGS_MARKER = re.compile(r"(?im)^\s*Steps:\s*\d+\s*,")


def clipped_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}... <{omitted} chars omitted>"


def bounded_scalar(value: Any, string_limit: int) -> Any:
    if isinstance(value, str):
        return clipped_text(value, string_limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clipped_text(repr(value), string_limit)


def collect_prompt_fields(
    value: Any,
    *,
    path: str = "",
    output: dict[str, Any],
    string_limit: int,
    field_limit: int,
    depth: int = 0,
) -> None:
    if depth > 32 or len(output) >= field_limit:
        return

    if isinstance(value, dict):
        for key, child in value.items():
            if len(output) >= field_limit:
                break
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            key_norm = key_text.casefold()
            if key_norm in PROMPT_FIELD_NAMES and not isinstance(child, (dict, list)):
                output[child_path] = bounded_scalar(child, string_limit)
            else:
                collect_prompt_fields(
                    child,
                    path=child_path,
                    output=output,
                    string_limit=string_limit,
                    field_limit=field_limit,
                    depth=depth + 1,
                )
        return

    if isinstance(value, list):
        for index, child in enumerate(value[:field_limit]):
            if len(output) >= field_limit:
                break
            collect_prompt_fields(
                child,
                path=f"{path}[{index}]",
                output=output,
                string_limit=string_limit,
                field_limit=field_limit,
                depth=depth + 1,
            )


def summarize_comment(comment: Any, string_limit: int, field_limit: int) -> dict[str, Any]:
    if not isinstance(comment, str):
        return {}
    try:
        parsed = json.loads(comment)
    except json.JSONDecodeError:
        return {"comment_preview": clipped_text(comment, string_limit)}
    if not isinstance(parsed, dict):
        return {"comment_preview": bounded_scalar(parsed, string_limit)}

    prompt_fields: dict[str, Any] = {}
    collect_prompt_fields(
        parsed,
        output=prompt_fields,
        string_limit=string_limit,
        field_limit=field_limit,
    )
    keys = [str(key) for key in parsed.keys()]
    return {
        "comment_keys": keys[:field_limit],
        "comment_key_count": len(keys),
        "prompt_fields": prompt_fields,
        "prompt_fields_limited": len(prompt_fields) >= field_limit,
    }


def summarize_parameters(parameters: Any, string_limit: int) -> dict[str, Any]:
    if not isinstance(parameters, str):
        return {}

    settings_match = SETTINGS_MARKER.search(parameters)
    prompt_area = parameters[: settings_match.start()] if settings_match else parameters
    negative_match = NEGATIVE_MARKER.search(prompt_area)
    if negative_match:
        positive = prompt_area[: negative_match.start()].strip()
        negative = prompt_area[negative_match.end() :].strip()
    else:
        positive = prompt_area.strip()
        negative = ""

    selected_settings = {
        match.group(1).casefold().replace(" ", "_"): match.group(2).strip()
        for match in SETTINGS_FIELD_PATTERN.finditer(parameters)
    }
    result: dict[str, Any] = {
        "positive_prompt": clipped_text(positive, string_limit),
        "settings": selected_settings,
    }
    if negative:
        result["negative_prompt"] = clipped_text(negative, string_limit)
    return result


def run_exiftool(exiftool: Path, paths: list[Path]) -> list[dict[str, Any]]:
    command = [
        str(exiftool),
        "-json",
        "-Software",
        "-Source",
        "-Comment",
        "-Parameters",
        "-Description",
    ]
    if any(path.is_dir() for path in paths):
        command.extend(["-r", "-ext", "png", "-ext", "jpg", "-ext", "jpeg", "-ext", "webp"])
    command.extend(str(path) for path in paths)

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode not in (0, 1):
        stderr = clipped_text(completed.stderr.strip(), 2000)
        raise RuntimeError(f"ExifTool failed with exit code {completed.returncode}: {stderr}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ExifTool returned invalid JSON: {exc}") from exc
    if not isinstance(result, list):
        raise RuntimeError("ExifTool JSON output was not a list")
    return [item for item in result if isinstance(item, dict)]


def build_summary(item: dict[str, Any], string_limit: int, field_limit: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "file": item.get("SourceFile"),
        "software": item.get("Software"),
        "source": item.get("Source"),
    }
    summary.update(summarize_comment(item.get("Comment"), string_limit, field_limit))
    parameters = summarize_parameters(item.get("Parameters"), string_limit)
    if parameters:
        summary["parameters"] = parameters
    description = item.get("Description")
    if isinstance(description, str) and description.strip():
        summary["description"] = clipped_text(description, string_limit)
    return {key: value for key, value in summary.items() if value not in (None, {}, [])}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Image files or directories to inspect")
    parser.add_argument(
        "--exiftool",
        type=Path,
        default=Path("参考图示例/exiftool.exe"),
        help="Path to exiftool executable",
    )
    parser.add_argument(
        "--max-string",
        type=int,
        default=1200,
        help="Maximum characters emitted for one text value (80-10000)",
    )
    parser.add_argument(
        "--max-fields",
        type=int,
        default=80,
        help="Maximum prompt-like JSON fields emitted per image (1-500)",
    )
    args = parser.parse_args(argv)
    args.max_string = min(10_000, max(80, args.max_string))
    args.max_fields = min(500, max(1, args.max_fields))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        items = run_exiftool(args.exiftool, args.paths)
        summaries = [
            build_summary(item, args.max_string, args.max_fields)
            for item in items
        ]
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(summaries, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
