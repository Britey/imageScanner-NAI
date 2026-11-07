"""Correctness and work bounds for persistent projections and paginated search."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from imgdupe import bands, db, hashing, query
from image_fixtures import textured


class SearchProjectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.init_db(self.conn)

    def add(self, image_id, tags=("common",), prompt="prompt", configured=None):
        self.conn.execute("INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at) VALUES(?,?,1,?,'now')",
                          (image_id, f"{image_id}.png", image_id))
        self.conn.execute("INSERT INTO image_prompts(image_id,prompt,prompt_indexed_at,configured_model) VALUES(?,?,'now',?)",
                          (image_id, prompt, configured))
        self.conn.executemany("INSERT INTO prompt_tags VALUES(?,?,?,0,'prompt')", ((image_id, tag, tag.casefold()) for tag in tags))

    def assert_projection(self):
        expected = [tuple(row) for row in self.conn.execute("""SELECT pt.tag_norm,pt.tag_kind,pt.tag,COUNT(*)
            FROM prompt_tags pt JOIN image_prompts ip ON ip.image_id=pt.image_id
            WHERE NOT(pt.tag_kind='model_type' AND ip.configured_model IS NOT NULL)
            GROUP BY pt.tag_norm,pt.tag_kind,pt.tag ORDER BY 1,2,3""")]
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM tag_stats ORDER BY 1,2,3")], expected)
        actual = db.fetch_stats(self.conn)
        oracle = self.conn.execute("""SELECT COUNT(*),COALESCE(SUM(i.decode_error IS NULL AND i.missing_at IS NULL),0),
            COALESCE(SUM(i.decode_error IS NOT NULL OR (ip.prompt_error IS NOT NULL AND ip.prompt_error NOT IN ('no_metadata','no_prompt'))),0),
            COALESCE(SUM(ip.prompt IS NOT NULL),0)
            FROM images i LEFT JOIN image_prompts ip ON ip.image_id=i.id""").fetchone()
        self.assertEqual(tuple(actual[name] for name in ('indexed','ok','failed','prompts')), tuple(oracle))
        self.assertEqual(actual['unique_tags'], len({row[0] for row in expected}))
        for name, sql in [('hashed','SELECT COUNT(*) FROM hashes'), ('matches','SELECT COUNT(*) FROM matches'),
                          ('groups','SELECT COUNT(DISTINCT cluster_id) FROM clusters')]:
            self.assertEqual(actual[name], self.conn.execute(sql).fetchone()[0])

    def test_projections_follow_direct_writes_replacements_cascades_and_rollback(self):
        self.add(1, ('Cat', '@model:sd'))
        self.add(2, ('cat',), prompt=None)
        self.conn.execute("UPDATE prompt_tags SET tag_kind='model_type' WHERE tag_norm='@model:sd'")
        self.conn.execute("INSERT INTO hashes(image_id) VALUES(1)")
        self.conn.execute("INSERT INTO matches(image_id_a,image_id_b,score,decision,created_at) VALUES(1,2,90,'review','now')")
        self.conn.executemany("INSERT INTO clusters VALUES(?,?,0)", [(1,1),(1,2),(2,1)])
        self.assert_projection()
        self.assertEqual(db.fetch_tag_suggestions(self.conn, 'cat'), [{'tag':'Cat','count':2}])
        self.assertEqual(db.fetch_model_options(self.conn), [('sd','SD')])
        self.conn.commit()
        for sql in ["UPDATE images SET decode_error='bad',missing_at='now' WHERE id=1",
                    "UPDATE image_prompts SET prompt_error='broken',configured_model='' WHERE image_id=2",
                    "UPDATE prompt_tags SET image_id=2,tag='CAT',tag_norm='new' WHERE image_id=1 AND tag_norm='cat'",
                    "UPDATE clusters SET cluster_id=3 WHERE cluster_id=1 AND image_id=2",
                    "INSERT OR REPLACE INTO prompt_tags VALUES(2,'new','new',0,'prompt')",
                    "INSERT OR REPLACE INTO image_prompts(image_id,prompt,prompt_indexed_at) VALUES(1,'prompt','now')",
                    "INSERT OR REPLACE INTO hashes(image_id) VALUES(1)",
                    "DELETE FROM images WHERE id=1",
                    "DELETE FROM image_prompts WHERE image_id=2",
                    "DELETE FROM matches", "DELETE FROM clusters"]:
            self.conn.execute(sql)
            self.assert_projection()
        self.conn.rollback()
        self.assert_projection()
        self.assertEqual(db.fetch_tag_suggestions(self.conn, 'cat')[0]['tag'], 'Cat')
        self.conn.execute("DELETE FROM prompt_tags WHERE image_id=1 AND tag_norm='cat'")
        self.assertEqual(db.fetch_tag_suggestions(self.conn, 'cat'), [{'tag':'cat','count':1}])

    def test_reassigning_prompt_owner_moves_visibility_and_releases_old_text(self):
        self.add(1, ('first',), prompt='first text')
        self.add(2, ('second',), prompt='second text')
        self.conn.execute("DELETE FROM image_prompts WHERE image_id=2")
        self.conn.execute("UPDATE image_prompts SET image_id=2 WHERE image_id=1")
        self.assert_projection()
        self.assertEqual(db.fetch_stats(self.conn)['prompts'], 1)
        self.assertEqual(query.query_prompt_tags(self.conn,'first'), [])
        self.assertEqual([row.image_row['id'] for row in query.query_prompt_tags(self.conn,'second')], [2])
        self.assertEqual([row[0] for row in self.conn.execute('SELECT prompt FROM prompt_texts')], ['first text'])
        self.conn.execute("UPDATE image_prompts SET prompt='replacement' WHERE image_id=2")
        self.assertEqual([row[0] for row in self.conn.execute('SELECT prompt FROM prompt_texts')], ['replacement'])
        self.conn.execute("DELETE FROM images")
        self.assert_projection()
        self.assertEqual(self.conn.execute('SELECT * FROM prompt_texts').fetchall(), [])
        self.conn.execute("INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at) VALUES(3,'third',1,1,'now')")
        self.conn.execute('UPDATE images SET id=4 WHERE id=3')
        self.assertEqual(self.conn.execute('SELECT image_id FROM prompt_search').fetchone()[0], 4)

    def test_backfill_is_atomic_and_reopening_does_not_revisit_payloads(self):
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        db.init_db(other, defer_search_index=True)
        other.execute("INSERT INTO images VALUES(1,'old.png',1,1,NULL,NULL,NULL,NULL,NULL,NULL,'now',NULL,NULL)")
        other.execute("INSERT INTO image_prompts(image_id,prompt,prompt_indexed_at) VALUES(1,'legacy','now')")
        other.execute("INSERT INTO prompt_tags VALUES(1,'legacy','legacy',0,'prompt')")
        other.commit()
        with patch.object(db, 'SEARCH_SCHEMA', db.SEARCH_SCHEMA + 'THIS IS NOT SQL;'):
            with self.assertRaises(sqlite3.OperationalError):
                db.init_db(other)
        self.assertIsNone(db.get_metadata(other, 'search_schema_version'))
        self.assertIsNone(other.execute("SELECT 1 FROM sqlite_master WHERE name='prompt_search'").fetchone())
        db.init_db(other)
        self.assertEqual(db.fetch_stats(other)['unique_tags'], 1)
        statements = []
        other.set_trace_callback(statements.append)
        db.init_db(other)
        other.set_trace_callback(None)
        self.assertFalse(any('INSERT INTO prompt_search' in sql or 'SELECT DISTINCT prompt' in sql for sql in statements))

    def test_summary_work_is_independent_of_repeated_occurrences(self):
        def measure():
            ticks = 0
            def progress():
                nonlocal ticks
                ticks += 1
                return 0
            self.conn.set_progress_handler(progress, 1)
            try:
                db.fetch_stats(self.conn)
                db.fetch_tag_suggestions(self.conn, 'common')
                db.fetch_model_options(self.conn)
            finally:
                self.conn.set_progress_handler(None, 0)
            return ticks
        self.add(1)
        baseline = measure()
        for image_id in range(2, 1002):
            self.add(image_id)
        self.assertLessEqual(measure(), baseline + 10)

    def test_selective_all_query_does_not_scan_popular_tag_occurrences(self):
        self.add(1, ('common','rare'))
        def measure():
            ticks = 0
            def progress():
                nonlocal ticks
                ticks += 1
                return 0
            self.conn.set_progress_handler(progress,100)
            try:
                result = query.query_prompt_tags(self.conn,'common,rare')
            finally:
                self.conn.set_progress_handler(None,0)
            self.assertEqual([item.image_row['id'] for item in result],[1])
            return ticks
        baseline = measure()
        for image_id in range(2, 2002):
            self.add(image_id)
        self.assertLessEqual(measure(), baseline + 15)

    def test_large_positive_negative_lists_and_legacy_comma_model_groups(self):
        self.add(1, tuple(f'tag{i}' for i in range(1100)))
        self.add(2, ('other',), prompt='other')
        self.conn.executemany("INSERT INTO prompt_tags VALUES(?,?,?,?,?)", [
            (1,'@model:Foo, Bar','@model:foo, bar',9999,'model_identity'),
            (2,'@model:Foo Bar','@model:foo bar',9999,'model_identity')])
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
        found = query.query_prompt_tags(self.conn, ','.join(f'tag{i}' for i in range(1100)))
        self.assertEqual([(item.image_row['id'],item.matched_count) for item in found],[(1,1100)])
        self.assertEqual(query.query_prompt_tags(self.conn,'tag0',negative_text_or_tags=','.join(f'tag{i}' for i in range(1100))),[])
        for text in ('@model:Foo, Bar','(@model:Foo, Bar | absent)', '@model:Foo, Bar | @model:Foo Bar'):
            self.assertEqual({item.image_row['id'] for item in query.query_prompt_tags(self.conn,text)}, {1,2})
        self.assertEqual(query.query_prompt_tags(self.conn,'tag0',negative_text_or_tags='@model:Foo, Bar'),[])
        self.assertEqual(query.query_prompt_tags(self.conn,'@model:Foo, Bar, absent'),[])
        results = query.query_prompt_tags(self.conn, 'tag0 | tag1, tag1 | tag0')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.matched_count, 1)

    def test_single_term_matches_general_or_path_with_filters_and_pagination(self):
        for image_id in range(1, 6):
            self.add(image_id, ('common', 'excluded') if image_id == 5 else ('common',), prompt=str(image_id // 2))
        self.conn.execute("UPDATE images SET missing_at='now' WHERE id=1")
        for mode in ('all','any'):
            for unique in (False,True):
                options = dict(match_mode=mode,unique_prompts=unique,limit=2,offset=1,negative_text_or_tags='excluded')
                single = query.query_prompt_tags(self.conn,'common',**options)
                general = query.query_prompt_tags(self.conn,'common|absent',**options)
                expected = [3] if unique else [3, 2]
                self.assertEqual([r.image_row['id'] for r in single], expected)
                self.assertEqual([r.image_row['id'] for r in general], expected)
                self.assertTrue(all(r.matched_tags == ('common',) for r in single + general))

    def test_unique_null_prompts_never_collide_with_integer_prompt_identity(self):
        self.add(-1, prompt=None)
        self.add(2, prompt='one')
        self.add(3, prompt=None)
        self.assertEqual({item.image_row['id'] for item in query.query_prompt_tags(self.conn,'common',unique_prompts=True)}, {-1,2,3})

    def test_model_name_matching_does_not_consume_explicit_or_operator(self):
        self.add(1, ('bar',))
        self.conn.executemany("INSERT INTO prompt_tags VALUES(1,?,?,0,'model_identity')", [('@model:Foo','@model:foo'),('@model:Foo Bar','@model:foo bar')])
        self.assertEqual([r.image_row['id'] for r in query.query_prompt_tags(self.conn, '@model:Foo|bar')], [1])
        results = query.query_prompt_tags(self.conn, '@model:Foo|absent')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].matched_tags, ('@model:Foo',))
        self.conn.execute("DELETE FROM prompt_tags WHERE tag_norm='@model:foo'")
        self.assertEqual(query.query_prompt_tags(self.conn,'@model:Foo'), [])

    def test_legacy_model_names_scan_through_parentheses(self):
        names = ['@model:Foo', '@model:Foo (Special)', '@model:Foo (Bar, Baz)', '@model:Foo, Bar (Baz)', '@model:Foo (a (b), c)']
        for image_id, name in enumerate(names, 1):
            self.add(image_id, ('cat',))
            self.conn.execute("INSERT INTO prompt_tags VALUES(?,?,?,0,'model_identity')", (image_id, name, name.casefold()))
        def found(text):
            return {item.image_row['id'] for item in query.query_prompt_tags(self.conn, text)}
        for image_id, name in enumerate(names, 1):
            for text in (name, f'({name})', f'{name}, cat', f'cat, ({name} | absent)', f'{name} , cat'):
                self.assertEqual(found(text), {image_id}, text)
        self.assertEqual(found('(@model:Foo (Special)), cat'), {2})
        self.assertEqual(found('@model:Foo (absent)'), set())
        self.assertEqual(found('@model:Foo (Special) extra'), set())
        self.assertEqual(found('@model:Foo (Special'), set())

    def test_matched_tags_keep_commas_inside_legacy_model_names(self):
        self.add(1, ('cat',), prompt='cat')
        self.conn.execute("INSERT INTO prompt_tags VALUES(1,'@model:Foo, Bar','@model:foo, bar',0,'model_identity')")
        results = query.query_prompt_tags(self.conn, 'cat, @model:Foo, Bar')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(sorted(result.matched_tags), ['@model:Foo, Bar', 'cat'])
        results = query.query_prompt_tags(self.conn, 'cat', negative_text_or_tags='absent')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.matched_tags, ('cat',))
        results = query.query_prompt_tags(self.conn, '', negative_text_or_tags='absent')
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.matched_tags, ())

    def test_query_transactions_failure_cleanup_and_invalid_limits(self):
        self.add(1)
        self.conn.commit()
        query.query_prompt_tags(self.conn,'common')
        self.assertFalse(self.conn.in_transaction)
        for function, argument in ((query.query_prompt_tags,'common'),(query.query_image,Path('missing.png'))):
            for limit in (0,-1):
                with self.assertRaisesRegex(ValueError,'limit'):
                    function(self.conn,argument,limit=limit)
        with self.assertRaisesRegex(RuntimeError,'failure'):
            with query._temporary_terms(self.conn,[(1,'a',0)]):
                raise RuntimeError('failure')
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT name FROM sqlite_temp_master").fetchall(),[])

    def test_image_candidates_are_batched_and_late_best_score_displaces_an_early_tie(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload.png"
            textured(12).save(path)
            hashes, _ = hashing.compute_image_hashes(path)
            original = hashes['phash256']
            # All candidates share exactly twelve pHash bands. The last image
            # is visually closer, so candidate hit order cannot supply top-k.
            for image_id in range(1, 404):
                self.add(image_id)
                mask = 1 if image_id == 403 else 15
                value = original[:24] + bytes(byte ^ mask for byte in original[24:])
                db.replace_hashes(self.conn, image_id, {'phash256': value})
                bands.insert_bands(self.conn, image_id, {'phash256': value})
            self.add(404)
            bands.insert_bands(self.conn, 404, {'phash256': original})  # stale bands without a hash row
            self.conn.commit()
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                result = query.query_image(self.conn, path, limit=2)
            finally:
                self.conn.set_trace_callback(None)
        self.assertEqual([item[0]['id'] for item in result], [403, 1])
        self.assertGreater(result[0][2].score, result[1][2].score)
        self.assertEqual(sum('SELECT h.*' in sql for sql in statements), 2)
        self.assertEqual(sum('SELECT i.*' in sql for sql in statements), 2)
        self.assertFalse(self.conn.in_transaction)
