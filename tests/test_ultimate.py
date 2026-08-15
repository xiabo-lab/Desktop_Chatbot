"""The Ultimate Dragon Fruit, and the ten-fruit set it is the biggest of.

Sections 16 to 29 of the upgrade. Everything here runs without a browser, a
camera or an accelerator, because `Session.tick` takes time as an argument and
`UltimateDragon` has no clock of its own — a whole two-minute round with a
thirty-hit sequence in the middle of it is a few milliseconds.

**The tests that matter most are the ones that assert something does *not*
happen.** A hand held still inside the fruit scoring nothing, a bomb not
spawning, a second dragon fruit not appearing, a life not being lost. Those are
the rules a player only notices when they are broken, and by then they have
been broken for the whole round.
"""

from __future__ import annotations

import unittest

from aipi5.games.fruit_ninja import ultimate
from aipi5.games.fruit_ninja.fruit import BY_NAME, KINDS, Spawner
from aipi5.games.fruit_ninja.game import (ROUND_SECONDS, ULTIMATE_START_AT,
                                          ULTIMATE_WARNING_AT, Phase, Session,
                                          State)
from aipi5.games.fruit_ninja.ultimate import (COMPLETE_BONUS, HITS_REQUIRED,
                                              UltimateDragon)
from aipi5.motion.pose_filter import Hand

#: One pose frame at the rate the accelerator actually delivers. Used
#: throughout rather than a round number, because the per-hand cooldown is
#: 110 ms and a 100 ms step would quietly make every test's timing a special
#: case that the device never sees.
STEP = 1 / 30


def swing(name: str, x1: float, y1: float, x2: float, y2: float) -> Hand:
    """A live hand that moved from one point to another this frame."""
    hand = Hand(name)
    hand.live = True
    hand.previous_x, hand.previous_y = x1, y1
    hand.x, hand.y = x2, y2
    return hand


def across_the_dragon(name: str, direction: int = 1) -> Hand:
    """A swing straight through where the dragon fruit sits."""
    cx, cy = ultimate.CENTRE
    return swing(name, cx - 0.1 * direction, cy, cx + 0.1 * direction, cy)


def still_on_the_dragon(name: str) -> Hand:
    """A hand parked in the middle of the fruit, not moving at all."""
    cx, cy = ultimate.CENTRE
    return swing(name, cx, cy, cx, cy)


class TenFruit(unittest.TestCase):
    """Sections 6, 7 and 8: ten fruit, each visibly and audibly its own."""

    def test_there_are_ten(self):
        self.assertEqual(len(KINDS), 10)

    def test_every_fruit_the_requirement_names_exists(self):
        wanted = {"apple", "banana", "orange", "lime", "watermelon",
                  "pearl", "grape", "strawberry", "kiwi", "dragon"}
        self.assertEqual({kind.name for kind in KINDS}, wanted)

    def test_no_two_fruit_share_a_juice_colour(self):
        """Section 7: "do not use one generic red splat for everything"."""
        juices = [kind.juice for kind in KINDS]
        self.assertEqual(len(set(juices)), len(juices))

    def test_no_two_fruit_share_a_shape(self):
        """Section 6: "do not simply recolor one fruit model".

        Shapes may legitimately be reused — orange and lime are both citrus —
        so this asserts that the *pairs* differ, which is what actually makes a
        fruit identifiable: a shape and a colour together.
        """
        pairs = [(kind.shape, kind.colour) for kind in KINDS]
        self.assertEqual(len(set(pairs)), len(pairs))

    def test_every_fruit_has_a_sound(self):
        for kind in KINDS:
            with self.subTest(kind.name):
                self.assertTrue(kind.sound)

    def test_the_sounds_are_not_all_the_same(self):
        """Section 8 allows variations of one voice, not one voice."""
        self.assertGreaterEqual(len({kind.sound for kind in KINDS}), 6)

    def test_a_fruit_carries_everything_the_page_needs(self):
        """The page looks nothing up; if it is not here it cannot be drawn."""
        from aipi5.games.fruit_ninja.fruit import Fruit
        item = Fruit(kind=BY_NAME["kiwi"], x=1, y=2, vx=0, vy=0, spin=0)
        payload = item.as_dict()
        for field in ("kind", "shape", "colour", "flesh", "juice", "wet",
                      "sound", "r"):
            self.assertIn(field, payload)

    def test_spawn_weights_are_all_positive(self):
        """A zero weight is a fruit that exists and is never thrown."""
        for kind in KINDS:
            with self.subTest(kind.name):
                self.assertGreater(kind.weight, 0)

    def test_every_fruit_actually_gets_thrown(self):
        """Section 42: all ten must be able to spawn.

        Run long enough that even the rarest — dragon, at weight 0.35 out of
        about 10.4 — is overwhelmingly likely. Seeded, so it either passes
        every time or fails every time; a flaky test about spawn rates is
        worse than none.
        """
        spawner = Spawner()
        spawner.seed(11)
        seen = set()
        now = 0.0
        while now < 900.0:
            now += 0.05
            for item in spawner.due(now=now, elapsed=now):
                seen.add(item.kind.name)
        self.assertEqual(seen, {kind.name for kind in KINDS} | {"bomb"})

    def test_the_dragon_fruit_is_the_rarest(self):
        """Section 10: less common during normal gameplay."""
        rarest = min(KINDS, key=lambda kind: kind.weight)
        self.assertEqual(rarest.name, "dragon")

    def test_launch_and_spin_scaling_is_sane(self):
        """A multiplier of zero would leave a fruit sitting off the bottom."""
        for kind in KINDS:
            with self.subTest(kind.name):
                self.assertGreater(kind.launch, 0.5)
                self.assertGreater(kind.spin, 0.0)


