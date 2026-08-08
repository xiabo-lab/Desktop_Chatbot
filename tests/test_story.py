"""Bedtime story requests: length, subject, and the rules they run under.

Small tests for a small module, and worth having because this is the one thing
the assistant generates freely, for a child, at length. The parsing is fed a
speech transcript and must never fail on an unexpected shape — a request it
cannot read becomes a story of the default length about nothing in particular,
which is a perfectly good bedtime story.
"""

from __future__ import annotations

import unittest

from aipi5.tools.story import SAFETY_RULES, instructions, parse


class TestSubject(unittest.TestCase):

    def test_strips_the_lead_in(self):
        self.assertEqual(parse("tell me a story about a dragon who bakes").subject,
                         "a dragon who bakes")

    def test_handles_a_bare_request(self):
        self.assertEqual(parse("tell me a bedtime story").subject, "")

    def test_a_bare_request_still_has_a_subject_to_send(self):
        # `about` is what reaches the model, and it is never empty — an empty
        # "Tell a bedtime story about: ." is a worse prompt than one that says
        # "anything gentle".
        self.assertTrue(parse("tell me a story").about)

    def test_mandarin(self):
        self.assertEqual(parse("讲一个关于小兔子的睡前故事", "zh").subject,
                         "关于小兔子的睡前故事")

    def test_length_words_do_not_become_the_subject(self):
        # "short story about a dragon" must not leave "short" stranded at the
        # front, where the model reads it as the dragon's defining feature.
        self.assertEqual(parse("tell me a short story about a dragon").subject,
                         "a dragon")

    def test_unparseable_input_is_still_a_request(self):
        request = parse("mmm hmm story yes")
        self.assertGreater(request.minutes, 0)


class TestLength(unittest.TestCase):

    def test_default(self):
        self.assertEqual(parse("tell me a story", default_minutes=4).minutes, 4)

    def test_short_and_long(self):
        self.assertLess(parse("a short story", default_minutes=4).minutes, 4)
        self.assertGreater(parse("a long story", default_minutes=4).minutes, 4)

    def test_very_beats_plain(self):
        # Ordered longest-phrase-first, so "a very short story" does not match
        # on "short" and lose the "very".
        self.assertLess(parse("a very short story").minutes,
                        parse("a short story").minutes)

    def test_an_explicit_number_beats_an_adjective(self):
        self.assertEqual(parse("tell me a short 7 minute story").minutes, 7)

    def test_capped(self):
        self.assertEqual(parse("a really long story", default_minutes=4,
                               max_minutes=6).minutes, 6)

    def test_the_budget_is_expressed_in_the_right_unit(self):
        self.assertIn("words", parse("a story", "en").budget)
        self.assertIn("characters", parse("讲个故事", "zh").budget)


class TestInstructions(unittest.TestCase):

    def test_every_safety_rule_is_sent(self):
        # A parent should be able to read the whole of what the model was told.
        text = instructions(parse("tell me a story about a fox"))
        for rule in SAFETY_RULES:
            self.assertIn(rule, text)

    def test_the_subject_and_length_are_sent(self):
        text = instructions(parse("tell me a 3 minute story about a fox"))
        self.assertIn("a fox", text)
        self.assertIn("450 words", text)

    def test_the_language_is_named(self):
        self.assertIn("Mandarin", instructions(parse("讲个故事", "zh")))
        self.assertIn("English", instructions(parse("tell me a story", "en")))


if __name__ == "__main__":
    unittest.main()
