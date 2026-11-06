"""Behavioral coverage for perceptual hashing, candidate lookup and scoring."""
from __future__ import annotations

import hashlib
import importlib.util
import builtins
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import imagehash
import numpy as np
from PIL import Image

from imgdupe import bands, hashing, match, cluster, query, review
from imgdupe.config import ScanConfig, Thresholds
from imgdupe.db import init_db, upsert_image, replace_hashes, replace_crop_hashes, set_crop_index_state


class SearchDatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.addCleanup(self.conn.close)

    def add_image(self, name, *, hashes=None, sha=None, size=(128, 96)):
        path = self.root / name
        Image.new("RGB", size, "red").save(path)
        image_id = upsert_image(self.conn, path=path, size_bytes=path.stat().st_size,
                                mtime_ns=1, indexed_at="now", width=size[0], height=size[1],
                                image_format="PNG", sha256=sha or hashing.sha256_file(path))
        if hashes is not None:
            replace_hashes(self.conn, image_id, hashes)
            bands.insert_bands(self.conn, image_id, hashes)
        return image_id, path


class HashingTests(SearchDatabaseCase):
    def test_digest_and_bit_packing(self):
        data = self.root / "chunks"
        data.write_bytes(b"abcdefg")
        self.assertEqual(hashing.sha256_file(data, chunk_size=2), hashlib.sha256(b"abcdefg").digest())
        self.assertEqual(hashing.imagehash_to_bytes(imagehash.ImageHash(np.array([[1, 0, 1], [0, 1, 0]], bool))), b"*")
        self.assertEqual(hashing.hamming_bytes(b"\x00\xff", b"\xff\x0f"), 12)
        with self.assertRaisesRegex(ValueError, "lengths 1 and 2"):
            hashing.hamming_bytes(b"a", b"ab")

    def test_transparency_is_composited_onto_white_and_source_format_preserved(self):
        for mode in ("RGBA", "LA", "P", "RGB"):
            with self.subTest(mode=mode):
                path = self.root / f"{mode}.png"
                image = Image.new(mode, (32, 32))
                if mode == "P":
                    image.info["transparency"] = 0
                image.save(path)
                normalized = hashing.load_normalized(path)
                self.assertEqual(normalized.mode, "RGB")
                self.assertEqual(normalized.info["source_format"], "PNG")
                self.assertEqual(normalized.getpixel((0, 0)), (0, 0, 0) if mode == "RGB" else (255, 255, 255))
        path = self.root / "palette-opaque.png"
        Image.new("P", (32, 32)).save(path)
        self.assertEqual(hashing.load_normalized(path).getpixel((0, 0)), (0, 0, 0))
        for dimensions in ((31, 32), (32, 31)):
            Image.new("RGB", dimensions).save(path)
            with self.assertRaisesRegex(hashing.ImageTooSmallError, "too small"):
                hashing.load_normalized(path)

    def test_exif_orientation_changes_dimensions(self):
        path = self.root / "oriented.jpg"
        image = Image.new("RGB", (80, 40))
        exif = Image.Exif()
        exif[274] = 6
        image.save(path, exif=exif)
        self.assertEqual(hashing.load_normalized(path).size, (40, 80))

    def test_region_hashes_and_small_image_cutoff(self):
        large = Image.new("RGB", (128, 128), "blue")
        small = Image.new("RGB", (16, 16), "blue")
        boxes = hashing.crop_region_boxes(128, 128)
        self.assertEqual(boxes["top_25"], (0, 0, 128, 32))
        self.assertEqual(boxes["bottom_right_quarter"], (64, 64, 128, 128))
        self.assertEqual(boxes["center_50"], (32, 32, 96, 96))
        self.assertEqual(hashing._center_box(1, 1, .1), (0, 0, 1, 1))
        self.assertEqual(len(hashing.grid_hashes(large)), 9)
        self.assertEqual(set(hashing.crop_region_hashes(large)), set(boxes))
        self.assertEqual(hashing.crop_region_hashes(small), {})
        self.assertEqual(hashing.tile_hashes(small), {})
        self.assertEqual(len(hashing.tile_hashes(large)), sum(x * y for x, y in hashing.TRYHARD_TILE_LAYOUTS))
        query_hashes = hashing.tryhard_query_hashes(large)
        self.assertIn("crop:center_50", query_hashes)
        self.assertIn("tile:1x1_0_0", query_hashes)
        self.assertTrue(all(len(value) == 32 for value in query_hashes.values()))

    def test_compute_hashes_and_reuse_digest(self):
        _, path = self.add_image("hash.png")
        hashes, metadata = hashing.compute_image_hashes(path)
        self.assertEqual(hashes["sha256"], hashing.sha256_file(path))
        self.assertEqual(set(hashes), {"sha256", *bands.WHOLE_HASH_TYPES, *bands.GRID_HASH_TYPES})
        self.assertEqual(metadata, {"width": 128, "height": 96, "format": "PNG"})
        with patch.object(hashing, "sha256_file", side_effect=AssertionError("must reuse precomputed digest")):
            cropped, _ = hashing.compute_image_hashes(path, include_crop_regions=True, precomputed_sha256=b"digest")
        self.assertEqual(cropped["sha256"], b"digest")
        self.assertIn("crop:center_50", cropped)


