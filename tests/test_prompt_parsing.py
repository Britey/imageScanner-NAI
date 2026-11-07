from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from imgdupe.nai_prompt import (
    OTHER_MODEL_IDENTITY,
    PromptResult,
    extract_nai_prompt,
    normalize_prompt_tag,
    parse_a1111_parameters,
    parse_novelai_prompt,
    sanitize_model_name,
    split_prompt_tags,
)


NAI45_SOURCE = "NovelAI Diffusion V4.5 4BDE2A90"
NAI4_SOURCE = "NovelAI Diffusion V4 37442FCA"
NAI3_SOURCE = "Stable Diffusion XL C1E1DE52"


def novelai_text(
    comment: dict[str, object] | str | None,
    source: str | None = NAI45_SOURCE,
    *,
    description: str | None = None,
    software: str = "NovelAI",
) -> dict[str, str]:
    text = {"Title": "NovelAI generated image"}
    if description is not None:
        text["Description"] = description
    text["Software"] = software
    if source is not None:
        text["Source"] = source
    text["Generation time"] = "5.83"
    if comment is not None:
        text["Comment"] = comment if isinstance(comment, str) else json.dumps(comment)
    return text


class PngExtractionMixin:
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.written = 0

    def extract(self, text: dict[str, str]) -> PromptResult:
        info = PngInfo()
        for key, value in text.items():
            info.add_text(key, value)
        self.written += 1
        path = self.root / f"generated-{self.written}.png"
        Image.new("RGB", (4, 4), (40, 80, 120)).save(path, pnginfo=info)
        return extract_nai_prompt(path, stealth_fallback=False)


