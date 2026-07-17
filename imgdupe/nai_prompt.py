from __future__ import annotations

import json
import re
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


STEALTH_SIGNATURES = {"stealth_pngcomp": True, "stealth_pnginfo": False}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_NESTING = 64
MAX_TEXT_CHUNK_BYTES = 4 * 1024 * 1024
NOVELAI_RAW_FIELDS = (
    "Software",
    "Source",
    "prompt",
    "uc",
    "v4_prompt",
    "v4_negative_prompt",
    "actual_prompts",
)
PROMPT_PARSER_VERSION = 3


class PromptMetadataTooLarge(ValueError):
    pass

COMMA_SEPARATORS = ",，、､︐︑﹐﹑;；︔﹔\n\r\x85\u2028\u2029"
CJK_FULL_STOPS = "。．｡︒﹒"
PIPE_CHARS = "|｜"
SEPARATOR_TRANSLATION = str.maketrans(
    {character: "," for character in COMMA_SEPARATORS + CJK_FULL_STOPS + PIPE_CHARS}
)
SPACE_PATTERN = re.compile(r"\s+")
LORA_PATTERN = re.compile(r"<\s*lora\s*:[^<>]*>", re.IGNORECASE)
SETTINGS_PATTERN = re.compile(
    r"(?im)^\s*Steps\s*[:：﹕]\s*[+\-－−]?\d+(?:[.．]\d+)?\s*[,，]"
)
NEGATIVE_PATTERN = re.compile(r"(?im)^\s*Negative\s+prompt\s*[:：﹕]\s*")
MODEL_HASH_PATTERN = re.compile(
    r"(?i)(?:^|[,，]\s*)Model\s+hash\s*[:：﹕]\s*([0-9a-f]+)(?=\s*(?:[,，]|$))"
)
SOURCE_HASH_PATTERN = re.compile(r"(?i)([0-9a-f]{6,})\s*$")
NUMBER_PATTERN = re.compile(r"^[+\-]?\d+(?:\.\d+)?$")
TRAILING_MIX_WEIGHT = re.compile(r"(?s)^(.*?):([+\-]?\d+(?:\.\d+)?)\s*$")

SYNTAX_TRANSLATION = str.maketrans(
    {
        "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}",
        "：": ":", "﹕": ":", "｜": "|",
    }
)
NUMERIC_TRANSLATION = str.maketrans(
    {**{chr(0xFF10 + index): str(index) for index in range(10)}, "－": "-", "−": "-", "．": "."}
)

# Private-use sentinels protect escaped punctuation while structural syntax is parsed.
ESCAPED = {character: chr(0xE000 + index) for index, character in enumerate("()[]{}|,;<>\\")}
UNESCAPED = {value: key for key, value in ESCAPED.items()}


@dataclass(frozen=True)
class PromptResult:
    prompt: str | None
    metadata_source: str | None
    error: str | None = None
    prompt_format: str | None = None
    model_type: str = "sd"
    model_hash: str | None = None
    tags: tuple[str, ...] = ()
    raw_metadata: str | None = None
    model_version: str | None = None


def extract_nai_prompt(path: Path, *, stealth_fallback: bool = True) -> PromptResult:
    """Compatibility entry point; now extracts NovelAI and A1111 prompt metadata."""
    try:
        values: dict[str, str] = {}
        if path.suffix.lower() == ".png":
            values = read_png_text_chunks(path)
        else:
            values = read_pil_text_values(path)

        result = parse_text_metadata(values)
        if result.error == "no_metadata" and stealth_fallback and path.suffix.lower() == ".png":
            with Image.open(path) as image:
                stealth = read_stealth_metadata(image)
            if stealth:
                source = stealth.pop("_metadata_source", "stealth")
                return parse_novelai_metadata(stealth, metadata_source=source)
        return result
    except PromptMetadataTooLarge:
        return PromptResult(None, None, "prompt_metadata_too_large")
    except Exception as exc:
        return PromptResult(None, None, f"{type(exc).__name__}: {exc}")


extract_prompt = extract_nai_prompt


