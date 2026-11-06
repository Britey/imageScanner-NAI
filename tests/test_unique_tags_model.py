"""Bind the finite TLA+ model's visibility abstraction to actual SQLite queries."""

from __future__ import annotations

import itertools
import sqlite3
import unittest

from imgdupe.db import init_db
from imgdupe.web import _count_unique_tags


class UniqueTagsModelConformanceTests(unittest.TestCase):
    def test_every_two_image_two_tag_database_matches_the_visibility_spec(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.executemany("INSERT INTO images(id,path,size_bytes,mtime_ns,indexed_at) "
                         "VALUES(?,?,1,1,'now')", [(1, "1.png"), (2, "2.png")])
        # Four concrete states refine the model's absent/null/non-null states;
        # SQL's non-null case includes both an empty and a nonempty label.
        states = ("absent", "null", "empty", "label")
        labels = {"null": None, "empty": "", "label": "custom"}
        cells = ((1, "cat"), (1, "@model:sd"), (2, "cat"), (2, "@model:sd"))
        kinds = (None, "prompt", "model_type", "model_config", "model_hash")
        checked = 0
        legacy = """
            SELECT COUNT(DISTINCT pt.tag_norm)
            FROM prompt_tags pt JOIN image_prompts ip ON ip.image_id=pt.image_id
            WHERE NOT (pt.tag_kind='model_type' AND ip.configured_model IS NOT NULL)
        """
        for prompt_states in itertools.product(states, repeat=2):
            conn.execute("DELETE FROM image_prompts")
            conn.executemany("INSERT INTO image_prompts(image_id,prompt_indexed_at,configured_model) "
                             "VALUES(?,'now',?)",
                             [(i, labels[state]) for i, state in enumerate(prompt_states, 1)
                              if state != "absent"])
            for assignment in itertools.product(kinds, repeat=4):
                conn.execute("DELETE FROM prompt_tags")
                conn.executemany("INSERT INTO prompt_tags(image_id,tag,tag_norm,position,tag_kind) "
                                 "VALUES(?,?,?,0,?)",
                                 [(i, tag, tag, kind) for (i, tag), kind in zip(cells, assignment)
                                  if kind is not None])
                expected = set()
                for (i, tag), kind in zip(cells, assignment):
                    state = prompt_states[i - 1]
                    if kind is None or state == "absent":
                        continue
                    if kind == "model_type" and state in ("empty", "label"):
                        continue
                    expected.add(tag)
                actual = (_count_unique_tags(conn), conn.execute(legacy).fetchone()[0])
                self.assertEqual(actual, (len(expected), len(expected)),
                                 msg=f"prompts={prompt_states}, rows={assignment}")
                checked += 1
        self.assertEqual(checked, 10_000)
