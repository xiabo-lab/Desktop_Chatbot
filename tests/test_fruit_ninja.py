"""The game itself: physics, scoring, lives, bombs and game over.

Sections 24 to 28. Every one of these plays a real session — `tick` takes time
as an argument and never reads a clock, so a two-minute game runs in a
millisecond and runs the same way twice.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aipi5.games.fruit_ninja import fruit as fruit_mod
from aipi5.games.fruit_ninja.fruit import BOMB, KINDS, Fruit, Spawner
from aipi5.games.fruit_ninja.game import (MIN_SLASH_SPEED, STARTING_LIVES,
                                          HighScores, Session, State)
from aipi5.motion.pose_filter import Hand


def hand(name="right_wrist", x=0.5, y=0.5, px=None, py=None, live=True):
    """A `Hand` positioned in normalised camera space, as the filter emits."""
    return Hand(name=name, x=x, y=y,
                previous_x=x if px is None else px,
                previous_y=y if py is None else py,
                confidence=0.9, live=live, last_seen=0.0)


def screen_hand(name, x1, y1, x2, y2):
    """A hand whose slash runs between two *screen pixel* points.

    The session converts normalised coordinates to the 1280x800 panel itself,
    so tests that care about hitting a fruit at a known pixel have to go the
    other way. Doing it here keeps that arithmetic in one place.
    """
    return Hand(name=name,
                previous_x=x1 / fruit_mod.WIDTH, previous_y=y1 / fruit_mod.HEIGHT,
                x=x2 / fruit_mod.WIDTH, y=y2 / fruit_mod.HEIGHT,
                confidence=0.9, live=True, last_seen=0.0)


class TestPhysics(unittest.TestCase):
    """Section 24: delta time, never a frame count."""

    def test_a_fruit_rises_and_falls(self):
        item = Fruit(kind=KINDS[0], x=640, y=860, vx=0, vy=-1200, spin=0)
        heights = []
        for _ in range(120):
            item.advance(1 / 60)
            heights.append(item.y)
        self.assertLess(min(heights), 200)
        self.assertGreater(heights[-1], heights[len(heights) // 2])

    def test_physics_does_not_depend_on_the_frame_rate(self):
        """The same second of simulation, stepped two different ways.

        The reference implementation added a constant to velocity every frame,
        so it played differently on a fast display than a slow one. This is the
        test that would have caught it.
        """
        slow = Fruit(kind=KINDS[0], x=640, y=860, vx=50, vy=-1200, spin=0)
        fast = Fruit(kind=KINDS[0], x=640, y=860, vx=50, vy=-1200, spin=0)

        for _ in range(30):
            slow.advance(1 / 30)
        for _ in range(120):
            fast.advance(1 / 120)

        # Exactly equal, not merely close: `advance` integrates the closed form
        # for constant acceleration, so the step size cancels completely.
        self.assertAlmostEqual(slow.x, fast.x, places=6)
        self.assertAlmostEqual(slow.y, fast.y, places=6)

    def test_previous_position_tracks_the_last_step(self):
        item = Fruit(kind=KINDS[0], x=100, y=500, vx=200, vy=0, spin=0)
        item.advance(0.1)
        self.assertAlmostEqual(item.previous_x, 100)
        self.assertAlmostEqual(item.x, 120)

    def test_a_launched_fruit_is_not_instantly_a_miss(self):
        """Fruit start below the screen; without the check they all count."""
        item = Fruit(kind=KINDS[0], x=640, y=fruit_mod.SPAWN_Y, vx=0,
                     vy=-1200, spin=0)
        self.assertFalse(item.missed)

    def test_a_fruit_that_falls_past_the_bottom_is_a_miss(self):
        item = Fruit(kind=KINDS[0], x=640, y=fruit_mod.GONE_Y + 10, vx=0,
                     vy=400, spin=0)
        self.assertTrue(item.missed)

    def test_a_sliced_fruit_is_never_a_miss(self):
        item = Fruit(kind=KINDS[0], x=640, y=fruit_mod.GONE_Y + 10, vx=0,
                     vy=400, spin=0, sliced=True)
        self.assertFalse(item.missed)

    def test_the_apex_maths_matches_the_simulation(self):
        vy = -1200
        item = Fruit(kind=KINDS[0], x=640, y=800, vx=0, vy=vy, spin=0)
        lowest = 800
        # Time to apex is |vy|/g = 1.2 s, so this has to run past that or it
        # measures how high the fruit got before the test gave up.
        for _ in range(480):
            item.advance(1 / 240)
            lowest = min(lowest, item.y)
        self.assertAlmostEqual(800 - lowest, fruit_mod.apex_height(vy), delta=5)


class TestSlicing(unittest.TestCase):

    def setUp(self):
        self.session = Session()
        self.session.start(now=0.0)
        self.session.spawner.seed(7)

    def place(self, kind=KINDS[0], x=640, y=400):
        item = Fruit(kind=kind, x=x, y=y, vx=0, vy=0, spin=0, id=99)
        self.session.fruit.append(item)
        return item

    def test_a_slash_through_a_fruit_scores(self):
        item = self.place()
        blade = screen_hand("right_wrist", 400, 400, 900, 400)
        self.session.tick(now=0.033, hands=[blade])
        self.assertTrue(item.sliced)
        self.assertEqual(self.session.score, 10)

    def test_a_slash_that_misses_scores_nothing(self):
        item = self.place()
        blade = screen_hand("right_wrist", 100, 50, 300, 60)
        self.session.tick(now=0.033, hands=[blade])
        self.assertFalse(item.sliced)
        self.assertEqual(self.session.score, 0)

    def test_a_resting_hand_does_not_slice(self):
        """Section 15's other half: certain data that is not moving."""
        item = self.place()
        still = screen_hand("right_wrist", 640, 400, 640, 400)
        self.session.tick(now=0.033, hands=[still])
        self.assertFalse(item.sliced)

    def test_a_hand_below_the_slash_speed_does_not_slice(self):
        item = self.place()
        # A movement small enough that its speed is under the threshold.
        creep = Hand(name="right_wrist", previous_x=0.50, previous_y=0.50,
                     x=0.505, y=0.50, confidence=0.9, live=True)
        self.assertLess(creep.speed(0.033), MIN_SLASH_SPEED)
        self.session.tick(now=0.033, hands=[creep])
        self.assertFalse(item.sliced)

    def test_a_dead_hand_never_slices(self):
        item = self.place()
        blade = screen_hand("right_wrist", 400, 400, 900, 400)
        blade.live = False
        self.session.tick(now=0.033, hands=[blade])
        self.assertFalse(item.sliced)

    def test_a_reappearing_hand_never_slices(self):
        item = self.place()
        blade = screen_hand("right_wrist", 400, 400, 900, 400)
        blade.reappeared = True
        self.session.tick(now=0.033, hands=[blade])
        self.assertFalse(item.sliced)

    def test_a_fruit_cannot_be_sliced_twice(self):
        item = self.place()
        blade = screen_hand("right_wrist", 400, 400, 900, 400)
        self.session.tick(now=0.033, hands=[blade])
        self.session.tick(now=0.066, hands=[blade])
        self.assertEqual(self.session.score, 10)
        self.assertEqual(self.session.sliced_total, 1)

    def test_one_slash_can_take_several_fruit(self):
        for x in (400, 640, 900):
            self.place(x=x, y=400)
        blade = screen_hand("right_wrist", 200, 400, 1100, 400)
        self.session.tick(now=0.033, hands=[blade])
        self.assertEqual(self.session.sliced_total, 3)

    def test_both_hands_can_slice(self):
        left = self.place(x=300, y=400)
        right = self.place(x=980, y=400)
        blades = [screen_hand("left_wrist", 150, 400, 450, 400),
                  screen_hand("right_wrist", 830, 400, 1130, 400)]
        self.session.tick(now=0.033, hands=blades)
        self.assertTrue(left.sliced)
        self.assertTrue(right.sliced)

    def test_a_streak_earns_a_bonus(self):
        for _ in range(4):
            self.place()
            self.session.tick(now=self.session._last_tick + 0.033,
                              hands=[screen_hand("right_wrist", 400, 400, 900, 400)])
        # 10, 10, 11, 12 — the bonus starts on the third of a run.
        self.assertEqual(self.session.score, 43)
        self.assertEqual(self.session.best_streak, 4)