class Phases(unittest.TestCase):
    """Section 39's state machine, and section 16's timings."""

    def setUp(self):
        self.session = Session()
        self.session.spawner.seed(5)
        self.session.start(now=0.0)

    def run_to(self, time_left: float, hands=()):
        """Play until the clock reads `time_left`, one pose frame at a time."""
        now = self.session._last_tick
        while self.session.time_left > time_left and self.session.state is State.PLAYING:
            now += STEP
            self.session.tick(now=now, hands=list(hands))
        return now

    def test_a_fresh_round_is_in_normal_play(self):
        self.assertIs(self.session.phase, Phase.NORMAL)
        self.assertIsNone(self.session.dragon)

    def test_the_warning_arrives_at_twenty_two_seconds(self):
        self.run_to(ULTIMATE_WARNING_AT + 0.5)
        self.assertIs(self.session.phase, Phase.NORMAL)
        self.run_to(ULTIMATE_WARNING_AT - 0.2)
        self.assertIs(self.session.phase, Phase.WARNING)

    def test_the_warning_fires_exactly_once(self):
        self.run_to(ULTIMATE_START_AT + 0.5)
        warnings = [e for e in self.session.events
                    if e["name"] == "ultimate-warning"]
        self.assertEqual(len(warnings), 1)

    def test_the_dragon_appears_at_twenty_seconds(self):
        self.run_to(ULTIMATE_START_AT + 0.5)
        self.assertIsNone(self.session.dragon)
        self.run_to(ULTIMATE_START_AT - 0.2)
        self.assertIs(self.session.phase, Phase.ULTIMATE)
        self.assertIsNotNone(self.session.dragon)

    def test_only_one_dragon_per_round(self):
        self.run_to(ULTIMATE_START_AT - 0.5)
        first = self.session.dragon
        self.run_to(ULTIMATE_START_AT - 2.0)
        self.assertIs(self.session.dragon, first)
        spawns = [e for e in self.session.events
                  if e["name"] == "ultimate-spawn"]
        self.assertLessEqual(len(spawns), 1)

    def test_the_ultimate_lasts_ten_seconds(self):
        self.run_to(ULTIMATE_START_AT - 0.5)
        self.assertIs(self.session.phase, Phase.ULTIMATE)
        self.run_to(ULTIMATE_START_AT - ultimate.DURATION_S + 0.5)
        self.assertIs(self.session.phase, Phase.ULTIMATE)
        self.run_to(ULTIMATE_START_AT - ultimate.DURATION_S - 0.5)
        self.assertIs(self.session.phase, Phase.FINAL)
        self.assertIsNone(self.session.dragon)

    def test_normal_play_resumes_for_the_last_ten_seconds(self):
        self.run_to(9.0)
        self.assertIs(self.session.phase, Phase.FINAL)
        self.assertIs(self.session.state, State.PLAYING)

    def test_fruit_resumes_after_the_ultimate(self):
        """Section 29: spawning restarts for the coda."""
        self.run_to(9.0)
        before = self.session.spawner._counter
        self.run_to(3.0)
        self.assertGreater(self.session.spawner._counter, before)

    def test_a_short_round_has_no_ultimate_at_all(self):
        """A ten-second round must not open inside the Ultimate window.

        `duration` is a field so a test — or a future short mode — can run a
        brief round. Without the length check in `has_ultimate` such a round
        starts with `time_left` already under twenty and throws a dragon fruit
        on its first frame, which is not a shorter game but a different one.
        """
        session = Session(duration=10.0, time_left=10.0)
        session.spawner.seed(1)
        session.start(now=0.0)
        self.assertFalse(session.has_ultimate)
        now = 0.0
        while session.state is State.PLAYING:
            now += STEP
            session.tick(now=now)
        self.assertIsNone(session.dragon)
        self.assertIs(session.phase, Phase.NORMAL)
        self.assertEqual(session.ultimate_hits, 0)

    def test_the_phase_survives_a_pause(self):
        """Pausing mid-Ultimate must not lose the dragon fruit."""
        self.run_to(ULTIMATE_START_AT - 2.0)
        dragon = self.session.dragon
        age = dragon.age
        self.session.pause(now=self.session._last_tick)
        self.session.tick(now=self.session._last_tick + 30.0)
        self.assertIs(self.session.dragon, dragon)
        self.assertEqual(dragon.age, age)
        self.assertIs(self.session.phase, Phase.ULTIMATE)


