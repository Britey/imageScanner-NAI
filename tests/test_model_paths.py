from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from imgdupe import model_paths
from imgdupe.model_paths import (
    ModelPathConfig,
    build_config,
    discover_legacy_markers,
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
        for config in (build_config((outer, inner)), build_config((inner, outer))):
            with self.subTest(order=[rule.label for rule in config.rules]):
                self.assertEqual(config.resolve(self.base / "images" / "special" / "set" / "x.png"), inner)
                self.assertEqual(config.resolve(self.base / "images" / "special"), inner)
                self.assertEqual(config.resolve(self.base / "images" / "other" / "x.png"), outer)
                self.assertEqual(config.resolve(self.base / "images" / "special-set" / "x.png"), outer)
                self.assertIsNone(config.resolve(self.base / "images2" / "x.png"))
                self.assertIsNone(config.resolve(self.base / "elsewhere" / "x.png"))

    def test_rules_on_another_drive_never_match(self) -> None:
        config = build_config((make_rule(self.base / "images", "SD"),))
        with patch.object(model_paths.os.path, "commonpath", side_effect=ValueError("different drives")):
            self.assertIsNone(config.resolve(self.base / "images" / "x.png"))

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

    def test_invalid_sections_are_reported_while_valid_rules_still_load(self) -> None:
        config_path = self.base / "model_paths.ini"
        valid = make_rule(self.base / "valid", "Valid")
        config_path.write_text(
            "\n".join((
                "[general]", "note = sections without the rule. prefix are ignored",
                "[rule.valid]", f"id = {valid.rule_id}", f"path = {valid.path}", "label = Valid",
                "[rule.same-id]", f"id = {valid.rule_id}", f"path = {self.base / 'other'}", "label = Other",
                "[rule.no-label]", f"path = {self.base / 'unlabelled'}",
                "[rule.bad-label]", f"path = {self.base / 'bad'}", "label = a|b",
                "[rule.bad-id]", "id = not-a-uuid", f"path = {self.base / 'id'}", "label = Id",
                "[rule.blank-path]", "path =", "label = Blank",
            )),
            encoding="utf-8",
        )

        loaded = load_model_paths(config_path)

        self.assertEqual(loaded.rules, (valid,))
        self.assertEqual([issue.section for issue in loaded.issues],
                         ["rule.same-id", "rule.no-label", "rule.bad-label", "rule.bad-id", "rule.blank-path"])
        self.assertIn("duplicate rule ID", loaded.issues[0].message)
        self.assertIn("must not be empty", loaded.issues[-1].message)

    def test_unparseable_files_are_reported_instead_of_raising(self) -> None:
        config_path = self.base / "model_paths.ini"
        self.assertEqual(load_model_paths(config_path), ModelPathConfig())
        for content in (b"[rule.1\npath = x\n", b"[rule.1]\nlabel = \xff\n"):
            with self.subTest(content=content):
                config_path.write_bytes(content)
                loaded = load_model_paths(config_path)
                self.assertEqual(loaded.rules, ())
                self.assertEqual([issue.section for issue in loaded.issues], ["<file>"])

    def test_failed_save_keeps_existing_file_and_leaves_no_temporary_file(self) -> None:
        config_path = self.base / "model_paths.ini"
        save_model_paths(config_path, [make_rule(self.base / "one", "One")])
        original = config_path.read_bytes()
        replacement = [make_rule(self.base / "two", "Two")]

        with patch.object(model_paths.os, "replace", side_effect=OSError("drive went offline")), \
                self.assertRaisesRegex(OSError, "offline"):
            save_model_paths(config_path, replacement)
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["model_paths.ini"])

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

    def test_scan_roots_enumerates_duplicate_configured_paths_once(self) -> None:
        first = make_rule(self.base / "images", "A")
        second = make_rule(self.base / "images", "B")
        # Direct API callers may build the public dataclass without the INI validator.
        config = ModelPathConfig((first, second))
        self.assertEqual(config.scan_roots(), (first.path,))

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

    def test_legacy_import_ignores_invalid_empty_and_undecodable_markers_without_touching_them(self) -> None:
        root = self.base / "images"
        contents = {
            "comments": b"# only a comment\n\n",
            "operator": b"bad|label\n",
            "binary": b"\xff\xfe\n",
            "valid": b"\n# label follows\nGood Label\nignored\n",
        }
        for name, content in contents.items():
            (root / name).mkdir(parents=True)
            (root / name / ".model_type").write_bytes(content)

        _config, imported = import_legacy_markers(ModelPathConfig(), (root,))

        self.assertEqual([(rule.path, rule.label) for rule in imported], [(root / "valid", "Good Label")])
        for name, content in contents.items():
            self.assertEqual((root / name / ".model_type").read_bytes(), content)

    def test_legacy_discovery_skips_an_unreadable_root_but_keeps_other_roots(self) -> None:
        offline, online = self.base / "offline", self.base / "online"
        for root in (offline, online):
            root.mkdir()
            (root / ".model_type").write_text(root.name, encoding="utf-8")
        real_rglob = Path.rglob

        def rglob(path, pattern):
            if path == offline:
                raise OSError("network share disconnected")
            return real_rglob(path, pattern)

        with patch.object(Path, "rglob", rglob):
            discovered = discover_legacy_markers((offline, online))

        self.assertEqual([(rule.path, rule.label) for rule in discovered], [(online, "online")])

    def test_legacy_rule_already_imported_and_moved_by_the_user_is_not_reimported(self) -> None:
        root = self.base / "images"
        root.mkdir()
        (root / ".model_type").write_text("Legacy\n", encoding="utf-8")
        imported_once = discover_legacy_markers((root,))[0]
        moved = make_rule(self.base / "moved", "Legacy", imported_once.rule_id)

        config, imported = import_legacy_markers(ModelPathConfig((moved,)), (root,))

        self.assertEqual(imported, ())
        self.assertEqual(config.rules, (moved,))


if __name__ == "__main__":
    unittest.main()