class BandTests(SearchDatabaseCase):
    def test_band_splitting_ignores_incomplete_tail(self):
        self.assertEqual(list(bands.split_bands(b"abcde")), [(0, b"ab"), (1, b"cd")])
        self.assertEqual(list(bands.split_bands(b"")), [])
        self.assertEqual(list(bands.split_bands(b"abc", 1)), [(0, b"a"), (1, b"b"), (2, b"c")])

    def test_candidate_thresholds_aliases_exclusion_and_order(self):
        complete = {"phash256": b"abcdefgh", "grid0": b"12", "crop:center_50": b"34"}
        a, _ = self.add_image("a.png", hashes=complete)
        b, _ = self.add_image("b.png", hashes={"phash256": b"abcd"})
        c, _ = self.add_image("c.png", hashes={"phash256": b"ab"})
        d, _ = self.add_image("d.png", hashes={"grid0": b"12"})
        e, _ = self.add_image("e.png", hashes={"crop:center_50": b"34"})
        lookup = {"phash256:query_crop:0": b"abcdefgh", "grid0": b"12", "crop:center_50": b"34", "nonsense": b"xx", "whash256": b""}
        result = bands.find_candidates(self.conn, lookup)
        self.assertEqual([(row.image_id, row.total_hits) for row in result], [(a, 6), (b, 2), (d, 1), (e, 1)])
        self.assertEqual(result[0].hits_by_type, {"phash256": 4, "grid0": 1, "crop:center_50": 1})
        self.assertNotIn(c, [row.image_id for row in result])
        self.assertNotIn(a, [row.image_id for row in bands.find_candidates(self.conn, lookup, exclude_image_id=a)])
        before = self.conn.execute("SELECT COUNT(*) FROM hash_bands").fetchone()[0]
        bands.insert_bands(self.conn, a, complete)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hash_bands").fetchone()[0], before)


