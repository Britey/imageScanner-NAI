"""Crop candidates must be retrieved before all-direction crop scoring can help."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from imgdupe import bands, hashing, query
from imgdupe.db import init_db, replace_crop_hashes, replace_hashes, set_crop_index_state, upsert_image


class CropRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.query_path = self.root / "query.png"
        Image.new("RGB", (128, 128), "blue").save(self.query_path)
        self.shared = b"\x88" * 32
        self.query_whole = b"\x11" * 32
        self.other_whole = b"\xee" * 32

    def index_candidate(self, hashes, *, name="candidate.png"):
        image_id = upsert_image(self.conn, path=self.root / name, size_bytes=1,
                                mtime_ns=1, indexed_at="now", sha256=b"candidate",
                                width=128, height=128, image_format="PNG")
        replace_hashes(self.conn, image_id, hashes)
        replace_crop_hashes(self.conn, image_id, hashes)
        bands.insert_bands(self.conn, image_id, hashes)
        return image_id

    def search(self, regions, *, whole=None, tryhard=True, ready=True):
        set_crop_index_state(self.conn, "ready" if ready else "incomplete")
        def compute(_path, **options):
            hashes = {"sha256": b"query", "phash256": self.query_whole if whole is None else whole}
            if options["include_query_tiles"]:
                hashes.update(regions)
            return hashes, {}
        with patch.object(query, "compute_image_hashes", side_effect=compute) as computed:
            result = query.query_image(self.conn, self.query_path, tryhard=tryhard)
        self.assertEqual(computed.call_count, 1)
        return result

    def assert_crop_only_result(self, result, image_id, stored_type):
        self.assertEqual([row["id"] for row, _, _ in result], [image_id])
        _, candidate, score = result[0]
        # There are no coincidental whole/grid hits: the sole matching region
        # alone retrieves the candidate and reaches the real scoring function.
        self.assertEqual(candidate.hits_by_type, {stored_type: 16})
        self.assertEqual(candidate.total_hits, 16)
        self.assertEqual((score.crop_min_dist, score.score, score.decision), (0, 90, "strong_duplicate"))

    def test_query_crop_is_not_overwritten_by_whole_image_hash(self):
        image_id = self.index_candidate({"phash256": self.other_whole, "crop:top_25": self.shared})
        result = self.search({"crop:top_25": self.shared})
        self.assert_crop_only_result(result, image_id, "crop:top_25")

    def test_query_crop_matches_different_candidate_region(self):
        image_id = self.index_candidate({"phash256": self.other_whole, "crop:bottom_50": self.shared})
        result = self.search({"crop:top_25": self.shared})
        self.assert_crop_only_result(result, image_id, "crop:bottom_50")

    def test_query_tile_matches_candidate_crop(self):
        image_id = self.index_candidate({"phash256": self.other_whole, "crop:center_50": self.shared})
        result = self.search({"tile:2x2_0_0": self.shared})
        self.assert_crop_only_result(result, image_id, "crop:center_50")

    def test_whole_query_still_matches_candidate_crop(self):
        image_id = self.index_candidate({"phash256": self.other_whole, "crop:bottom_50": self.shared})
        result = self.search({"crop:top_25": self.query_whole}, whole=self.shared)
        self.assert_crop_only_result(result, image_id, "crop:bottom_50")

    def test_query_crop_and_tile_still_match_candidate_whole_without_crop_index(self):
        image_id = self.index_candidate({"phash256": self.shared})
        for name in ("crop:top_25", "tile:2x2_0_0"):
            for ready in (False, True):
                with self.subTest(name=name, ready=ready):
                    result = self.search({name: self.shared}, ready=ready)
                    self.assert_crop_only_result(result, image_id, "phash256")

    def test_cross_region_lookup_requires_tryhard_and_ready_crop_index(self):
        self.index_candidate({"phash256": self.other_whole, "crop:bottom_50": self.shared})
        for tryhard, ready in ((False, False), (False, True), (True, False)):
            with self.subTest(tryhard=tryhard, ready=ready):
                self.assertEqual(self.search({"crop:top_25": self.shared}, tryhard=tryhard, ready=ready), [])

    def test_generated_images_retrieve_shared_quadrant_at_different_positions(self):
        random = np.random.default_rng(407)
        shared_pixels = random.integers(0, 256, (128, 128, 3), dtype=np.uint8)
        query_pixels = random.integers(0, 256, (256, 256, 3), dtype=np.uint8)
        candidate_pixels = random.integers(0, 256, (256, 256, 3), dtype=np.uint8)
        query_pixels[:128, :128] = shared_pixels
        candidate_pixels[128:, 128:] = shared_pixels
        Image.fromarray(query_pixels).save(self.query_path)
        candidate_path = self.root / "candidate.png"
        Image.fromarray(candidate_pixels).save(candidate_path)
        candidate_hashes, _ = hashing.compute_image_hashes(candidate_path, include_crop_regions=True)
        query_hashes, _ = hashing.compute_image_hashes(self.query_path, include_crop_regions=True)
        self.assertGreater(hashing.hamming_bytes(query_hashes["phash256"], candidate_hashes["phash256"]), 40)
        self.assertEqual(query_hashes["crop:top_left_quarter"], candidate_hashes["crop:bottom_right_quarter"])
        image_id = self.index_candidate(candidate_hashes)
        set_crop_index_state(self.conn, "ready")
        results = query.query_image(self.conn, self.query_path, tryhard=True)
        self.assertEqual([row["id"] for row, _, _ in results], [image_id])
        self.assertEqual(results[0][2].crop_min_dist, 0)
        self.assertEqual(results[0][2].decision, "strong_duplicate")

    def test_duplicate_aliases_do_not_turn_one_whole_band_into_a_candidate(self):
        self.index_candidate({"phash256": b"ab"})
        self.assertEqual(bands.find_candidates(self.conn, {
            "phash256:query_crop:0": b"ab", "phash256:query_crop:1": b"ab"
        }), [])

    def test_deduplicated_batched_probes_have_stable_hits_and_indexed_lookup(self):
        value = b"\x00\x01" * 16
        first = self.index_candidate({"phash256": value, "crop:center_50": value})
        second = self.index_candidate({"phash256": value, "crop:center_50": value}, name="second.png")
        values = [number.to_bytes(2, "big") * 16 for number in range(1, 16)]
        hashes = {f"phash256:query_crop:{index}": item for index, item in enumerate(values * 4)}
        hashes["crop:center_50"] = value
        # Exercise multiple batches against the oldest SQLite variable ceiling.
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            result = bands.find_candidates(self.conn, hashes, crop_query_hashes=values * 4)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual([(item.image_id, item.total_hits, item.hits_by_type) for item in result], [
            (first, 32, {"phash256": 16, "crop:center_50": 16}),
            (second, 32, {"phash256": 16, "crop:center_50": 16}),
        ])
        lookups = [statement for statement in statements if "WITH wanted(" in statement]
        # 240 distinct whole probes and 240 cross-region probes: two batches each,
        # independent of aliases and of the 38 candidate crop region names.
        self.assertEqual(len(lookups), 4)
        for statement in lookups:
            plans = self.conn.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
            self.assertTrue(any("hash_type=? AND band_index=? AND band_value=?" in row["detail"] for row in plans))
        reversed_result = bands.find_candidates(self.conn, dict(reversed(list(hashes.items()))),
                                                 crop_query_hashes=reversed(values * 4))
        self.assertEqual(reversed_result, result)
        self.assertEqual([item.image_id for item in bands.find_candidates(
            self.conn, hashes, crop_query_hashes=values, exclude_image_id=first
        )], [second])