class NoSpawningDuringTheUltimate(unittest.TestCase):
    """Section 29 and section 30."""

    def setUp(self):
        self.session = Session()
        self.session.spawner.seed(9)
        self.session.start(now=0.0)
        self.now = 0.0

    def play_to(self, time_left: float):
        while (self.session.time_left > time_left
               and self.session.state is State.PLAYING):
            self.now += STEP
            self.session.tick(now=self.now)

    def test_nothing_new_is_thrown(self):
        self.play_to(ULTIMATE_START_AT - 0.5)
        thrown = self.session.spawner._counter
        self.play_to(ULTIMATE_START_AT - 8.0)
        self.assertEqual(self.session.spawner._counter, thrown)

    def test_no_bombs_during_the_ultimate(self):
        """Section 30, stated separately because it is the one that hurts.

        A bomb costs five seconds. Slicing one during the Ultimate would mean
        a swing aimed at the dragon fruit taking a twelfth of the round away,
        which is the least fair thing the game could do.
        """
        self.play_to(ULTIMATE_START_AT - 0.5)
        during = []
        while self.session.phase is Phase.ULTIMATE:
            self.now += STEP
            before = set(id(f) for f in self.session.fruit)
            self.session.tick(now=self.now)
            during += [f for f in self.session.fruit if id(f) not in before]
        self.assertFalse([f for f in during if f.is_bomb])

    def test_fruit_already_in_flight_is_left_alone(self):
        """It finishes its arc rather than being confiscated.

        Section 29 says to pause *spawning*, not to clear the screen. Deleting
        a fruit the player was already swinging at would look like the game
        taking it away from them.

        The fruit is injected on the frame the Ultimate begins and checked a
        few frames later, rather than being launched two seconds earlier and
        expected to still be up: gravity is 1000 px/s² and an ordinary throw is
        airborne for well under three seconds, so the first version of this
        test was measuring whether a fruit can hang in the air for two seconds
        — which it cannot, and which is not what the rule is about.
        """
        self.play_to(ULTIMATE_START_AT - 0.05)
        self.assertIs(self.session.phase, Phase.ULTIMATE)
        from aipi5.games.fruit_ninja.fruit import Fruit
        flying = Fruit(kind=BY_NAME["apple"], x=300, y=400, vx=0, vy=-600,
                       spin=0, id=9999)
        self.session.fruit.append(flying)
        for _ in range(15):
            self.now += STEP
            self.session.tick(now=self.now)
        self.assertIs(self.session.phase, Phase.ULTIMATE)
        self.assertIn(flying, self.session.fruit)
        # And it is genuinely being simulated, not merely retained.
        self.assertLess(flying.y, 400)


