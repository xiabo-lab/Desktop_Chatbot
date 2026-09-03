"""Easy, Normal and Hard, and the high scores that must survive them.

Two things carry the weight here.

**`normal` is not a preset.** It is an empty override dict, so a normal round
is built from `Spawner`'s own field defaults — the tuning reasoning written
across those fields stays the one source of truth and cannot drift from a
table repeating it.

**A default round keeps the bare score key.** Two minutes on normal is the game
as it was before either setting existed, so it still answers to `fruit-ninja`
and the scores already on the deployed device stay visible. Anything else earns
its own table, the way boxing and yoga already do.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aipi5.core.config import GamesConfig, MotionConfig
from aipi5.games.fruit_ninja.fruit import (DEFAULT_DIFFICULTY, DIFFICULTY,
                                           Spawner, spawner_for)
from aipi5.games.manager import DIFFICULTY_OPTIONS, GameError, GameManager
# The camera, ducker and screen fakes already exist for the lifecycle tests,
# and none of this file's subject touches them — reusing them beats a second
# set that can drift from the contract they encode.
from tests.test_game_lifecycle import FakeAudio, FakeCamera, FakeScreen

#: The four fields a preset may move. Everything else about the game is the
#: game, not its difficulty.
TUNED = ("interval", "floor", "ramp_over", "bomb_chance", "bomb_after")


class TestThePresets(unittest.TestCase):

    def test_normal_is_the_spawners_own_defaults(self):
        """Not a copy of them — the same values, by construction. A table that
        repeated the tuning would be a second place for it to drift."""
        self.assertEqual({}, DIFFICULTY["normal"])
        plain, normal = Spawner(), spawner_for("normal")
        for field in TUNED:
            with self.subTest(field=field):
                self.assertEqual(getattr(plain, field), getattr(normal, field))

    def test_easy_is_gentler_and_hard_is_busier(self):
        easy, normal, hard = (spawner_for(n) for n in ("easy", "normal", "hard"))
        # More room between throws, and a floor that never gets as thick.
        self.assertGreater(easy.interval, normal.interval)
        self.assertGreater(normal.interval, hard.interval)
        self.assertGreater(easy.floor, normal.floor)
        self.assertGreater(normal.floor, hard.floor)
        # And the climb to it takes longer on easy, less time on hard.
        self.assertGreater(easy.ramp_over, hard.ramp_over)

    def test_bombs_are_rarer_and_later_on_easy(self):
        easy, hard = spawner_for("easy"), spawner_for("hard")
        self.assertLess(easy.bomb_chance, hard.bomb_chance)
        self.assertGreater(easy.bomb_after, hard.bomb_after)

    def test_nothing_but_the_pace_and_the_bombs_moves(self):
        """A setting that changed the fruit size or the blade would be a
        different game wearing the same name."""
        plain = Spawner()
        for name in DIFFICULTY_OPTIONS:
            moved = {field for field in DIFFICULTY[name]}
            with self.subTest(difficulty=name):
                self.assertTrue(moved <= set(TUNED), f"{name} moves {moved}")
                self.assertEqual(plain.double_after, spawner_for(name).double_after)

    def test_an_unknown_name_plays_the_ordinary_round(self):
        """This value comes out of a JSON file somebody may have edited, and a
        game that will not start because of a typo is worse than one that plays
        its usual round."""
        self.assertEqual(spawner_for("lunar").interval, Spawner().interval)


class ManagerCase(unittest.TestCase):

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = Path(self.folder.name) / "game-settings.json"
        self.manager = self.make()
        self.addCleanup(self.manager.close)

    def make(self, **overrides):
        cfg = GamesConfig(scores=Path(self.folder.name) / "scores.json",
                          settings=self.settings, **overrides)
        return GameManager(cfg, motion_cfg=MotionConfig(),
                           camera=FakeCamera(), audio=FakeAudio(),
                           screen=FakeScreen())

    def saved(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))


class TestChoosingIt(ManagerCase):

    def test_the_default_is_normal(self):
        self.assertEqual("normal", self.manager.difficulty)
        self.assertEqual(DEFAULT_DIFFICULTY, self.manager.difficulty)

    def test_choosing_one_sticks_and_is_offered_back(self):
        answer = self.manager.set_difficulty("hard")
        self.assertEqual("hard", answer["difficulty"])
        self.assertEqual(["easy", "normal", "hard"],
                         [item["name"] for item in answer["difficulties"]])

    def test_it_survives_a_restart(self):
        self.manager.set_difficulty("easy")
        self.manager.close()
        again = self.make()
        self.addCleanup(again.close)
        self.assertEqual("easy", again.difficulty)

    def test_a_name_the_game_does_not_have(self):
        with self.assertRaises(GameError):
            self.manager.set_difficulty("impossible")
        self.assertEqual("normal", self.manager.difficulty)

    def test_it_cannot_change_under_a_round_in_progress(self):
        self.manager.active = "fruit-ninja"
        self.addCleanup(setattr, self.manager, "active", None)
        with self.assertRaises(GameError) as raised:
            self.manager.set_difficulty("hard")
        self.assertIn("leave the current game", str(raised.exception))


class TestTheSettingsFileHoldsAllThree(ManagerCase):
    """The file is rewritten whole, so saving only what changed would drop the
    rest — and the symptom would be a difficulty that quietly reset itself the
    next time somebody moved the round length."""

    def test_setting_one_keeps_the_others(self):
        self.manager.set_difficulty("hard")
        self.manager.set_round_seconds(300)
        self.manager.set_sound(False)
        saved = self.saved()
        self.assertEqual("hard", saved["difficulty"])
        self.assertEqual(300, saved["round_seconds"])
        self.assertIs(False, saved["sound"])

    def test_changing_the_round_length_does_not_reset_the_difficulty(self):
        self.manager.set_difficulty("easy")
        self.manager.set_round_seconds(60)
        self.assertEqual("easy", self.manager.difficulty)
        self.assertEqual("easy", self.saved()["difficulty"])

    def test_a_file_from_before_this_setting_existed_still_loads(self):
        """The deployed device has one with only `round_seconds` in it."""
        self.settings.write_text(json.dumps({"round_seconds": 180}),
                                 encoding="utf-8")
        made = self.make()
        self.addCleanup(made.close)
        self.assertEqual(180, made.round_seconds)
        self.assertEqual("normal", made.difficulty)
        self.assertIs(True, made.sound)

    def test_a_damaged_file_does_not_stop_the_games(self):
        self.settings.write_text("{ this is not json", encoding="utf-8")
        made = self.make()
        self.addCleanup(made.close)
        self.assertEqual("normal", made.difficulty)
        self.assertEqual(120, made.round_seconds)


class TestTheHighScoresSurvive(ManagerCase):
    """The one that would have been noticed by somebody losing a score."""

    def test_a_default_round_keeps_the_key_it_has_always_had(self):
        self.assertEqual("fruit-ninja",
                         self.manager._score_key("fruit-ninja", 120, "normal"))

    def test_a_different_length_keeps_the_key_it_had_before_difficulty(self):
        self.assertEqual("fruit-ninja:60",
                         self.manager._score_key("fruit-ninja", 60, "normal"))

    def test_another_difficulty_earns_its_own_table(self):
        """An easy score and a hard one are not the same achievement and must
        not compete for the same line on the tile."""
        self.assertEqual("fruit-ninja:hard",
                         self.manager._score_key("fruit-ninja", 120, "hard"))
        self.assertEqual("fruit-ninja:60:easy",
                         self.manager._score_key("fruit-ninja", 60, "easy"))

    def test_every_combination_is_distinct(self):
        keys = {self.manager._score_key("fruit-ninja", seconds, level)
                for seconds in (60, 120, 180, 240, 300)
                for level in DIFFICULTY_OPTIONS}
        self.assertEqual(15, len(keys))

    def test_a_score_is_filed_under_what_it_was_played_on(self):
        """Not under the setting selected now: somebody who changes it while
        the score screen is up must not have their run moved."""
        from types import SimpleNamespace

        played = SimpleNamespace(duration=120.0, difficulty="hard")
        self.manager.difficulty = "easy"
        self.assertEqual("fruit-ninja:hard",
                         self.manager._session_score_key("fruit-ninja", played))


class TestSound(ManagerCase):
    """`games.sound` was parsed into the configuration and read by nothing —
    the browser played every event unconditionally. It does something now."""

    def test_it_is_on_unless_the_configuration_says_otherwise(self):
        self.assertIs(True, self.manager.sound)
        quiet = self.make(sound=False)
        self.addCleanup(quiet.close)
        self.assertIs(False, quiet.sound)

    def test_turning_it_off_sticks_across_a_restart(self):
        self.manager.set_sound(False)
        self.manager.close()
        again = self.make()
        self.addCleanup(again.close)
        self.assertIs(False, again.sound)

    def test_the_page_is_told(self):
        self.assertIs(False, self.manager.set_sound(False)["sound"])
        self.assertIs(True, self.manager.set_sound(True)["sound"])

    def test_it_can_be_turned_off_during_a_round(self):
        """Unlike the other two. This one is wanted *because* something is too
        loud right now, and 'finish your game first' answers a question nobody
        asked."""
        self.manager.active = "fruit-ninja"
        self.addCleanup(setattr, self.manager, "active", None)
        self.assertIs(False, self.manager.set_sound(False)["sound"])

    def test_the_page_gates_every_noise_in_one_place(self):
        """`gameAudio()` is the chokepoint every game sound passes through, so
        the setting is honoured by the slices, the bombs and the ultimate
        alike rather than by whichever of them somebody remembered."""
        page = (Path(__file__).resolve().parent.parent / "aipi5" / "ui" / "web"
                / "index.html").read_text(encoding="utf-8")
        body = page[page.index("function gameAudio()"):]
        self.assertIn("if (!gameSoundOn) return null;",
                      body[:body.index("\n}")])


if __name__ == "__main__":
    unittest.main()
