from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from imgdupe import query as query_module

from imgdupe.db import init_db
from imgdupe.query import query_prompt_tags


class PromptQueryGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def add_image(
        self,
        image_id: int,
        *tags: tuple[str, str],
        prompt: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO images (id, path, size_bytes, mtime_ns, indexed_at)
            VALUES (?, ?, 1, ?, 'now')
            """,
            (image_id, f"image-{image_id}.png", image_id),
        )
        self.conn.execute(
            """
            INSERT INTO image_prompts (image_id, prompt, prompt_indexed_at)
            VALUES (?, ?, 'now')
            """,
            (image_id, prompt or f"prompt {image_id}"),
        )
        self.conn.executemany(
            """
            INSERT INTO prompt_tags (image_id, tag, tag_norm, position, tag_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (image_id, tag, tag.casefold(), position, tag_kind)
                for position, (tag, tag_kind) in enumerate(tags)
            ],
        )

    def result_ids(self, text: str, **kwargs) -> list[int]:
        return [
            int(result.image_row["id"])
            for result in query_prompt_tags(self.conn, text, sort_by="name_asc", **kwargs)
        ]

    def test_or_alternatives_are_grouped_and_comma_groups_are_anded(self) -> None:
        self.add_image(1, ("a", "prompt"), ("c", "prompt"))
        self.add_image(2, ("b", "prompt"), ("c", "prompt"))
        self.add_image(3, ("a", "prompt"))
        self.add_image(4, ("b", "prompt"), ("d", "prompt"))

        self.assertEqual(self.result_ids("a|b,c"), [1, 2])
        self.assertEqual(self.result_ids("(a｜b)，c"), [1, 2])
        self.assertEqual(self.result_ids("a|b,c|d"), [1, 2, 4])

    def test_match_mode_any_counts_satisfied_groups_not_alternatives(self) -> None:
        self.add_image(1, ("a", "prompt"), ("b", "prompt"), ("c", "prompt"))
        self.add_image(2, ("a", "prompt"))

        results = query_prompt_tags(self.conn, "a|b,c", match_mode="any", sort_by="name_asc")

        self.assertEqual([result.matched_count for result in results], [2, 1])
        self.assertEqual(set(results[0].matched_tags), {"a", "b", "c"})

    def test_duplicate_groups_and_alternatives_do_not_inflate_matches(self) -> None:
        self.add_image(1, ("a", "prompt"), ("b", "prompt"), ("c", "prompt"))
        self.add_image(2, ("a", "prompt"))
        results = query_prompt_tags(self.conn, "a|a|b,b|a,c,c", match_mode="any", sort_by="name_asc")
        self.assertEqual([result.image_row["id"] for result in results], [1, 2])
        self.assertEqual([result.matched_count for result in results], [2, 1])
        self.assertEqual(set(results[0].matched_tags), {"a", "b", "c"})

    def test_negative_terms_still_exclude_independently(self) -> None:
        self.add_image(1, ("cat", "prompt"), ("bad", "prompt"))
        self.add_image(2, ("cat", "prompt"), ("worse", "prompt"))
        self.add_image(3, ("cat", "prompt"))

        self.assertEqual(
            self.result_ids("cat", negative_text_or_tags="bad|worse"),
            [3],
        )

    def test_model_search_accepts_any_non_prompt_metadata_kind(self) -> None:
        self.add_image(1, ("@model:abc", "model_hash"))
        self.add_image(2, ("@model:abc", "model_identifier"))
        self.add_image(3, ("@model:abc", "prompt"))

        self.assertEqual(self.result_ids("@model:abc"), [1, 2])

    def test_configured_category_hides_legacy_detected_category_tag(self) -> None:
        self.add_image(
            1,
            ("cat", "prompt"),
            ("@model:sd", "model_type"),
            ("@model:noob", "model_config"),
            ("@model:abc123", "model_hash"),
        )
        self.conn.execute(
            """
            UPDATE image_prompts
            SET model_type = 'sd', configured_model = 'noob'
            WHERE image_id = 1
            """
        )

        self.assertEqual(self.result_ids("@model:noob"), [1])
        self.assertEqual(self.result_ids("@model:sd"), [])
        self.assertEqual(self.result_ids("@model:abc123"), [1])
        self.assertEqual(self.result_ids("cat", model_type="noob"), [1])
        self.assertEqual(self.result_ids("cat", model_type="sd"), [])

    def test_empty_queries_and_invalid_match_modes_do_not_run_a_broad_search(self):
        self.add_image(1, ("cat", "prompt"))
        for text in ("", " , | ", []):
            with self.subTest(text=text):
                self.assertEqual(query_prompt_tags(self.conn, text), [])
        with self.assertRaisesRegex(ValueError, "match_mode"):
            query_prompt_tags(self.conn, "cat", match_mode="everything")

    def test_list_inputs_and_literal_model_markers_preserve_tag_boundaries(self):
        self.add_image(1, ("cat", "prompt"), ("prefix@model:literal", "prompt"))
        self.add_image(2, ("cat", "prompt"))
        self.assertEqual(self.result_ids(["cat", "cat"]), [1, 2])
        self.assertEqual(self.result_ids("prefix@model:literal"), [1])
        self.assertEqual(self.result_ids("@model:literal"), [])

    def test_parentheses_around_alternatives_never_strip_independent_qualifiers(self):
        self.add_image(1, ("cat", "prompt"))
        self.add_image(2, ("dog", "prompt"))
        self.add_image(3, ("cat (sleeping)", "prompt"))
        self.assertEqual(self.result_ids("((cat | dog))"), [1, 2])
        # Structural wrappers can enclose each alternative independently;
        # parentheses inside a literal qualifier remain part of the tag.
        self.assertEqual(self.result_ids("(cat) | (dog)"), [1, 2])
        self.assertEqual(self.result_ids("((cat)"), [])
        self.assertEqual(self.result_ids("cat (sleeping)"), [3])


    def test_unknown_balanced_model_names_never_turn_inner_commas_into_prompt_terms(self):
        self.add_image(1, ('cat', 'prompt'), ('@model:Foo', 'model_identity'))
        self.add_image(2, ('cat', 'prompt'), ('Baz)', 'prompt'), ('Qux))', 'prompt'))
        for text in ('@model:Foo (Bar, Baz)', '@model:Foo (Bar, (Baz, Qux))',
                     '@model:Unknown (Bar, Baz)', '@model:Foo (Bar，Baz)'):
            with self.subTest(query=text):
                self.assertEqual(self.result_ids(text), [])
                self.assertEqual(self.result_ids(text, match_mode='any'), [])
                self.assertEqual(self.result_ids('cat', negative_text_or_tags=text), [1, 2])
        self.assertEqual(self.result_ids('@model:Foo, cat'), [1])

    def test_balanced_model_with_trailing_text_remains_one_exact_term(self):
        model = '@model:Foo (Bar Baz)'
        self.add_image(1, ('cat', 'prompt'), (model, 'model_identity'))
        self.add_image(2, ('cat', 'prompt'), ('Baz) cat', 'prompt'))
        self.add_image(3, (model, 'model_identity'))
        self.assertEqual(self.result_ids('@model:Foo (Bar, Baz)'), [1, 3])
        self.assertEqual(self.result_ids('@model:Foo (Bar, Baz), cat'), [1])
        text = '@model:Foo (Bar, Baz) cat'
        self.assertEqual(self.result_ids(text), [])
        self.assertEqual(self.result_ids(text, match_mode='any'), [])
        self.assertEqual(self.result_ids('cat', negative_text_or_tags=text), [1, 2])
        self.add_image(4, (model + ' cat', 'model_identity'))
        self.assertEqual(self.result_ids(text), [4])

    def test_parentheses_enclosing_model_and_comma_groups_expand_stored_names(self):
        names = ('@model:Foo (Bar, Baz)', '@model:Foo, Bar (Baz)', '@model:sd')
        for image_id, name in enumerate(names, 1):
            self.add_image(image_id, ('cat', 'prompt'), (name, 'model_identity'))
        self.add_image(4, ('cat', 'prompt'))
        self.add_image(5, (names[0], 'model_identity'))
        for image_id, name in enumerate(names, 1):
            for text in (f'({name}, cat)', f'(({name}, cat))', f'(cat, ({name}))'):
                with self.subTest(query=text):
                    results = query_prompt_tags(self.conn, text)
                    self.assertEqual([result.image_row['id'] for result in results], [image_id])
                    self.assertEqual(results[0].matched_count, 2)
                    self.assertEqual(set(results[0].matched_tags), {name, 'cat'})
            self.assertEqual(self.result_ids('cat', negative_text_or_tags=f'({name}, absent)'),
                             [other for other in (1, 2, 3, 4) if other != image_id])
        self.assertEqual(self.result_ids('(cat, absent)'), [])

    def test_embedded_parenthesized_comma_groups_keep_all_constraints(self):
        self.add_image(1, ('cat', 'prompt'), ('dog', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(2, ('cat', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(3, ('dog', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(4, ('cat', 'prompt'), ('dog', 'prompt'))
        self.add_image(5, ('cat', 'prompt'))
        for text in ('cat, (@model:sd, dog)', 'cat, ((@model:sd, dog))',
                     '(cat, (@model:sd, dog))', '(@model:sd, dog), cat'):
            with self.subTest(query=text):
                self.assertEqual(self.result_ids(text), [1])
        self.assertEqual(self.result_ids('cat', negative_text_or_tags='(@model:sd, dog)'), [5])

    def test_large_balanced_model_name_does_not_resanitize_every_inner_comma(self):
        self.add_image(1, ('@model:Foo', 'model_identity'))
        self.add_image(2, ('term999)', 'prompt'))
        text = '@model:Foo (' + ', '.join(f'term{index}' for index in range(1000)) + ')'
        processed_characters = 0
        original = query_module.sanitize_model_name
        def measured_sanitize(value):
            nonlocal processed_characters
            processed_characters += len(value)
            return original(value)
        with patch.object(query_module, 'sanitize_model_name', measured_sanitize):
            self.assertEqual(self.result_ids(text, match_mode='any'), [])
        self.assertLessEqual(processed_characters, 2 * len(text) + 100)


    def test_overlong_legacy_comma_names_do_not_leak_prompt_terms(self):
        self.add_image(1, ('cat', 'prompt'), ('@model:Foo, Bar', 'model_identity'))
        self.add_image(2, ('cat', 'prompt'), ('bar extra', 'prompt'), ('bar (baz) extra', 'prompt'),
                          ('bar-v2', 'prompt'), ('bar (baz)extra', 'prompt'))
        self.add_image(3, ('cat', 'prompt'), ('dog', 'prompt'), ('@model:Foo', 'model_identity'))
        self.add_image(4, ('cat', 'prompt'), ('@model:Foo, Bar (Baz)', 'model_identity'))
        for text in ('@model:Foo, Bar extra', '@model:Foo, Bar (Baz) extra', '@model:Foo，Bar extra',
                     '@model:Foo, Bar-v2', '@model:Foo, Bar (Baz)extra'):
            with self.subTest(query=text):
                self.assertEqual(self.result_ids(text), [])
                self.assertEqual(self.result_ids(text, match_mode='any'), [])
                self.assertEqual(self.result_ids('cat', negative_text_or_tags=text), [1, 2, 3, 4])
        self.assertEqual(self.result_ids('@model:Foo, dog'), [3])
        self.assertEqual(self.result_ids('@model:Foo, Bar, cat'), [1])
        self.add_image(5, ('@model:Foo, Bar extra', 'model_identity'))
        self.assertEqual(self.result_ids('@model:Foo, Bar extra'), [5])

    def test_unfinished_legacy_model_continuations_keep_inner_commas_in_metadata(self):
        self.add_image(1, ('cat', 'prompt'), ('@model:Foo, Bar', 'model_identity'))
        self.add_image(2, ('cat', 'prompt'), ('@model:Foo', 'model_identity'))
        self.add_image(3, ('cat', 'prompt'), ('bar (baz extra', 'prompt'),
                          ('bar (baz', 'prompt'), ('qux extra', 'prompt'))
        self.add_image(4, ('cat', 'prompt'), ('qux) extra', 'prompt'))
        self.add_image(5, ('cat', 'prompt'))
        cases = (
            ('@model:Foo, Bar (Baz extra', []),
            ('@model:Foo, Bar (Baz, Qux extra', []),
            ('@model:Foo，Bar (Baz，Qux extra', []),
            ('@model:Foo, Bar ((Baz, Qux extra', []),
            ('@model:Foo, Bar (Baz|Qux) extra', [4]),
            ('@model:Foo, Bar (Baz｜Qux) extra', [4]),
        )
        for text, expected in cases:
            with self.subTest(query=text):
                self.assertEqual(self.result_ids(text), expected)
                self.assertEqual(self.result_ids(text, match_mode='any'), expected)
                self.assertEqual(self.result_ids('cat', negative_text_or_tags=text),
                                 [image_id for image_id in range(1, 6) if image_id not in expected])
        # A fully stored unfinished name still matches exactly; explicit AND
        # groups and ordinary OR alternatives retain their existing meaning.
        self.add_image(6, ('@model:Foo, Bar (Baz extra', 'model_identity'))
        self.assertEqual(self.result_ids(cases[0][0]), [6])
        self.assertEqual(self.result_ids('@model:Foo, Bar, cat'), [1])
        self.assertEqual(self.result_ids('@model:Foo | qux) extra'), [2, 4])

    def test_closed_model_groups_finish_before_the_following_prompt_term(self):
        self.add_image(1, ('cat', 'prompt'), ('extra', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(2, ('cat', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(3, ('cat', 'prompt'), ('extra', 'prompt'))
        self.add_image(4, ('cat', 'prompt'))
        for text in ('(cat, @model:sd) extra', 'cat, (@model:sd) extra',
                     '(cat, (@model:sd)) extra', 'cat, (@model:sd))extra',
                     '(cat, @model:sd) (extra)', '(cat, @model:sd)(extra)',
                     'cat, (@model:sd) ((extra))'):
            with self.subTest(query=text):
                results = query_prompt_tags(self.conn, text)
                self.assertEqual([result.image_row['id'] for result in results], [1])
                self.assertEqual(results[0].matched_count, 3)
                self.assertEqual(set(results[0].matched_tags), {'cat', 'extra', '@model:sd'})
        self.assertEqual(self.result_ids('(@model:sd) | extra'), [1, 2, 3])
        self.assertEqual(self.result_ids('cat', negative_text_or_tags='(@model:sd) extra'), [4])

    def test_model_qualifier_and_stray_closer_do_not_stick_to_internal_tokens(self):
        name = '@model:NovelAI Diffusion V4.5 Full (Curated)'
        self.add_image(1, ('1girl', 'prompt'), ('extra', 'prompt'), (name, 'model_identity'))
        self.add_image(2, ('1girl', 'prompt'), (name, 'model_identity'))
        self.add_image(3, ('extra', 'prompt'), ('@model:Foo, Bar', 'model_identity'))
        self.assertEqual(self.result_ids(f'1girl, ({name}) extra'), [1])
        self.assertEqual(self.result_ids('@model:Foo, Bar)'), [3])
        self.assertEqual(self.result_ids('@model:Foo, Bar) extra'), [3])

    def test_only_complete_protected_tokens_expand_as_metadata(self):
        literal = 'cat (@model:sd) extra'
        self.add_image(1, (literal, 'prompt'))
        self.add_image(2, ('cat', 'prompt'), ('extra', 'prompt'), ('@model:sd', 'model_type'))
        self.add_image(3, ('cat ((sleeping))', 'prompt'))
        self.add_image(4, ('cat)', 'prompt'))
        self.assertEqual(self.result_ids(literal), [1])
        self.assertEqual(self.result_ids('cat ((sleeping))'), [3])
        self.assertEqual(self.result_ids('cat)'), [4])


if __name__ == "__main__":
    unittest.main()