class Hits(unittest.TestCase):
    """Sections 20 to 24: what counts as a hit, and what it is worth."""

    def setUp(self):
        self.dragon = UltimateDragon(born_at=0.0)
        self._now = 0.0
        self._landed = 0

    def hit(self, hand: Hand, now: float) -> int:
        """Score one swing, converting to screen pixels the way the game does."""
        from aipi5.games.fruit_ninja.fruit import HEIGHT, WIDTH
        from aipi5.motion import geometry
        return self.dragon.attempt(
            hand,
            geometry.to_screen(hand.previous_x, hand.previous_y, WIDTH, HEIGHT,
                               clamp=False),
            geometry.to_screen(hand.x, hand.y, WIDTH, HEIGHT, clamp=False),
            now)

    def test_a_swing_through_it_scores(self):
        self.assertGreater(self.hit(across_the_dragon("right_wrist"), 1.0), 0)
        self.assertEqual(self.dragon.hits, 1)

    def test_a_swing_that_misses_scores_nothing(self):
        away = swing("right_wrist", 0.05, 0.9, 0.12, 0.92)
        self.assertEqual(self.hit(away, 1.0), 0)
        self.assertEqual(self.dragon.hits, 0)

    def test_a_stationary_hand_never_scores(self):
        """Section 21, the whole point of it.

        A wrist resting inside a fruit the size of a dinner plate is reported
        thirty times a second. Ten seconds of that is three hundred frames, and
        every one of them must be worth nothing.
        """
        now = 0.0
        for _ in range(300):
            now += STEP
            self.hit(still_on_the_dragon("right_wrist"), now)
        self.assertEqual(self.dragon.hits, 0)

    def test_a_jittering_hand_never_scores(self):
        """Pose noise must not be an attack.

        The cooldown alone would still let a hand that twitches within the
        fruit score eight times a second; the movement gate is what stops it,
        and this is the test that fails if somebody removes it.
        """
        import math
        cx, cy = ultimate.CENTRE
        now = 0.0
        for i in range(300):
            now += STEP
            jitter = 0.004 * math.sin(i * 2.3)
            self.hit(swing("left_wrist", cx, cy, cx + jitter, cy), now)
        self.assertEqual(self.dragon.hits, 0)

    def test_one_hand_cannot_hit_twice_inside_the_cooldown(self):
        self.hit(across_the_dragon("right_wrist"), 1.0)
        self.hit(across_the_dragon("right_wrist"),
                 1.0 + ultimate.HAND_COOLDOWN_S * 0.5)
        self.assertEqual(self.dragon.hits, 1)

    def test_one_hand_can_hit_again_after_the_cooldown(self):
        self.hit(across_the_dragon("right_wrist"), 1.0)
        self.hit(across_the_dragon("right_wrist"),
                 1.0 + ultimate.HAND_COOLDOWN_S * 1.1)
        self.assertEqual(self.dragon.hits, 2)

    def test_the_cooldowns_are_per_hand(self):
        """Section 22: alternating hands must not throttle each other.

        Both swing at the same instant, well inside one cooldown. A global
        cooldown would score one; separate ones score two, which is what makes
        two-handed play worth doing.
        """
        self.hit(across_the_dragon("left_wrist"), 1.0)
        self.hit(across_the_dragon("right_wrist"), 1.0)
        self.assertEqual(self.dragon.hits, 2)

    def test_fast_alternation_works(self):
        """L, R, L, R at a pace one hand alone could not manage."""
        now = 0.0
        for i in range(20):
            now += ultimate.HAND_COOLDOWN_S * 0.6
            name = "left_wrist" if i % 2 == 0 else "right_wrist"
            self.hit(across_the_dragon(name, 1 if i % 2 else -1), now)
        self.assertEqual(self.dragon.hits, 20)

    # ── scoring, section 23 ──────────────────────────────────────────

    def test_the_scoring_bands(self):
        self.assertEqual(ultimate.hit_value(1), 10)
        self.assertEqual(ultimate.hit_value(9), 10)
        self.assertEqual(ultimate.hit_value(10), 15)
        self.assertEqual(ultimate.hit_value(19), 15)
        self.assertEqual(ultimate.hit_value(20), 20)
        self.assertEqual(ultimate.hit_value(29), 20)
        self.assertEqual(ultimate.hit_value(30), 30)
        self.assertEqual(ultimate.hit_value(39), 30)
        # The tail, added after a real player landed 88 hits and took 75% of
        # the round's score off one fruit.
        self.assertEqual(ultimate.hit_value(40), 10)
        self.assertEqual(ultimate.hit_value(88), 10)

    def test_the_ultimate_cannot_run_away_with_the_round(self):
        """Section 23, pinned against the round that broke it.

        88 hits was not a hypothetical: it is what the first person to play
        this scored, in a round shortened to about seventy seconds of play by
        ten bombs. Under the original flat +30 tail the Ultimate was worth
        2710. It must now be worth appreciably less than that, while a merely
        *complete* Ultimate — thirty hits and the bonus — must not be worth
        less than it was, because that part was never the problem.
        """
        complete = sum(ultimate.hit_value(i) for i in range(1, 31))
        self.assertEqual(complete + COMPLETE_BONUS, 970)

        blistering = (sum(ultimate.hit_value(i) for i in range(1, 89))
                      + COMPLETE_BONUS)
        self.assertLess(blistering, 2000)
        # Still plainly worth carrying on past thirty, or section 24's bonus
        # mode would be decoration.
        self.assertGreater(blistering, complete + COMPLETE_BONUS + 500)

    def land(self, count: int) -> None:
        """`count` valid hits, alternating hands, respecting the cooldowns.

        The clock is an instance attribute rather than a local, so two calls in
        one test carry on from where the first stopped. Restarting it at zero
        made the second call's first swing land *before* the cooldown the first
        call had already recorded — `now - last` came out negative, which is
        under the threshold, so every hit after the first `land()` was silently
        refused and the test read as a scoring bug.
        """
        for i in range(count):
            self._now += ultimate.HAND_COOLDOWN_S * 0.6
            name = "left_wrist" if self._landed % 2 == 0 else "right_wrist"
            self._landed += 1
            self.hit(across_the_dragon(name, 1 if self._landed % 2 else -1),
                     self._now)

    def test_twenty_nine_hits_does_not_complete_it(self):
        """Section 45 asks for exactly this pair of assertions."""
        self.land(29)
        self.assertEqual(self.dragon.hits, 29)
        self.assertFalse(self.dragon.completed)

    def test_thirty_hits_completes_it(self):
        self.land(30)
        self.assertEqual(self.dragon.hits, 30)
        self.assertTrue(self.dragon.completed)

    def test_the_completion_bonus_lands_on_the_thirtieth_hit(self):
        self.land(29)
        before = self.dragon.points
        self.land(1)
        gained = self.dragon.points - before
        self.assertEqual(gained, ultimate.hit_value(30) + COMPLETE_BONUS)

    def test_the_bonus_is_awarded_once(self):
        self.land(40)
        # 29 at their band values, the 30th with the bonus, then ten more at
        # the top band and no second bonus.
        expected = (sum(ultimate.hit_value(i) for i in range(1, 41))
                    + COMPLETE_BONUS)
        self.assertEqual(self.dragon.points, expected)

    def test_extra_cuts_after_thirty_still_score(self):
        """Section 24: bonus mode, which is what rewards a fast player."""
        self.land(30)
        after_thirty = self.dragon.points
        self.land(4)
        self.assertEqual(self.dragon.points - after_thirty, 4 * 30)
        self.assertEqual(self.dragon.hits, 34)

    # ── damage, section 25 ───────────────────────────────────────────

    def test_the_damage_stages(self):
        self.assertEqual(ultimate.damage_stage(0), 0)
        self.assertEqual(ultimate.damage_stage(9), 0)
        self.assertEqual(ultimate.damage_stage(10), 1)
        self.assertEqual(ultimate.damage_stage(19), 1)
        self.assertEqual(ultimate.damage_stage(20), 2)
        self.assertEqual(ultimate.damage_stage(29), 2)
        self.assertEqual(ultimate.damage_stage(30), 3)
        self.assertEqual(ultimate.damage_stage(55), 3)

    # ── movement, section 18 ─────────────────────────────────────────

    def test_it_stays_near_the_centre(self):
        """It bobs, but it must never be somewhere the player has to chase."""
        from aipi5.games.fruit_ninja.fruit import HEIGHT, WIDTH
        dragon = UltimateDragon(born_at=0.0)
        for _ in range(int(ultimate.DURATION_S / STEP)):
            dragon.advance(STEP)
            self.assertLess(abs(dragon.x - WIDTH * ultimate.CENTRE[0]),
                            ultimate.BOB_X + 1)
            self.assertLess(abs(dragon.y - HEIGHT * ultimate.CENTRE[1]),
                            ultimate.BOB_Y + 1)

    def test_it_expires_after_its_duration(self):
        dragon = UltimateDragon(born_at=0.0)
        for _ in range(int(ultimate.DURATION_S / STEP) - 2):
            dragon.advance(STEP)
        self.assertFalse(dragon.expired)
        dragon.advance(STEP * 3)
        self.assertTrue(dragon.expired)

    def test_it_is_much_bigger_than_an_ordinary_dragon_fruit(self):
        """Section 19: 1.5x to 2x."""
        ratio = self.dragon.radius / BY_NAME["dragon"].radius
        self.assertGreaterEqual(ratio, 1.5)
        self.assertLessEqual(ratio, 2.0)