class ScoringTests(unittest.TestCase):
    def test_grid_distance_includes_only_common_cells(self):
        self.assertEqual(match.grid_score({}, {}, cell_threshold=1), (0, None))
        self.assertEqual(match.grid_score({"grid0": b"\0", "grid1": b"\0", "grid2": b"\xff"}, {"grid0": b"\0", "grid1": b"\xff", "grid3": b"\0"}, cell_threshold=2), (1, 0))

    def test_crop_comparisons_ignore_mismatched_lengths(self):
        self.assertIsNone(match.crop_score({}, {}, {}, {}))
        self.assertIsNone(match.crop_score({"phash256": b"a"}, {"phash256": b"b"}, {"wrong": b"xx"}, {"wrong": b"yyy"}))
        self.assertEqual(match.crop_score({"phash256": b"\0"}, {"phash256": b"\xff"}, {"a": b"\1", "wrong": b"xx"}, {"b": b"\3", "wrong": b"yyy"}), 1)
        self.assertEqual(match.crop_score({}, {}, {"a": b"\0"}, {"b": b"\xff"}), 8)
        # Isolate tile-to-whole matching so other comparisons cannot hide a regression.
        self.assertEqual(match.crop_score({}, {"phash256": b"\xff"}, {}, {"tile": b"\xfe"}), 1)

    def test_score_exact_partial_and_rejected_matches(self):
        exact = match.score_hashes({}, {}, sha_equal=True)
        self.assertEqual((exact.score, exact.decision), (100, "exact_duplicate"))
        hashes = {key: bytes(32) for key in bands.WHOLE_HASH_TYPES}
        hashes.update({key: bytes(8) for key in bands.GRID_HASH_TYPES})
        identical = match.score_hashes(hashes, hashes)
        self.assertEqual((identical.score, identical.decision), (100, "strong_duplicate"))
        self.assertEqual(match.score_hashes({}, {}).decision, "reject")
        far = {key: b"\xff" * len(value) for key, value in hashes.items()}
        self.assertEqual(match.score_hashes(hashes, far).score, 0)
        crop = match.score_hashes({"phash256": bytes(32)}, {}, crop_hashes_b={"center": bytes(32)})
        self.assertEqual((crop.score, crop.crop_min_dist), (90, 0))
        self.assertIsNone(match._distance_or_none({}, {"x": b"a"}, "x"))
        self.assertIsNone(match._distance_or_none({"x": b"a"}, {}, "x"))

    def test_each_classification_signal_and_threshold(self):
        base = dict(score=0, dhash=None, phash=None, whash=None, grid_matches=0, crop_min=None, thresholds=Thresholds())
        cases = [("strong_duplicate", {"phash": 16}), ("strong_duplicate", {"phash": 24, "whash": 24}),
                 ("strong_duplicate", {"grid_matches": 7}), ("strong_duplicate", {"crop_min": 16}), ("strong_duplicate", {"score": 85}),
                 ("probable_duplicate", {"phash": 32, "whash": 32}), ("probable_duplicate", {"grid_matches": 5}),
                 ("probable_duplicate", {"crop_min": 32}), ("probable_duplicate", {"score": 70}),
                 ("review", {"phash": 40}), ("review", {"whash": 40}), ("review", {"grid_matches": 4}),
                 ("review", {"crop_min": 40}), ("review", {"score": 55}), ("reject", {})]
        for expected, changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(match.classify(**(base | changes)), expected)
        self.assertTrue(match._lte(3, 3))
        self.assertFalse(match._lte(4, 3))
        self.assertFalse(match._lte(None, 3))


