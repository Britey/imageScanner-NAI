"""Image search and review exports, using real images, hashes and SQLite."""
from __future__ import annotations

import builtins
import importlib.util
import json
import sys
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageEnhance, ImageFilter

from image_fixtures import textured
from imgdupe import bands, cluster, hashing, query, review
from imgdupe.db import init_db, replace_hashes, upsert_image


class ImageLibraryCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.addCleanup(self.conn.close)
        source = textured(1)
        self.original = self.add_image("original.png", source)
        copy_path = self.root / "copy&.png"
        shutil.copyfile(self.original[1], copy_path)
        self.exact = self.index(copy_path)
        self.near = self.add_image("reencoded.jpg", source, quality=70)
        self.review = self.add_image("bright.png", ImageEnhance.Brightness(source).enhance(2).filter(ImageFilter.GaussianBlur(1)))
        self.unrelated = self.add_image("unrelated.png", textured(2))

    def add_image(self, name, image, **save_options):
        path = self.root / name
        image.save(path, **save_options)
        return self.index(path)

    def index(self, path):
        hashes, metadata = hashing.compute_image_hashes(path)
        image_id = upsert_image(self.conn, path=path, size_bytes=path.stat().st_size,
                                mtime_ns=path.stat().st_mtime_ns, indexed_at="now",
                                width=metadata["width"], height=metadata["height"],
                                image_format=metadata["format"], sha256=hashes["sha256"])
        replace_hashes(self.conn, image_id, hashes)
        bands.insert_bands(self.conn, image_id, hashes)
        return image_id, path

    def search(self, **options):
        return query.query_image(self.conn, self.original[1], **options)


class ImageSearchTests(ImageLibraryCase):
    def test_reencoded_copy_ranks_above_changed_image_and_unrelated_is_rejected(self):
        results = self.search()
        self.assertEqual([row["id"] for row, _, _ in results], [self.exact[0], self.near[0], self.review[0]])
        self.assertEqual([score.decision for _, _, score in results],
                         ["exact_duplicate", "strong_duplicate", "review"])
        scores = [score.score for _, _, score in results]
        self.assertEqual(scores[0], 100)
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        self.assertNotIn(self.original[0], [row["id"] for row, _, _ in results])
        self.assertNotIn(self.unrelated[0], [row["id"] for row, _, _ in results])

    def test_limit_and_score_cutoff_keep_best_matches_and_hide_exact_keeps_visual_copy(self):
        self.assertEqual([row["id"] for row, _, _ in self.search(limit=2)], [self.exact[0], self.near[0]])
        self.assertEqual([row["id"] for row, _, _ in self.search(include_exact=False, min_score=55)], [self.near[0]])
        score = self.search()[1][2].score
        self.assertEqual([row["id"] for row, _, _ in self.search(min_score=score)], [self.exact[0], self.near[0]])
        self.assertEqual([row["id"] for row, _, _ in self.search(min_score=score + .01)], [self.exact[0]])
        self.assertEqual(self.search(min_score=101), [])
        with self.assertRaisesRegex(ValueError, "limit must be positive"):
            self.search(limit=0)

    def test_external_upload_has_same_results_plus_indexed_original(self):
        external = self.root / "upload.png"
        shutil.copyfile(self.original[1], external)
        results = query.query_image(self.conn, external)
        self.assertEqual({row["id"] for row, _, _ in results},
                         {self.original[0], self.exact[0], self.near[0], self.review[0]})
        self.assertEqual([score.decision for _, _, score in results[:2]], ["exact_duplicate", "exact_duplicate"])
        self.assertEqual(query.query_image(self.conn, external, include_exact=False), self.search(include_exact=False))

    def test_query_html_contains_real_result_links_decisions_and_escaped_names(self):
        out = self.root / "nested" / "query.html"
        query.write_query_html(out, self.original[1], self.search())
        html = out.read_text()
        self.assertIn(self.exact[1].as_uri().replace("&", "&amp;"), html)
        self.assertIn("copy&amp;.png", html)
        self.assertIn("exact_duplicate", html)
        self.assertIn("strong_duplicate", html)
        query.write_query_html(out, self.original[1], [])
        self.assertIn("<tbody></tbody>", out.read_text())