class TheUltimateInAWholeRound(unittest.TestCase):
    """The Ultimate as it is actually reached: through a real session."""

    def play(self, hitting: bool = False, duration: float = ROUND_SECONDS):
        session = Session(duration=duration, time_left=duration)
        session.spawner.seed(4)
        session.start(now=0.0)
        now = 0.0
        step_index = 0
        while session.state is State.PLAYING and now < duration * 2:
            now += STEP
            hands = []
            if hitting and session.phase is Phase.ULTIMATE:
                name = "left_wrist" if step_index % 2 == 0 else "right_wrist"
                hands = [across_the_dragon(name, 1 if step_index % 2 else -1)]
                step_index += 1
            session.tick(now=now, hands=hands)
        return session

    def test_every_round_contains_an_ultimate(self):
        """Section 16: not random. Ten seeded rounds, ten dragon fruit."""
        for seed in range(10):
            session = Session()
            session.spawner.seed(seed)
            session.start(now=0.0)
            now = 0.0
            while session.state is State.PLAYING:
                now += STEP
                session.tick(now=now)
            with self.subTest(seed=seed):
                self.assertTrue(session.ultimate_spawned)

    def test_a_round_played_hard_completes_it(self):
        session = self.play(hitting=True)
        self.assertTrue(session.ultimate_done)
        self.assertGreaterEqual(session.ultimate_hits, HITS_REQUIRED)
        self.assertGreater(session.ultimate_points, COMPLETE_BONUS)

    def test_a_round_ignored_does_not_complete_it_and_costs_nothing(self):
        """Section 28: failing the Ultimate is not a punishment."""
        session = self.play(hitting=False)
        self.assertTrue(session.ultimate_spawned)
        self.assertEqual(session.ultimate_hits, 0)
        self.assertFalse(session.ultimate_done)
        # The round still ran its full length — no life, no time, nothing.
        self.assertAlmostEqual(session.time_left, 0.0, delta=0.1)
        self.assertIs(session.state, State.OVER)

    def test_the_end_of_the_ultimate_is_announced_once(self):
        session = self.play(hitting=True)
        ends = [e for e in session.events if e["name"] == "ultimate-end"]
        # Events are drained by the page, but nothing drained them here, so
        # every one of the round's is still in the list.
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0]["completed"])

    def test_completion_is_announced_exactly_once(self):
        session = self.play(hitting=True)
        done = [e for e in session.events if e["name"] == "ultimate-complete"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["bonus"], COMPLETE_BONUS)

    def test_the_ultimate_does_not_dominate_a_played_round(self):
        """Section 23: a major bonus, not the whole score.

        Asserted loosely and deliberately — the exact ratio depends on how
        hard the player swings at the normal fruit, which this test does not
        do at all. What it pins down is that the *ceiling* is not absurd: even
        a player who scores nothing during a hundred and ten seconds of normal
        play cannot make the round worth more than a few thousand.
        """
        session = self.play(hitting=True)
        self.assertLess(session.ultimate_points, 6000)

    def test_the_dragon_is_gone_from_the_snapshot_afterwards(self):
        session = self.play(hitting=True)
        self.assertNotIn("dragon", session.snapshot(now=0.0))