def parse_text_metadata(values: dict[str, str]) -> PromptResult:
    software = _get_ci(values, "Software")
    if isinstance(software, str) and software.strip().casefold() == "novelai":
        comment = _get_ci(values, "Comment")
        if not isinstance(comment, str) or not comment.strip():
            return PromptResult(None, "text:Comment", "no_prompt", "novelai", "nai3")
        metadata = parse_jsonish_text(comment)
        if metadata is None:
            return PromptResult(None, "text:Comment", "malformed_metadata", "novelai", "nai3", raw_metadata=comment)
        source_value = _get_ci(values, "Source")
        if source_value and "Source" not in metadata:
            metadata["Source"] = source_value
        return parse_novelai_metadata(metadata, metadata_source="text:Comment", raw_metadata=comment)

    parameters = _get_ci(values, "parameters")
    if isinstance(parameters, str) and parameters.strip():
        return parse_a1111_parameters(parameters, metadata_source="text:parameters")

    # Old/stealth exports may expose the NovelAI JSON without Software.
    for key in ("Comment", "Description", "prompt"):
        value = _get_ci(values, key)
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = parse_jsonish_text(value)
        if parsed is not None:
            return parse_novelai_metadata(parsed, metadata_source=f"text:{key}", raw_metadata=value)
        if key.casefold() in {"description", "prompt"}:
            tags = tuple(split_prompt_tags(value))
            return _result_from_tags(tags, f"text:{key}", "novelai", "nai3", None, value)
    return PromptResult(None, None, "no_metadata", model_type="sd")


def parse_a1111_parameters(parameters: str, *, metadata_source: str = "parameters") -> PromptResult:
    normalized = parameters.translate(SYNTAX_TRANSLATION)
    settings_match = SETTINGS_PATTERN.search(normalized)
    settings = normalized[settings_match.start():] if settings_match else ""
    prompt_part = normalized[:settings_match.start()] if settings_match else normalized
    hash_match = MODEL_HASH_PATTERN.search(settings)
    model_hash = hash_match.group(1).casefold() if hash_match else None

    negative_match = NEGATIVE_PATTERN.search(prompt_part)
    positive = prompt_part[:negative_match.start()] if negative_match else prompt_part
    positive = LORA_PATTERN.sub(",", positive)
    positive = re.sub(r"(?<!\w)(?:BREAK|AND)(?!\w)", ",", positive)
    nesting_error = "prompt_nesting_too_deep" if _has_excessive_nesting(positive) else None
    expanded = _parse_a1111_fragment(_protect_escapes(positive))
    tags = tuple(split_prompt_tags(_restore_escapes(expanded)))
    return _result_from_tags(
        tags, metadata_source, "a1111", "sd", model_hash, parameters,
        error=nesting_error,
    )


