from __future__ import annotations

import html
from html.parser import HTMLParser
import unittest

from imgdupe.web import _highlight_prompt


class PromptHighlightingTests(unittest.TestCase):
    def test_entity_names_do_not_match_escaped_punctuation(self):
        prompt = "<cat> & \"dog\" 'bird'"
        self.assertEqual(_highlight_prompt(prompt, ("amp", "lt", "gt", "quot", "x27")),
                         html.escape(prompt))

    def test_literal_entity_names_are_highlighted_without_breaking_entities(self):
        self.assertEqual(_highlight_prompt("amp & lt < quot \" x27 '",
                                           ("amp", "lt", "quot", "x27")),
                         '<mark>amp</mark> &amp; <mark>lt</mark> &lt; '
                         '<mark>quot</mark> &quot; <mark>x27</mark> &#x27;')

    def test_matching_raw_special_characters_remains_escaped(self):
        self.assertEqual(_highlight_prompt('<cat> & "dog"', ("<cat>", '"dog"')),
                         '<mark>&lt;cat&gt;</mark> &amp; <mark>&quot;dog&quot;</mark>')

    def test_longest_overlapping_tags_case_unicode_and_regex_metacharacters(self):
        self.assertEqual(_highlight_prompt("Blue eyes, eyes, 猫(蓝色), a|b", (
            "eyes", "blue eyes", "猫(蓝色)", "a|b", "", "eyes"
        )), '<mark>Blue eyes</mark>, <mark>eyes</mark>, <mark>猫(蓝色)</mark>, <mark>a|b</mark>')

    def test_visible_text_always_equals_original_prompt(self):
        class TextReader(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.text = []

            def handle_data(self, data):
                self.text.append(data)

        for prompt in ("&amp;", "&lt;script&gt;", '<script>alert("x")</script>',
                       "O'Reilly & 猫", "no match", " ", "already <mark>marked</mark>"):
            with self.subTest(prompt=prompt):
                rendered = _highlight_prompt(prompt, ("amp", "lt", "script", "Reilly", "猫"))
                parser = TextReader()
                parser.feed(rendered)
                self.assertEqual("".join(parser.text), prompt.strip())
                self.assertNotIn("<script>", rendered)

    def test_empty_prompt_tags_and_no_matching_tag(self):
        self.assertEqual(_highlight_prompt("   ", ("cat",)), "")
        self.assertEqual(_highlight_prompt("  cat & dog  ", ("", "  ")),
                         "cat &amp; dog")
        self.assertEqual(_highlight_prompt("cat & dog", ("bird",)), "cat &amp; dog")