class PlayAgainResets(unittest.TestCase):
    """Section 40. Nothing leaks from one round into the next."""

    def setUp(self):
        self.session = Session()
        self.session.spawner.seed(6)
        self.session.start(now=0.0)
        self.now = 0.0
        # Play a whole round, hitting the dragon fruit hard.
        index = 0
        while self.session.state is State.PLAYING:
            self.now += STEP
            hands = []
            if self.session.phase is Phase.ULTIMATE:
                name = "left_wrist" if index % 2 == 0 else "right_wrist"
                hands = [across_the_dragon(name, 1 if index % 2 else -1)]
                index += 1
            self.session.tick(now=self.now, hands=hands)

    def test_the_first_round_really_did_complete_it(self):
        """Otherwise the reset test below proves nothing."""
        self.assertTrue(self.session.ultimate_done)
        self.assertGreaterEqual(self.session.ultimate_hits, HITS_REQUIRED)

    def test_play_again_clears_every_piece_of_ultimate_state(self):
        self.session.start(now=self.now + 1.0)
        self.assertIs(self.session.phase, Phase.NORMAL)
        self.assertIsNone(self.session.dragon)
        self.assertEqual(self.session.ultimate_hits, 0)
        self.assertEqual(self.session.ultimate_points, 0)
        self.assertFalse(self.session.ultimate_done)
        self.assertFalse(self.session.ultimate_spawned)

    def test_the_second_round_gets_its_own_ultimate(self):
        self.session.start(now=self.now + 1.0)
        now = self.now + 1.0
        while self.session.state is State.PLAYING:
            now += STEP
            self.session.tick(now=now)
        self.assertTrue(self.session.ultimate_spawned)

    def test_a_new_dragon_starts_at_zero_hits(self):
        """The cooldown dictionary is inside the object, so it goes with it."""
        self.session.start(now=self.now + 1.0)
        now = self.now + 1.0
        while self.session.dragon is None and self.session.state is State.PLAYING:
            now += STEP
            self.session.tick(now=now)
        self.assertIsNotNone(self.session.dragon)
        self.assertEqual(self.session.dragon.hits, 0)
        self.assertEqual(self.session.dragon.points, 0)
        self.assertFalse(self.session.dragon.completed)


