from __future__ import annotations

import gzip
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from imgdupe.nai_prompt import (
    OTHER_MODEL_IDENTITY,
    MAX_TEXT_CHUNK_BYTES,
    PromptMetadataTooLarge,
    _gzip_decompress_bounded,
    extract_nai_prompt,
    normalize_prompt_tag,
    parse_a1111_parameters,
    parse_novelai_metadata,
    parse_novelai_prompt,
    parse_png_itxt,
    parse_text_metadata,
    read_png_text_chunks,
    split_prompt_tags,
)


def test_itxt_uncompressed_and_compressed() -> None:
    plain = b"parameters\0\0\0\0\0hello"
    compressed = b"Comment\0\1\0\0\0" + zlib.compress("世界".encode())
    assert parse_png_itxt(plain) == ("parameters", "hello")
    assert parse_png_itxt(compressed) == ("Comment", "世界")


def test_reads_all_png_text_chunk_types(tmp_path: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"tEXt", b"Software\0NovelAI")
    png += chunk(b"zTXt", b"Source\0\0" + zlib.compress(b"NovelAI Diffusion V4 ABCDEF12"))
    png += chunk(b"iTXt", b"Comment\0\0\0\0\0{\"prompt\":\"cat\"}")
    png += chunk(b"IEND", b"")
    path = tmp_path / "text.png"
    path.write_bytes(png)
    assert read_png_text_chunks(path) == {
        "Software": "NovelAI",
        "Source": "NovelAI Diffusion V4 ABCDEF12",
        "Comment": '{"prompt":"cat"}',
    }


def test_a1111_sections_lora_emphasis_alternation_and_hash() -> None:
    result = parse_a1111_parameters(
        "kujou miyako, [9-nine-], <lora:miyako5:0.6>, "
        "(izumi tsubasu:0.75), [cow|horse], [fantasy:cyberpunk:0.5], "
        r"\[literal\], .chibi, v1.0" + "\n"
        "Negative prompt: bad anatomy, lowres\n"
        "Steps: 35, Sampler: Euler a, Model hash: 6681E8E4B1, Model: ignored"
    )
    assert result.prompt_format == "a1111"
    assert result.model_hash == "6681e8e4b1"
    assert result.tags == (
        "kujou miyako", "9-nine-", "izumi tsubasu", "cow", "horse",
        "fantasy", "cyberpunk", "[literal]", "chibi", "v1.0",
    )
    assert "bad anatomy" not in result.tags
    assert "miyako5" not in result.prompt


def test_a1111_grouped_tags_and_pipe_fallback() -> None:
    result = parse_a1111_parameters("(red hair, blue eyes:1.2), rain|snow")
    assert result.tags == ("red hair", "blue eyes", "rain", "snow")


def test_a1111_fullwidth_syntax_and_literal_lowercase_and() -> None:
    result = parse_a1111_parameters(
        "（青い目：０．７５），［cat］，black and white，red hair AND blue eyes\n"
        "Steps：２０，Sampler：Euler a，Model hash：A1B2C3D4"
    )
    assert result.tags == ("青い目", "cat", "black and white", "red hair", "blue eyes")
    assert result.model_hash == "a1b2c3d4"

    separators = parse_a1111_parameters("cat．dog。bird")
    assert separators.tags == ("cat", "dog", "bird")


def test_malformed_wrapper_is_literal_and_deep_nesting_is_reported() -> None:
    malformed = parse_a1111_parameters("cat, (blue eyes, dog")
    assert malformed.tags == ("cat", "(blue eyes", "dog")

    deep = parse_a1111_parameters("(" * 65 + "cat" + ")" * 65)
    assert deep.error == "prompt_nesting_too_deep"
    assert "cat" in deep.prompt


def test_oversized_png_text_chunk_has_distinct_status(tmp_path: Path) -> None:
    path = tmp_path / "oversized.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", MAX_TEXT_CHUNK_BYTES + 1)
        + b"tEXt"
    )
    result = extract_nai_prompt(path)
    assert result.error == "prompt_metadata_too_large"

    compressed_gzip = gzip.compress(b"a" * (MAX_TEXT_CHUNK_BYTES + 1))
    with unittest.TestCase().assertRaises(PromptMetadataTooLarge):
        _gzip_decompress_bounded(compressed_gzip)

    compressed = zlib.compress(b"a" * (MAX_TEXT_CHUNK_BYTES + 1))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(b"parameters\0\0" + compressed))
        + b"zTXt"
        + b"parameters\0\0"
        + compressed
        + b"\0\0\0\0"
    )
    result = extract_nai_prompt(path)
    assert result.error == "prompt_metadata_too_large"


