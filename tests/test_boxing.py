"""Boxing gesture, combat, training and swept-hit behaviour."""

from __future__ import annotations

import unittest

from aipi5.games.boxing.ai import OpponentAI
from aipi5.games.boxing.config import DIFFICULTIES
from aipi5.games.boxing.collision import swept_circle_hit
from aipi5.games.boxing.damage import DAMAGE_ZONES, FighterDamage
from aipi5.games.boxing.game import BoxingSession, TrainingPrompt
from aipi5.games.boxing.motion import BoxingAction, BoxingMotionAnalyzer, MotionResult
from aipi5.games.fruit_ninja.game import State
from aipi5.motion.pose_types import KEYPOINT_NAMES, KeyPoint, PersonPose


def pose(**overrides) -> PersonPose:
    points = {name: KeyPoint(0.5, 0.7, 0.05) for name in KEYPOINT_NAMES}
    base = {
        "nose": (0.50, 0.25),
        "left_shoulder": (0.40, 0.40), "right_shoulder": (0.60, 0.40),
        "left_elbow": (0.34, 0.50), "right_elbow": (0.66, 0.50),
        "left_wrist": (0.36, 0.56), "right_wrist": (0.64, 0.56),
    }
    base.update(overrides)
    for name, value in base.items():
        points[name] = KeyPoint(value[0], value[1], 0.98)
    return PersonPose(0.98, points, (0.25, 0.12, 0.5, 0.72), 1)


class TestSweptHit(unittest.TestCase):
    def test_fast_path_hits_between_frames(self):
        self.assertTrue(swept_circle_hit((0.1, 0.5), (0.9, 0.5),
                                        (0.5, 0.5), 0.05))

    def test_path_outside_region_misses(self):
        self.assertFalse(swept_circle_hit((0.1, 0.1), (0.9, 0.1),
                                         (0.5, 0.5), 0.1))