class TestLivesAndGameOver(unittest.TestCase):
    """Sections 27 and 28."""

    def setUp(self):
        self.session = Session()
        self.session.start(now=0.0)

    def drop(self, kind=KINDS[0]):
        """A fruit already past the bottom, on its way down."""
        self.session.fruit.append(
            Fruit(kind=kind, x=640, y=fruit_mod.GONE_Y + 5, vx=0, vy=500,
                  spin=0))

    def test_three_lives_to_begin_with(self):
        self.assertEqual(self.session.lives, STARTING_LIVES)

    def test_a_missed_fruit_costs_a_life(self):
        self.drop()
        self.session.tick(now=0.033, hands=[])
        self.assertEqual(self.session.lives, STARTING_LIVES - 1)
        self.assertEqual(self.session.missed_total, 1)

    def test_a_missed_bomb_costs_nothing(self):
        """Leaving a bomb alone is the correct play and must not be punished."""
        self.drop(kind=BOMB)
        self.session.tick(now=0.033, hands=[])
        self.assertEqual(self.session.lives, STARTING_LIVES)
        self.assertEqual(self.session.missed_total, 0)

    def test_a_sliced_bomb_costs_a_life(self):
        self.session.fruit.append(
            Fruit(kind=BOMB, x=640, y=400, vx=0, vy=0, spin=0))
        self.session.tick(now=0.033,
                          hands=[screen_hand("right_wrist", 400, 400, 900, 400)])
        self.assertEqual(self.session.lives, STARTING_LIVES - 1)
        self.assertEqual(self.session.bombs_hit, 1)

    def test_a_sliced_bomb_does_not_subtract_score(self):
        self.session.score = 50
        self.session.fruit.append(
            Fruit(kind=BOMB, x=640, y=400, vx=0, vy=0, spin=0))
        self.session.tick(now=0.033,
                          hands=[screen_hand("right_wrist", 400, 400, 900, 400)])
        self.assertEqual(self.session.score, 50)

    def test_a_miss_breaks_the_streak(self):
        self.session.streak = 5
        self.drop()
        self.session.tick(now=0.033, hands=[])
        self.assertEqual(self.session.streak, 0)

    def test_no_lives_is_game_over(self):
        now = 0.0
        for _ in range(STARTING_LIVES):
            self.drop()
            now += 0.033
            self.session.tick(now=now, hands=[])
        self.assertEqual(self.session.state, State.OVER)

    def test_a_finished_game_stops_simulating(self):
        self.session.finish(now=1.0)
        self.session.fruit.append(
            Fruit(kind=KINDS[0], x=640, y=400, vx=0, vy=100, spin=0))
        self.session.tick(now=2.0, hands=[])
        self.assertEqual(self.session.fruit[0].y, 400)

    def test_the_best_score_is_kept_at_game_over(self):
        self.session.score = 120
        self.session.finish(now=1.0)
        self.assertEqual(self.session.best, 120)

    def test_play_again_resets_everything_but_the_best(self):
        self.session.score = 120
        self.session.finish(now=1.0)
        self.session.start(now=2.0)
        self.assertEqual(self.session.score, 0)
        self.assertEqual(self.session.lives, STARTING_LIVES)
        self.assertEqual(self.session.best, 120)
        self.assertEqual(self.session.state, State.PLAYING)