def test_novelai_software_detection_requires_valid_comment() -> None:
    malformed = parse_text_metadata({"Software": " novelAI ", "Comment": "not json", "parameters": "cat"})
    assert malformed.prompt_format == "novelai"
    assert malformed.error == "malformed_metadata"


def test_malformed_novelai_comment_still_preserves_source_identity() -> None:
    result = parse_text_metadata({
        "Software": "NovelAI",
        "Source": "NovelAI Diffusion V4.5 37442FCA",
        "Comment": "{not valid json",
    })

    assert result.error == "malformed_metadata"
    assert result.model_type == "nai4"
    assert result.model_version == "4.5"
    assert result.model_hash == "37442fca"
    assert result.model_identity == "NovelAI Diffusion V4.5 37442FCA"


def test_nai3_prompt_and_source_hash() -> None:
    result = parse_text_metadata({
        "Software": "NovelAI",
        "Source": "Stable Diffusion XL C1E1DE52",
        "Comment": '{"prompt":"cat, blue_hair","uc":"bad anatomy"}',
    })
    assert result.model_type == "nai3"
    assert result.model_hash == "c1e1de52"
    assert result.tags == ("cat", "blue_hair")


def test_nai4_structured_base_and_characters_and_negative_exclusion() -> None:
    metadata = {
        "Source": "NovelAI Diffusion V4.5 4BDE2A90",
        "prompt": "duplicate top level",
        "v4_prompt": {"caption": {
            "base_caption": "1girl, outdoors",
            "char_captions": [{"char_caption": "blue hair"}, {"caption": "red eyes"}],
        }},
        "v4_negative_prompt": {"caption": {"base_caption": "bad anatomy"}},
    }
    result = parse_novelai_metadata(metadata)
    assert result.model_type == "nai4"
    assert result.model_version == "4.5"
    assert result.model_hash == "4bde2a90"
    assert result.tags == ("1girl", "outdoors", "blue hair", "red eyes")


def test_actual_prompt_takes_precedence() -> None:
    metadata = {
        "Source": "NovelAI Diffusion V4 37442FCA",
        "v4_prompt": {"caption": {"base_caption": "||black|white|| thighhighs"}},
        "actual_prompts": {"prompt": {"base_caption": "white thighhighs"}},
    }
    result = parse_novelai_metadata(metadata)
    assert result.model_version == "4"
    assert result.tags == ("white thighhighs",)


def test_novelai_numeric_scopes_exclude_negative_and_zero() -> None:
    tags = parse_novelai_prompt(
        "1girl, 1.5::rain, night::, -2.5::hat, glasses::, 0::tie::, "
        "-1::negative outer, -1::positive inner:: ::, outdoors"
    )
    assert tags == ["1girl", "rain", "night", "positive inner", "outdoors"]


def test_unclosed_negative_scope_continues_to_end() -> None:
    assert parse_novelai_prompt("cat, -1::hat, glasses") == ["cat"]


def test_nai3_prompt_mixing_uses_trailing_weight_sign() -> None:
    assert parse_novelai_prompt("cat:1|happy:-0.2|cute:-0.3|style:name", model_type="nai3") == ["cat", "style:name"]


def test_pipe_fallback_and_unicode_separators() -> None:
    tags = parse_novelai_prompt("||red|blue|| hair｜green，青い目。solo、白髪；帽子")
    assert tags == ["red", "blue", "hair", "green", "青い目", "solo", "白髪", "帽子"]


def test_fullwidth_weight_syntax_and_cjk_preservation() -> None:
    tags = parse_novelai_prompt("（青い目：0.75），－１：：帽子：：，「title」，《作品》")
    assert tags == ["青い目", "「title」", "《作品》"]


def test_novelai_parenthesized_tag_qualifiers_are_preserved() -> None:
    tags = parse_novelai_prompt(
        "yuuki noa, yuuki (princess connect!), yuuki (sao), "
        "akizuki kanna(cafe stella to shinigami no chou), "
        "0.5::yd(orange maru), pottsness::"
    )
    assert tags == [
        "yuuki noa",
        "yuuki (princess connect!)",
        "yuuki (sao)",
        "akizuki kanna(cafe stella to shinigami no chou)",
        "yd(orange maru)",
        "pottsness",
    ]