class NovelAIPngExtractionTests(PngExtractionMixin, unittest.TestCase):
    def test_v45_indexes_only_positive_base_and_character_captions(self) -> None:
        base = (
            "1girl, yuuki (sao), {{masterpiece}}, [[sketch]], "
            "1.3::blue_eyes::, -1::hat::, outdoors"
        )
        comment = {
            "prompt": base,
            "steps": 28,
            "height": 1216,
            "width": 832,
            "scale": 5.0,
            "seed": 1234567890,
            "sampler": "k_euler_ancestral",
            "noise_schedule": "karras",
            "uc": "lowres, bad anatomy",
            "v4_prompt": {
                "caption": {
                    "base_caption": base,
                    "char_captions": [
                        {"char_caption": "girl, Blue_Eyes, smile", "centers": [{"x": 0.5, "y": 0.5}]},
                    ],
                },
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {
                    "base_caption": "lowres, bad anatomy",
                    "char_captions": [{"char_caption": "frown", "centers": [{"x": 0.5, "y": 0.5}]}],
                },
                "legacy_uc": False,
            },
        }

        result = self.extract(novelai_text(comment, description=base))

        self.assertEqual(result.prompt_format, "novelai")
        self.assertEqual(result.metadata_source, "text:Comment")
        self.assertEqual(result.model_type, "nai4")
        self.assertEqual(result.model_version, "4.5")
        self.assertEqual(result.model_hash, "4bde2a90")
        self.assertEqual(result.model_identity, NAI45_SOURCE)
        self.assertEqual(result.model_name, NAI45_SOURCE)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.tags,
            ("1girl", "yuuki (sao)", "masterpiece", "sketch", "blue_eyes", "outdoors", "girl", "smile"),
        )
        self.assertEqual(result.prompt, ", ".join(result.tags))
        self.assertEqual(
            json.loads(result.raw_metadata),
            {
                "Source": NAI45_SOURCE,
                "prompt": base,
                "uc": comment["uc"],
                "v4_prompt": comment["v4_prompt"],
                "v4_negative_prompt": comment["v4_negative_prompt"],
            },
        )

    def test_nai3_source_ignores_v4_fields_and_drops_non_positive_mix_segments(self) -> None:
        comment = {
            "prompt": "cat:1|happy dog:0|rainy:-0.3|night sky, 0::hidden::, {best quality}",
            "uc": "bad anatomy",
            "v4_prompt": {"caption": {"base_caption": "v4 only tag"}},
        }

        result = self.extract(novelai_text(comment, NAI3_SOURCE))

        self.assertEqual(result.model_type, "nai3")
        self.assertIsNone(result.model_version)
        self.assertEqual(result.model_hash, "c1e1de52")
        self.assertEqual(result.model_identity, NAI3_SOURCE)
        self.assertIsNone(result.error)
        self.assertEqual(result.tags, ("cat", "night sky", "best quality"))

    def test_actual_prompts_replace_randomized_prompt_syntax(self) -> None:
        comment = {
            "prompt": "||black|white|| thighhighs",
            "v4_prompt": {"caption": {"base_caption": "||black|white|| thighhighs", "char_captions": []}},
            "actual_prompts": {"prompt": {"base_caption": "white thighhighs"}},
        }

        result = self.extract(novelai_text(comment, NAI4_SOURCE))

        self.assertEqual(result.model_version, "4")
        self.assertEqual(result.model_hash, "37442fca")
        self.assertEqual(result.tags, ("white thighhighs",))

    def test_v4_without_structured_caption_uses_plain_prompt(self) -> None:
        fallback = self.extract(novelai_text({"prompt": "cat, rain"}, NAI4_SOURCE))
        self.assertEqual(fallback.model_type, "nai4")
        self.assertEqual(fallback.tags, ("cat", "rain"))
        self.assertIsNone(fallback.error)

        empty = self.extract(novelai_text({"steps": 28}, NAI4_SOURCE))
        self.assertEqual(empty.model_type, "nai4")
        self.assertEqual(empty.error, "no_prompt")
        self.assertEqual(empty.tags, ())
        self.assertIsNone(empty.prompt)
        self.assertEqual(empty.model_hash, "37442fca")

    def test_non_text_nai3_prompt_records_no_prompt_or_missing_hash(self) -> None:
        with_source = self.extract(novelai_text({"prompt": 5}, NAI3_SOURCE))
        self.assertEqual(with_source.error, "no_prompt")
        self.assertEqual(with_source.model_type, "nai3")
        self.assertEqual(with_source.model_identity, NAI3_SOURCE)

        without_source = self.extract(novelai_text({"prompt": 5}, None))
        self.assertEqual(without_source.error, "missing_model_hash")
        self.assertEqual(without_source.model_identity, OTHER_MODEL_IDENTITY)
        self.assertIsNone(without_source.model_hash)

    def test_prompt_with_only_excluded_spans_has_no_searchable_tags(self) -> None:
        result = self.extract(novelai_text({"prompt": "-1::lowres::, 0::watermark::"}, NAI3_SOURCE))
        self.assertEqual(result.tags, ())
        self.assertIsNone(result.prompt)
        self.assertEqual(result.error, "no_prompt")
        self.assertEqual(result.model_hash, "c1e1de52")

    def test_missing_source_uses_other_model_and_missing_hash_but_keeps_tags(self) -> None:
        result = self.extract(novelai_text({"prompt": "cat, smile"}, None))
        self.assertEqual(result.tags, ("cat", "smile"))
        self.assertEqual(result.error, "missing_model_hash")
        self.assertEqual(result.model_identity, OTHER_MODEL_IDENTITY)
        self.assertEqual(result.model_name, OTHER_MODEL_IDENTITY)
        self.assertEqual(result.model_type, "nai3")

    def test_source_without_hash_is_missing_model_hash_with_full_identity(self) -> None:
        result = self.extract(novelai_text({"prompt": "dog"}, "NovelAI Diffusion V4.5"))
        self.assertEqual(result.tags, ("dog",))
        self.assertEqual(result.error, "missing_model_hash")
        self.assertEqual(result.model_type, "nai4")
        self.assertIsNone(result.model_hash)
        self.assertEqual(result.model_identity, "NovelAI Diffusion V4.5")

    def test_novelai_without_comment_records_identity_without_prompt(self) -> None:
        with_hash = self.extract(novelai_text(None, NAI3_SOURCE))
        self.assertEqual(with_hash.prompt_format, "novelai")
        self.assertEqual(with_hash.metadata_source, "text:Comment")
        self.assertEqual(with_hash.error, "no_prompt")
        self.assertEqual(with_hash.model_type, "nai3")
        self.assertEqual(with_hash.model_hash, "c1e1de52")
        self.assertEqual(with_hash.model_identity, NAI3_SOURCE)
        self.assertEqual(with_hash.tags, ())

        v4 = self.extract(novelai_text(None, NAI45_SOURCE))
        self.assertEqual(v4.model_type, "nai4")
        self.assertEqual(v4.error, "no_prompt")

        bare = self.extract(novelai_text(None, None))
        self.assertEqual(bare.error, "missing_model_hash")
        self.assertEqual(bare.model_identity, OTHER_MODEL_IDENTITY)
        self.assertIsNone(bare.model_hash)

    def test_malformed_comment_is_reported_and_preserves_source_identity(self) -> None:
        result = self.extract(novelai_text("{not valid json", NAI45_SOURCE))
        self.assertEqual(result.error, "malformed_metadata")
        self.assertEqual(result.prompt_format, "novelai")
        self.assertEqual(result.model_type, "nai4")
        self.assertEqual(result.model_version, "4.5")
        self.assertEqual(result.model_hash, "4bde2a90")
        self.assertEqual(result.model_identity, NAI45_SOURCE)
        self.assertEqual(result.raw_metadata, "{not valid json")
        self.assertEqual(result.tags, ())

        no_source = self.extract(novelai_text("[]", None))
        self.assertEqual(no_source.error, "malformed_metadata")
        self.assertEqual(no_source.model_type, "nai3")
        self.assertIsNone(no_source.model_hash)
        self.assertIsNone(no_source.model_version)
        self.assertEqual(no_source.model_identity, OTHER_MODEL_IDENTITY)

    def test_software_field_is_authoritative_even_with_a1111_parameters(self) -> None:
        text = novelai_text("not json", NAI3_SOURCE, software=" novelAI ")
        text["parameters"] = "cat\nSteps: 20, Model hash: abcdef12"
        result = self.extract(text)
        self.assertEqual(result.prompt_format, "novelai")
        self.assertEqual(result.error, "malformed_metadata")
        self.assertEqual(result.tags, ())

    def test_partial_character_captions_skip_invalid_entries(self) -> None:
        comment = {
            "v4_prompt": {
                "caption": {
                    "base_caption": "  ",
                    "char_captions": ["solo", {"caption": "red eyes"}, {"char_caption": 3}, 7, "", {"char_caption": "SOLO"}],
                },
            },
        }
        result = self.extract(novelai_text(comment, NAI45_SOURCE))
        self.assertEqual(result.tags, ("solo", "red eyes"))
        self.assertIsNone(result.error)

    def test_pathological_nesting_is_flagged_but_tag_is_still_indexed(self) -> None:
        nested = "(" * 130 + "cat" + ")" * 130
        result = self.extract(novelai_text({"prompt": nested}, NAI4_SOURCE))
        self.assertEqual(result.error, "prompt_nesting_too_deep")
        self.assertEqual(result.tags, ("cat",))

    def test_json_comment_without_novelai_software_is_not_prompt_metadata(self) -> None:
        result = self.extract({"Comment": '{"prompt":"cat"}'})
        self.assertEqual(result.error, "no_metadata")
        self.assertEqual(result.model_type, "sd")
        self.assertIsNone(result.model_identity)
        self.assertEqual(result.tags, ())