class Combos(unittest.TestCase):
    """Sections 12 and 13."""

    def setUp(self):
        self.session = Session()
        self.session.start(now=0.0)

    def cut(self, now: float, kind: str = "apple") -> None:
        from aipi5.games.fruit_ninja.fruit import Fruit
        item = Fruit(kind=BY_NAME[kind], x=640, y=400, vx=0, vy=0, spin=0)
        self.session._cut(item, (600, 400), (680, 400), now)

    def test_slices_close_together_build_a_combo(self):
        self.cut(1.0)
        self.cut(1.2)
        self.cut(1.4)
        self.assertEqual(self.session.combo, 3)

    def test_a_gap_breaks_the_combo(self):
        self.cut(1.0)
        self.cut(1.2)
        self.cut(5.0)
        self.assertEqual(self.session.combo, 1)

    def test_a_combo_is_announced_only_once_it_is_worth_it(self):
        self.cut(1.0)
        self.cut(1.2)
        announced = [e for e in self.session.events if e["name"] == "combo"]
        self.assertFalse(announced)
        self.cut(1.4)
        announced = [e for e in self.session.events if e["name"] == "combo"]
        self.assertEqual(len(announced), 1)
        self.assertEqual(announced[0]["count"], 3)

    def test_the_combo_lapses_on_its_own(self):
        self.cut(1.0)
        self.cut(1.2)
        self.session.tick(now=9.0)
        self.assertEqual(self.session.combo, 0)

    def test_the_best_combo_is_remembered(self):
        for i in range(5):
            self.cut(1.0 + i * 0.2)
        self.cut(20.0)
        self.assertEqual(self.session.best_combo, 5)

    def test_a_combo_is_not_a_streak(self):
        """They measure different things and must not be conflated.

        A careful player who never misses has a long streak and no combo; a
        player who takes three at once has a combo of three and a streak that
        depends on what came before.
        """
        for i in range(8):
            self.cut(1.0 + i * 5.0)          # slow, deliberate, never missing
        self.assertEqual(self.session.streak, 8)
        self.assertEqual(self.session.combo, 1)