class TestBoxingMotion(unittest.TestCase):
    def setUp(self):
        self.motion = BoxingMotionAnalyzer()
        self.motion.update(pose(), 1.0)

    def test_fast_extension_is_one_left_punch(self):
        result = self.motion.update(pose(
            left_elbow=(0.30, 0.43), left_wrist=(0.18, 0.39)), 1.1)
        self.assertIn("left_punch", result.names)
        held = self.motion.update(pose(
            left_elbow=(0.30, 0.43), left_wrist=(0.18, 0.39)), 1.2)
        self.assertNotIn("left_punch", held.names)

    def test_the_same_punch_registers_at_any_pose_rate(self):
        """The regression the faster pipeline would otherwise have caused.

        Every punch threshold is a distance, and a distance only means anything
        over a stated time. They were measured against the previous *frame* at
        about thirty frames a second; the pipeline now runs at forty-five, so
        the gap between two frames is a third smaller and the identical punch
        used to stop clearing `punch_travel`. A latency improvement making the
        game harder to play is the worst shape a regression can have, because
        it presents as the tracking having got worse.

        So: one physical punch, described once as a function of wall-clock
        time, sampled at three rates. All three must see it.
        """
        # 450 ms from guard to full extension. Deliberately not a fast jab:
        # a fast one clears every gate at any frame rate and would prove
        # nothing. Measured against the per-frame version this replaces, the
        # slowest punch it would still see fell from 610 ms at 30 fps to 420 ms
        # at 45 and 310 ms at 60 — so this is a punch that the old code sees at
        # the rate it was tuned at and stops seeing at the rate it now runs at.
        # Which is to say: a deliberate punch, thrown by somebody who is not in
        # a hurry, going unrecognised because the pipeline got faster.
        DURATION = 0.45

        def wrist_at(t):
            travel = min(1.0, max(0.0, t / DURATION))
            return (0.36 - 0.18 * travel, 0.56 - 0.17 * travel)

        def elbow_at(t):
            travel = min(1.0, max(0.0, t / DURATION))
            return (0.34 - 0.04 * travel, 0.50 - 0.07 * travel)

        for rate in (30.0, 45.0, 60.0):
            with self.subTest(pose_fps=rate):
                motion = BoxingMotionAnalyzer()
                step = 1 / rate
                # Half a second of guard first, so the neutral stance is
                # settled and the wrist history is full before the jab.
                at = 0.0
                while at < 0.5:
                    motion.update(pose(), at)
                    at += step
                thrown = False
                start = at
                while at < start + DURATION + 0.15:
                    result = motion.update(
                        pose(left_elbow=elbow_at(at - start),
                             left_wrist=wrist_at(at - start)), at)
                    thrown = thrown or "left_punch" in result.names
                    at += step
                self.assertTrue(thrown, f"no punch seen at {rate:.0f} fps")

    def test_a_slow_reach_is_still_not_a_punch_at_any_rate(self):
        """The other half: rate independence must not mean "always yes".

        The same arm extending over a second and a half is somebody reaching
        for a glass of water, and the speed gate is what tells the two apart.
        """
        for rate in (30.0, 45.0, 60.0):
            with self.subTest(pose_fps=rate):
                motion = BoxingMotionAnalyzer()
                step = 1 / rate
                at = 0.0
                while at < 0.5:
                    motion.update(pose(), at)
                    at += step
                start, thrown = at, False
                while at < start + 1.5:
                    travel = min(1.0, (at - start) / 1.5)
                    result = motion.update(
                        pose(left_elbow=(0.34 - 0.04 * travel, 0.50 - 0.07 * travel),
                             left_wrist=(0.36 - 0.18 * travel, 0.56 - 0.17 * travel)),
                        at)
                    thrown = thrown or "left_punch" in result.names
                    at += step
                self.assertFalse(thrown, f"a reach scored at {rate:.0f} fps")

    def test_torso_position_selects_left_middle_and_right_lanes(self):
        self.assertEqual(self.motion.update(pose(), 1.1).posture["lane"], "middle")
        left = self.motion.update(pose(
            left_shoulder=(0.33, 0.40), right_shoulder=(0.53, 0.40)), 1.2)
        self.assertEqual(left.posture["lane"], "left")
        right = self.motion.update(pose(
            left_shoulder=(0.47, 0.40), right_shoulder=(0.67, 0.40)), 1.3)
        self.assertEqual(right.posture["lane"], "right")

    def test_horizontal_bent_arm_is_a_hook(self):
        result = self.motion.update(pose(
            right_elbow=(0.60, 0.55), right_wrist=(0.84, 0.50)), 1.1)
        self.assertIn("right_hook", result.names)

    def test_head_and_torso_shift_is_a_dodge(self):
        result = self.motion.update(pose(
            nose=(0.43, 0.25), left_shoulder=(0.33, 0.40),
            right_shoulder=(0.53, 0.40), left_elbow=(0.27, 0.50),
            right_elbow=(0.59, 0.50), left_wrist=(0.29, 0.56),
            right_wrist=(0.57, 0.56)), 1.1)
        self.assertIn("dodge_left", result.names)

    def test_lowered_head_and_shoulders_is_a_duck(self):
        result = self.motion.update(pose(
            nose=(0.50, 0.32), left_shoulder=(0.40, 0.45),
            right_shoulder=(0.60, 0.45)), 1.1)
        self.assertIn("duck", result.names)

    def test_two_hands_covering_face_is_guard_not_parry(self):
        result = self.motion.update(pose(
            left_wrist=(0.46, 0.29), right_wrist=(0.54, 0.29)), 1.1)
        self.assertEqual(result.guard, "two_hand_guard")
        self.assertNotIn("parry", result.names)
        stationary = self.motion.update(pose(
            left_wrist=(0.46, 0.29), right_wrist=(0.54, 0.29)), 1.2,
            incoming={"target": "head", "impact_in": 0.1, "parry_window": 0.2})
        self.assertNotIn("parry", stationary.names)

    def test_moving_hand_inside_timing_window_is_parry(self):
        result = self.motion.update(pose(
            left_elbow=(0.40, 0.38), left_wrist=(0.49, 0.29)), 1.1,
            incoming={"target": "head", "impact_in": 0.1, "parry_window": 0.2})
        self.assertIn("parry", result.names)