class ClusterTests(SearchDatabaseCase):
    def test_union_by_rank_and_compression(self):
        uf = cluster.UnionFind(list(range(1, 8)))
        uf.union(1, 2)  # equal ranks
        uf.union(1, 3)  # left rank wins
        uf.union(4, 1)  # right rank wins
        uf.union(5, 6)
        uf.union(5, 7)
        uf.union(1, 5)
        uf.union(2, 6)  # already connected
        self.assertEqual({uf.find(i) for i in range(1, 8)}, {uf.find(1)})
        self.assertEqual(cluster._groups_from_union_find(uf), [list(range(1, 8))])

    def test_real_clusters_choose_largest_representative_and_review_pages(self):
        hashes = {key: bytes(32) for key in bands.WHOLE_HASH_TYPES}
        a, pa = self.add_image("a&.png", hashes=hashes, sha=b"same", size=(64, 64))
        b, pb = self.add_image("b.png", hashes=hashes, sha=b"same", size=(128, 96))
        c, _ = self.add_image("c.png", hashes={"phash256": b"\xff" * 32}, sha=b"different")
        stats = cluster.build_clusters(self.conn)
        self.assertEqual((stats.images, stats.scored_pairs, stats.stored_matches, stats.clusters, stats.clustered_images), (3, 1, 1, 1, 2))
        self.assertEqual(self.conn.execute("SELECT image_id FROM clusters WHERE representative=1").fetchone()[0], b)
        first = review.generate_review(self.conn, self.root / "review", thumbnail_size=48)
        self.assertEqual((first.clusters, first.images, first.thumbnails), (1, 2, 2))
        payload = __import__("json").loads((first.out_dir / "clusters.json").read_text())["clusters"][0]
        self.assertEqual(payload["representative"], str(pb))
        self.assertEqual(payload["matches"][0]["decision"], "exact_duplicate")
        self.assertIn("a&amp;.png", (first.out_dir / "cluster_000001.html").read_text())
        self.assertIn("cluster_000001.html", (first.out_dir / "index.html").read_text())
        pa.unlink()
        second = review.generate_review(self.conn, first.out_dir, thumbnail_size=48)
        self.assertEqual(second.thumbnails, 1)
        cluster.build_clusters(self.conn, min_score=101)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0], 0)
        empty = review.generate_review(self.conn, self.root / "empty")
        self.assertEqual((empty.images, empty.clusters), (0, 0))

    def test_rejected_review_and_missing_hash_candidates(self):
        a, _ = self.add_image("a.png", hashes={"phash256": b"same"}, sha=b"a")
        b, _ = self.add_image("b.png", hashes={"phash256": b"same"}, sha=b"b")
        c, _ = self.add_image("c.png", hashes={"phash256": b"same"}, sha=b"c")
        d, _ = self.add_image("d.png", hashes={"phash256": b"same"}, sha=b"d")
        loaded = cluster._load_hashes(self.conn, [{"id": a}, {"id": 999}])
        self.assertEqual(set(loaded), {a})
        def candidates(_conn, _hashes, **kwargs):
            return [bands.Candidate(i, 2, {}) for i in (a, b, c, d, 999)]
        rejected = match.PairScore(200, 200, 200, 0, None, None, 0, "reject")
        low = match.PairScore(30, 30, 30, 0, None, None, 60, "probable_duplicate")
        review_pair = match.PairScore(30, 30, 30, 0, None, None, 80, "review")
        with patch.object(cluster, "_load_hashes", return_value={a: {}, b: {}, c: {}}), patch.object(cluster, "find_candidates", side_effect=candidates), patch.object(cluster, "score_hashes", side_effect=[rejected, low, review_pair]):
            stats = cluster.build_clusters(self.conn)
        self.assertEqual((stats.scored_pairs, stats.stored_matches, stats.clusters), (3, 1, 0))
        self.assertEqual(self.conn.execute("SELECT decision FROM matches").fetchone()[0], "review")
        # Updating a pair in reverse order must replace the same canonical record.
        cluster._store_match(self.conn, c, b, low, "later")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT score FROM matches").fetchone()[0], 60)

    def test_review_empty_and_incomplete_display_records(self):
        self.assertEqual(review._fetch_cluster_matches(self.conn, []), [])
        self.assertIsNone(review._representative_path([]))
        self.assertEqual(review._representative_path([{"path": "fallback", "representative": 0}]), "fallback")
        self.assertEqual(review._representative_path([{"path": "a", "representative": 0}, {"path": "b", "representative": 1}]), "b")
        row = dict(id=1, path="x", representative=0, width=None, height=None, size_bytes=0, format=None, mtime_ns=0)
        self.assertEqual(review._image_json(row, "thumb")["dimensions"], "")
        review._write_index_page(self.root / "empty-cluster.html", [{"cluster_id": 1, "count": 0, "images": [], "representative": None}])
        self.assertIn('src=""', (self.root / "empty-cluster.html").read_text())


