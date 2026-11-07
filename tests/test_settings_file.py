from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from imgdupe.settings import AppSettings, load_settings


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "settings.ini"

    def load(self, text: str) -> AppSettings:
        self.path.write_text(text, encoding="utf-8")
        return load_settings(self.path)

    def test_missing_file_uses_defaults(self):
        self.assertEqual(load_settings(self.path), AppSettings(thumbnail_cache_limit=1000,
                                                               disable_advanced_options=False))

    def test_configured_web_values_are_read(self):
        for limit in (25, 12345):
            with self.subTest(limit=limit):
                self.assertEqual(
                    self.load(f"[web]\nthumbnail_cache_limit = {limit}\ndisable_advanced_options = yes\n"),
                    AppSettings(limit, True),
                )

    def test_each_invalid_value_falls_back_to_its_own_default(self):
        self.assertEqual(self.load("[web]\nthumbnail_cache_limit = lots\ndisable_advanced_options = maybe\n"),
                         AppSettings())
        self.assertEqual(self.load("[web]\nthumbnail_cache_limit = lots\ndisable_advanced_options = true\n"),
                         AppSettings(1000, True))
        self.assertEqual(self.load("[web]\nthumbnail_cache_limit = 7\ndisable_advanced_options = maybe\n"),
                         AppSettings(7, False))

    def test_nonpositive_cache_limit_keeps_at_least_one_thumbnail(self):
        for value in ("0", "-4"):
            with self.subTest(value=value):
                self.assertEqual(self.load(f"[web]\nthumbnail_cache_limit = {value}\n").thumbnail_cache_limit, 1)


if __name__ == "__main__":
    unittest.main()