def test_novelai_cleans_only_round_wrappers_at_tag_boundaries() -> None:
    tags = parse_novelai_prompt(
        "(masterpiece), (digital painting:1.2), artist_name_(circle), "
        "character (series)"
    )
    assert tags == [
        "masterpiece", "digital painting", "artist_name_(circle)",
        "character (series)",
    ]


def test_novelai_cleans_unmatched_foreign_round_punctuation_per_tag() -> None:
    assert parse_novelai_prompt(
        "(legendary Dragon Queen::, (1 girl), hair flowing) detailed sky"
    ) == ["legendary Dragon Queen::", "1 girl", "hair flowing detailed sky"]


def test_novelai_escaped_round_punctuation_remains_literal() -> None:
    assert parse_novelai_prompt(
        r"\(literal wrapper\), artist\(circle\), \(unmatched"
    ) == ["(literal wrapper)", "artist(circle)", "(unmatched"]


def test_tag_normalization_preserves_display_but_unifies_underscore() -> None:
    assert split_prompt_tags("blue_hair, blue hair") == ["blue_hair"]
    assert normalize_prompt_tag("Blue_Hair") == "blue hair"


def test_structured_novelai_generator_identity_uses_full_source() -> None:
    result = parse_text_metadata({
        "Software": "NovelAI",
        "Source": "NovelAI Diffusion V4.5 4BDE2A90",
        "Comment": '{"prompt":"cat"}',
    })
    assert result.model_type == "nai4"
    assert result.model_identity == "NovelAI Diffusion V4.5 4BDE2A90"
    assert result.model_name == result.model_identity
    assert result.model_hash == "4bde2a90"
    assert result.error is None


def test_novelai_source_is_authoritative_for_generation() -> None:
    result = parse_text_metadata({
        "Software": "NovelAI",
        "Source": "Stable Diffusion XL C1E1DE52",
        "Comment": '{"prompt":"cat","v4_prompt":{"caption":{"base_caption":"v4 text"}}}',
    })
    assert result.model_type == "nai3"
    assert result.tags == ("cat",)


def test_non_novelai_json_comment_does_not_claim_novelai() -> None:
    result = parse_text_metadata({"Comment": '{"prompt":"cat"}'})
    assert result.error == "no_metadata"
    assert result.model_type == "sd"
    assert result.model_identity is None


def test_a1111_model_name_and_hash_are_extracted() -> None:
    result = parse_a1111_parameters(
        "cat\nSteps: 20, Sampler: Euler, Model hash: ABCDEF12, "
        "Model: blended_model-v2, Clip skip: 2"
    )
    assert result.model_identity == "blended_model-v2"
    assert result.model_name == "blended_model-v2"
    assert result.model_hash == "abcdef12"
    assert result.error is None


def test_prompt_metadata_without_name_uses_bilingual_other_model_identity() -> None:
    result = parse_a1111_parameters("cat\nSteps: 20, Sampler: Euler, Model hash: ABCDEF12")
    assert result.model_identity == OTHER_MODEL_IDENTITY
    assert result.model_name == OTHER_MODEL_IDENTITY


def test_prompt_metadata_without_hash_returns_distinct_error_but_keeps_tags() -> None:
    sd = parse_a1111_parameters("cat\nSteps: 20, Sampler: Euler, Model: example")
    nai = parse_text_metadata({
        "Software": "NovelAI",
        "Source": "NovelAI Diffusion V4.5",
        "Comment": '{"prompt":"dog"}',
    })
    assert sd.tags == ("cat",)
    assert sd.error == "missing_model_hash"
    assert nai.tags == ("dog",)
    assert nai.error == "missing_model_hash"


def test_prompt_metadata_without_name_or_hash_uses_other_and_missing_hash() -> None:
    result = parse_text_metadata({"Description": "cat"})
    assert result.model_type == "sd"
    assert result.model_identity == OTHER_MODEL_IDENTITY
    assert result.error == "missing_model_hash"


def load_tests(_loader, _tests, _pattern):
    """Expose the lightweight function tests to the project's unittest suite."""
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        if function.__code__.co_argcount == 1:
            def run_with_temp_path(test_function=function):
                with tempfile.TemporaryDirectory() as directory:
                    test_function(Path(directory))

            test = unittest.FunctionTestCase(run_with_temp_path, description=name)
        else:
            test = unittest.FunctionTestCase(function, description=name)
        suite.addTest(test)
    return suite
