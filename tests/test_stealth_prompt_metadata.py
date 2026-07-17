from __future__ import annotations

import gzip
import json
import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from imgdupe.nai_prompt import (
    extract_nai_prompt,
    read_png_text_chunks,
    read_stealth_metadata,
)
from scripts.compare_reference_prompt_tags import load_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = PROJECT_ROOT / "参考图示例"
MANIFEST_PATH = PROJECT_ROOT / "tests" / "fixtures" / "reference_prompt_tags.json"


def _byte_bits(data: bytes) -> list[int]:
    return [
        (byte >> shift) & 1
        for byte in data
        for shift in range(7, -1, -1)
    ]


def _write_stealth_png(
    path: Path,
    metadata: dict[str, object],
    *,
    compressed: bool,
    column_major: bool,
) -> str:
    signature = "stealth_pngcomp" if compressed else "stealth_pnginfo"
    payload = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if compressed:
        payload = gzip.compress(payload)

    length_in_bits = len(payload) * 8
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
    image.save(path, format="PNG")
    return signature


class StealthPromptMetadataTests(unittest.TestCase):
    def test_reads_compressed_and_uncompressed_stealth_payloads(self) -> None:
        metadata = {
            "Source": "NovelAI Diffusion V4.5 4BDE2A90",
            "prompt": "fallback prompt",
            "v4_prompt": {"caption": {"base_caption": "cat, blue eyes"}},
            "v4_negative_prompt": {"caption": {"base_caption": "bad anatomy"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for compressed in (False, True):
                for column_major in (False, True):
                    with self.subTest(
                        compressed=compressed,
                        column_major=column_major,
                    ):
                        path = root / f"stealth-{compressed}-{column_major}.png"
                        signature = _write_stealth_png(
                            path,
                            metadata,
                            compressed=compressed,
                            column_major=column_major,
                        )

                        self.assertEqual(read_png_text_chunks(path), {})
                        disabled = extract_nai_prompt(path, stealth_fallback=False)
                        self.assertEqual(disabled.error, "no_metadata")

                        with Image.open(path) as image:
                            decoded = read_stealth_metadata(image)
                        self.assertIsNotNone(decoded)
                        self.assertEqual(decoded["_metadata_source"], signature)
                        self.assertEqual(decoded["v4_prompt"], metadata["v4_prompt"])

                        result = extract_nai_prompt(path, stealth_fallback=True)
                        self.assertEqual(result.metadata_source, signature)
                        self.assertEqual(result.model_type, "nai4")
                        self.assertEqual(result.model_version, "4.5")
                        self.assertEqual(result.model_hash, "4bde2a90")
                        self.assertEqual(result.tags, ("cat", "blue eyes"))
                        self.assertNotIn("bad anatomy", result.tags)

    @unittest.skipUnless(
        REFERENCE_ROOT.is_dir(),
        "local ignored reference corpus is unavailable",
    )
    def test_all_novelai_references_match_through_both_stealth_formats(self) -> None:
        entries = [
            entry
            for entry in load_manifest(MANIFEST_PATH)["images"]
            if entry["prompt_format"] == "novelai"
        ]
        self.assertEqual(len(entries), 12)

        with tempfile.TemporaryDirectory() as directory:
            carriers = Path(directory)
            for entry in entries:
                source_path = REFERENCE_ROOT / Path(entry["path"])
                text_values = read_png_text_chunks(source_path)
                self.assertEqual(text_values.get("Software"), "NovelAI")
                metadata = json.loads(text_values["Comment"])
                if text_values.get("Source"):
                    metadata.setdefault("Source", text_values["Source"])

                for compressed in (False, True):
                    with self.subTest(path=entry["path"], compressed=compressed):
                        carrier = carriers / (
                            f"{entry['sha256']}-{'comp' if compressed else 'plain'}.png"
                        )
                        signature = _write_stealth_png(
                            carrier,
                            metadata,
                            compressed=compressed,
                            column_major=compressed,
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
