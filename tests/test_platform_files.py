from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from imgdupe.utils import human_size, iter_image_paths


class ImageFileDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "images"
        self.root.mkdir()

    def touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_directories_are_searched_recursively_for_supported_extensions(self):
        expected = [
            self.touch("upper.PNG"), self.touch("photo.jpeg"), self.touch("a/b/c/deep.webp"),
            self.touch("folder.jpg/inside.gif"), self.touch("scan.TIFF"),
        ]
        for ignored in ("notes.txt", "image.png.bak", "a/no_extension"):
            self.touch(ignored)

        self.assertEqual(sorted(iter_image_paths([self.root])), sorted(expected))

    def test_file_roots_are_included_only_when_they_are_images(self):
        image = self.touch("single.Jpg")
        text = self.touch("single.txt")
        self.assertEqual(list(iter_image_paths([text, image])), [image])

    @unittest.skipUnless(os.name == "posix", "symlink creation requires POSIX permissions")
    def test_symlinked_images_resolve_to_their_targets_and_broken_links_are_skipped(self):
        outside = self.base / "outside"
        outside.mkdir()
        target = outside / "real.png"
        target.touch()
        (self.root / "link.png").symlink_to(target)
        (self.root / "broken.png").symlink_to(outside / "missing.png")

        self.assertEqual(list(iter_image_paths([self.root])), [target])


class HumanSizeTests(unittest.TestCase):
    def test_sizes_use_binary_units_up_to_gigabytes(self):
        cases = {
            None: "", 0: "0 B", 1023: "1023 B", 1024: "1.0 KB", 1536: "1.5 KB",
            1024 * 1023: "1023.0 KB", 3 * 1024 ** 2 // 2: "1.5 MB", 5 * 1024 ** 3: "5.0 GB",
            2048 * 1024 ** 3: "2048.0 GB",
        }
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(human_size(size), expected)


if __name__ == "__main__":
    unittest.main()
