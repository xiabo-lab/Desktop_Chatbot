"""Two languages on one screen.

The mechanism is small; what these tests are really for is the part that rots.
A table like this goes wrong in three ways, all of them silent: a key used and
never defined, a key defined in one language only, and a string added to the
markup that nobody remembered to mark. The last one is the reason
`test_no_visible_english_is_unmarked` has an allowlist rather than a threshold
— every exception is a decision somebody wrote down.

**The English stays in the markup** as each element's own text, with
`data-i18n` naming the key beside it. Not redundancy: the page reads as HTML,
it still says something if the script dies, and the dozens of existing tests
that assert on English strings keep passing.
"""

from __future__ import annotations

import json
import re
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "aipi5" / "ui" / "web" / "index.html"


def page_text() -> str:
    return PAGE.read_text(encoding="utf-8")


def tables() -> dict:
    """`STRINGS`, read out of the page by running it under Node.

    Parsed by the language rather than by a regex over a JavaScript object
    literal, which is the technique `test_ui_script_runs` already owns — a
    regex here would be a second, worse JavaScript parser.
    """
    text = page_text()
    block = text[text.index("const STRINGS = {"):]
    block = block[:block.index("\n};") + 3]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "strings.mjs"
        path.write_text(block + "\nconsole.log(JSON.stringify(STRINGS));",
                        encoding="utf-8")
        # `encoding` explicitly: Node writes UTF-8 and this suite also runs on
        # Windows, where the default would decode a Chinese table as cp1252.
        done = subprocess.run(["node", str(path)], capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed here")
class TestTheTwoTablesAgree(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.strings = tables()

    def test_both_languages_have_the_same_keys(self):
        """The classic failure: a key added to English only, which renders as
        English inside a Chinese page and looks like a missed sweep."""
        self.assertEqual(set(self.strings["en"]), set(self.strings["zh"]))

    def test_nothing_is_empty(self):
        for lang, table in self.strings.items():
            for key, value in table.items():
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(value.strip(), f"{lang}.{key} is blank")

    def test_the_chinese_is_actually_chinese(self):
        """Guards against a key copied across untranslated. The allowlist is
        the point — each entry is a word that is the same in both languages,
        written down rather than tolerated by a percentage."""
        same_in_both = {
            "settings.google_photos",     # a product name
            "settings.time_weather",      # "Time + Weather" -> uses + and CJK
        }
        for key, english in self.strings["en"].items():
            chinese = self.strings["zh"][key]
            if key in same_in_both or not re.search(r"[A-Za-z]", english):
                continue
            with self.subTest(key=key):
                self.assertTrue(re.search(r"[一-鿿]", chinese),
                                f"{key} has no Chinese in it: {chinese!r}")

    def test_a_placeholder_survives_translation(self):
        """The classic translation bug: `{n}` dropped, so the sentence renders
        with no number in it."""
        for key, english in self.strings["en"].items():
            wanted = set(re.findall(r"\{(\w+)\}", english))
            with self.subTest(key=key):
                self.assertEqual(
                    wanted, set(re.findall(r"\{(\w+)\}", self.strings["zh"][key])))


@unittest.skipUnless(shutil.which("node"), "node is not installed here")
class TestEveryKeyIsRealAndUsed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.page = page_text()
        cls.defined = set(tables()["en"])
        cls.used = set(re.findall(r'data-i18n(?:-[\w-]+)?="([\w.]+)"', cls.page))
        # A boundary before the `t`, or this matches `createElemen*t(*"button")`
        # and every other call whose name happens to end in one.
        cls.used |= set(re.findall(r'(?<![\w.$])t\("([\w.]+)"', cls.page))
        # Some keys are reached a step removed: `MODE_TEXT` maps a wire value
        # to a key and the call site does `t(MODE_TEXT[mode])`, because the
        # language can change long after that table was built. A key named
        # anywhere outside the table counts as used — what this is really for
        # is the key nothing mentions at all.
        table = cls.page.index("const STRINGS = {")
        outside = (cls.page[:table]
                   + cls.page[cls.page.index("\n};", table):])
        cls.used |= {key for key in cls.defined if f'"{key}"' in outside}

    def test_every_key_the_page_asks_for_exists(self):
        """`t()` falls back to the key itself, so a missing one renders as
        `settings.volume` on screen — visible, but only to whoever is looking
        at that panel."""
        self.assertEqual(set(), self.used - self.defined)

    def test_every_key_defined_is_used(self):
        """The check that stops the table rotting over years."""
        self.assertEqual(set(), self.defined - self.used)


class TestTheSweepCoveredThePage(unittest.TestCase):
    """Did anything get missed. Walks the markup for visible English and
    asserts each piece carries a key."""

    #: Text that is deliberately not translated, and why. Each of these is a
    #: decision rather than an oversight.
    ALLOWED = {
        # The device's own name, in its own language, everywhere it appears.
        "小爱同学",
        # Written in its own language on purpose: somebody who cannot read the
        # language the screen is in has to be able to find their way back.
        "English", "中文",
        # Units, symbols and separators.
        "—", "·", "‹", "›", "↑", "✎", "🎙", "%", "°F", "°C",
        # Brand and hardware names that are the same in both languages.
        "AIPI5", "Google Photos", "Kodama-Lite", "Hailo", "Wi-Fi",
    }

    def test_no_visible_english_is_unmarked(self):
        text = page_text()
        body = text[text.index("<body"):]
        body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)

        missed = []
        for match in re.finditer(r"<(\w+)((?:\s[^<>]*)?)>([^<>]+)</\1>", body):
            attrs, content = match.group(2), match.group(3).strip()
            if "data-i18n" in attrs:
                continue
            if content in self.ALLOWED or len(content) < 2:
                continue
            if not re.search(r"[A-Za-z]{2}", content):
                continue
            missed.append(content)
        self.assertEqual([], missed,
                         "these read as English and carry no key: " + repr(missed))


class TestApplyingIt(unittest.TestCase):

    def setUp(self):
        self.page = page_text()

    def body_of(self, name: str) -> str:
        rest = self.page[self.page.index(f"function {name}("):]
        return rest[:rest.index("\nfunction ")]

    def test_the_document_language_changes_with_the_words(self):
        """Not cosmetic: Chromium picks CJK line-breaking and font variants
        from it, so a page that changed its words without this wraps Chinese as
        though it were English."""
        self.assertIn("document.documentElement.lang", self.body_of("applyLanguage"))

    def test_the_choice_is_remembered(self):
        body = self.body_of("applyLanguage")
        self.assertIn("localStorage.setItem(LANG_KEY", body)
        self.assertIn("catch", body)          # private mode must not break it

    def test_a_query_parameter_can_set_it_too(self):
        """This panel has no keyboard, and an ssh tunnel is how it is reached
        when something has gone wrong — the same reasoning `fromHash` records
        for pages."""
        self.assertIn('.get("lang")', self.body_of("startLanguage"))

    def test_an_unknown_language_falls_back_to_english(self):
        self.assertIn('next === "zh" ? "zh" : "en"', self.body_of("applyLanguage"))

    def test_it_runs_at_startup(self):
        self.assertIn("startLanguage();", self.page)

    def test_the_language_buttons_are_never_translated(self):
        """Each is written in its own language so that whoever cannot read the
        current one can still get back. A `data-i18n` on either would defeat
        the whole control."""
        strip = self.page[self.page.index('id="set-languages"'):]
        strip = strip[:strip.index("</div>")]
        self.assertIn(">English</button>", strip)
        self.assertIn(">中文</button>", strip)
        self.assertNotIn("data-i18n", strip)

    def test_the_settings_panel_is_redrawn_at_once(self):
        """Everything else corrects itself on its next poll, but the settings
        page is the one somebody is looking at when they press the button."""
        self.assertIn('if (currentPage === "settings") drawSettings();',
                      self.body_of("applyLanguage"))

    def test_it_does_not_touch_what_the_assistant_answers_in(self):
        """A household where one person speaks English and another Chinese
        would be badly served by a screen setting that overrode what it
        heard. The reply language is decided per sentence, in Python."""
        self.assertNotIn("/api/language", self.page)
        self.assertNotIn("reply_language", self.page)


if __name__ == "__main__":
    unittest.main()
