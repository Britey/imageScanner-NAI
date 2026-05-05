from __future__ import annotations

import gzip
import json
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


STEALTH_SIGNATURES = {
    "stealth_pngcomp": True,
    "stealth_pnginfo": False,
}
TEXT_KEYS = ("Comment", "Description", "parameters", "prompt")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SPLIT_PATTERN = re.compile(r"[,，\n]+")
SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class PromptResult:
    prompt: str | None
    metadata_source: str | None
    error: str | None = None


def extract_nai_prompt(path: Path, *, stealth_fallback: bool = True) -> PromptResult:
    try:
        metadata: dict[str, Any] | None
        source: str | None
        if path.suffix.lower() == ".png":
            metadata, source = metadata_from_text_values(read_png_text_chunks(path))
        else:
            metadata, source = read_pil_text_metadata(path)

        if not metadata and stealth_fallback and path.suffix.lower() == ".png":
            with Image.open(path) as image:
                metadata = read_stealth_metadata(image)
            source = metadata.get("_metadata_source") if metadata else None

        if not metadata:
            return PromptResult(prompt=None, metadata_source=None, error="no_metadata")

        prompt = (
            metadata.get("prompt")
            or metadata.get("v4_prompt", {}).get("caption", {}).get("base_caption")
            or metadata.get("Description")
        )
        if not isinstance(prompt, str) or not prompt.strip():
            return PromptResult(prompt=None, metadata_source=source, error="no_prompt")

        return PromptResult(prompt=prompt, metadata_source=source, error=None)
    except Exception as exc:
        return PromptResult(
            prompt=None,
            metadata_source=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def split_prompt_tags(prompt: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in SPLIT_PATTERN.split(prompt):
        tag = normalize_prompt_tag(raw_tag)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def normalize_prompt_tag(tag: str) -> str:
    tag = SPACE_PATTERN.sub(" ", tag.strip())
    return tag.casefold()


def bits_to_bytes(bits: list[int]) -> bytes:
    if len(bits) % 8:
        bits = bits[: len(bits) - (len(bits) % 8)]
    return bytes(
        int("".join(str(bit) for bit in bits[index : index + 8]), 2)
        for index in range(0, len(bits), 8)
    )


def read_stealth_metadata(image: Image.Image) -> dict[str, Any] | None:
    import numpy as np

    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba, dtype=np.uint8)[:, :, 3]
    bit_streams = (
        (alpha.T.reshape(-1) & 1).astype(np.uint8).tolist(),
        (alpha.reshape(-1) & 1).astype(np.uint8).tolist(),
    )

    max_signature_bits = max(len(sig) for sig in STEALTH_SIGNATURES) * 8
    for bits in bit_streams:
        signature_prefix = bits_to_bytes(bits[:max_signature_bits]).decode(
            "utf-8", errors="ignore"
        )
        signature = next(
            (sig for sig in STEALTH_SIGNATURES if signature_prefix.startswith(sig)),
            None,
        )
        if not signature:
            continue

        offset = len(signature) * 8
        length_bits = bits[offset : offset + 32]
        if len(length_bits) != 32:
            continue

        payload_bit_length = int("".join(str(bit) for bit in length_bits), 2)
        offset += 32
        payload = bits_to_bytes(bits[offset : offset + payload_bit_length])
        if STEALTH_SIGNATURES[signature]:
            payload = gzip.decompress(payload)

        text = payload.decode("utf-8")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = {"prompt": text}
        if not isinstance(value, dict):
            return None
        value["_metadata_source"] = signature
        return value

    return None


def parse_jsonish_text(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def decode_png_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_png_itxt(data: bytes) -> tuple[str, str] | None:
    parts = data.split(b"\x00", 5)
    if len(parts) != 6:
        return None

    keyword_raw, compression_flag, compression_method, _, _, text_raw = parts
    if compression_flag not in (b"\x00", b"\x01"):
        return None
    if compression_flag == b"\x01":
        if compression_method != b"\x00":
            return None
        text_raw = zlib.decompress(text_raw)

    return decode_png_text(keyword_raw), decode_png_text(text_raw)


def read_png_text_chunks(path: Path) -> dict[str, str]:
    chunks: dict[str, str] = {}
    with path.open("rb") as file:
        if file.read(8) != PNG_SIGNATURE:
            return chunks

        while True:
            length_raw = file.read(4)
            if len(length_raw) != 4:
                break

            length = int.from_bytes(length_raw, "big")
            chunk_type = file.read(4)
            if len(chunk_type) != 4:
                break

            if chunk_type not in {b"tEXt", b"zTXt", b"iTXt"}:
                file.seek(length + 4, 1)
                if chunk_type == b"IEND":
                    break
                continue

            data = file.read(length)
            file.seek(4, 1)
            try:
                if chunk_type == b"tEXt":
                    keyword_raw, text_raw = data.split(b"\x00", 1)
                    chunks[decode_png_text(keyword_raw)] = decode_png_text(text_raw)
                elif chunk_type == b"zTXt":
                    keyword_raw, rest = data.split(b"\x00", 1)
                    compression_method = rest[:1]
                    if compression_method == b"\x00":
                        chunks[decode_png_text(keyword_raw)] = decode_png_text(
                            zlib.decompress(rest[1:])
                        )
                elif chunk_type == b"iTXt":
                    parsed = parse_png_itxt(data)
                    if parsed:
                        key, value = parsed
                        chunks[key] = value
            except Exception:
                continue

            if chunk_type == b"IEND":
                break

    return chunks


def metadata_from_text_values(values: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    for key in TEXT_KEYS:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            continue

        parsed = parse_jsonish_text(value)
        if parsed:
            return parsed, f"text:{key}"

        if key.lower() == "prompt" or key == "Description":
            return {"prompt": value}, f"text:{key}"

    return None, None


def read_pil_text_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    with Image.open(path) as image:
        values = {
            key: value
            for key, value in image.info.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    return metadata_from_text_values(values)
