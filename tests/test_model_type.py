from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from imgdupe import model_type

from imgdupe.model_type import ModelTypeResolver


class ModelTypeResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name).resolve()
        self.root = self.base / "scan"
        self.image_directory = self.root / "collection" / "nested"
        self.image_directory.mkdir(parents=True)
        self.image = self.image_directory / "image.png"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_closest_marker_overrides_ancestor(self) -> None:
        (self.root / ".model_type").write_text("sd\n", encoding="utf-8")
        child_marker = self.root / "collection" / ".model_type"
        child_marker.write_text("noob\n", encoding="utf-8")

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "found")
        self.assertTrue(result.is_valid_marker)
        self.assertEqual(result.model_type, "noob")
        self.assertEqual(result.marker_path, child_marker.resolve())
        self.assertIsNotNone(result.marker_signature)

    def test_lookup_includes_root_but_stops_above_it(self) -> None:
        (self.base / ".model_type").write_text("nai4\n", encoding="utf-8")
        resolver = ModelTypeResolver(self.root)

        self.assertEqual(resolver.resolve_for_image(self.image).status, "not_found")

        (self.root / ".model_type").write_text("nai3\n", encoding="utf-8")
        resolver.clear_cache()
        result = resolver.resolve_for_image(self.image)
        self.assertEqual(result.model_type, "nai3")

    def test_value_is_case_insensitive_and_skips_blanks_and_comments(self) -> None:
        marker = self.root / ".model_type"
        marker.write_text("\ufeff  \n  # explanation\n  NaI4  \nignored\n", encoding="utf-8")

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "found")
        self.assertTrue(result.is_valid_marker)
        self.assertEqual(result.model_type, "nai4")

    def test_every_supported_value_is_canonicalized(self) -> None:
        marker = self.root / ".model_type"
        resolver = ModelTypeResolver(self.root)
        for supplied, expected in (
            ("SD", "sd"),
            ("NoOb", "noob"),
            ("NAI3", "nai3"),
            ("nai4", "nai4"),
        ):
            marker.write_text(supplied, encoding="utf-8")
            resolver.clear_cache()
            self.assertEqual(resolver.resolve_for_image(self.image).model_type, expected)

    def test_invalid_marker_is_explicit_and_overrides_valid_ancestor(self) -> None:
        (self.root / ".model_type").write_text("sd", encoding="utf-8")
        invalid_marker = self.root / "collection" / ".model_type"
        invalid_marker.write_text("sdxl", encoding="utf-8")

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "invalid_model_marker")
        self.assertFalse(result.is_valid_marker)
        self.assertIsNone(result.model_type)
        self.assertEqual(result.marker_path, invalid_marker.resolve())
        self.assertIn("unknown model type", result.error or "")

    def test_blank_marker_is_invalid_instead_of_silently_falling_back(self) -> None:
        marker = self.root / ".model_type"
        marker.write_text(" \n# only a comment\n", encoding="utf-8")

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "invalid_model_marker")
        self.assertFalse(result.is_valid_marker)
        self.assertIn("contains no model type", result.error or "")

    def test_directory_results_are_cached_for_one_scan(self) -> None:
        marker = self.root / ".model_type"
        marker.write_text("sd", encoding="utf-8")
        resolver = ModelTypeResolver(self.root)

        first = resolver.resolve_for_image(self.image)
        cache_size = resolver.cached_directory_count
        marker.write_text("nai4", encoding="utf-8")
        second = resolver.resolve_for_image(self.image_directory / "other.png")

        self.assertIs(first, second)
        self.assertEqual(resolver.cached_directory_count, cache_size)
        resolver.clear_cache()
        self.assertEqual(resolver.resolve_for_image(self.image).model_type, "nai4")

    def test_marker_addition_and_deletion_are_visible_after_cache_reset(self) -> None:
        resolver = ModelTypeResolver(self.root)
        self.assertEqual(resolver.resolve_for_image(self.image).status, "not_found")

        marker = self.root / "collection" / ".model_type"
        marker.write_text("noob", encoding="utf-8")
        resolver.clear_cache()
        added = resolver.resolve_for_image(self.image)
        self.assertEqual(added.model_type, "noob")

        marker.unlink()
        resolver.clear_cache()
        self.assertEqual(resolver.resolve_for_image(self.image).status, "not_found")

    def test_paths_outside_scan_root_are_rejected(self) -> None:
        outside = self.base / "outside" / "image.png"
        with self.assertRaisesRegex(ValueError, "outside scan root"):
            ModelTypeResolver(self.root).resolve_for_image(outside)

    def test_unreadable_marker_is_invalid_and_does_not_fall_back_to_ancestor(self) -> None:
        (self.root / ".model_type").write_text("sd", encoding="utf-8")
        marker = self.image_directory / ".model_type"
        marker.mkdir()

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "invalid_model_marker")
        self.assertFalse(result.is_valid_marker)
        self.assertIsNone(result.model_type)
        self.assertIsNone(result.marker_signature)
        self.assertIn("could not read model marker", result.error)

    def test_non_utf8_marker_is_invalid_with_its_signature_preserved(self) -> None:
        marker = self.root / ".model_type"
        marker.write_bytes(b"\xffnai4")

        result = ModelTypeResolver(self.root).resolve_for_image(self.image)

        self.assertEqual(result.status, "invalid_model_marker")
        self.assertFalse(result.is_valid_marker)
        self.assertIsNone(result.model_type)
        self.assertIsNotNone(result.marker_signature)
        self.assertIn("not valid UTF-8", result.error)

    def test_paths_on_another_drive_are_rejected(self) -> None:
        resolver = ModelTypeResolver(self.root)
        with patch.object(model_type.os.path, "commonpath", side_effect=ValueError("different drives")), \
                self.assertRaisesRegex(ValueError, "outside scan root"):
            resolver.resolve_directory(self.root)


if __name__ == "__main__":
    unittest.main()
