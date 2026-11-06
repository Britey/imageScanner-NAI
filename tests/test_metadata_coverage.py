"""Parser boundary cases and malformed metadata are part of the public contract."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe import nai_prompt as prompt


def chunk(kind: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + kind + payload + b"\0" * 4


def stealth_image(payload: bytes, *, signature="stealth_pnginfo", order="row"):
    bits = [int(bit) for value in signature.encode() for bit in f"{value:08b}"]
    bits += [int(bit) for bit in f"{len(payload) * 8:032b}"]
    bits += [int(bit) for value in payload for bit in f"{value:08b}"]
    side = int(len(bits) ** .5) + 1
    alpha = np.full(side * side, 254, dtype=np.uint8)
    alpha[:len(bits)] |= np.asarray(bits, dtype=np.uint8)
    alpha = alpha.reshape(side, side)
    if order == "column":
        alpha = alpha.T
    pixels = np.zeros((side, side, 4), dtype=np.uint8)
    pixels[:, :, 3] = alpha
    return Image.fromarray(pixels)


class ParserBoundaryCoverageTests(unittest.TestCase):
    def test_nai3_mix_discards_zero_weight_but_keeps_small_positive_weight(self):
        self.assertEqual(prompt.parse_novelai_prompt("cat:1|dog:0", model_type="nai3"), ["cat"])
        self.assertEqual(prompt.parse_novelai_prompt("cat:1|dog:0.01", model_type="nai3"), ["cat", "dog"])

    def test_non_png_text_and_legacy_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.tiff"
            Image.new("RGB", (2, 2)).save(path)
            self.assertEqual(prompt.extract_nai_prompt(path).error, "no_metadata")
            self.assertNotIn("prompt", prompt.read_pil_text_values(path))
            self.assertEqual(prompt.read_pil_text_metadata(path), (None, None))
        with patch.object(prompt.Image, "open") as opened:
            opened.return_value.__enter__.return_value.info = {"prompt": "cat", 1: "wrong", "binary": b"wrong"}
            self.assertEqual(prompt.read_pil_text_values(Path("fake.jpg")), {"prompt": "cat"})
        metadata, source = prompt.metadata_from_text_values({"parameters": "cat\nSteps: 20, Model hash: abcdef"})
        self.assertEqual(metadata["prompt"], "cat")
        self.assertEqual(source, "text:parameters")
        self.assertEqual(prompt.metadata_from_text_values({}), (None, None))
        self.assertEqual(prompt.metadata_from_text_values({"Comment": '{"prompt":"cat"}'}),
                         ({"prompt": "cat"}, "text:Comment"))
        self.assertEqual(prompt.metadata_from_text_values({"Comment": "[]"}), (None, None))
        self.assertEqual(prompt.metadata_from_text_values({"Description": "cat"}),
                         ({"prompt": "cat"}, "text:Description"))
        self.assertEqual(prompt.metadata_from_text_values({"Software": "NovelAI", "Source": "NovelAI V4 abcdef12", "Comment": '{"prompt":"cat"}'}),
                         ({"prompt": "cat"}, "text:Comment"))

    def test_legacy_helpers_preserve_plain_prompt_without_parameters(self):
        for key in ("Description", "prompt"):
            for parameters in (None, "", "   "):
                with self.subTest(key=key, parameters=parameters):
                    values = {key: "cat, blue eyes"}
                    if parameters is not None:
                        values["parameters"] = parameters
                    self.assertEqual(prompt.metadata_from_text_values(values),
                                     ({"prompt": "cat, blue eyes"}, f"text:{key}"))
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "plain-prompt.png"
                info = PngInfo()
                info.add_text(key, "cat, blue eyes")
                Image.new("RGB", (2, 2)).save(path, pnginfo=info)
                self.assertEqual(prompt.read_pil_text_metadata(path),
                                 ({"prompt": "cat, blue eyes"}, f"text:{key}"))
        for values in ({}, {"parameters": ""}, {"parameters": "   "}, {"Description": "", "prompt": " "}):
            with self.subTest(values=values):
                self.assertEqual(prompt.metadata_from_text_values(values), (None, None))
        # Actual A1111 parameters retain the original payload even if their
        # positive prompt is empty, and take precedence over plain text fields.
        parameters = "Steps: 20, Model hash: abcdef"
        self.assertEqual(prompt.metadata_from_text_values({"parameters": parameters, "Description": "ignored"}),
                         ({"parameters": parameters, "prompt": None}, "text:parameters"))

    def test_stealth_fallback_without_payload_and_exception_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "blank.png"
            Image.new("RGB", (2, 2)).save(path)
            self.assertEqual(prompt.extract_nai_prompt(path).error, "no_metadata")
            with patch.object(prompt, "read_png_text_chunks", side_effect=prompt.PromptMetadataTooLarge()):
                self.assertEqual(prompt.extract_nai_prompt(path).error, "prompt_metadata_too_large")
            with patch.object(prompt, "read_png_text_chunks", side_effect=OSError("unreadable")):
                self.assertEqual(prompt.extract_nai_prompt(path).error, "OSError: unreadable")

    def test_novelai_empty_malformed_and_duplicate_captions(self):
        for source, error in (("Stable Diffusion XL abcdef12", "no_prompt"), (None, "missing_model_hash")):
            result = prompt.parse_text_metadata({"Software": "NovelAI", "Source": source})
            self.assertEqual(result.error, error)
        metadata = {"Software": "NovelAI", "Source": "Stable Diffusion XL abcdef12", "Comment": "[]"}
        self.assertEqual(prompt.parse_text_metadata(metadata).error, "malformed_metadata")
        metadata["Comment"] = json.dumps({"Source": "NovelAI V4 abcdef12", "prompt": "cat"})
        self.assertEqual(prompt.parse_text_metadata(metadata).model_type, "nai4")
        result = prompt.parse_novelai_metadata({"Source": "NovelAI V4 abcdef12", "prompt": "cat, cat"})
        self.assertEqual(result.tags, ("cat",))
        self.assertEqual(prompt.parse_novelai_metadata({"prompt": 1}).error, "missing_model_hash")
        self.assertEqual(prompt.parse_novelai_metadata({"actual_prompts": {"prompt": {"char_captions": ["cat", "cat"]}}}).tags, ("cat",))
        self.assertEqual(prompt.parse_novelai_metadata({"Source": "NovelAI V3 abcdef12", "prompt": ""}).error, "no_prompt")
        result = prompt.parse_novelai_metadata({"Source": "NovelAI V4 abcdef12", "prompt": "(" * 65 + "cat" + ")" * 65})
        self.assertEqual(result.error, "prompt_nesting_too_deep")
        self.assertEqual(prompt._caption_texts({"char_captions": ["", "cat", {"caption": "dog"}, {"caption": 2}, 1]}), ["cat", "dog"])
        self.assertIsNone(prompt._novelai_model_version(None))

    def test_delimiter_limits_unmatched_rounds_and_scope_limits(self):
        self.assertEqual(prompt._parse_a1111_fragment("cat", prompt.MAX_NESTING), "cat")
        self.assertEqual(prompt._parse_a1111_fragment("[cat]"), "cat")
        self.assertEqual(prompt._strip_novelai_round_wrappers("cat", prompt.MAX_NESTING), "cat")
        self.assertEqual(prompt._strip_novelai_round_wrappers(" (cat"), " (cat")
        self.assertEqual(prompt._strip_novelai_round_wrappers(" (cat), dog"), " cat, dog")
        self.assertEqual(prompt._clean_novelai_round_punctuation("cat)"), "cat")
        self.assertEqual(prompt._clean_novelai_round_punctuation("(cat"), "cat")
        self.assertEqual(prompt._clean_novelai_round_punctuation("(" * 65 + "cat" + ")" * 65), "cat")
        self.assertEqual(prompt._filter_novelai_numeric_scopes("1::" * 65 + "cat" + "::" * 66), "cat")
        self.assertEqual(prompt.parse_novelai_prompt("cat, cat"), ["cat"])
        self.assertEqual(prompt.parse_novelai_prompt("cat, ("), ["cat"])
        self.assertEqual(prompt._split_top_level("(" * 65 + "cat:" + ")" * 65 + ":end", ":")[-1], "end")

    def test_bit_truncation_and_both_stealth_orders(self):
        self.assertEqual(prompt.bits_to_bytes([0, 1, 0, 0, 0, 0, 0, 1, 1]), b"A")
        for order in ("row", "column"):
            result = prompt.read_stealth_metadata(stealth_image(b"cat", order=order))
            self.assertEqual(result, {"prompt": "cat", "_metadata_source": "stealth_pnginfo"})
        compressed = stealth_image(gzip.compress(b'{"prompt":"cat"}'), signature="stealth_pngcomp")
        self.assertEqual(prompt.read_stealth_metadata(compressed)["prompt"], "cat")
        with patch.object(prompt, "MAX_TEXT_CHUNK_BYTES", 2):
            with self.assertRaises(prompt.PromptMetadataTooLarge):
                prompt.read_stealth_metadata(stealth_image(b"cat"))
        # A valid signature with no room for its 32-bit length is ignored.
        signature = "stealth_pnginfo"
        bits = [int(bit) for value in signature.encode() for bit in f"{value:08b}"]
        pixels = np.zeros((1, len(bits), 4), dtype=np.uint8)
        pixels[0, :, 3] = np.asarray(bits, dtype=np.uint8) | 254
        self.assertIsNone(prompt.read_stealth_metadata(Image.fromarray(pixels)))

    def test_itxt_and_latin1_boundaries(self):
        self.assertEqual(prompt.decode_png_text(b"caf\xe9"), "café")
        for malformed in (b"no-null", b"x\0", b"x\0\2\0\0\0", b"x\0\0\1\0\0", b"x\0\0\0abc", b"x\0\0\0\0x"):
            with self.subTest(data=malformed):
                self.assertIsNone(prompt.parse_png_itxt(malformed))
        self.assertEqual(prompt.parse_png_itxt(b"prompt\0\0\0\0\0cat"), ("prompt", "cat"))

    def test_decompressor_final_output_bound(self):
        for function in (prompt._zlib_decompress_bounded, prompt._gzip_decompress_bounded):
            decompressor = Mock(unconsumed_tail=b"")
            decompressor.decompress.return_value = b"12"
            decompressor.flush.return_value = b"3"
            with patch.object(prompt, "MAX_TEXT_CHUNK_BYTES", 2), patch.object(prompt.zlib, "decompressobj", return_value=decompressor):
                with self.assertRaises(prompt.PromptMetadataTooLarge):
                    function(b"compressed")

    def test_png_truncation_bad_text_and_unsupported_compression(self):
        bodies = [b"bad", prompt.PNG_SIGNATURE, prompt.PNG_SIGNATURE + b"\0\0\0\0x",
                  prompt.PNG_SIGNATURE + b"\0\0\0\5tEXtx",
                  prompt.PNG_SIGNATURE + chunk(b"tEXt", b"missing-null"),
                  prompt.PNG_SIGNATURE + chunk(b"zTXt", b"prompt\0\1bad"),
                  prompt.PNG_SIGNATURE + chunk(b"iTXt", b"malformed")]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.png"
            for body in bodies:
                with self.subTest(body=body):
                    path.write_bytes(body)
                    self.assertEqual(prompt.read_png_text_chunks(path), {})