class TestBoxingSession(unittest.TestCase):
    def test_mode_is_required_before_start(self):
        session = BoxingSession()
        with self.assertRaises(ValueError):
            session.start(1.0)
        self.assertEqual(session.gesture_phase, "select")

    def test_training_is_exactly_ninety_seconds(self):
        session = BoxingSession()
        session.select_mode("training")
        self.assertEqual(session.duration, 90.0)

    def test_training_records_reaction_and_score(self):
        session = BoxingSession()
        session.select_mode("training")
        session.start(0.0)
        session._world_time = 10.0
        session._prompt = TrainingPrompt("left_punch", "head", 9.0, 12.0, 1)
        session._last_result = MotionResult(actions=[
            BoxingAction("left_punch", "left", "head", 0.9)])
        session._tick_training(10.0)
        self.assertEqual(session.successful_hits, 1)
        self.assertGreater(session.score, 0)
        self.assertEqual(session.best_reaction_s, 1.0)

    def test_head_and_body_damage_follow_configured_rules(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session._round_started = True
        session.opponent_ai.defend = lambda *args: "none"
        session._last_result = MotionResult(actions=[
            BoxingAction("left_punch", "left", "body", 0.9,
                         trajectory=(0.3, 0.5, 0.2, 0.6))])
        session._player_attacks(5.0)
        self.assertEqual(session.opponent_damage.hp, 219)
        session._last_result = MotionResult(actions=[
            BoxingAction("right_punch", "right", "head", 0.9,
                         trajectory=(0.7, 0.5, 0.8, 0.3))])
        session._player_attacks(6.0)
        self.assertEqual(session.opponent_damage.hp, 217)

    def test_normal_opponent_survives_an_opening_thirteen_hit_flurry(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session._round_started = True
        session.opponent_ai.defend = lambda *args: "none"
        for index in range(13):
            side = "left" if index % 2 == 0 else "right"
            session._last_result = MotionResult(actions=[
                BoxingAction(f"{side}_punch", side, "head", 0.9,
                             trajectory=(0.4, 0.4, 0.5, 0.3))])
            session._player_attacks(5.0 + index * 0.4)
        self.assertIs(session.state, State.PLAYING)
        self.assertEqual(session.opponent_damage.hp, 194)

    def test_hooks_use_the_same_target_damage_as_straight_punches(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session._round_started = True
        session.opponent_ai.defend = lambda *args: "none"
        session._last_result = MotionResult(actions=[
            BoxingAction("left_hook", "left", "body", 0.9,
                         trajectory=(0.3, 0.5, 0.4, 0.5)),
            BoxingAction("right_hook", "right", "head", 0.9,
                         trajectory=(0.7, 0.4, 0.6, 0.3)),
        ])
        session._player_attacks(5.0)
        self.assertEqual(session.opponent_damage.hp, 217)

    def test_opponent_health_scales_with_difficulty(self):
        for difficulty, params in DIFFICULTIES.items():
            with self.subTest(difficulty=difficulty):
                session = BoxingSession()
                session.select_mode("fight")
                session.select_difficulty(difficulty)
                session.start(0.0)
                self.assertEqual(session.opponent_damage.max_hp,
                                 params["opponent_hp"])
                self.assertEqual(session.opponent_damage.hp,
                                 params["opponent_hp"])

    def test_timeout_compares_health_percentage_not_raw_hp(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session.opponent_damage.hp = 110  # 50% of Normal's 220 HP.
        session.player_damage.hp = 60     # 60% of the player's 100 HP.
        session.finish(180.0)
        self.assertEqual(session.result, "win")

    def test_perfect_parry_slows_only_world_simulation(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session._start_counter(5.0)
        self.assertTrue(session.snapshot(5.0)["slow_motion"])
        self.assertEqual(session.snapshot(5.0)["simulation_scale"], 0.1)
        self.assertEqual(session.successful_parries, 1)

    def test_knockout_ends_the_match(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session.opponent_damage.hp = 1
        session._last_result = MotionResult(actions=[
            BoxingAction("right_punch", "right", "head", 1.0,
                         trajectory=(0.7, 0.5, 0.8, 0.3))])
        session.opponent_ai.defend = lambda *args: "none"
        session._player_attacks(5.0)
        self.assertIs(session.state, State.OVER)
        self.assertEqual(session.result, "win")

    def test_restarting_clears_injuries_and_statistics(self):
        session = BoxingSession()
        session.select_mode("fight")
        session.start(0.0)
        session.opponent_damage.hit(10, "left_eye")
        session.punches_thrown = 4
        session.finish(1.0, "win")
        session.start(2.0)
        self.assertEqual(session.opponent_damage.hp, 220)
        self.assertEqual(session.opponent_damage.injuries, {})
        self.assertEqual(session.punches_thrown, 0)

    def test_punches_map_only_to_the_four_visible_damage_locations(self):
        cases = (
            (BoxingAction("left_punch", "left", "head", .9), "right_eye"),
            (BoxingAction("right_hook", "right", "head", .9), "left_eye"),
            (BoxingAction("left_punch", "left", "body", .9), "right_shoulder"),
            (BoxingAction("right_hook", "right", "body", .9), "left_shoulder"),
        )
        for action, expected in cases:
            with self.subTest(action=action.name, side=action.side,
                              target=action.target):
                self.assertEqual(BoxingSession._zone_for(action), expected)


class TestFighterDamage(unittest.TestCase):
    def test_first_second_and_third_hits_have_explicit_visual_stages(self):
        fighter = FighterDamage(max_hp=100, hp=100,
                                display_hp=100, delayed_hp=100)
        expected = (
            ("bruise", 0, False),
            ("bruise-swelling", 1, False),
            ("bruise-strong-swelling-bleeding", 2, True),
        )
        for stage, swelling, bleeding in expected:
            fighter.hit(1, "left_eye")
            injury = fighter.snapshot()["injuries"][0]
            self.assertEqual(injury["stage"], stage)
            self.assertEqual(injury["swelling"], swelling)
            self.assertIs(injury["bleeding"], bleeding)

    def test_low_arcade_damage_still_records_an_injury(self):
        fighter = FighterDamage()
        fighter.hit(1, "left_shoulder")
        self.assertEqual(fighter.injuries, {"left_shoulder": 1})

    def test_damage_matrix_has_four_locations_and_caps_at_third_punch(self):
        fighter = FighterDamage()
        for _ in range(6):
            fighter.hit(1, "right_eye")
        fighter.hit(1, "left_shoulder")
        snapshot = fighter.snapshot()
        self.assertEqual(tuple(snapshot["damage_matrix"]), DAMAGE_ZONES)
        self.assertEqual(snapshot["damage_key"], "0310")
        self.assertEqual(fighter.injuries["right_eye"], 3)

    def test_unsupported_damage_location_is_not_added_to_matrix(self):
        fighter = FighterDamage()
        fighter.hit(1, "forehead")
        self.assertEqual(fighter.injuries, {})
        self.assertEqual(fighter.snapshot()["damage_key"], "0000")


class TestOpponentAI(unittest.TestCase):
    def test_attack_has_visible_windup_and_recovery_phases(self):
        ai = OpponentAI("normal", seed=7)
        events = ai.tick(ai.next_action_at)
        self.assertEqual(events[0]["name"], "opponent-attack")
        attack = events[0]["attack"]

        windup = attack.snapshot((attack.started_at + attack.impact_at) / 2)
        self.assertEqual(windup["phase"], "windup")
        self.assertGreater(windup["extension"], 0)
        self.assertLess(windup["extension"], 1)

        recovery = attack.snapshot((attack.impact_at + attack.recover_at) / 2)
        self.assertEqual(recovery["phase"], "recover")
        self.assertGreater(recovery["extension"], 0)
        self.assertLess(recovery["extension"], 1)

    def test_attacks_move_between_center_and_outer_lanes(self):
        ai = OpponentAI("normal", seed=7)
        self.assertEqual(ai.snapshot(0.0)["lane"], 0)
        ai._begin_attack(1.0)
        self.assertIn(ai.snapshot(1.0)["lane"], (-1, 1))
        ai.active = None
        ai._begin_attack(2.0)
        self.assertEqual(ai.snapshot(2.0)["lane"], 0)