class StableDiffusionPngExtractionTests(PngExtractionMixin, unittest.TestCase):
    def test_forge_parameters_index_positive_prompt_and_model_identity(self) -> None:
        parameters = (
            "masterpiece, (kujou miyako:1.2), [9-nine-], <lora:miyako5:0.6>, [cow|horse], "
            r"[fantasy:cyberpunk:0.5], [:sunset:0.7], \(artist\), .chibi, BREAK yuuki (sao), sanoba_witch"
            "\nNegative prompt: lowres, bad anatomy\n"
            "Steps: 28, Sampler: Euler a, Schedule type: Automatic, CFG scale: 7, Seed: 1234, "
            "Size: 832x1216, Model hash: 6681E8E4B1, Model: animagine-xl-3.1, Version: f2.0.1v1.10.1"
        )

        result = self.extract({"parameters": parameters})

        self.assertEqual(result.prompt_format, "a1111")
        self.assertEqual(result.metadata_source, "text:parameters")
        self.assertEqual(result.model_type, "sd")
        self.assertEqual(result.model_hash, "6681e8e4b1")
        self.assertEqual(result.model_identity, "animagine-xl-3.1")
        self.assertEqual(result.model_name, "animagine-xl-3.1")
        self.assertIsNone(result.model_version)
        self.assertIsNone(result.error)
        self.assertEqual(result.raw_metadata, parameters)
        self.assertEqual(
            result.tags,
            (
                "masterpiece", "kujou miyako", "9-nine-", "cow", "horse", "fantasy", "cyberpunk",
                "sunset", "(artist)", "chibi", "yuuki sao", "sanoba_witch",
            ),
        )
        self.assertNotIn("miyako5", result.prompt)
        self.assertNotIn("lowres", result.prompt)

    def test_fullwidth_ime_syntax_in_itxt_parameters(self) -> None:
        result = self.extract({
            "parameters": (
                "（青い目：０．７５），［cat］，black and white，red hair AND blue eyes\n"
                "Steps：２０，Sampler：Euler a，Model hash：A1B2C3D4"
            )
        })
        self.assertEqual(result.tags, ("青い目", "cat", "black and white", "red hair", "blue eyes"))
        self.assertEqual(result.model_hash, "a1b2c3d4")
        self.assertEqual(result.model_identity, OTHER_MODEL_IDENTITY)
        self.assertIsNone(result.error)

    def test_parameters_without_settings_have_missing_hash_and_other_model(self) -> None:
        result = self.extract({"parameters": "cat, dog"})
        self.assertEqual(result.tags, ("cat", "dog"))
        self.assertEqual(result.error, "missing_model_hash")
        self.assertEqual(result.model_identity, OTHER_MODEL_IDENTITY)
        self.assertIsNone(result.model_hash)

    def test_settings_only_parameters_have_no_prompt(self) -> None:
        result = self.extract({"parameters": "Steps: 20, Sampler: Euler, Model hash: abcdef12, Model: example"})
        self.assertEqual(result.tags, ())
        self.assertIsNone(result.prompt)
        self.assertEqual(result.error, "no_prompt")
        self.assertEqual(result.model_hash, "abcdef12")
        self.assertEqual(result.model_identity, "example")

    def test_pathological_nesting_is_flagged_and_bounded(self) -> None:
        result = self.extract({"parameters": "(" * 66 + "cat" + ")" * 66})
        self.assertEqual(result.error, "prompt_nesting_too_deep")
        self.assertTrue(any("cat" in tag for tag in result.tags))

    def test_plain_prompt_fields_are_stable_diffusion_without_model(self) -> None:
        for key in ("Description", "prompt"):
            with self.subTest(key=key):
                text = {"parameters": "   ", key: "cat, blue_eyes"}
                if key == "prompt":
                    text["Description"] = " "
                result = self.extract(text)
                self.assertEqual(result.metadata_source, f"text:{key}")
                self.assertEqual(result.prompt_format, "a1111")
                self.assertEqual(result.model_type, "sd")
                self.assertEqual(result.model_identity, OTHER_MODEL_IDENTITY)
                self.assertEqual(result.error, "missing_model_hash")
                self.assertEqual(result.tags, ("cat", "blue_eyes"))

    def test_image_without_generation_metadata_is_no_metadata(self) -> None:
        result = self.extract({"Title": "holiday photo"})
        self.assertEqual(result.error, "no_metadata")
        self.assertEqual(result.model_type, "sd")
        self.assertIsNone(result.prompt)
        self.assertIsNone(result.metadata_source)


