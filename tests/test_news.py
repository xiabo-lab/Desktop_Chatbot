"""Feed parsing, interleaving and de-duplication.

The three things that decide whether "what's the local news" is worth
listening to, and all three fail quietly: a feed whose description arrives with
markup in it is read aloud as markup, a concatenated fetch means the busiest
publisher is the only one heard, and three outlets covering the same council
vote fill all five slots with one story.
"""

from __future__ import annotations

import unittest

from aipi5.tools.news import Story, clean, interleave, parse_feed

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Bay Area News</title>
  <item>
    <title>City council approves East San Jose park funding - The Mercury News</title>
    <link>https://example.com/a</link>
    <description>&lt;p&gt;The council voted 8-3 on Tuesday.&lt;/p&gt;&lt;img src="x"&gt;</description>
    <pubDate>Tue, 05 Aug 2026 10:00:00 GMT</pubDate>
    <source>The Mercury News</source>
  </item>
  <item>
    <title>VTA announces new light rail schedule</title>
    <link>https://example.com/b</link>
    <description>Service changes begin in September.</description>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>San Jose Spotlight</title>
  <entry>
    <title>Santa Clara County budget passes</title>
    <link href="https://example.com/c"/>
    <summary>Supervisors approved the plan.</summary>
    <updated>2026-08-05T12:00:00Z</updated>
  </entry>
</feed>
"""


class TestClean(unittest.TestCase):

    def test_strips_markup(self):
        self.assertEqual(clean("<p>Hello <b>there</b></p>"), "Hello there")

    def test_unescapes_before_stripping(self):
        # The order matters. Unescaping after the strip would turn `&lt;p&gt;`
        # back into a tag that nothing removes, and Piper would say
        # "less than p greater than" out loud.
        self.assertEqual(clean("&lt;p&gt;Hello&lt;/p&gt;"), "Hello")

    def test_collapses_whitespace(self):
        self.assertEqual(clean("a\n\n   b\tc"), "a b c")

    def test_empty_is_empty(self):
        self.assertEqual(clean(""), "")


class TestParseFeed(unittest.TestCase):

    def test_reads_rss(self):
        stories = parse_feed(RSS)
        self.assertEqual(len(stories), 2)
        self.assertEqual(stories[0].summary, "The council voted 8-3 on Tuesday.")
        self.assertEqual(stories[0].link, "https://example.com/a")

    def test_drops_the_syndication_suffix(self):
        # Google News appends " - Publisher" to every headline. Useful in a
        # reader; noise when five are read aloud one after another and the
        # publisher is named separately anyway.
        self.assertEqual(parse_feed(RSS)[0].title,
                         "City council approves East San Jose park funding")

    def test_prefers_the_declared_source_over_the_feed_title(self):
        stories = parse_feed(RSS)
        self.assertEqual(stories[0].source, "The Mercury News")
        self.assertEqual(stories[1].source, "Bay Area News",
                         "falls back to the feed's own title")

    def test_reads_atom(self):
        stories = parse_feed(ATOM)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].title, "Santa Clara County budget passes")
        # Atom puts the URL in an attribute rather than in the body — the one
        # place the two formats disagree about where a value lives.
        self.assertEqual(stories[0].link, "https://example.com/c")

    def test_broken_xml_is_an_empty_feed_not_an_exception(self):
        # A truncated download or an error page served with a 200. Normal
        # things to receive from a network, and neither may end the tool.
        self.assertEqual(parse_feed("<rss><channel><item>"), [])
        self.assertEqual(parse_feed(""), [])
        self.assertEqual(parse_feed("<html><body>403 Forbidden</body></html>"), [])

    def test_an_item_with_no_title_is_skipped(self):
        feed = "<rss><channel><item><link>x</link></item></channel></rss>"
        self.assertEqual(parse_feed(feed), [])


def story(title: str, source: str = "s") -> Story:
    return Story(title=title, summary="", source=source, link="", published="")


class TestInterleave(unittest.TestCase):

    def test_takes_one_from_each_feed_in_turn(self):
        busy = [story(f"Busy story {n}") for n in range(10)]
        quiet = [story("Quiet local story")]
        picked = interleave([busy, quiet], limit=3)
        self.assertIn("Quiet local story", [s.title for s in picked],
                      "the quiet feed must be heard before the busy one repeats")

    def test_collapses_the_same_story_from_two_outlets(self):
        # What a county's news actually looks like: one vote, three write-ups.
        first = [story("Santa Clara County supervisors approve housing budget")]
        second = [story("Supervisors approve Santa Clara County housing budget plan")]
        picked = interleave([first, second], limit=5)
        self.assertEqual(len(picked), 1)

    def test_keeps_genuinely_different_stories(self):
        first = [story("Santa Clara County supervisors approve housing budget")]
        second = [story("VTA light rail service returns to Alum Rock")]
        self.assertEqual(len(interleave([first, second], limit=5)), 2)

    def test_respects_the_limit(self):
        # Genuinely different headlines. An earlier version of this test used
        # "Story 0" … "Story 19", which the de-duplicator correctly collapsed
        # to one — every key reduced to {"story"}, since words of three
        # characters or fewer are dropped. That is the algorithm working, not
        # failing, and it is worth knowing that headlines carrying one
        # distinguishing word are treated as the same story.
        titles = ["Alum Rock light rail reopens after repairs",
                  "Downtown San Jose housing tower breaks ground",
                  "County health clinics extend weekend hours",
                  "Willow Glen library renovation finishes early",
                  "Eastridge transit project receives federal grant",
                  "Berryessa flea market vendors reach agreement"]
        self.assertEqual(len(interleave([[story(t) for t in titles]], limit=5)), 5)

    def test_headlines_sharing_only_short_words_are_still_collapsed(self):
        # The behaviour the test above ran into, pinned deliberately: keys are
        # built from words longer than three characters, so two headlines whose
        # only long word is the same read as one story.
        picked = interleave([[story("The VTA bus is on time"),
                              story("A VTA bus was not on time")]], limit=5)
        self.assertEqual(len(picked), 1)

    def test_no_feeds_is_no_stories(self):
        self.assertEqual(interleave([], limit=5), [])
        self.assertEqual(interleave([[]], limit=5), [])


if __name__ == "__main__":
    unittest.main()