class TestPause(unittest.TestCase):
    """Section 29: physics, spawning and scoring all stop."""

    def setUp(self):
        self.session = Session()
        self.session.start(now=0.0)

    def test_pausing_freezes_the_fruit(self):
        item = Fruit(kind=KINDS[0], x=640, y=400, vx=100, vy=0, spin=0)
        self.session.fruit.append(item)
        self.session.pause(now=1.0)
        self.session.tick(now=2.0, hands=[])
        self.assertEqual(item.x, 640)

    def test_pausing_stops_spawning(self):
        self.session.pause(now=1.0)
        self.session.tick(now=10.0, hands=[])
        self.assertEqual(self.session.fruit, [])

    def test_pausing_stops_scoring(self):
        self.session.fruit.append(
            Fruit(kind=KINDS[0], x=640, y=400, vx=0, vy=0, spin=0))
        self.session.pause(now=1.0)
        self.session.tick(now=1.033,
                          hands=[screen_hand("right_wrist", 400, 400, 900, 400)])
        self.assertEqual(self.session.score, 0)

    def test_resuming_does_not_fast_forward(self):
        """A long pause must not teleport everything on the first step."""
        item = Fruit(kind=KINDS[0], x=640, y=400, vx=100, vy=0, spin=0)
        self.session.fruit.append(item)
        self.session.pause(now=1.0)
        self.session.resume(now=120.0)
        self.session.tick(now=120.033, hands=[])
        self.assertAlmostEqual(item.x, 640 + 100 * 0.033, places=3)

    def test_a_huge_step_is_clamped_even_without_a_pause(self):
        item = Fruit(kind=KINDS[0], x=640, y=400, vx=100, vy=0, spin=0)
        self.session.fruit.append(item)
        self.session.tick(now=60.0, hands=[])
        self.assertLess(item.x, 660)

    def test_resume_only_works_from_paused(self):
        self.assertFalse(self.session.resume(now=1.0))

    def test_pause_only_works_while_playing(self):
        self.session.finish(now=1.0)
        self.assertFalse(self.session.pause(now=2.0))


