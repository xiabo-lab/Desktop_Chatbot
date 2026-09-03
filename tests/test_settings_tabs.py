"""Four tabs instead of one long scroll.

The Settings page had grown six unrelated cards on one scrolling list —
volume, screensaver, Google Photos, Fruit Ninja, AI Motion, display — and on a
1280×800 screen that is a scroll with no organising idea.

**The cards moved unchanged and kept every id**, which is the property most of
this file is about: the six hundred lines of script that fill them by
`el("set-…")` needed no edit at all, and every test that asserted on their
markup still passes untouched. A move that had to rewrite its callers would
have been a rewrite wearing a move's clothes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "aipi5" / "ui" / "web" / "index.html"

TABS = ("hardware", "game", "system", "screensaver")


def between(text: str, start: str, stop: str) -> str:
    """From just after `start` up to the next `stop`, or to the end."""
    rest = text[text.index(start) + len(start):]
    end = rest.find(stop)
    return rest if end == -1 else rest[:end]


class TabCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding="utf-8")
        cls.settings = between(cls.page, '<div id="page-settings"',
                               '\n<div id="page-')

    def panel(self, name: str) -> str:
        """One panel's markup, up to where the next one begins.

        Sliced inside `#settings-body` rather than the whole page block: the
        last panel would otherwise run past the body's close and swallow the
        `#picker` overlay that sits after it.
        """
        closes_body = "\n  </div>"
        body = between(self.settings, '<div id="settings-body">', closes_body)
        return between(body, f'data-set-panel="{name}"', 'data-set-panel="')

    def function(self, name: str) -> str:
        return between(self.page, f"function {name}(", "\nfunction ")


class TestTheStripAndThePanels(TabCase):

    def test_there_are_four_tabs_and_four_panels(self):
        self.assertEqual(list(TABS),
                         re.findall(r'data-set-tab="(\w+)"', self.settings))
        self.assertEqual(list(TABS),
                         re.findall(r'data-set-panel="(\w+)"', self.settings))

    def test_exactly_one_of_each_starts_open(self):
        """A page opening with every panel hidden is a blank page; one opening
        with two has two scrollbars."""
        self.assertEqual(1, self.settings.count('class="set-tab on"'))
        self.assertEqual(1, self.settings.count('class="set-panel on"'))

    def test_the_strip_sits_outside_the_scrolling_body(self):
        """Inside it, the tabs scroll away with the content they switch."""
        self.assertLess(self.settings.index('id="settings-tabs"'),
                        self.settings.index('id="settings-body"'))

    def test_every_tab_is_reachable_by_touch(self):
        """62px is the target the rest of this page settled on."""
        self.assertIn("height: 62px", between(self.page, "  .set-tab {", "}"))


class TestNothingWasLostInTheMove(TabCase):
    """The failure this guards against is silent: a section dropped out of the
    page still leaves a script that reads its ids, and `el()` on a missing id
    returns null — a blank panel and a TypeError nobody on a kiosk can see."""

    #: Every id the settings script fills, by the tab it now lives in. Named
    #: here rather than derived, so moving one between tabs is a decision
    #: somebody wrote down rather than something a test quietly follows.
    EXPECTED = {
        "hardware": ("set-hailo", "set-hailo-fw", "set-pose-model",
                     "set-motion-camera", "set-motion-fps", "set-debug-pose",
                     "set-motion-note"),
        "game": ("set-game-time", "set-game-times", "set-game-note",
                 "set-game-difficulty", "set-game-difficulties",
                 "set-difficulty-note", "set-game-sound", "set-game-sounds"),
        "system": ("set-volume", "set-volume-value", "set-volume-note",
                   "set-fullscreen", "set-go-fullscreen",
                   "set-version", "set-update"),
        "screensaver": ("set-enabled", "set-timeout", "set-day", "set-night",
                        "set-mode", "set-camera-idle", "set-schedule-note",
                        "set-account", "set-album", "set-interval", "set-cache",
                        "set-pick", "set-sync", "set-reconnect",
                        "set-disconnect", "set-photos-note", "set-collections"),
    }

    def test_every_setting_is_in_the_tab_it_belongs_to(self):
        for tab, ids in self.EXPECTED.items():
            body = self.panel(tab)
            for ident in ids:
                with self.subTest(tab=tab, id=ident):
                    self.assertIn(f'id="{ident}"', body)

    def test_no_setting_appears_in_two_tabs(self):
        for ids in self.EXPECTED.values():
            for ident in ids:
                with self.subTest(id=ident):
                    self.assertEqual(1, self.settings.count(f'id="{ident}"'))

    def test_the_six_cards_are_all_still_there(self):
        """Matched on the title text rather than the whole tag: the sweep that
        made the page bilingual added a `data-i18n` attribute to every heading,
        and the English stays in the element as its own text — which is the
        property worth asserting."""
        for title in ("Volume", "Screensaver", "Slideshow source",
                      "Fruit Ninja", "AI Motion", "Display"):
            with self.subTest(card=title):
                self.assertRegex(self.settings,
                                 r'class="set-title"[^>]*>' + title + "<")

    def test_the_qr_overlay_is_not_inside_a_panel(self):
        """`#picker` is a fixed overlay that must survive a tab switch — it is
        on screen while somebody stands there with a phone. It lives after the
        page's own closing div, which is why the check is per-panel rather
        than over the whole block."""
        for tab in TABS:
            with self.subTest(tab=tab):
                self.assertNotIn('id="picker"', self.panel(tab))


class TestThePlaceholderLooksLikeOne(TabCase):
    """A control that responds and does nothing is one somebody presses twice
    and then reports as broken. A greyed one with a sentence under it has never
    once been mistaken for a feature."""

    def test_the_button_is_disabled_in_the_markup(self):
        self.assertIn('id="set-update" disabled', self.settings)

    def test_nothing_listens_to_it(self):
        """Not even a handler that says "coming soon" — that is a response,
        and a response is what makes it look wired."""
        self.assertNotIn('el("set-update")', self.page)
        self.assertNotIn("set-update\").addEventListener", self.page)

    def test_it_says_how_the_device_is_actually_updated(self):
        """An honest placeholder says what to do instead."""
        panel = self.panel("system")
        self.assertIn("deploy.sh", panel)
        self.assertIn("Not built yet", panel)

    def test_the_version_row_is_not_inert(self):
        """It answers "what am I running", which is the real question behind
        reaching for the button."""
        body = between(self.page, "async function drawSettings()",
                       "\nfunction ")
        self.assertIn('el("set-version").textContent', body)


class TestSwitchingTabs(TabCase):

    def test_the_buttons_are_wired(self):
        self.assertIn('document.querySelectorAll("[data-set-tab]")', self.page)
        self.assertIn("showSettingsTab(button.dataset.setTab)", self.page)

    def test_the_choice_is_remembered(self):
        """Somebody adjusting a boundary leaves and returns several times."""
        self.assertIn('const SET_TAB_KEY = "aipi5.settings.tab"', self.page)
        body = self.function("showSettingsTab")
        self.assertIn("localStorage.setItem(SET_TAB_KEY", body)
        self.assertIn("catch", body)          # private mode must not break it

    def test_an_unknown_name_falls_back_rather_than_blanking_the_page(self):
        self.assertIn('SET_TABS.includes(name) ? name : "hardware"',
                      self.function("showSettingsTab"))

    def test_the_scroll_position_does_not_follow_you_to_the_next_tab(self):
        self.assertIn('el("settings-body").scrollTop = 0',
                      self.function("showSettingsTab"))

    def test_leaving_the_screensaver_tab_takes_the_qr_code_with_it(self):
        """Otherwise a live picking session hangs over the Game settings."""
        self.assertIn('if (settingsTab !== "screensaver") closePicker("hide")',
                      self.function("showSettingsTab"))

    def test_a_new_tab_draws_at_once_rather_than_after_four_seconds(self):
        self.assertIn("drawSettings();", self.function("showSettingsTab"))


class TestOnlyTheVisibleTabDoesWork(TabCase):
    """`/api/system` answers every tab in one request and is fetched
    regardless. The other three are separate round trips, and
    `drawMotionSettings` opens the accelerator over PCIe to read its firmware
    version — real work to repeat every four seconds for somebody reading a
    different tab."""

    def setUp(self):
        self.body = between(self.page, "async function drawSettings()",
                            "\nfunction ")

    def test_the_photo_and_pick_draws_are_screensaver_only(self):
        self.assertIn('if (settingsTab === "screensaver") {', self.body)

    def test_the_game_fetch_is_game_only(self):
        self.assertIn('if (settingsTab === "game") drawGameSettings();',
                      self.body)

    def test_the_accelerator_is_only_opened_for_the_tab_that_shows_it(self):
        """Both hardware draws sit behind the one guard. Asserted as "inside
        the branch" rather than as an exact line, so adding a third row to
        that tab does not fail a test about polling."""
        closes = "\n  }"
        guarded = between(self.body, 'if (settingsTab === "hardware") {', closes)
        self.assertIn("drawMotionSettings();", guarded)
        self.assertIn("drawHardware(", guarded)

    def test_the_one_shared_request_is_still_unconditional(self):
        """Gating it would save nothing and add a fourth thing to be stale."""
        self.assertIn('fetch("/api/system"', self.body)
        self.assertNotIn("if (settingsTab",
                         self.body[:self.body.index("/api/system")])


if __name__ == "__main__":
    unittest.main()
