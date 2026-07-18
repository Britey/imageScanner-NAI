from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from imgdupe.model_paths import (
    ModelPathConfig,
    build_config,
    import_legacy_markers,
    load_model_paths,
    make_rule,
    save_model_paths,
    validate_label,
)


class ModelPathConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_round_trip_preserves_windows_colons_unicode_and_stable_ids(self) -> None:
        config_path = self.base / "model_paths.ini"
        rule = make_rule(self.base / "D：盘" / "画像", "Noob 混合版 v1.5_test-set")

        save_model_paths(config_path, ModelPathConfig((rule,)))
        text = config_path.read_text(encoding="utf-8")
        loaded = load_model_paths(config_path)

        self.assertIn("[rule.1]", text)
        self.assertIn("id = ", text)
        self.assertEqual(loaded.rules, (rule,))
        self.assertEqual(loaded.issues, ())

    def test_actual_windows_drive_colon_is_not_an_ini_delimiter(self) -> None:
        config_path = self.base / "model_paths.ini"
        rule_id = str(uuid.uuid4())
        config_path.write_text(
            f"[rule.1]\nid = {rule_id}\npath = C:\\画像\\Noob\nlabel = 混合 model\n",
            encoding="utf-8",
        )

        loaded = load_model_paths(config_path)

        self.assertEqual(len(loaded.rules), 1)
        self.assertEqual(loaded.rules[0].label, "混合 model")
        self.assertEqual(loaded.rules[0].rule_id, rule_id)

    def test_manual_rule_without_id_gets_a_stable_generated_id(self) -> None:
        config_path = self.base / "model_paths.ini"
        config_path.write_text(
            f"[rule.manual]\npath = {self.base / 'images'}\nlabel = sd1.5\n",
            encoding="utf-8",
        )

        first = load_model_paths(config_path)
        second = load_model_paths(config_path)

        self.assertEqual(len(first.rules), 1)
        self.assertEqual(first.rules[0].rule_id, second.rules[0].rule_id)
        self.assertEqual(first.rules[0].label, "sd1.5")

    def test_closest_configured_ancestor_wins(self) -> None:
        outer = make_rule(self.base / "images", "SD")
        inner = make_rule(self.base / "images" / "special", "Noob")
        config = build_config((outer, inner))

        result = config.resolve(self.base / "images" / "special" / "set" / "x.png")

        self.assertEqual(result, inner)
        self.assertEqual(config.resolve(self.base / "elsewhere" / "x.png"), None)

    def test_labels_allow_unicode_spaces_dots_hyphens_and_underscores(self) -> None:
        label = "  模型 Noob.v1-修正版_test  "
        self.assertEqual(validate_label(label), "模型 Noob.v1-修正版_test")

    def test_labels_reject_query_operators_and_controls(self) -> None:
        for value in ("", "   ", "one,two", "一、二", "a|b", "a｜b", "a\nb", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_label(value)

    def test_duplicate_ids_and_paths_are_rejected_when_saving(self) -> None:
        first = make_rule(self.base / "one", "A")
        duplicate_id = make_rule(self.base / "two", "B", first.rule_id)
        duplicate_path = make_rule(self.base / "one", "C")

        with self.assertRaisesRegex(ValueError, "duplicate.*ID"):
            build_config((first, duplicate_id))
        with self.assertRaisesRegex(ValueError, "duplicate.*path"):
            build_config((first, duplicate_path))

    def test_loader_keeps_first_duplicate_and_reports_later_section(self) -> None:
        config_path = self.base / "model_paths.ini"
        first_id = str(uuid.uuid4())
        config_path.write_text(
            "\n".join(
                (
                    "[rule.1]",
                    f"id = {first_id}",
                    f"path = {self.base / 'same'}",
                    "label = First",
                    "[rule.2]",
                    f"id = {uuid.uuid4()}",
                    f"path = {self.base / 'same'}",
                    "label = Second",
                )
            ),
            encoding="utf-8",
        )

        loaded = load_model_paths(config_path)

        self.assertEqual([rule.label for rule in loaded.rules], ["First"])
        self.assertEqual(len(loaded.issues), 1)
        self.assertEqual(loaded.issues[0].section, "rule.2")

    def test_missing_paths_are_loaded_saved_and_enumerated(self) -> None:
        missing = self.base / "offline-drive" / "images"
        rule = make_rule(missing, "Offline")
        config_path = self.base / "model_paths.ini"

        save_model_paths(config_path, ModelPathConfig((rule,)))
        loaded = load_model_paths(config_path)
        save_model_paths(config_path, loaded)

        self.assertFalse(missing.exists())
        self.assertEqual(loaded.scan_roots(), (missing.resolve(),))

    def test_nested_roots_are_deduplicated_for_rescan(self) -> None:
        outer = make_rule(self.base / "images", "Outer")
        nested = make_rule(self.base / "images" / "nested", "Inner")
        separate = make_rule(self.base / "other", "Other")
        config = build_config((nested, separate, outer))

        roots = config.deduplicated_rescan_roots()

        self.assertEqual(set(roots), {outer.path, separate.path})

    def test_legacy_import_is_stable_non_destructive_and_deduplicated(self) -> None:
        root = self.base / "images"
        marked = root / "nested"
        marked.mkdir(parents=True)
        marker = marked / ".model_type"
        marker.write_text("# old marker\nNoob Legacy\n", encoding="utf-8")

        first_config, first_imported = import_legacy_markers(ModelPathConfig(), (root,))
        second_config, second_imported = import_legacy_markers(first_config, (root,))

        self.assertEqual(len(first_imported), 1)
        self.assertEqual(first_imported[0].path, marked.resolve())
        self.assertEqual(first_imported[0].label, "Noob Legacy")
        self.assertEqual(second_imported, ())
        self.assertEqual(second_config.rules, first_config.rules)
        self.assertTrue(marker.exists())

    def test_legacy_import_skips_missing_roots(self) -> None:
        config, imported = import_legacy_markers(
            ModelPathConfig(),
            (self.base / "missing-network-drive",),
        )
        self.assertEqual(config.rules, ())
        self.assertEqual(imported, ())


if __name__ == "__main__":
    unittest.main()