class TestSpawner(unittest.TestCase):

    def test_nothing_is_thrown_immediately(self):
        spawner = Spawner()
        spawner.seed(1)
        self.assertEqual(spawner.due(now=0.0, score=0), [])

    def test_fruit_arrive_after_the_opening_delay(self):
        spawner = Spawner()
        spawner.seed(1)
        spawner.due(now=0.0, score=0)
        self.assertTrue(spawner.due(now=1.0, score=0))

    def test_it_speeds_up_with_the_score(self):
        easy, hard = Spawner(), Spawner()
        easy.seed(2)
        hard.seed(2)
        easy.due(now=0.0, score=0)
        hard.due(now=0.0, score=0)
        easy.due(now=1.0, score=0)
        hard.due(now=1.0, score=5000)
        self.assertGreater(easy._next_at, hard._next_at)

    def test_no_bombs_early_on(self):
        spawner = Spawner()
        spawner.seed(3)
        now = 0.0
        thrown = []
        for _ in range(200):
            now += 0.2
            thrown.extend(spawner.due(now=now, score=0))
        self.assertTrue(thrown)
        self.assertFalse(any(item.is_bomb for item in thrown))

    def test_bombs_appear_once_the_player_is_going_well(self):
        spawner = Spawner()
        spawner.seed(3)
        now = 0.0
        thrown = []
        for _ in range(400):
            now += 0.2
            thrown.extend(spawner.due(now=now, score=800))
        self.assertTrue(any(item.is_bomb for item in thrown))

    def test_fruit_are_launched_upwards_from_below_the_screen(self):
        spawner = Spawner()
        spawner.seed(4)
        spawner.due(now=0.0, score=0)
        thrown = spawner.due(now=1.0, score=0)
        for item in thrown:
            self.assertLess(item.vy, 0)
            self.assertGreater(item.y, fruit_mod.HEIGHT)

    def test_edge_spawns_are_aimed_inwards(self):
        spawner = Spawner()
        spawner.seed(11)
        now = 0.0
        for _ in range(60):
            now += 0.5
            for item in spawner.due(now=now, score=0):
                if item.x < fruit_mod.WIDTH * 0.25:
                    self.assertGreaterEqual(item.vx, 0)
                elif item.x > fruit_mod.WIDTH * 0.75:
                    self.assertLessEqual(item.vx, 0)

    def test_a_seeded_spawner_is_reproducible(self):
        def run():
            spawner = Spawner()
            spawner.seed(99)
            now, kinds = 0.0, []
            for _ in range(40):
                now += 0.5
                kinds.extend(i.kind.name for i in spawner.due(now=now, score=300))
            return kinds

        self.assertEqual(run(), run())


class TestHighScores(unittest.TestCase):
    """Section 28: local, and never fatal."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "scores.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_a_missing_file_is_no_score_rather_than_an_error(self):
        self.assertEqual(HighScores(self.path).best("fruit-ninja"), 0)

    def test_a_record_is_kept(self):
        scores = HighScores(self.path)
        self.assertTrue(scores.record("fruit-ninja", 250))
        self.assertEqual(HighScores(self.path).best("fruit-ninja"), 250)

    def test_a_lesser_score_does_not_overwrite(self):
        scores = HighScores(self.path)
        scores.record("fruit-ninja", 250)
        self.assertFalse(scores.record("fruit-ninja", 100))
        self.assertEqual(scores.best("fruit-ninja"), 250)

    def test_games_keep_separate_records(self):
        scores = HighScores(self.path)
        scores.record("fruit-ninja", 250)
        scores.record("boxing", 40)
        self.assertEqual(scores.best("fruit-ninja"), 250)
        self.assertEqual(scores.best("boxing"), 40)

    def test_a_corrupt_file_is_not_fatal(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(HighScores(self.path).best("fruit-ninja"), 0)

    def test_an_unwritable_location_is_not_fatal(self):
        scores = HighScores(Path(self._dir.name) / "nope" / "x" / "scores.json")
        scores.record("fruit-ninja", 10)
        self.assertEqual(scores.best("fruit-ninja"), 10)


if __name__ == "__main__":
    unittest.main()