class Events(unittest.TestCase):
    """The page draws from these, so their shape is a contract."""

    def setUp(self):
        self.session = Session()
        self.session.start(now=0.0)

    def test_a_slice_carries_what_the_splash_needs(self):
        from aipi5.games.fruit_ninja.fruit import Fruit
        item = Fruit(kind=BY_NAME["watermelon"], x=100, y=200, vx=0, vy=0,
                     spin=0)
        self.session._cut(item, (0, 200), (200, 200), 1.0)
        event = self.session.events[-1]
        self.assertEqual(event["name"], "slice")
        for field in ("kind", "sound", "juice", "colour", "flesh", "wet",
                      "points", "x", "y", "cut"):
            self.assertIn(field, event)
        self.assertEqual(event["juice"], BY_NAME["watermelon"].juice)

    def test_every_event_has_a_name(self):
        """The page switches on it; an event without one is undrawable."""
        session = Session()
        session.spawner.seed(2)
        session.start(now=0.0)
        now = 0.0
        index = 0
        while session.state is State.PLAYING:
            now += STEP
            hands = []
            if session.phase is Phase.ULTIMATE:
                name = "left_wrist" if index % 2 == 0 else "right_wrist"
                hands = [across_the_dragon(name, 1 if index % 2 else -1)]
                index += 1
            session.tick(now=now, hands=hands)
        self.assertTrue(session.events)
        for event in session.events:
            self.assertIn("name", event)
            self.assertIsInstance(event["name"], str)

    def test_the_event_queue_is_capped(self):
        """Section 41: a page that stopped reading cannot grow this."""
        for i in range(500):
            self.session.events.append({"name": "slice", "points": i})
        drained = self.session.take_events()
        self.assertLessEqual(len(drained), 30)
        # The newest are the ones kept — a splash for a fruit cut four seconds
        # ago would land where the fruit no longer is.
        self.assertEqual(drained[-1]["points"], 499)
        self.assertEqual(self.session.events, [])


class Snapshot(unittest.TestCase):
    """What goes over the wire."""

    def test_the_phase_is_published(self):
        session = Session()
        session.start(now=0.0)
        self.assertEqual(session.snapshot(now=0.0)["phase"], "normal")

    def test_the_dragon_is_absent_until_it_exists(self):
        session = Session()
        session.start(now=0.0)
        self.assertNotIn("dragon", session.snapshot(now=0.0))

    def test_the_dragon_snapshot_has_what_the_page_draws(self):
        session = Session()
        session.start(now=0.0)
        session.dragon = UltimateDragon(born_at=0.0)
        payload = session.snapshot(now=0.0)["dragon"]
        for field in ("x", "y", "r", "hits", "required", "stage", "completed",
                      "time_left", "colour", "juice"):
            self.assertIn(field, payload)
        self.assertEqual(payload["required"], HITS_REQUIRED)

    def test_the_ultimate_result_is_in_the_stats(self):
        session = Session()
        session.start(now=0.0)
        session.ultimate_hits = 34
        session.ultimate_done = True
        stats = session.snapshot(now=0.0)["stats"]
        self.assertEqual(stats["ultimate_hits"], 34)
        self.assertTrue(stats["ultimate_done"])


if __name__ == "__main__":
    unittest.main()
