"""Perceptual hashes, band candidate retrieval and pair scoring."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

from imgdupe import bands, hashing, match
from imgdupe.config import Thresholds
from imgdupe.db import init_db, replace_hashes, upsert_image


from image_fixtures import textured


class HashingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save(self, image: Image.Image, name: str, **options) -> Path:
        path = self.root / name
        image.save(path, **options)
        return path

    def test_sha256_streams_the_complete_file(self):
        data = self.root / "chunks"
        data.write_bytes(b"abcdefg")
        self.assertEqual(hashing.sha256_file(data, chunk_size=2), hashlib.sha256(b"abcdefg").digest())

    def test_hamming_distance_counts_only_differing_bits(self):
        self.assertEqual(hashing.hamming_bytes(b"\x00\xff", b"\xff\x0f"), 12)
        self.assertEqual(hashing.hamming_bytes(b"\xa5\x5a", b"\xa5\x5a"), 0)
        self.assertEqual(hashing.hamming_bytes(b"\xff\xff", b"\xff\xfe"), 1)
        with self.assertRaisesRegex(ValueError, "lengths 1 and 2"):
            hashing.hamming_bytes(b"a", b"ab")

    def test_hash_bits_are_packed_most_significant_first(self):
        bits = imagehash.ImageHash(np.array([[1, 0, 1], [0, 1, 0]], bool))
        self.assertEqual(hashing.imagehash_to_bytes(bits), b"*")

    def test_transparency_is_composited_onto_white_and_source_format_preserved(self):
        for mode in ("RGBA", "LA", "P", "RGB"):
            with self.subTest(mode=mode):
                image = Image.new(mode, (32, 32))
                if mode == "P":
                    image.info["transparency"] = 0
                normalized = hashing.load_normalized(self.save(image, f"{mode}.png"))
                self.assertEqual(normalized.mode, "RGB")
                self.assertEqual(normalized.info["source_format"], "PNG")
                self.assertEqual(normalized.getpixel((0, 0)), (0, 0, 0) if mode == "RGB" else (255, 255, 255))
        opaque_palette = self.save(Image.new("P", (32, 32)), "palette-opaque.png")
        self.assertEqual(hashing.load_normalized(opaque_palette).getpixel((0, 0)), (0, 0, 0))

    def test_images_too_narrow_or_too_short_are_rejected(self):
        for dimensions in ((31, 32), (32, 31), (31, 31)):
            with self.subTest(dimensions=dimensions):
                path = self.save(Image.new("RGB", dimensions), "small.png")
                with self.assertRaisesRegex(hashing.ImageTooSmallError, "too small"):
                    hashing.compute_image_hashes(path)
        self.assertEqual(hashing.load_normalized(self.save(Image.new("RGB", (32, 32)), "minimum.png")).size, (32, 32))
        wide = self.save(Image.new("RGB", (48, 100)), "configured.png")
        with self.assertRaises(hashing.ImageTooSmallError):
            hashing.compute_image_hashes(wide, min_width=64)
        with self.assertRaises(hashing.ImageTooSmallError):
            hashing.compute_image_hashes(wide, min_height=128)

    def test_exif_orientation_is_applied_before_hashing(self):
        exif = Image.Exif()
        exif[274] = 6
        path = self.save(textured(12, (80, 40)), "oriented.jpg", exif=exif)
        with Image.open(path) as decoded:
            expected = decoded.convert("RGB").transpose(Image.Transpose.ROTATE_270)
        normalized = hashing.load_normalized(path)
        self.assertEqual(normalized.tobytes(), expected.tobytes())
        expected_path = self.save(expected, "expected.png")
        oriented_hashes, _ = hashing.compute_image_hashes(path)
        expected_hashes, _ = hashing.compute_image_hashes(expected_path)
        for signal in bands.WHOLE_HASH_TYPES + bands.GRID_HASH_TYPES:
            self.assertEqual(oriented_hashes[signal], expected_hashes[signal], signal)
        self.assertEqual(hashing.load_normalized(path).size, (40, 80))
        _, metadata = hashing.compute_image_hashes(path)
        self.assertEqual((metadata["width"], metadata["height"], metadata["format"]), (40, 80, "JPEG"))

    def test_resized_and_reencoded_copies_hash_close_while_unrelated_images_do_not(self):
        source = textured(1)
        original, _ = hashing.compute_image_hashes(self.save(source, "source.png"))
        variants = {
            "larger": self.save(source.resize((512, 384), Image.Resampling.LANCZOS), "larger.png"),
            "jpeg": self.save(source, "source.jpg", quality=70),
        }
        for name, path in variants.items():
            hashes, _ = hashing.compute_image_hashes(path)
            for hash_type in bands.WHOLE_HASH_TYPES:
                with self.subTest(variant=name, hash_type=hash_type):
                    self.assertLessEqual(hashing.hamming_bytes(original[hash_type], hashes[hash_type]), 8)
        for seed in (2, 3, 4):
            unrelated, _ = hashing.compute_image_hashes(self.save(textured(seed), f"unrelated-{seed}.png"))
            with self.subTest(seed=seed):
                self.assertGreater(hashing.hamming_bytes(original["phash256"], unrelated["phash256"]), 80)
                self.assertGreater(hashing.hamming_bytes(original["whash256"], unrelated["whash256"]), 80)

    def test_crop_regions_reproduce_the_hash_of_the_cropped_image(self):
        source = textured(5)
        hashes, _ = hashing.compute_image_hashes(self.save(source, "source.png"), include_crop_regions=True)
        boxes = hashing.crop_region_boxes(256, 192)
        self.assertEqual(set(boxes), set(bands.CROP_REGION_NAMES))
        self.assertEqual(boxes["top_25"], (0, 0, 256, 48))
        self.assertEqual(boxes["right_50"], (128, 0, 256, 192))
        self.assertEqual(boxes["center_50"], (64, 48, 192, 144))
        self.assertEqual(boxes["bottom_right_quarter"], (128, 96, 256, 192))
        for region in ("top_left_quarter", "bottom_right_quarter", "center_50", "left_50"):
            with self.subTest(region=region):
                crop, _ = hashing.compute_image_hashes(self.save(source.crop(boxes[region]), f"{region}.png"))
                self.assertEqual(hashes[f"crop:{region}"], crop["phash256"])
        self.assertEqual(hashing.crop_region_boxes(1, 1)["center_50"], (0, 0, 1, 1))

    def test_regions_and_tiles_below_minimum_size_are_skipped(self):
        small = Image.new("RGB", (48, 48), "blue")
        self.assertEqual(set(hashing.tile_hashes(small)), {"1x1_0_0"})
        self.assertEqual(set(hashing.crop_region_hashes(small)),
                         {"top_67", "bottom_67", "left_67", "right_67", "top_75", "bottom_75", "left_75", "right_75",
                          "center_95", "center_90", "center_80", "center_70"})
        self.assertEqual(hashing.crop_region_hashes(Image.new("RGB", (16, 16))), {})
        large = textured(6, (128, 128))
        self.assertEqual(len(hashing.grid_hashes(large)), 9)
        tiles = hashing.tile_hashes(large)
        self.assertEqual(len(tiles), sum(columns * rows for columns, rows in hashing.TRYHARD_TILE_LAYOUTS))
        self.assertEqual(tiles["1x1_0_0"], hashing.imagehash_to_bytes(imagehash.phash(large, hash_size=16)))

    def test_compute_hashes_selects_signals_and_reuses_a_known_digest(self):
        path = self.save(textured(7), "image.png")
        hashes, metadata = hashing.compute_image_hashes(path)
        self.assertEqual(set(hashes), {"sha256", *bands.WHOLE_HASH_TYPES, *bands.GRID_HASH_TYPES})
        self.assertEqual(hashes["sha256"], hashing.sha256_file(path))
        self.assertEqual(metadata, {"width": 256, "height": 192, "format": "PNG"})
        self.assertTrue(all(len(hashes[key]) == 32 for key in bands.WHOLE_HASH_TYPES))
        self.assertTrue(all(len(hashes[key]) == 8 for key in bands.GRID_HASH_TYPES))
        cropped, _ = hashing.compute_image_hashes(path, include_crop_regions=True, precomputed_sha256=b"digest")
        self.assertEqual(cropped["sha256"], b"digest")
        self.assertEqual({key for key in cropped if key.startswith("crop:")}, set(bands.CROP_HASH_TYPES))
        self.assertFalse(any(key.startswith("tile:") for key in cropped))
        tryhard, _ = hashing.compute_image_hashes(path, include_query_tiles=True)
        self.assertEqual({key: value for key, value in tryhard.items() if key.startswith(("crop:", "tile:"))},
                         hashing.tryhard_query_hashes(hashing.load_normalized(path)))


class BandTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_db(self.conn)

    def add(self, name, hashes):
        image_id = upsert_image(self.conn, path=Path(name), size_bytes=1, mtime_ns=1, indexed_at="now",
                                width=64, height=64, image_format="PNG", sha256=name.encode())
        replace_hashes(self.conn, image_id, hashes)
        bands.insert_bands(self.conn, image_id, hashes)
        return image_id

    def test_bands_ignore_an_incomplete_tail(self):
        self.assertEqual(list(bands.split_bands(b"abcde")), [(0, b"ab"), (1, b"cd")])
        self.assertEqual(list(bands.split_bands(b"")), [])
        self.assertEqual(list(bands.split_bands(b"abc", 1)), [(0, b"a"), (1, b"b"), (2, b"c")])

    def test_candidates_need_two_whole_bands_or_any_grid_or_crop_band(self):
        complete = {"phash256": b"abcdefgh", "grid0": b"12", "crop:center_50": b"34"}
        a = self.add("a", complete)
        b = self.add("b", {"phash256": b"abcd"})
        c = self.add("c", {"phash256": b"ab"})
        d = self.add("d", {"grid0": b"12"})
        e = self.add("e", {"crop:center_50": b"34"})
        lookup = {"phash256:query_crop:0": b"abcdefgh", "grid0": b"12", "crop:center_50": b"34",
                  "nonsense": b"xx", "whash256": b""}
        result = bands.find_candidates(self.conn, lookup)
        self.assertEqual([(row.image_id, row.total_hits) for row in result], [(a, 6), (b, 2), (d, 1), (e, 1)])
        self.assertEqual(result[0].hits_by_type, {"phash256": 4, "grid0": 1, "crop:center_50": 1})
        self.assertNotIn(c, [row.image_id for row in result])
        self.assertNotIn(a, [row.image_id for row in bands.find_candidates(self.conn, lookup, exclude_image_id=a)])
        self.assertEqual([row.image_id for row in bands.find_candidates(self.conn, lookup, min_image_id=b)], [d, e])

    def test_reinserting_bands_is_idempotent(self):
        hashes = {"phash256": b"abcdefgh", "grid0": b"12"}
        image_id = self.add("a", hashes)
        before = self.conn.execute("SELECT COUNT(*) FROM hash_bands").fetchone()[0]
        bands.insert_bands(self.conn, image_id, hashes)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hash_bands").fetchone()[0], before)
        self.assertEqual(before, 5)


class ScoringTests(unittest.TestCase):
    def test_grid_matches_count_only_cells_present_in_both_images(self):
        self.assertEqual(match.grid_score({}, {}, cell_threshold=1), (0, None))
        self.assertEqual(match.grid_score({"grid0": b"\0", "grid1": b"\0", "grid2": b"\xff"},
                                          {"grid0": b"\0", "grid1": b"\xff", "grid3": b"\0"}, cell_threshold=2), (1, 0))

    def test_crop_distance_compares_every_direction_and_ignores_mismatched_lengths(self):
        self.assertIsNone(match.crop_score({}, {}, {}, {}))
        self.assertIsNone(match.crop_score({"phash256": b"a"}, {"phash256": b"b"}, {"wrong": b"xx"}, {"wrong": b"yyy"}))
        self.assertEqual(match.crop_score({"phash256": b"\0"}, {"phash256": b"\xff"},
                                          {"a": b"\1", "wrong": b"xx"}, {"b": b"\3", "wrong": b"yyy"}), 1)
        self.assertEqual(match.crop_score({}, {}, {"a": b"\0"}, {"b": b"\xff"}), 8)
        self.assertEqual(match.crop_score({"phash256": b"\xff"}, {}, {"a": b"\xfe"}, {}), 1)
        self.assertEqual(match.crop_score({}, {"phash256": b"\xff", "dhash256": b"\x00"}, {}, {"tile": b"\xfe"}), 1)
        self.assertEqual(match.crop_score({}, {"phash256": b"\xfe", "dhash256": b"\xfe"}, {}, {"tile": b"\xfe"}), 0)

    def test_scores_for_exact_identical_partial_and_unrelated_hashes(self):
        exact = match.score_hashes({}, {}, sha_equal=True)
        self.assertEqual((exact.score, exact.decision), (100, "exact_duplicate"))
        hashes = {key: bytes(32) for key in bands.WHOLE_HASH_TYPES}
        hashes.update({key: bytes(8) for key in bands.GRID_HASH_TYPES})
        identical = match.score_hashes(hashes, hashes)
        self.assertEqual((identical.score, identical.decision, identical.grid_match_count), (100, "strong_duplicate", 9))
        self.assertEqual(match.score_hashes({}, {}).decision, "reject")
        far = {key: b"\xff" * len(value) for key, value in hashes.items()}
        rejected = match.score_hashes(hashes, far)
        self.assertEqual((rejected.score, rejected.decision), (0, "reject"))
        crop = match.score_hashes({"phash256": bytes(32)}, {}, crop_hashes_b={"center": bytes(32)})
        self.assertEqual((crop.score, crop.crop_min_dist, crop.phash_dist), (90, 0, None))

    def test_each_classification_signal_and_inclusive_threshold(self):
        base = dict(score=0, dhash=None, phash=None, whash=None, grid_matches=0, crop_min=None, thresholds=Thresholds())
        cases = [("strong_duplicate", {"phash": 16}), ("strong_duplicate", {"phash": 24, "whash": 24}),
                 ("strong_duplicate", {"grid_matches": 7}), ("strong_duplicate", {"crop_min": 16}),
                 ("strong_duplicate", {"score": 85}),
                 ("probable_duplicate", {"phash": 17, "whash": 32}), ("probable_duplicate", {"phash": 24, "whash": 25}),
                 ("probable_duplicate", {"phash": 32, "whash": 32}), ("probable_duplicate", {"grid_matches": 5}),
                 ("probable_duplicate", {"crop_min": 32}), ("probable_duplicate", {"score": 70}),
                 ("review", {"phash": 33, "whash": 32}), ("review", {"phash": 40}), ("review", {"whash": 40}),
                 ("review", {"grid_matches": 4}), ("review", {"crop_min": 40}), ("review", {"score": 55}),
                 ("reject", {}), ("reject", {"phash": 41, "whash": 41, "dhash": 0}), ("reject", {"score": 54.99}),
                 ("reject", {"grid_matches": 3, "crop_min": 41})]
        for expected, changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(match.classify(**(base | changes)), expected)


if __name__ == "__main__":
    unittest.main()
