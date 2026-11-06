"""Regression tests for exact pruning and bounded large-index work."""
from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from imgdupe import bands, cluster, hashing, match, nai_prompt, review
from imgdupe.db import init_db


class ExactScoringTests(unittest.TestCase):
    def test_crop_scores_match_exhaustive_reference_with_duplicates_and_lengths(self):
        rng = random.Random(41)
        for _ in range(150):
            def values():
                return {str(i): rng.randbytes(rng.choice((0, 1, 2, 32))) for i in range(rng.randrange(6))}
            qa, cb = values(), values()
            q, c = values(), values()
            if q:
                q = {"phash256": next(iter(q.values()))}
            if c:
                c = {"phash256": next(iter(c.values()))}
            pairs = []
            if "phash256" in q:
                pairs.extend((q["phash256"], x) for x in cb.values())
            if "phash256" in c:
                pairs.extend((x, c["phash256"]) for x in qa.values())
            pairs.extend((a, b) for a in qa.values() for b in cb.values())
            distances = [hashing.hamming_bytes(a, b) for a, b in pairs if len(a) == len(b)]
            self.assertEqual(match.crop_score(q, c, cb, qa), min(distances) if distances else None)
        self.assertEqual(match.crop_score({"phash256": b"same"}, {}, {"a": b"same"}, {}), 0)
        self.assertEqual(match.crop_score({}, {"phash256": b"same"}, {}, {"a": b"same"}), 0)
        self.assertEqual(match.crop_score({}, {}, {"a": b"same"}, {"a": b"same", "dup": b"same"}), 0)

    def test_upper_bound_skips_grid_only_when_score_cannot_qualify(self):
        low = {key: b"\x00" * 32 for key in bands.WHOLE_HASH_TYPES}
        far = {key: b"\xff" * 32 for key in bands.WHOLE_HASH_TYPES}
        with patch.object(match, "grid_score", side_effect=AssertionError("grid must be skipped")):
            self.assertIsNone(match.score_hashes(low, far, minimum_score=70))
        q = {"phash256": b"\x00", **{f"grid{i}": b"\x00" for i in range(9)}}
        c = {**q, "phash256": b"\x01"}
        reference = match.score_hashes(q, c)
        self.assertEqual(reference.score, 54.27)
        self.assertEqual(match.score_hashes(q, c, minimum_score=54.27), reference)
        self.assertIsNone(match.score_hashes(q, c, minimum_score=54.271))
        self.assertEqual(match.score_hashes(low, far, minimum_score=99, sha_equal=True).score, 100)
        self.assertIsNotNone(match.score_hashes(low, far, minimum_score=99, crop_hashes_b={"x": b"\x00" * 32}))
        self.assertIsNotNone(match.score_hashes(low, far, minimum_score=99, query_crop_hashes={"x": b"\xff" * 32}))
        self.assertIsNotNone(match.score_hashes({}, {}, minimum_score=0))
        with self.assertRaises(ValueError):
            match.score_hashes({"phash256": b"a"}, {"phash256": b"ab"})
        rng = random.Random(19)
        for _ in range(100):
            a = {key: rng.randbytes(32) for key in bands.WHOLE_HASH_TYPES}
            b = {key: rng.randbytes(32) for key in bands.WHOLE_HASH_TYPES}
            for i in range(9):
                a[f"grid{i}"] = rng.randbytes(8)
                b[f"grid{i}"] = a[f"grid{i}"] if rng.random() < .7 else rng.randbytes(8)
            expected = match.score_hashes(a, b)
            for threshold in (0, 10, 20, 55, 70, 100):
                actual = match.score_hashes(a, b, minimum_score=threshold)
                if actual is None:
                    self.assertLess(expected.score, threshold)
                else:
                    self.assertEqual(actual, expected)

    def test_tryhard_hashing_decodes_once_and_reuses_crops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            Image.new("RGB", (128, 128), "red").save(path)
            with patch.object(hashing, "load_normalized", wraps=hashing.load_normalized) as decode:
                hashes, _ = hashing.compute_image_hashes(path, include_query_tiles=True)
            self.assertEqual(decode.call_count, 1)
            expected = hashing.tryhard_query_hashes(hashing.load_normalized(path))
            self.assertEqual({k: v for k, v in hashes.items() if k.startswith(("crop:", "tile:"))}, expected)


class SparseDatabaseWorkTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.conn.executemany("INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at,sha256) VALUES (?, ?, 1, 1, 'now', ?)",
                              [(i, f"missing-{i}.png", str(i).encode()) for i in range(1, 402)])
        self.conn.executemany("INSERT INTO hashes(image_id,phash256,grid0) VALUES (?, ?, ?)",
                              [(i, b"hash", b"grid") for i in range(1, 402)])
        self.conn.executemany("INSERT INTO matches(image_id_a,image_id_b,score,decision,created_at) VALUES (?, ?, 90, 'strong_duplicate','now')",
                              [(i, i + 1) for i in range(1, 401)])
        self.conn.commit()

    def test_sparse_review_work_grows_with_edges_not_members_squared(self):
        counts = []
        for size in (100, 200):
            count = 0
            def progress():
                nonlocal count
                count += 1
                return 0
            self.conn.set_progress_handler(progress, 1)
            rows = review._fetch_cluster_matches(self.conn, list(range(1, size + 1)))
            self.conn.set_progress_handler(None, 0)
            self.assertEqual([(row["image_id_a"], row["image_id_b"]) for row in rows], [(i, i + 1) for i in range(1, size)])
            counts.append(count)
        self.assertLess(counts[1], counts[0] * 2.5)

    def test_bulk_hash_load_and_sql_candidate_lower_bound(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        loaded = cluster._load_hashes(self.conn, [{"id": i} for i in (1, 2, 3, 999)])
        self.assertEqual(set(loaded), {1, 2, 3})
        self.assertEqual(len([s for s in statements if s.startswith("SELECT")]), 1)
        for i in (1, 2, 3):
            bands.insert_bands(self.conn, i, {"grid0": b"grid", "crop:top_90": b"crop"})
        hashes = {"grid0": b"grid"}
        all_candidates = bands.find_candidates(self.conn, hashes, crop_query_hashes=[b"crop"])
        filtered = bands.find_candidates(self.conn, hashes, min_image_id=1, sort=False, crop_query_hashes=[b"crop"])
        self.assertEqual(sorted(filtered, key=lambda x: x.image_id), [c for c in all_candidates if c.image_id > 1])
        self.assertTrue(any("hb.image_id > 1" in sql for sql in statements))

    def test_cluster_common_grid_false_positive_skips_full_scoring(self):
        self.conn.execute("DELETE FROM hashes WHERE image_id > 2")
        for image_id, byte in ((1, b"\x00"), (2, b"\xff")):
            hashes = {key: byte * 32 for key in bands.WHOLE_HASH_TYPES}
            hashes["grid0"] = b"common00"
            self.conn.execute("UPDATE hashes SET dhash256=?,phash256=?,whash256=?,grid0=? WHERE image_id=?",
                              (*[hashes[key] for key in bands.WHOLE_HASH_TYPES], hashes["grid0"], image_id))
            bands.insert_bands(self.conn, image_id, hashes)
        self.conn.commit()
        stats = cluster.build_clusters(self.conn)
        self.assertEqual((stats.images, stats.candidate_pairs, stats.scored_pairs, stats.stored_matches), (2, 1, 0, 0))

    def test_review_manifest_streams_and_index_keeps_only_first_image(self):
        self.conn.executemany("INSERT INTO clusters(cluster_id,image_id,representative) VALUES (?, ?, ?)",
                              [(1, 1, 1), (1, 2, 0), (2, 3, 1), (2, 4, 0)])
        self.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(review, "_write_thumbnail", return_value=False), patch.object(review, "_write_index_page") as index:
                stats = review.generate_review(self.conn, path)
            manifest = json.loads((path / "clusters.json").read_text())
            self.assertEqual((stats.clusters, stats.images), (2, 4))
            self.assertEqual([len(c["images"]) for c in manifest["clusters"]], [2, 2])
            self.assertEqual([len(c["matches"]) for c in manifest["clusters"]], [1, 1])
            self.assertEqual([len(c["images"]) for c in index.call_args.args[1]], [1, 1])
            self.assertEqual([c["matches"] for c in index.call_args.args[1]], [[], []])

    def test_failed_review_preserves_manifest_and_page_and_cleans_temporaries(self):
        self.conn.executemany("INSERT INTO clusters(cluster_id,image_id,representative) VALUES (1,?,?)",
                              [(1, 1), (2, 0), (3, 0)])
        self.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest, page = path / "clusters.json", path / "cluster_000001.html"
            manifest.write_text("previous manifest")
            page.write_text("previous page")
            real_match = review._match_json
            calls = 0
            def fail_after_first(row):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated export failure")
                return real_match(row)
            with patch.object(review, "_write_thumbnail", return_value=False), patch.object(review, "_match_json", side_effect=fail_after_first):
                with self.assertRaisesRegex(OSError, "simulated export failure"):
                    review.generate_review(self.conn, path)
            self.assertEqual(manifest.read_text(), "previous manifest")
            self.assertEqual(page.read_text(), "previous page")
            self.assertEqual(list(path.glob(".*.tmp")), [])
            self.assertEqual(self.conn.execute("SELECT name FROM sqlite_temp_master WHERE type='table'").fetchall(), [])
            with patch.object(review, "_write_thumbnail", return_value=False), patch.object(review, "_write_index_page", side_effect=OSError("index failed")):
                with self.assertRaisesRegex(OSError, "index failed"):
                    review.generate_review(self.conn, path)
            self.assertEqual(manifest.read_text(), "previous manifest")
            self.assertEqual(list(path.glob(".*.tmp")), [])
            with patch.object(review.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    with review._atomic_text(manifest) as output:
                        output.write("replacement")
            self.assertEqual(manifest.read_text(), "previous manifest")
            self.assertEqual(list(path.glob(".*.tmp")), [])

    def test_single_cluster_streams_edges_without_retaining_row_payloads(self):
        self.conn.executemany("INSERT INTO clusters(cluster_id,image_id,representative) VALUES (1,?,?)",
                              [(1, 1), (2, 0)])
        self.conn.commit()
        sample = dict(self.conn.execute("SELECT * FROM matches LIMIT 1").fetchone())
        class Tracked(dict):
            alive = 0
            peak = 0
            def __init__(self, value):
                super().__init__(value)
                Tracked.alive += 1
                Tracked.peak = max(Tracked.peak, Tracked.alive)
            def __del__(self):
                Tracked.alive -= 1
        real_match = review._match_json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(review, "_write_thumbnail", return_value=False), \
                 patch.object(review, "_fetch_cluster_matches", side_effect=AssertionError("materialized edges")), \
                 patch.object(review, "_iter_cluster_matches", side_effect=lambda *_args: (sample for _ in range(1000))), \
                 patch.object(review, "_match_json", side_effect=lambda row: Tracked(real_match(row))):
                review.generate_review(self.conn, path)
            self.assertLessEqual(Tracked.peak, 3)
            self.assertEqual(Tracked.alive, 0)
            manifest = json.loads((path / "clusters.json").read_text())
            self.assertEqual(len(manifest["clusters"][0]["matches"]), 1000)
            self.assertEqual((path / "cluster_000001.html").read_text().count("strong_duplicate"), 1000)


class LinearMetadataTests(unittest.TestCase):
    def test_numeric_scopes_preserve_literal_delimiters_and_tag_boundaries(self):
        self.assertEqual(nai_prompt.parse_novelai_prompt("1.5::masterpiece::1girl"), ["masterpiece", "1girl"])
        self.assertEqual(nai_prompt.parse_novelai_prompt("cat::dog"), ["cat::dog"])
        self.assertEqual(nai_prompt.parse_novelai_prompt("-1::hidden -1::visible:: hidden::tail"), ["visible", "tail"])
        self.assertEqual(nai_prompt.parse_novelai_prompt("zero, 0::hidden::tail"), ["zero", "tail"])
        self.assertEqual(nai_prompt._filter_novelai_numeric_scopes("x" * 100000 + "::tail"), "x" * 100000 + "::tail")
        self.assertEqual(nai_prompt.sanitize_model_name(" Foo, Bar；Baz。 Quux｜model "), "Foo Bar Baz Quux model")

    def test_stealth_short_header_and_declared_oversize_do_not_materialize_pixels(self):
        self.assertEqual(nai_prompt.bits_to_bytes([0] * 8), b"\x00")
        self.assertIsNone(nai_prompt.read_stealth_metadata(Image.new("RGB", (64, 64))))
        self.assertIsNone(nai_prompt.read_stealth_metadata(Image.new("RGBA", (1, 1))))
        signature = b"stealth_pnginfo"
        raw = signature + (1000000).to_bytes(4, "big")
        bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))
        pixels = np.zeros((1, len(bits), 4), dtype=np.uint8)
        pixels[0, :, 3] = bits | 254
        self.assertIsNone(nai_prompt.read_stealth_metadata(Image.fromarray(pixels)))
        palette = Image.new("P", (64, 64))
        palette.info["transparency"] = 0
        self.assertIsNone(nai_prompt.read_stealth_metadata(palette))


class AtomicReviewWindowsCleanupTests(unittest.TestCase):
    def test_failed_output_closes_handle_before_unlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            target.write_text("previous")
            opened = []
            create = tempfile.NamedTemporaryFile
            unlink = Path.unlink

            def record_file(*args, **kwargs):
                output = create(*args, **kwargs)
                opened.append(output)
                return output

            def require_closed(path, *args, **kwargs):
                self.assertTrue(opened[-1].closed)
                return unlink(path, *args, **kwargs)

            with patch.object(review.tempfile, "NamedTemporaryFile", record_file), patch.object(Path, "unlink", require_closed):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    with review._atomic_text(target) as output:
                        output.write("partial")
                        raise RuntimeError("render failed")
            self.assertEqual(target.read_text(), "previous")
            self.assertEqual(list(target.parent.iterdir()), [target])