class NovelAIPromptSyntaxTests(unittest.TestCase):
    def test_numeric_scopes_exclude_negative_and_zero_weight_spans(self) -> None:
        tags = parse_novelai_prompt(
            "1girl, 1.5::rain, night::, -2.5::hat, glasses::, 0::tie::, "
            "-1::negative outer, -1::positive inner:: ::, outdoors"
        )
        self.assertEqual(tags, ["1girl", "rain", "night", "positive inner", "outdoors"])

    def test_unclosed_negative_scope_excludes_rest_of_prompt(self) -> None:
        self.assertEqual(parse_novelai_prompt("cat, -1::hat, glasses"), ["cat"])

    def test_weighted_scope_closer_separates_tags(self) -> None:
        self.assertEqual(parse_novelai_prompt("1.2::sanoba_witch::1girl"), ["sanoba_witch", "1girl"])
        self.assertEqual(parse_novelai_prompt("cat::dog"), ["cat::dog"])

    def test_nai3_mix_keeps_only_positive_weight_segments(self) -> None:
        self.assertEqual(
            parse_novelai_prompt("cat:1|happy:-0.2|cute:-0.3|style:name", model_type="nai3"),
            ["cat", "style:name"],
        )
        self.assertEqual(parse_novelai_prompt("cat:1|dog:0", model_type="nai3"), ["cat"])
        self.assertEqual(parse_novelai_prompt("cat:1|dog:0.01", model_type="nai3"), ["cat", "dog"])

    def test_pipe_fallback_and_cjk_separators(self) -> None:
        tags = parse_novelai_prompt("||red|blue|| hair｜green，青い目。solo、白髪；帽子")
        self.assertEqual(tags, ["red", "blue", "hair", "green", "青い目", "solo", "白髪", "帽子"])

    def test_fullwidth_weight_syntax_and_cjk_brackets_are_preserved(self) -> None:
        tags = parse_novelai_prompt("（青い目：0.75），－１：：帽子：：，「title」，《作品》")
        self.assertEqual(tags, ["青い目", "「title」", "《作品》"])

    def test_parenthesized_tag_qualifiers_are_preserved(self) -> None:
        tags = parse_novelai_prompt(
            "yuuki noa, yuuki (princess connect!), yuuki (sao), "
            "akizuki kanna(cafe stella to shinigami no chou), "
            "0.5::yd(orange maru), pottsness::"
        )
        self.assertEqual(tags, [
            "yuuki noa",
            "yuuki (princess connect!)",
            "yuuki (sao)",
            "akizuki kanna(cafe stella to shinigami no chou)",
            "yd(orange maru)",
            "pottsness",
        ])

    def test_round_wrappers_are_cleaned_only_at_tag_boundaries(self) -> None:
        tags = parse_novelai_prompt(
            "(masterpiece), (digital painting:1.2), artist_name_(circle), character (series)"
        )
        self.assertEqual(tags, ["masterpiece", "digital painting", "artist_name_(circle)", "character (series)"])

    def test_unmatched_round_punctuation_is_removed_per_tag(self) -> None:
        self.assertEqual(
            parse_novelai_prompt("(legendary Dragon Queen::, (1 girl), hair flowing) detailed sky"),
            ["legendary Dragon Queen::", "1 girl", "hair flowing detailed sky"],
        )
        self.assertEqual(parse_novelai_prompt("cat, ("), ["cat"])
        self.assertEqual(parse_novelai_prompt("cat), dog"), ["cat", "dog"])

    def test_escaped_round_punctuation_remains_literal(self) -> None:
        self.assertEqual(
            parse_novelai_prompt(r"\(literal wrapper\), artist\(circle\), \(unmatched"),
            ["(literal wrapper)", "artist(circle)", "(unmatched"],
        )

    def test_duplicates_collapse_case_insensitively_keeping_first_spelling(self) -> None:
        self.assertEqual(parse_novelai_prompt("Blue Eyes, blue_eyes, BLUE  EYES, cat"), ["Blue Eyes", "cat"])

    def test_scope_nesting_beyond_limit_is_bounded_and_keeps_following_tags(self) -> None:
        self.assertEqual(parse_novelai_prompt("1::" * 65 + "cat" + "::" * 64 + ", dog"), ["cat", "dog"])