class QueryTests(SearchDatabaseCase):
    def test_real_image_search_exact_filter_limit_and_tryhard(self):
        a, pa = self.add_image("a.png")
        hashes, _ = hashing.compute_image_hashes(pa, include_crop_regions=True)
        replace_hashes(self.conn, a, hashes)
        replace_crop_hashes(self.conn, a, hashes)
        bands.insert_bands(self.conn, a, hashes)
        b, pb = self.add_image("b.png", hashes=hashes)
        replace_crop_hashes(self.conn, b, hashes)
        found = query.query_image(self.conn, pa)
        self.assertEqual([item[0]["id"] for item in found], [b])
        self.assertEqual(found[0][2].decision, "exact_duplicate")
        self.assertEqual(query.query_image(self.conn, pa, include_exact=False), [])
        self.assertEqual([item[0]["id"] for item in query.query_image(self.conn, pa, min_score=100)], [b])
        self.assertEqual(query.query_image(self.conn, pa, min_score=101), [])
        with self.assertRaisesRegex(ValueError, "limit must be positive"):
            query.query_image(self.conn, pa, limit=0)
        self.assertEqual(query.query_image(self.conn, pb, tryhard=True)[0][0]["id"], a)
        set_crop_index_state(self.conn, "ready")
        self.assertEqual(query.query_image(self.conn, pa, tryhard=True)[0][0]["id"], b)
        external = self.root / "not-indexed.png"
        Image.new("RGB", (128, 96), "red").save(external)
        self.assertEqual(len(query.query_image(self.conn, external, limit=1)), 1)
        out = self.root / "nested" / "query.html"
        query.write_query_html(out, pa, found)
        html = out.read_text()
        self.assertIn(pb.as_uri(), html)
        self.assertIn("100.00", html)
        self.assertIn("exact_duplicate", html)
        query.write_query_html(out, pa, [])
        self.assertIn("<tbody></tbody>", out.read_text())

    def test_stale_candidates_are_skipped_and_rejects_filtered(self):
        a, pa = self.add_image("a.png", hashes={"phash256": bytes(32)}, sha=b"a")
        b, pb = self.add_image("b.png", hashes={"phash256": bytes(32)}, sha=b"b")
        candidates = [bands.Candidate(999, 2, {}), bands.Candidate(998, 2, {}), bands.Candidate(b, 2, {})]
        rejected = match.PairScore(None, None, None, 0, None, None, 0, "reject")
        with patch.object(query, "find_candidates", return_value=candidates), patch.object(query, "score_hashes", return_value=rejected):
            self.assertEqual(query.query_image(self.conn, pa), [])
        # A partial hash result without pHash is valid: tryhard still searches tiles.
        set_crop_index_state(self.conn, "ready")
        with patch.object(query, "compute_image_hashes", return_value=({"sha256": b"q"}, {})), patch.object(query, "find_candidates", return_value=[]):
            self.assertEqual(query.query_image(self.conn, pa, tryhard=True), [])

    def test_prompt_query_validation_group_normalization_and_parens(self):
        self.assertEqual(query.query_prompt_tags(self.conn, ""), [])
        with self.assertRaisesRegex(ValueError, "match_mode"):
            query.query_prompt_tags(self.conn, "cat", match_mode="bad")
        self.assertEqual(query._prompt_tag_norms(["cat | dog", "cat | dog", "cat | cat", "", "@model:sd"]), ["cat", "dog", "cat", "@model:sd"])
        self.assertEqual(query._negative_prompt_tag_terms("cat | dog, cat"), [("cat", False), ("dog", False)])
        self.assertEqual(query._prompt_tag_groups(None), [])
        cases = {"((cat | dog))": "cat | dog", "(cat) | (dog)": "(cat) | (dog)", "(cat))": "(cat))", "((cat)": "((cat)", "(cat))(dog)": "(cat))(dog)"}
        for text, expected in cases.items():
            self.assertEqual(query._strip_whole_group_parentheses(text), expected)

    def test_prompt_negative_only_and_model_filter(self):
        a, _ = self.add_image("a.png")
        self.conn.execute("INSERT INTO image_prompts(image_id, prompt, prompt_indexed_at, model_type) VALUES (?, 'cat', 'now', 'sd')", (a,))
        self.conn.executemany("INSERT INTO prompt_tags(image_id, tag, tag_norm, position, tag_kind) VALUES (?, ?, ?, 0, ?)", [(a, "cat", "cat", "prompt"), (a, "@model:sd", "@model:sd", "model_type")])
        results = query.query_prompt_tags(self.conn, "", negative_text_or_tags="dog", model_type="SD", unique_prompts=True)
        self.assertEqual([item.image_row["id"] for item in results], [a])
        self.assertEqual(query.query_prompt_tags(self.conn, "cat", model_type="nai3"), [])
        any_results = query.query_prompt_tags(self.conn, "cat | dog, mouse", match_mode="any")
        self.assertEqual([(item.image_row["id"], item.matched_count) for item in any_results], [(a, 1)])
        self.assertEqual(query.query_prompt_tags(self.conn, "", negative_text_or_tags="cat"), [])


class OptionalProgressTests(unittest.TestCase):
    def test_clustering_and_review_work_without_tqdm(self):
        original_import = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == "tqdm":
                raise ImportError("optional progress display unavailable")
            return original_import(name, *args, **kwargs)
        for original in (cluster, review):
            name = f"imgdupe._coverage_optional_{original.__name__.rsplit('.', 1)[1]}"
            spec = importlib.util.spec_from_file_location(name, original.__file__)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {name: module}), patch("builtins.__import__", side_effect=importing):
                spec.loader.exec_module(module)
                self.assertEqual(module.tqdm([1, 2]), [1, 2])
                self.assertEqual(module.tqdm(), ())
