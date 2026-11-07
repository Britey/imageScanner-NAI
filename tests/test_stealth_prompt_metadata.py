from __future__ import annotations

import gzip
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe import nai_prompt
from imgdupe.nai_prompt import (
    MAX_TEXT_CHUNK_BYTES,
    OTHER_MODEL_IDENTITY,
    extract_nai_prompt,
    read_png_text_chunks,
)
from scripts.compare_reference_prompt_tags import load_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = PROJECT_ROOT / "参考图示例"
MANIFEST_PATH = PROJECT_ROOT / "tests" / "fixtures" / "reference_prompt_tags.json"

NAI45_METADATA = {
    "Source": "NovelAI Diffusion V4.5 4BDE2A90",
    "prompt": "fallback prompt",
    "v4_prompt": {"caption": {"base_caption": "cat, blue eyes", "char_captions": []}},
    "v4_negative_prompt": {"caption": {"base_caption": "bad anatomy", "char_captions": []}},
}


def _byte_bits(data: bytes) -> list[int]:
    return [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]


def _metadata_payload(metadata: dict[str, object]) -> bytes:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _stealth_image(
    payload: bytes,
    *,
    compressed: bool,
    column_major: bool,
    declared_bits: int | None = None,
) -> tuple[Image.Image, str]:
    signature = "stealth_pngcomp" if compressed else "stealth_pnginfo"
    if compressed:
        payload = gzip.compress(payload)
    length_in_bits = len(payload) * 8 if declared_bits is None else declared_bits
    stream = (
        _byte_bits(signature.encode("utf-8"))
        + _byte_bits(length_in_bits.to_bytes(4, "big"))
        + _byte_bits(payload)
    )
    width = min(257, max(1, len(stream)))
    height = math.ceil(len(stream) / width)
    alpha = [254] * (width * height)
    for index, bit in enumerate(stream):
        if column_major:
            x, y = divmod(index, height)
            pixel_index = y * width + x
        else:
            pixel_index = index
        alpha[pixel_index] |= bit
    image = Image.new("RGBA", (width, height), (0, 0, 0, 254))
    image.putalpha(Image.frombytes("L", image.size, bytes(alpha)))
    return image, signature


def _write_stealth_png(
    path: Path,
    payload: bytes,
    *,
    compressed: bool,
    column_major: bool,
    pnginfo: PngInfo | None = None,
    declared_bits: int | None = None,
) -> str:
    image, signature = _stealth_image(
        payload, compressed=compressed, column_major=column_major, declared_bits=declared_bits,
    )
    image.save(path, format="PNG", pnginfo=pnginfo)
    return signature


class StealthPromptMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_reads_compressed_and_plain_payloads_in_both_pixel_orders(self) -> None:
        payload = _metadata_payload(NAI45_METADATA)
        for compressed in (False, True):
            for column_major in (False, True):
                with self.subTest(compressed=compressed, column_major=column_major):
                    path = self.root / f"stealth-{compressed}-{column_major}.png"
                    signature = _write_stealth_png(
                        path, payload, compressed=compressed, column_major=column_major,
                    )

                    self.assertEqual(read_png_text_chunks(path), {})
                    self.assertEqual(extract_nai_prompt(path, stealth_fallback=False).error, "no_metadata")

                    result = extract_nai_prompt(path, stealth_fallback=True)
                    self.assertEqual(result.metadata_source, signature)
                    self.assertEqual(result.prompt_format, "novelai")
                    self.assertEqual(result.model_type, "nai4")
                    self.assertEqual(result.model_version, "4.5")
                    self.assertEqual(result.model_hash, "4bde2a90")
                    self.assertEqual(result.model_identity, NAI45_METADATA["Source"])
                    self.assertIsNone(result.error)
                    self.assertEqual(result.tags, ("cat", "blue eyes"))

    def test_plain_text_payload_is_indexed_as_novelai_prompt(self) -> None:
        path = self.root / "plain-text.png"
        _write_stealth_png(path, "1girl, smile".encode(), compressed=False, column_major=True)

        result = extract_nai_prompt(path)

        self.assertEqual(result.metadata_source, "stealth_pnginfo")
        self.assertEqual(result.prompt_format, "novelai")
        self.assertEqual(result.tags, ("1girl", "smile"))
        self.assertEqual(result.error, "missing_model_hash")
        self.assertEqual(result.model_identity, OTHER_MODEL_IDENTITY)

    def test_text_chunks_take_precedence_over_stealth_pixels(self) -> None:
        info = PngInfo()
        info.add_text("parameters", "dog\nSteps: 20, Model hash: abcdef12, Model: example")
        path = self.root / "both.png"
        _write_stealth_png(
            path, _metadata_payload(NAI45_METADATA), compressed=True, column_major=True, pnginfo=info,
        )

        result = extract_nai_prompt(path)

        self.assertEqual(result.metadata_source, "text:parameters")
        self.assertEqual(result.tags, ("dog",))
        self.assertEqual(result.model_identity, "example")

    def test_stealth_is_only_read_from_png_files(self) -> None:
        image, _signature = _stealth_image(
            _metadata_payload(NAI45_METADATA), compressed=False, column_major=True,
        )
        path = self.root / "stealth.tiff"
        image.save(path, format="TIFF", dpi=(72, 72))

        result = extract_nai_prompt(path, stealth_fallback=True)

        self.assertEqual(result.error, "no_metadata")
        self.assertEqual(result.tags, ())

    def test_images_without_stealth_alpha_have_no_metadata(self) -> None:
        cases = {
            "rgb": Image.new("RGB", (16, 16), (10, 20, 30)),
            "opaque-alpha": Image.new("RGBA", (16, 16), (10, 20, 30, 255)),
        }
        palette = Image.new("P", (16, 16), 0)
        cases["palette-transparency"] = palette
        for name, image in cases.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.png"
                if name == "palette-transparency":
                    image.save(path, transparency=0)
                else:
                    image.save(path)
                result = extract_nai_prompt(path, stealth_fallback=True)
                self.assertEqual(result.error, "no_metadata")
                self.assertEqual(result.model_type, "sd")

    def test_truncated_or_impossible_headers_are_ignored(self) -> None:
        signature_bits = _byte_bits(b"stealth_pnginfo")
        header_only = Image.new("RGBA", (len(signature_bits), 1), (0, 0, 0, 254))
        header_only.putalpha(Image.frombytes("L", header_only.size, bytes(254 | bit for bit in signature_bits)))
        header_only.save(self.root / "header-only.png")

        _write_stealth_png(
            self.root / "oversized-length.png", b"cat", compressed=False, column_major=True,
            declared_bits=1_000_000,
        )

        for name in ("header-only.png", "oversized-length.png"):
            with self.subTest(name=name):
                result = extract_nai_prompt(self.root / name, stealth_fallback=True)
                self.assertEqual(result.error, "no_metadata")
                self.assertEqual(result.tags, ())

    def test_compressed_payload_expanding_past_ceiling_is_rejected(self) -> None:
        path = self.root / "gzip-bomb.png"
        for excess in (1, 2):
            with self.subTest(excess=excess):
                _write_stealth_png(path, b"a" * (MAX_TEXT_CHUNK_BYTES + excess), compressed=True, column_major=True)

                result = extract_nai_prompt(path, stealth_fallback=True)

                self.assertEqual(result.error, "prompt_metadata_too_large")
                self.assertIsNone(result.prompt)

    def test_declared_payload_above_ceiling_is_rejected_before_decoding(self) -> None:
        path = self.root / "large-payload.png"
        _write_stealth_png(path, b"cat", compressed=False, column_major=True)

        with patch.object(nai_prompt, "MAX_TEXT_CHUNK_BYTES", 2):
            result = extract_nai_prompt(path, stealth_fallback=True)

        self.assertEqual(result.error, "prompt_metadata_too_large")

    @unittest.skipUnless(REFERENCE_ROOT.is_dir(), "local ignored reference corpus is unavailable")
    def test_all_novelai_references_match_through_both_stealth_formats(self) -> None:
        entries = [
            entry
            for entry in load_manifest(MANIFEST_PATH)["images"]
            if entry["prompt_format"] == "novelai"
        ]
        self.assertEqual(len(entries), 15)

        for entry in entries:
            source_path = REFERENCE_ROOT / Path(entry["path"])
            text_values = read_png_text_chunks(source_path)
            self.assertEqual(text_values.get("Software"), "NovelAI")
            metadata = json.loads(text_values["Comment"])
            if text_values.get("Source"):
                metadata.setdefault("Source", text_values["Source"])

            for compressed in (False, True):
                with self.subTest(path=entry["path"], compressed=compressed):
                    carrier = self.root / f"{entry['sha256']}-{'comp' if compressed else 'plain'}.png"
                    signature = _write_stealth_png(
                        carrier, _metadata_payload(metadata), compressed=compressed, column_major=compressed,
                    )
                    result = extract_nai_prompt(carrier, stealth_fallback=True)

                    self.assertEqual(result.metadata_source, signature)
                    self.assertEqual(result.prompt_format, entry["prompt_format"])
                    self.assertEqual(result.model_type, entry["detected_model_type"])
                    self.assertEqual(result.model_version, entry["model_version"])
                    self.assertEqual(result.model_hash, entry["model_hash"])
                    self.assertEqual(list(result.tags), entry["tags"])


if __name__ == "__main__":
    unittest.main()