class StableDiffusionPromptSyntaxTests(unittest.TestCase):
    def test_grouped_emphasis_and_pipe_fallback(self) -> None:
        result = parse_a1111_parameters("(red hair, blue eyes:1.2), rain|snow")
        self.assertEqual(result.tags, ("red hair", "blue eyes", "rain", "snow"))

    def test_prompt_editing_keeps_every_scheduled_branch(self) -> None:
        result = parse_a1111_parameters("[dog:0.5], [cat:fox:10], [:hat:0.2]")
        self.assertEqual(result.tags, ("dog", "cat", "fox", "hat"))

    def test_cjk_full_stops_separate_tags(self) -> None:
        self.assertEqual(parse_a1111_parameters("cat．dog。bird").tags, ("cat", "dog", "bird"))

    def test_unclosed_wrapper_is_kept_literally(self) -> None:
        self.assertEqual(parse_a1111_parameters("cat, (blue eyes, dog").tags, ("cat", "(blue eyes", "dog"))


class TagNormalizationTests(unittest.TestCase):
    def test_display_keeps_underscores_while_matching_ignores_them_and_case(self) -> None:
        self.assertEqual(split_prompt_tags("blue_hair, Blue Hair"), ["blue_hair"])
        self.assertEqual(normalize_prompt_tag("Sanoba_Witch"), "sanoba witch")
        self.assertEqual(normalize_prompt_tag("sanoba  witch"), normalize_prompt_tag("sanoba_witch"))

    def test_unicode_forms_are_unified(self) -> None:
        self.assertEqual(normalize_prompt_tag("Cafe\u0301"), normalize_prompt_tag("café"))

    def test_model_identity_stays_one_searchable_tag(self) -> None:
        self.assertEqual(sanitize_model_name(" Foo, Bar；Baz。 Quux｜model "), "Foo Bar Baz Quux model")


if __name__ == "__main__":
    unittest.main()