def parse_novelai_metadata(
    metadata: dict[str, Any], *, metadata_source: str = "novelai", raw_metadata: str | None = None
) -> PromptResult:
    source = metadata.get("Source") or metadata.get("source")
    model_hash = _model_hash_from_source(source)
    is_v4 = _is_nai4(metadata, source)
    model_type = "nai4" if is_v4 else "nai3"
    model_version = _novelai_model_version(source) if is_v4 else None

    actual = metadata.get("actual_prompts")
    actual_prompt = actual.get("prompt") if isinstance(actual, dict) else None
    texts: list[str] = []
    if actual_prompt is not None:
        texts = _caption_texts(actual_prompt)
    elif is_v4:
        texts = _caption_texts(metadata.get("v4_prompt"))
        if not texts and isinstance(metadata.get("prompt"), str):
            texts = [metadata["prompt"]]
    elif isinstance(metadata.get("prompt"), str):
        texts = [metadata["prompt"]]

    all_tags: list[str] = []
    seen: set[str] = set()
    nesting_error = None
    for text in texts:
        if _has_excessive_nesting(text.translate(SYNTAX_TRANSLATION)):
            nesting_error = "prompt_nesting_too_deep"
        parsed = parse_novelai_prompt(text, model_type=model_type)
        for tag in parsed:
            normalized = normalize_prompt_tag(tag)
            if normalized and normalized not in seen:
                seen.add(normalized)
                all_tags.append(tag)

    raw_metadata = json.dumps(
        {key: metadata[key] for key in NOVELAI_RAW_FIELDS if key in metadata},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if not all_tags and not texts:
        return PromptResult(
            None, metadata_source, "no_prompt", "novelai", model_type,
            model_hash, raw_metadata=raw_metadata, model_version=model_version,
        )
    return _result_from_tags(
        tuple(all_tags), metadata_source, "novelai", model_type, model_hash,
        raw_metadata, error=nesting_error, model_version=model_version,
    )


def parse_novelai_prompt(prompt: str, *, model_type: str = "nai4") -> list[str]:
    text = _protect_escapes(prompt.translate(SYNTAX_TRANSLATION))
    eligible = _filter_novelai_numeric_scopes(text)
    if model_type == "nai3":
        eligible = _filter_nai3_mix_segments(eligible)
    eligible = _strip_round_emphasis(eligible)
    eligible = eligible.translate(str.maketrans({"{": "", "}": "", "[": "", "]": ""}))
    return split_prompt_tags(_restore_escapes(eligible))


def split_prompt_tags(prompt: str) -> list[str]:
    text = prompt.translate(SEPARATOR_TRANSLATION)
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in text.split(","):
        tag = clean_prompt_tag(raw_tag)
        tag_norm = normalize_prompt_tag(tag)
        if not tag_norm or tag_norm in seen:
            continue
        seen.add(tag_norm)
        tags.append(tag)
    return tags


def clean_prompt_tag(tag: str) -> str:
    tag = SPACE_PATTERN.sub(" ", tag.replace("\u3000", " ").strip())
    tag = tag.lstrip(".").rstrip(".").strip()
    if not tag or all(character in COMMA_SEPARATORS + CJK_FULL_STOPS + PIPE_CHARS + "." for character in tag):
        return ""
    return tag


def normalize_prompt_tag(tag: str) -> str:
    cleaned = clean_prompt_tag(tag).replace("_", " ")
    return SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", cleaned)).casefold()


def _parse_a1111_fragment(text: str, depth: int = 0) -> str:
    if depth >= MAX_NESTING:
        return text
    output: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character not in "([":
            output.append(character)
            index += 1
            continue
        close = ")" if character == "(" else "]"
        end = _matching_delimiter(text, index, character, close)
        if end is None:
            output.append(text[index:])
            break
        inner = text[index + 1:end]
        if character == "[":
            pipe_parts = _split_top_level(inner, "|")
            colon_parts = _split_top_level(inner, ":")
            if len(pipe_parts) > 1:
                replacement = ",".join(_parse_a1111_fragment(part, depth + 1) for part in pipe_parts)
            elif len(colon_parts) in (2, 3) and _is_number(colon_parts[-1]):
                branches = colon_parts[:-1]
                replacement = ",".join(_parse_a1111_fragment(part, depth + 1) for part in branches if part.strip())
            else:
                replacement = _parse_a1111_fragment(inner, depth + 1)
        else:
            inner = _strip_final_weight(inner)
            replacement = _parse_a1111_fragment(inner, depth + 1)
        output.append(replacement)
        index = end + 1
    return "".join(output)


def _filter_novelai_numeric_scopes(text: str) -> str:
    output: list[str] = []
    scopes: list[float] = []
    index = 0
    while index < len(text):
        if text.startswith("::", index):
            if scopes:
                scopes.pop()
            index += 2
            continue
        delimiter = text.find("::", index)
        if delimiter >= 0:
            candidate = text[index:delimiter].strip().translate(NUMERIC_TRANSLATION)
            if _is_number(candidate):
                if len(scopes) < MAX_NESTING:
                    scopes.append(float(candidate))
                index = delimiter + 2
                continue
        eligible = bool(not scopes or (all(value != 0 for value in scopes) and sum(value < 0 for value in scopes) % 2 == 0))
        if eligible:
            output.append(text[index])
        index += 1
    return "".join(output)


def _filter_nai3_mix_segments(text: str) -> str:
    kept: list[str] = []
    for segment in text.split("|"):
        match = TRAILING_MIX_WEIGHT.match(segment)
        if match:
            segment, weight = match.groups()
            if float(weight) <= 0:
                continue
        kept.append(segment)
    return ",".join(kept)


def _strip_round_emphasis(text: str, depth: int = 0) -> str:
    if depth >= MAX_NESTING:
        return text
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "(":
            output.append(text[index])
            index += 1
            continue
        end = _matching_delimiter(text, index, "(", ")")
        if end is None:
            output.append(text[index:])
            break
        inner = _strip_final_weight(text[index + 1:end])
        output.append(_strip_round_emphasis(inner, depth + 1))
        index = end + 1
    return "".join(output)


def _caption_texts(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    caption = value.get("caption") if isinstance(value.get("caption"), dict) else value
    texts: list[str] = []
    base = caption.get("base_caption")
    if isinstance(base, str) and base.strip():
        texts.append(base)
    characters = caption.get("char_captions")
    if isinstance(characters, list):
        for item in characters:
            if isinstance(item, str) and item.strip():
                texts.append(item)
            elif isinstance(item, dict):
                candidate = item.get("char_caption") or item.get("caption")
                if isinstance(candidate, str) and candidate.strip():
                    texts.append(candidate)
    return texts


def _is_nai4(metadata: dict[str, Any], source: Any) -> bool:
    if isinstance(metadata.get("v4_prompt"), dict):
        return True
    return isinstance(source, str) and bool(re.search(r"(?i)\bV4(?:\.5)?\b", source))


def _model_hash_from_source(source: Any) -> str | None:
    if not isinstance(source, str):
        return None
    match = SOURCE_HASH_PATTERN.search(source)
    return match.group(1).casefold() if match else None


def _novelai_model_version(source: Any) -> str | None:
    if not isinstance(source, str):
        return None
    match = re.search(r"(?i)\bV(4(?:\.5)?)\b", source)
    return match.group(1) if match else None


def _result_from_tags(
    tags: Iterable[str], source: str, prompt_format: str, model_type: str,
    model_hash: str | None, raw_metadata: str | None, *, error: str | None = None,
    model_version: str | None = None,
) -> PromptResult:
    items = list(tags)
    prompt = ", ".join(items) if items else None
    status = error if error is not None else (None if items else "no_prompt")
    return PromptResult(
        prompt, source, status, prompt_format, model_type, model_hash,
        tuple(items), raw_metadata, model_version,
    )


def _has_excessive_nesting(text: str) -> bool:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for character in _protect_escapes(text):
        if character in "([{":
            stack.append(character)
            if len(stack) > MAX_NESTING:
                return True
        elif character in pairs and stack and stack[-1] == pairs[character]:
            stack.pop()
    return False


def _get_ci(values: dict[str, str], key: str) -> str | None:
    folded = key.casefold()
    return next((value for name, value in values.items() if name.casefold() == folded), None)


def _protect_escapes(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in ESCAPED:
            output.append(ESCAPED[text[index + 1]])
            index += 2
        else:
            output.append(text[index])
            index += 1
    return "".join(output)


def _restore_escapes(text: str) -> str:
    return "".join(UNESCAPED.get(character, character) for character in text)


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(text):
        if character in "([{" and len(stack) < MAX_NESTING:
            stack.append(character)
        elif character in pairs and stack and stack[-1] == pairs[character]:
            stack.pop()
        elif character == separator and not stack:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _strip_final_weight(text: str) -> str:
    parts = _split_top_level(text, ":")
    return ":".join(parts[:-1]) if len(parts) > 1 and _is_number(parts[-1]) else text


def _is_number(value: str) -> bool:
    return bool(NUMBER_PATTERN.fullmatch(value.strip().translate(NUMERIC_TRANSLATION)))


def bits_to_bytes(bits: list[int]) -> bytes:
    if len(bits) % 8:
        bits = bits[: len(bits) - len(bits) % 8]
    return bytes(int("".join(str(bit) for bit in bits[index:index + 8]), 2) for index in range(0, len(bits), 8))


def read_stealth_metadata(image: Image.Image) -> dict[str, Any] | None:
    import numpy as np
    alpha = np.asarray(image.convert("RGBA"), dtype=np.uint8)[:, :, 3]
    streams = ((alpha.T.reshape(-1) & 1).tolist(), (alpha.reshape(-1) & 1).tolist())
    prefix_bits = max(map(len, STEALTH_SIGNATURES)) * 8
    for bits in streams:
        prefix = bits_to_bytes(bits[:prefix_bits]).decode("utf-8", errors="ignore")
        signature = next((item for item in STEALTH_SIGNATURES if prefix.startswith(item)), None)
        if not signature:
            continue
        offset = len(signature) * 8
        length_bits = bits[offset:offset + 32]
        if len(length_bits) != 32:
            continue
        length = int("".join(map(str, length_bits)), 2)
        payload = bits_to_bytes(bits[offset + 32:offset + 32 + length])
        if STEALTH_SIGNATURES[signature]:
            payload = _gzip_decompress_bounded(payload)
        elif len(payload) > MAX_TEXT_CHUNK_BYTES:
            raise PromptMetadataTooLarge("prompt_metadata_too_large")
        text = payload.decode("utf-8")
        value = parse_jsonish_text(text) or {"prompt": text}
        value["_metadata_source"] = signature
        return value
    return None


def parse_jsonish_text(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def decode_png_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_png_itxt(data: bytes) -> tuple[str, str] | None:
    keyword_end = data.find(b"\x00")
    if keyword_end < 0 or len(data) < keyword_end + 5:
        return None
    keyword = data[:keyword_end]
    flag = data[keyword_end + 1]
    method = data[keyword_end + 2]
    if flag not in (0, 1) or method != 0:
        return None
    cursor = keyword_end + 3
    language_end = data.find(b"\x00", cursor)
    if language_end < 0:
        return None
    translated_end = data.find(b"\x00", language_end + 1)
    if translated_end < 0:
        return None
    text = data[translated_end + 1:]
    if flag == 1:
        text = _zlib_decompress_bounded(text)
    return decode_png_text(keyword), text.decode("utf-8", errors="replace")


def _zlib_decompress_bounded(data: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(data, MAX_TEXT_CHUNK_BYTES + 1)
    if decompressor.unconsumed_tail or len(decoded) > MAX_TEXT_CHUNK_BYTES:
        raise PromptMetadataTooLarge("prompt_metadata_too_large")
    decoded += decompressor.flush()
    if len(decoded) > MAX_TEXT_CHUNK_BYTES:
        raise PromptMetadataTooLarge("prompt_metadata_too_large")
    return decoded


def _gzip_decompress_bounded(data: bytes) -> bytes:
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    decoded = decompressor.decompress(data, MAX_TEXT_CHUNK_BYTES + 1)
    if decompressor.unconsumed_tail or len(decoded) > MAX_TEXT_CHUNK_BYTES:
        raise PromptMetadataTooLarge("prompt_metadata_too_large")
    decoded += decompressor.flush()
    if len(decoded) > MAX_TEXT_CHUNK_BYTES:
        raise PromptMetadataTooLarge("prompt_metadata_too_large")
    return decoded


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
            if length > MAX_TEXT_CHUNK_BYTES:
                raise PromptMetadataTooLarge("prompt_metadata_too_large")
            data = file.read(length)
            crc = file.read(4)
            if len(data) != length or len(crc) != 4:
                break
            try:
                if chunk_type == b"tEXt":
                    key, value = data.split(b"\x00", 1)
                    chunks[decode_png_text(key)] = decode_png_text(value)
                elif chunk_type == b"zTXt":
                    key, rest = data.split(b"\x00", 1)
                    if rest[:1] == b"\x00":
                        chunks[decode_png_text(key)] = decode_png_text(
                            _zlib_decompress_bounded(rest[1:])
                        )
                elif chunk_type == b"iTXt":
                    parsed = parse_png_itxt(data)
                    if parsed:
                        chunks[parsed[0]] = parsed[1]
            except PromptMetadataTooLarge:
                raise
            except (ValueError, zlib.error, UnicodeError):
                pass
            if chunk_type == b"IEND":
                break
    return chunks


def metadata_from_text_values(values: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """Legacy helper retained for callers that expect decoded metadata dictionaries."""
    result = parse_text_metadata(values)
    if result.prompt_format == "a1111":
        parameters = _get_ci(values, "parameters")
        return ({"parameters": parameters, "prompt": result.prompt} if parameters else None), result.metadata_source
    for key in ("Comment", "Description", "prompt"):
        value = _get_ci(values, key)
        if isinstance(value, str):
            parsed = parse_jsonish_text(value)
            if parsed is not None:
                return parsed, f"text:{key}"
    return ({"prompt": result.prompt} if result.prompt else None), result.metadata_source


def read_pil_text_values(path: Path) -> dict[str, str]:
    with Image.open(path) as image:
        return {key: value for key, value in image.info.items() if isinstance(key, str) and isinstance(value, str)}


def read_pil_text_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    return metadata_from_text_values(read_pil_text_values(path))