class ClusterReviewTests(ImageLibraryCase):
    def test_real_clusters_keep_review_only_pairs_out_and_export_full_membership(self):
        stats = cluster.build_clusters(self.conn, min_score=0)
        members = {row["image_id"] for row in self.conn.execute("SELECT image_id FROM clusters")}
        self.assertEqual(members, {self.original[0], self.exact[0], self.near[0]})
        self.assertEqual((stats.images, stats.clusters, stats.clustered_images), (5, 1, 3))
        self.assertTrue(self.conn.execute("SELECT 1 FROM matches WHERE decision='review'").fetchone())
        representative = max((self.original, self.exact, self.near),
                             key=lambda item: (item[1].stat().st_size, -item[0]))
        self.assertEqual(self.conn.execute("SELECT image_id FROM clusters WHERE representative=1").fetchone()[0], representative[0])
        exported = review.generate_review(self.conn, self.root / "review", thumbnail_size=48)
        self.assertEqual((exported.clusters, exported.images, exported.thumbnails), (1, 3, 3))
        [group] = json.loads((exported.out_dir / "clusters.json").read_text())["clusters"]
        self.assertEqual({image["id"] for image in group["images"]}, members)
        self.assertEqual(group["representative"], str(representative[1]))
        self.assertEqual(len(group["matches"]), 3)
        page = (exported.out_dir / "cluster_000001.html").read_text()
        self.assertIn("copy&amp;.png", page)
        self.assertIn("cluster_000001.html", (exported.out_dir / "index.html").read_text())
        for image in group["images"]:
            with Image.open(exported.out_dir / image["thumbnail"]) as thumb:
                self.assertLessEqual(max(thumb.size), 48)

    def test_rebuild_replaces_old_groups_and_review_handles_missing_source_files(self):
        cluster.build_clusters(self.conn)
        self.exact[1].unlink()
        stats = review.generate_review(self.conn, self.root / "review", thumbnail_size=48)
        self.assertEqual((stats.images, stats.thumbnails), (3, 2))
        cluster.build_clusters(self.conn, min_score=101)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        empty = review.generate_review(self.conn, self.root / "empty")
        self.assertEqual((empty.images, empty.clusters, empty.thumbnails), (0, 0, 0))
        self.assertEqual(json.loads((empty.out_dir / "clusters.json").read_text()), {"clusters": []})

    def test_transitive_matches_form_one_group_without_a_direct_match_for_every_pair(self):
        # A controlled hash chain isolates transitive membership from image-hash
        # sensitivity: A-B and B-C each share two bands; A-C share none.
        self.conn.execute("DELETE FROM images")
        hashes = (b"aaaabbbb", b"aaaacccc", b"ddddcccc")
        ids = []
        for index, phash in enumerate(hashes):
            image_id, _ = self.add_image(f"chain-{index}.png", textured(index + 10))
            self.conn.execute("DELETE FROM hash_bands WHERE image_id=?", (image_id,))
            replace_hashes(self.conn, image_id, {"phash256": phash})
            bands.insert_bands(self.conn, image_id, {"phash256": phash})
            ids.append(image_id)
        stats = cluster.build_clusters(self.conn, min_score=0)
        self.assertEqual((stats.clusters, stats.clustered_images), (1, 3))
        self.assertEqual([(row["image_id_a"], row["image_id_b"]) for row in self.conn.execute("SELECT * FROM matches ORDER BY image_id_a,image_id_b")],
                         [(ids[0], ids[1]), (ids[1], ids[2])])


class ClusterBoundaryTests(ImageLibraryCase):
    def test_filtered_images_with_bands_are_not_pulled_back_into_groups(self):
        self.conn.execute("UPDATE images SET decode_error='damaged' WHERE id=?", (self.exact[0],))
        self.conn.execute("UPDATE images SET missing_at='now' WHERE id=?", (self.near[0],))
        stats = cluster.build_clusters(self.conn)
        self.assertEqual((stats.images, stats.clusters, stats.stored_matches), (3, 0, 0))

    def test_hash_removed_by_another_connection_between_reads_is_skipped(self):
        path = self.root / "concurrent.sqlite"
        self.conn.commit()
        with sqlite3.connect(path) as initial:
            self.conn.backup(initial)
        reader = sqlite3.connect(path)
        reader.row_factory = sqlite3.Row
        writer = sqlite3.connect(path)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        read_images = cluster._iter_indexed_images
        def read_then_remove(conn):
            rows = read_images(conn)
            writer.execute("DELETE FROM hashes WHERE image_id=?", (self.original[0],))
            writer.commit()
            return rows
        with patch.object(cluster, "_iter_indexed_images", side_effect=read_then_remove):
            stats = cluster.build_clusters(reader)
        self.assertEqual((stats.images, stats.clusters, stats.clustered_images), (5, 1, 2))
        self.assertEqual({row[0] for row in reader.execute("SELECT image_id FROM clusters")},
                         {self.exact[0], self.near[0]})

    def test_clusters_merge_components_when_first_edge_points_into_larger_component(self):
        union = cluster.UnionFind([1, 2, 3, 4, 5, 6])
        # UnionFind's public contract is connectivity irrespective of edge
        # direction/order, including a smaller component merged into a larger.
        for left, right in ((1, 2), (3, 4), (1, 3), (5, 6), (5, 1), (6, 2)):
            union.union(left, right)
        self.assertEqual(len({union.find(image_id) for image_id in range(1, 7)}), 1)

    def test_export_without_representative_flag_uses_first_sorted_image(self):
        cluster.build_clusters(self.conn)
        self.conn.execute("UPDATE clusters SET representative=0")
        self.conn.commit()
        export = review.generate_review(self.conn, self.root / "legacy-review", thumbnail_size=48)
        [group] = json.loads((export.out_dir / "clusters.json").read_text())["clusters"]
        self.assertEqual(group["representative"], group["images"][0]["path"])
        self.assertEqual(len(group["images"]), 3)

    def test_cluster_and_export_work_when_optional_progress_package_is_unavailable(self):
        original_import = builtins.__import__
        def without_progress(name, *args, **kwargs):
            if name == "tqdm":
                raise ImportError("progress unavailable")
            return original_import(name, *args, **kwargs)
        loaded = []
        with patch("builtins.__import__", side_effect=without_progress):
            for original in (cluster, review):
                name = "imgdupe._optional_" + original.__name__.rsplit(".", 1)[1]
                spec = importlib.util.spec_from_file_location(name, original.__file__)
                module = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, {name: module}):
                    spec.loader.exec_module(module)
                loaded.append(module)
        stats = loaded[0].build_clusters(self.conn)
        export = loaded[1].generate_review(self.conn, self.root / "without-progress", thumbnail_size=48)
        self.assertEqual((stats.clusters, stats.clustered_images), (1, 3))
        self.assertEqual((export.clusters, export.images, export.thumbnails), (1, 3, 3))
        self.assertTrue((export.out_dir / "cluster_000001.html").exists())
