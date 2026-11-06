from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imgdupe import model_paths as mp, model_type as mt, single_instance as si
from imgdupe.settings import AppSettings, load_settings
from imgdupe.utils import human_size, iter_image_paths


class PlatformCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_settings_invalid_values_and_minimum(self):
        path = self.root / "settings.ini"
        self.assertEqual(load_settings(path), AppSettings())
        path.write_text("[web]\nthumbnail_cache_limit=no\ndisable_advanced_options=nope\n")
        self.assertEqual(load_settings(path), AppSettings())
        path.write_text("[web]\nthumbnail_cache_limit=-4\ndisable_advanced_options=yes\n")
        self.assertEqual(load_settings(path), AppSettings(1, True))

    def test_image_iteration_and_sizes(self):
        image = self.root / "x.PNG"
        image.touch()
        text = self.root / "x.txt"
        text.touch()
        (self.root / "subdir").mkdir()
        self.assertEqual(list(iter_image_paths([text])), [])
        self.assertEqual(list(iter_image_paths([image])), [image.resolve()])
        self.assertEqual(list(iter_image_paths([self.root])), [image.resolve()])
        self.assertEqual([human_size(x) for x in (None, 1, 1024, 1024**2, 1024**3, 1024**5)],
                         ["", "1 B", "1.0 KB", "1.0 MB", "1.0 GB", "1048576.0 GB"])

    @unittest.skipIf(os.name == "nt", "POSIX flock integration; Win32 mutex is tested separately")
    def test_file_lock_contention_reentrancy_and_cleanup(self):
        # A private lock directory avoids interfering with a running application.
        with patch.object(si.tempfile, "gettempdir", return_value=str(self.root)):
            first, second = si.SingleInstanceLock("test / lock"), si.SingleInstanceLock("test / lock")
            self.addCleanup(first.release)
            self.addCleanup(second.release)
            self.assertTrue(first.acquire())
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            self.assertEqual(list(self.root.iterdir())[0].name, "test---lock.lock")

    @unittest.skipIf(os.name == "nt", "fcntl is POSIX-only")
    def test_file_unlock_failure_still_closes_file(self):
        lock = si.SingleInstanceLock()
        file = lock._file = Mock()
        with patch("fcntl.flock", side_effect=OSError("unlock")), self.assertRaises(OSError):
            lock.release()
        file.close.assert_called_once()
        self.assertIsNone(lock._file)

    def test_windows_mutex_success_reentrancy_duplicate_and_failure(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 123
        # Add only the absent Windows APIs on Linux, never call the real Win32 APIs.
        with patch.object(si.ctypes, "WinDLL", return_value=kernel, create=True), patch.object(
            si.ctypes, "set_last_error", create=True
        ), patch.object(si.ctypes, "get_last_error", return_value=0, create=True) as error, patch.object(
            si.ctypes, "WinError", side_effect=lambda code: OSError(code, "mutex"), create=True
        ), patch.object(si, "os", SimpleNamespace(name="nt")):
            lock = si.SingleInstanceLock("private")
            self.assertTrue(lock.acquire())
            self.assertTrue(lock.acquire())
            kernel.CreateMutexW.assert_called_once_with(None, False, "Local\\private")
            lock.release()
            kernel.CloseHandle.assert_called_with(123)
            self.assertIsNone(lock._handle)
            error.return_value = si.ERROR_ALREADY_EXISTS
            self.assertFalse(lock.acquire())
            self.assertIsNone(lock._handle)
            kernel.CreateMutexW.return_value = None
            error.return_value = 5
            with self.assertRaises(OSError):
                lock.acquire()

    def test_notifications_work_without_windows_or_on_windows_api_error(self):
        with patch.object(si, "os", SimpleNamespace(name="posix")), contextlib.redirect_stdout(io.StringIO()) as output:
            si.notify_already_running()
        self.assertIn("already running", output.getvalue())
        windows = Mock()
        with patch.object(si, "os", SimpleNamespace(name="nt")), patch.object(
            si.ctypes, "windll", windows, create=True
        ), contextlib.redirect_stdout(io.StringIO()):
            si.notify_already_running()
            windows.user32.MessageBoxW.assert_called_once()
            windows.user32.MessageBoxW.side_effect = OSError("no UI")
            si.notify_already_running()

    def test_model_config_malformed_file_sections_ids_and_empty_paths(self):
        path = self.root / "model.ini"
        path.write_text("[broken")
        self.assertEqual(mp.load_model_paths(path).issues[0].section, "<file>")
        rule = mp.make_rule(self.root / "one", "A")
        path.write_text(f"[unrelated]\nx=y\n[rule.1]\nid={rule.rule_id}\npath={rule.path}\nlabel=A\n"
                        f"[rule.2]\nid={rule.rule_id}\npath={self.root / 'two'}\nlabel=B\n")
        loaded = mp.load_model_paths(path)
        self.assertEqual(loaded.rules, (rule,))
        self.assertIn("duplicate rule ID", loaded.issues[0].message)
        with self.assertRaisesRegex(ValueError, "path must not be empty"):
            mp.canonical_path(" ")
        with self.assertRaisesRegex(ValueError, "invalid.*ID"):
            mp.make_rule(self.root, "A", "invalid")
        duplicate = mp.ModelPathConfig((rule, rule))
        self.assertEqual(duplicate.scan_roots(), (rule.path,))
        with patch.object(mp.os.path, "commonpath", side_effect=ValueError("drives")):
            self.assertFalse(mp.path_is_within(self.root, self.root))

    def test_atomic_save_removes_temporary_file_after_error(self):
        rule = mp.make_rule(self.root, "A")
        target = self.root / "config.ini"
        mp.save_model_paths(target, [rule])
        original = target.read_bytes()
        with patch.object(mp.os, "replace", side_effect=OSError("offline")), self.assertRaises(OSError):
            mp.save_model_paths(target, (rule,))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        real_unlink = Path.unlink
        def missing_after_unlink(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            raise FileNotFoundError(path)
        with patch.object(mp.os, "replace", side_effect=OSError("offline")), patch.object(
            Path, "unlink", missing_after_unlink
        ), self.assertRaises(OSError):
            mp.save_model_paths(target, (rule,))
        # An error before a temporary file exists must preserve the destination too.
        with patch.object(mp.tempfile, "NamedTemporaryFile", side_effect=OSError("full")), self.assertRaises(OSError):
            mp.save_model_paths(target, (rule,))
        self.assertEqual(target.read_bytes(), original)

    def test_legacy_markers_invalid_empty_unreadable_and_walk_errors(self):
        marker = self.root / ".model_type"
        marker.write_text("# comment\n\n")
        self.assertIsNone(mp._read_legacy_label(marker))
        marker.write_text("bad|label\n")
        self.assertIsNone(mp._read_legacy_label(marker))
        self.assertEqual(mp.discover_legacy_markers([self.root]), ())
        marker.write_bytes(b"\xff")
        self.assertIsNone(mp._read_legacy_label(marker))
        with patch.object(Path, "rglob", side_effect=OSError("offline")):
            self.assertEqual(mp.discover_legacy_markers([self.root]), ())
        marker.write_text("Legacy\n")
        found = mp.discover_legacy_markers([self.root])[0]
        existing = mp.make_rule(self.root / "other", "Other", found.rule_id)
        config, imported = mp.import_legacy_markers(mp.ModelPathConfig((existing,)), [self.root])
        self.assertEqual(imported, ())
        self.assertEqual(config.rules, (existing,))

    def test_marker_resolution_diagnostics_errors_and_clear_cache(self):
        resolver = mt.ModelTypeResolver(self.root)
        marker = self.root / ".model_type"
        marker.write_text("sd\n")
        result = resolver.resolve_directory(self.root)
        self.assertTrue(result.is_valid_marker)
        self.assertEqual(resolver.cached_directory_count, 1)
        resolver.clear_cache()
        self.assertEqual(resolver.cached_directory_count, 0)
        with patch.object(Path, "read_bytes", side_effect=OSError("offline")):
            result = mt._read_marker(marker)
            self.assertEqual(result.status, "invalid_model_marker")
            self.assertIn("could not read", result.error)
        marker.write_bytes(b"\xff")
        self.assertIn("UTF-8", mt._read_marker(marker).error)
        marker.write_text("# comment\n\n")
        self.assertIn("no model type", mt._read_marker(marker).error)
        with patch.object(mt.os.path, "commonpath", side_effect=ValueError("drive")), self.assertRaisesRegex(
            ValueError, "outside scan root"
        ):
            resolver.resolve_directory(self.root)
