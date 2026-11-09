"""Read metadata from actual PNG carriers, including hostile text chunks."""

from __future__ import annotations

import io
import tempfile
import unittest
import zlib
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe.nai_prompt import MAX_TEXT_CHUNK_BYTES, extract_nai_prompt, read_pil_text_metadata, bits_to_bytes


PARAMETERS = "猫, café\nNegative prompt: watermark\nSteps: 20, Model hash: ABCDEF12, Model: example"


def chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload)
    return len(payload).to_bytes(4, "big") + kind + payload + checksum.to_bytes(4, "big")


class PngMetadataBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "metadata.png"
        stream = io.BytesIO()
        Image.new("RGB", (4, 4), (20, 80, 120)).save(stream, format="PNG")
        self.carrier = stream.getvalue()

    def write_chunks(self, *chunks: bytes) -> None:
        # Insert after the image data and before IEND, preserving a decodable image.
        self.path.write_bytes(self.carrier[:-12] + b"".join(chunks) + self.carrier[-12:])

    def assert_prompt(self, expected_tags=("猫", "café")) -> None:
        result = extract_nai_prompt(self.path)
        self.assertEqual(result.tags, expected_tags)
        self.assertEqual(result.model_identity, "example")
        self.assertEqual(result.model_hash, "abcdef12")
        self.assertIsNone(result.error)

    def test_all_png_text_encodings_preserve_unicode_prompt_and_model(self) -> None:
        for encoding in ("text", "compressed-text", "international", "compressed-international"):
            with self.subTest(encoding=encoding):
                info = PngInfo()
                if "international" in encoding:
                    info.add_itxt("parameters", PARAMETERS, lang="zh-TW", tkey="生成參數",
                                  zip=encoding.startswith("compressed"))
                else:
                    # A UTF-8 tEXt carrier is also accepted, as found in generated images.
                    payload = PARAMETERS.encode("utf-8")
                    if encoding.startswith("compressed"):
                        self.write_chunks(chunk(b"zTXt", b"parameters\0\0" + zlib.compress(payload)))
                    else:
                        self.write_chunks(chunk(b"tEXt", b"parameters\0" + payload))
                if "international" in encoding:
                    Image.new("RGB", (4, 4)).save(self.path, pnginfo=info)
                self.assert_prompt()

    def test_latin1_text_chunks_are_read_without_losing_accents(self) -> None:
        parameters = "café\nSteps: 20, Model hash: ABCDEF12, Model: example"
        self.write_chunks(chunk(b"tEXt", b"parameters\0" + parameters.encode("latin-1")))
        self.assert_prompt(("café",))

    def test_malformed_chunks_do_not_hide_later_valid_metadata(self) -> None:
        cases = (
            (b"tEXt", b"missing-null"),
            (b"zTXt", b"ignored\0\1unsupported-method"),
            (b"zTXt", b"ignored\0\0invalid-zlib"),
            (b"iTXt", b"missing-null"),
            (b"iTXt", b"ignored\0\2\0\0\0bad-flag"),
            (b"iTXt", b"ignored\0\0\1\0\0bad-method"),
            (b"iTXt", b"ignored\0\0\0language-without-terminator"),
            (b"iTXt", b"ignored\0\0\0\0translated-without-terminator"),
        )
        valid = chunk(b"tEXt", b"parameters\0" + PARAMETERS.encode("utf-8"))
        for kind, payload in cases:
            with self.subTest(kind=kind, payload=payload):
                self.write_chunks(chunk(kind, payload), valid)
                self.assert_prompt()

    def test_declared_oversized_text_chunk_is_rejected_before_reading_payload(self) -> None:
        for kind in (b"tEXt", b"zTXt", b"iTXt"):
            with self.subTest(kind=kind):
                self.path.write_bytes(self.carrier[:8] + (MAX_TEXT_CHUNK_BYTES + 1).to_bytes(4, "big") + kind)
                result = extract_nai_prompt(self.path, stealth_fallback=False)
                self.assertEqual(result.error, "prompt_metadata_too_large")
                self.assertEqual(result.tags, ())

    def test_compressed_text_expanding_past_limit_is_rejected(self) -> None:
        for excess in (1, 2):
            compressed = zlib.compress(b"a" * (MAX_TEXT_CHUNK_BYTES + excess))
            for kind, prefix in ((b"zTXt", b"parameters\0\0"), (b"iTXt", b"parameters\0\1\0\0\0")):
                with self.subTest(kind=kind, excess=excess):
                    self.write_chunks(chunk(kind, prefix + compressed))
                    result = extract_nai_prompt(self.path)
                    self.assertEqual(result.error, "prompt_metadata_too_large")
                    self.assertIsNone(result.prompt)

    def test_truncated_text_chunk_never_indexes_partial_prompt(self) -> None:
        complete = chunk(b"tEXt", b"parameters\0" + PARAMETERS.encode("utf-8"))
        for missing_bytes in (1, 6, len(complete) - 3):
            with self.subTest(missing_bytes=missing_bytes):
                self.path.write_bytes(self.carrier[:8] + complete[:-missing_bytes])
                result = extract_nai_prompt(self.path, stealth_fallback=False)
                self.assertEqual(result.error, "no_metadata")
                self.assertEqual(result.tags, ())

    def test_truncated_chunk_type_never_indexes_metadata(self) -> None:
        self.path.write_bytes(self.carrier[:8] + (10).to_bytes(4, "big") + b"tE")
        result = extract_nai_prompt(self.path, stealth_fallback=False)
        self.assertEqual(result.error, "no_metadata")
        self.assertEqual(result.tags, ())

    def test_legacy_bit_decoder_ignores_incomplete_trailing_byte(self) -> None:
        self.assertEqual(bits_to_bytes([0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 1]), b"A")

    def test_legacy_reader_returns_the_json_object_from_comment(self) -> None:
        info = PngInfo()
        info.add_text("Comment", '{"prompt":"cat", "seed":42}')
        Image.new("RGB", (4, 4)).save(self.path, pnginfo=info)
        self.assertEqual(read_pil_text_metadata(self.path), ({"prompt": "cat", "seed": 42}, "text:Comment"))

    def test_missing_file_reports_extraction_failure_instead_of_raising(self) -> None:
        result = extract_nai_prompt(self.path)
        self.assertTrue(result.error.startswith("FileNotFoundError:"))
        self.assertEqual(result.tags, ())

    def test_non_png_image_without_text_metadata_is_not_an_extraction_error(self) -> None:
        path = self.path.with_suffix(".tiff")
        Image.new("RGB", (4, 4)).save(path)
        result = extract_nai_prompt(path)
        self.assertEqual(result.error, "no_metadata")
        self.assertEqual(result.tags, ())

    def test_legacy_image_metadata_reader_preserves_plain_prompt_and_parameters_contract(self) -> None:
        for key in ('Description', 'prompt'):
            with self.subTest(key=key):
                info = PngInfo()
                info.add_text(key, 'cat')
                Image.new('RGB', (4, 4)).save(self.path, pnginfo=info)
                metadata, source = read_pil_text_metadata(self.path)
                self.assertEqual(metadata, {'prompt': 'cat'})
                self.assertEqual(source, f'text:{key}')
        parameters = 'cat\nNegative prompt: blurry\nSteps: 20, Model hash: ABCDEF12'
        info = PngInfo()
        info.add_text('parameters', parameters)
        Image.new('RGB', (4, 4)).save(self.path, pnginfo=info)
        metadata, source = read_pil_text_metadata(self.path)
        self.assertEqual(metadata, {'parameters': parameters, 'prompt': 'cat'})
        self.assertEqual(source, 'text:parameters')
