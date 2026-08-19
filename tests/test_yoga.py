"""Yoga Coach: the rig, the pose table, the three lessons and the session.

The rig tests are the load-bearing ones. Everything else in this game — what
the coach is drawn doing, what the player is marked against, what the
correction line says — is derived from fourteen numbers per pose, so a rig that
is subtly wrong is a game that is wrong everywhere at once and says so nowhere.
Two of these tests exist because they caught exactly that during the build:
the back-view orientation check found a forward fold whose hips were on the
wrong sides, which had silently halved every stance measurement taken from it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from aipi5.games.fruit_ninja.game import State
from aipi5.games.yoga import lesson as syllabus
from aipi5.games.yoga.game import Phase, YogaSession
from aipi5.games.yoga.lesson import (
    COUNTDOWN_SECONDS, DIFFICULTIES, LESSONS, LESSON_SECONDS)
from aipi5.games.yoga.poses import POSES
from aipi5.games.yoga.rig import (
    ANGLE_METRICS, RIG, angular_distance, forward_kinematics, measure,
    mirrored, player_joints)
from aipi5.games.yoga.scoring import (
    Calibration, assess, band, correction, pose_score)
from aipi5.motion.pose_types import KEYPOINT_NAMES, KeyPoint, PersonPose

YOGA_JS = (Path(__file__).resolve().parents[1] / "aipi5" / "ui" / "web"
           / "assets" / "yoga" / "yoga.js")

#: rig joint -> the landmark the pose model calls it.
LANDMARKS = {
    "head": "nose",
    "left_shoulder": "left_shoulder", "right_shoulder": "right_shoulder",
    "left_elbow": "left_elbow", "right_elbow": "right_elbow",
    "left_wrist": "left_wrist", "right_wrist": "right_wrist",
    "left_hip": "left_hip", "right_hip": "right_hip",
    "left_knee": "left_knee", "right_knee": "right_knee",
    "left_ankle": "left_ankle", "right_ankle": "right_ankle",
}


def body(pose_id: str, *, scale: float = 0.30, cx: float = 0.5,
         cy: float = 0.45, confidence: float = 0.95, legs: float = 1.0,
         swap_sides: bool = False, drop: tuple[str, ...] = ()) -> PersonPose:
    """A player standing in one of the coach's poses, as the camera sees them.

    `legs` stretches the lower body, which is how a differently proportioned
    person is simulated. `swap_sides` reflects them, which is how somebody
    doing a beautiful Warrior II on the wrong side is.
    """
    pose = POSES[pose_id]
    joints = forward_kinematics(pose.table, pose.scales)
    points = {name: KeyPoint(0.5, 0.5, 0.02) for name in KEYPOINT_NAMES}
    for joint, landmark in LANDMARKS.items():
        x, y = joints[joint]
        if joint.split("_")[-1] in ("hip", "knee", "ankle"):
            y *= legs
        if swap_sides:
            x = -x
        points[landmark] = KeyPoint(cx + x * scale, cy + y * scale, confidence)
    if swap_sides:
        for left, right in (("shoulder", "shoulder"), ("elbow", "elbow"),
                            ("wrist", "wrist"), ("hip", "hip"),
                            ("knee", "knee"), ("ankle", "ankle")):
            a, b = f"left_{left}", f"right_{right}"
            points[a], points[b] = points[b], points[a]
    for name in drop:
        old = points[name]
        points[name] = KeyPoint(old.x, old.y, 0.02)
    return PersonPose(0.97, points, (0.2, 0.1, 0.6, 0.85), 1)


def metrics_for(pose_id: str, **kwargs) -> dict:
    return measure(player_joints(body(pose_id, **kwargs), 0.30))


def settle(session: YogaSession, now: float, seconds: float,
           pose_id: str | None = None, **kwargs) -> float:
    """Feed `seconds` of a player standing in a pose, at thirty frames."""
    for _ in range(int(seconds * 30)):
        step = session.step
        target = pose_id or (step.pose_id if step else "mountain")
        session.tick_pose(now, body(target, **kwargs), now)
        now += 1 / 30
    return now


def ready(difficulty: str = "beginner") -> tuple[YogaSession, float]:
    """A session with a level chosen and the player already calibrated."""
    session = YogaSession()
    session.select_difficulty(difficulty)
    now = settle(session, 100.0, 3.0, "mountain")
    return session, now


class TestRig(unittest.TestCase):

    def test_every_sided_pose_is_the_exact_mirror_of_its_partner(self):
        """Left and right are not two hand-written tables that resemble.

        Warrior II on the left and Warrior II on the right have to be the same
        pose reflected, to the degree, or the scoring is harsher on one side
        than the other for reasons no player could ever discover.
        """
        checked = 0
        for pose in POSES.values():
            if not pose.side:
                continue
            partner = POSES[pose.mirror_id]
            flipped = mirrored(pose.targets)
            self.assertEqual(set(flipped), set(partner.targets), pose.id)
            for name, value in flipped.items():
                other = partner.targets[name]
                gap = (angular_distance(value, other) if name in ANGLE_METRICS
                       else abs(value - other))
                self.assertLess(gap, 0.01, f"{pose.id}.{name}")
            checked += 1
        self.assertGreaterEqual(checked, 18)

    def test_the_coach_is_never_drawn_facing_the_wrong_way(self):
        """The coach is seen from behind, so her left is at the smaller x.

        This is what makes "raise your left arm" mean the same thing to the
        coach and to the mirrored pose stream, with no conversion anywhere.
        It caught a real bug: a forward fold that set `shoulder_line` and not
        `hip_line` had its hips swapped, because the default derives both from
        the spine and this pose's spine points downwards.
        """
        for pose in POSES.values():
            joints = forward_kinematics(pose.table, pose.scales)
            self.assertLess(joints["left_shoulder"][0],
                            joints["right_shoulder"][0], pose.id)
            self.assertLess(joints["left_hip"][0], joints["right_hip"][0],
                            pose.id)

    def test_measurements_do_not_change_with_distance_or_position(self):
        near = metrics_for("warrior_two_left", scale=0.40, cx=0.5, cy=0.4)
        far = metrics_for("warrior_two_left", scale=0.18, cx=0.24, cy=0.62)
        self.assertEqual(set(near), set(far))
        for name, value in near.items():
            gap = (angular_distance(value, far[name]) if name in ANGLE_METRICS
                   else abs(value - far[name]))
            self.assertLess(gap, 0.02, name)

    def test_a_metric_is_dropped_rather_than_guessed(self):
        full = metrics_for("mountain")
        legless = metrics_for("mountain", drop=("left_ankle", "right_ankle"))
        self.assertIn("stance", full)
        self.assertNotIn("stance", legless)
        self.assertNotIn("stack", legless)
        self.assertIn("left_elbow", legless)

    def test_the_browser_rig_matches_the_one_poses_are_built_from(self):
        """The coach is drawn in the browser from a copy of these numbers.

        If the two drift, the coach demonstrates one shape and the player is
        marked against another, and nothing on screen would say so.
        """
        source = YOGA_JS.read_text(encoding="utf-8")
        block = re.search(r"const RIG = \{(.*?)\};", source, re.S)
        self.assertIsNotNone(block, "yoga.js has no RIG table")
        found = {name: float(value) for name, value in
                 re.findall(r"(\w+):\s*([0-9.]+)", block.group(1))}
        self.assertEqual(found, RIG)

    def test_the_browser_forward_kinematics_keeps_the_two_default_lines(self):
        source = YOGA_JS.read_text(encoding="utf-8")
        self.assertIn("bones.shoulder_line !== undefined", source)
        self.assertIn("bones.hip_line !== undefined ? bones.hip_line : spine - 90",
                      source)
        self.assertIn("function kinematics(bones, scales)", source)
        # Angles are interpolated the short way round, or a wrist crossing
        # 180 degrees sweeps an arm through the coach's body.
        self.assertIn("return from + wrap(to - from) * t;", source)


class TestPoses(unittest.TestCase):

    def test_a_player_standing_still_does_not_pass_the_real_poses(self):
        """The gradient this whole game rests on.

        Not a classifier — a body that is a bit wrong should score a bit lower.
        But somebody who simply stands there while the coach demonstrates
        Warrior II has to land well under the threshold that credits a hold,
        on the most generous difficulty, or the scoring means nothing.
        """
        still = POSES["mountain"].targets
        floor = syllabus.HOLD_THRESHOLD["beginner"]
        for pose in POSES.values():
            if pose.family in ("balance", "standing") and pose.level >= 2:
                accuracy = assess(pose, still, tolerance_scale=1.35).accuracy
                self.assertLess(accuracy, floor, pose.id)

    def test_each_pose_scores_itself_perfectly(self):
        for pose in POSES.values():
            result = assess(pose, pose.targets, tolerance_scale=0.88)
            self.assertEqual(round(result.accuracy, 6), 1.0, pose.id)
            self.assertTrue(result.tracked)
            self.assertFalse(result.wrong_side)

    def test_natural_variation_is_not_punished(self):
        """Ten degrees of hip and knee is a different body, not a worse pose."""
        pose = POSES["warrior_two_left"]
        loosened = dict(pose.targets)
        for name in ("left_knee", "right_hip", "left_shoulder", "torso"):
            loosened[name] += 10.0
        self.assertGreater(
            assess(pose, loosened, tolerance_scale=1.05).accuracy, 0.92)

    def test_the_wrong_side_is_reported_rather_than_marked_down_silently(self):
        pose = POSES["warrior_two_left"]
        result = assess(pose, metrics_for("warrior_two_left", swap_sides=True),
                        tolerance_scale=1.35)
        self.assertTrue(result.wrong_side)
        self.assertGreater(result.mirror_accuracy, result.accuracy)
        message = correction(pose, {}, result)
        self.assertIn("Other side", message)
        self.assertIn("left", message)

    def test_a_symmetric_pose_never_claims_the_wrong_side(self):
        for pose_id in ("mountain", "goddess", "star", "chair", "cactus_arms"):
            pose = POSES[pose_id]
            self.assertFalse(
                assess(pose, metrics_for(pose_id, swap_sides=True)).wrong_side,
                pose_id)

    def test_a_correction_names_the_joint_and_the_direction(self):
        pose = POSES["warrior_two_left"]
        lazy = dict(pose.targets)
        # A front knee left almost straight: the classic Warrior II mistake.
        lazy["left_knee"] = -8.0
        result = assess(pose, lazy, tolerance_scale=1.05)
        self.assertEqual(result.worst, "left_knee")
        self.assertIn("left knee", correction(pose, lazy, result))

    def test_calibration_removes_the_player_from_the_measurement(self):
        """A long-legged player is not permanently told to sink lower."""
        pose = POSES["chair"]
        tall = metrics_for("chair", legs=1.18)
        uncalibrated = assess(pose, tall, tolerance_scale=1.05).accuracy

        calibration = Calibration()
        for _ in range(40):
            calibration.add(metrics_for("mountain", legs=1.18),
                            POSES["mountain"].targets)
        calibrated = assess(pose, tall, tolerance_scale=1.05,
                            calibration=calibration).accuracy
        self.assertGreater(calibration.leg, 1.05)
        self.assertGreater(calibrated, uncalibrated)
        self.assertGreater(calibrated, 0.95)

    def test_nothing_is_calibrated_from_a_body_that_is_not_standing(self):
        """A limb ratio learned during Warrior II would be nonsense.

        The guard is in the session rather than in `Calibration.add`, because
        deciding whether a body is standing needs the pose table and the
        calibration deliberately does not have it.
        """
        session = YogaSession()
        session.select_difficulty("beginner")
        for _ in range(40):
            session.tick_pose(0.0, body("warrior_two_left"), 0.0)
        self.assertEqual(session.calibration.samples, 0)
        for _ in range(40):
            session.tick_pose(0.0, body("mountain"), 0.0)
        self.assertGreater(session.calibration.samples, 0)


class TestScoreShape(unittest.TestCase):

    def test_a_complete_steady_hold_beats_a_moment_of_perfection(self):
        """The spec's central requirement about what a pose score means."""
        whole = pose_score(quality=0.88, coverage=1.0, stability=0.95)
        flash = pose_score(quality=1.0, coverage=0.05, stability=0.2)
        self.assertGreater(whole, flash)
        self.assertLess(flash, 60)

    def test_the_bands_read_out_the_way_the_examples_do(self):
        self.assertEqual(band(94), "Excellent")
        self.assertEqual(band(86), "Great")
        self.assertEqual(band(74), "Good")
        self.assertEqual(band(61), "Keep Practicing")

    def test_a_perfect_pose_is_a_hundred_and_nothing_is_over(self):
        self.assertEqual(pose_score(1.0, 1.0, 1.0), 100)
        self.assertEqual(pose_score(2.0, 2.0, 2.0), 100)
        self.assertEqual(pose_score(-1.0, 0.0, 0.0), 0)


class TestLessons(unittest.TestCase):

    def test_every_lesson_is_about_twenty_minutes(self):
        for name, lesson in LESSONS.items():
            with self.subTest(name):
                self.assertGreaterEqual(lesson.total_seconds, 19 * 60)
                self.assertLessEqual(lesson.total_seconds, 21 * 60)

    def test_a_lesson_fits_inside_the_clock_it_is_measured_against(self):
        """A class that could not finish would always cut its own cooldown."""
        for name, lesson in LESSONS.items():
            with self.subTest(name):
                self.assertLessEqual(
                    lesson.total_seconds + COUNTDOWN_SECONDS, LESSON_SECONDS)

    def test_the_shape_of_a_class_is_warm_up_through_peak_to_cooldown(self):
        for name, lesson in LESSONS.items():
            self.assertEqual(
                lesson.segments,
                (syllabus.WARMUP, syllabus.FOUNDATION, syllabus.STRENGTH,
                 syllabus.PEAK, syllabus.COOLDOWN), name)

    def test_poses_get_harder_and_then_gentle_again(self):
        """Easy to hard *within* a difficulty, which the spec asks for twice."""
        for name, lesson in LESSONS.items():
            with self.subTest(name):
                levels = {}
                for segment in lesson.segments:
                    steps = [s for s in lesson.steps if s.segment == segment]
                    levels[segment] = sum(s.pose.level for s in steps) / len(steps)
                self.assertLess(levels[syllabus.WARMUP],
                                levels[syllabus.FOUNDATION])
                self.assertLessEqual(levels[syllabus.FOUNDATION],
                                     levels[syllabus.STRENGTH])
                self.assertLess(levels[syllabus.STRENGTH], levels[syllabus.PEAK])
                self.assertLess(levels[syllabus.COOLDOWN], levels[syllabus.PEAK])

    def test_balance_belongs_to_the_peak_and_nowhere_else(self):
        """Standing on one leg is the hardest thing any of these classes asks.

        A numeric level average is a blunt instrument — a rest pose in the
        middle of the peak drags it down — so this is the progression claim
        stated the way it is actually true.
        """
        for name, lesson in LESSONS.items():
            with self.subTest(name):
                for step in lesson.steps:
                    if step.pose.family == "balance":
                        self.assertEqual(step.segment, syllabus.PEAK,
                                         step.pose_id)
                peak = [s for s in lesson.steps if s.segment == syllabus.PEAK]
                self.assertTrue(any(s.pose.family == "balance" for s in peak))

    def test_no_balance_pose_arrives_before_the_body_is_warm(self):
        for name, lesson in LESSONS.items():
            elapsed = 0.0
            for step in lesson.steps:
                if step.pose.family == "balance":
                    self.assertGreater(elapsed, 7 * 60, f"{name}: {step.pose_id}")
                    break
                elapsed += step.hold_s + lesson.transition_s

    def test_both_sides_of_every_asymmetric_pose_are_practised(self):
        for name, lesson in LESSONS.items():
            sided = {s.pose_id for s in lesson.steps if s.pose.side}
            for pose_id in sided:
                self.assertIn(POSES[pose_id].mirror_id, sided, name)

    def test_harder_lessons_hold_longer_and_are_marked_more_closely(self):
        holds = {name: lesson.hold_seconds for name, lesson in LESSONS.items()}
        self.assertLess(holds["beginner"], holds["intermediate"])
        self.assertLess(holds["intermediate"], holds["advanced"])
        scales = syllabus.TOLERANCE_SCALE
        self.assertGreater(scales["beginner"], scales["intermediate"])
        self.assertGreater(scales["intermediate"], scales["advanced"])
        floors = syllabus.HOLD_THRESHOLD
        self.assertLess(floors["beginner"], floors["intermediate"])
        self.assertLess(floors["intermediate"], floors["advanced"])

    def test_every_lesson_ends_on_something_restful(self):
        for name, lesson in LESSONS.items():
            self.assertEqual(lesson.steps[-1].pose_id, "mountain_breath", name)

    def test_a_mistyped_pose_in_a_sequence_is_refused(self):
        with self.assertRaises(KeyError):
            syllabus.Step("half_lotus_on_a_unicycle", 20, "Peak").pose


class TestSession(unittest.TestCase):

    def test_a_level_has_to_be_chosen_before_the_gesture_will_start_anything(self):
        session = YogaSession()
        self.assertFalse(session.startable)
        self.assertEqual(session.gesture_phase, "select")
        with self.assertRaises(ValueError):
            session.start(0.0)
        session.select_difficulty("intermediate")
        self.assertTrue(session.startable)
        self.assertEqual(session.gesture_phase, "ready")

    def test_an_unknown_level_is_refused(self):
        with self.assertRaises(ValueError):
            YogaSession().select_difficulty("expert")

    def test_the_ready_screen_asks_the_player_to_step_back_for_their_feet(self):
        session = YogaSession()
        session.select_difficulty("beginner")
        session.tick_pose(0.0, body("mountain", drop=("left_ankle",
                                                      "right_ankle",
                                                      "left_knee",
                                                      "right_knee")), 0.0)
        self.assertIn("feet", session.snapshot(0.0)["framing"])
        session.tick_pose(0.1, body("mountain"), 0.1)
        self.assertEqual(session.snapshot(0.1)["framing"], "")

    def test_the_countdown_runs_before_the_first_pose(self):
        session, now = ready()
        session.start(now)
        self.assertIs(session.phase, Phase.COUNTDOWN)
        self.assertEqual(session.snapshot(now)["countdown"], "READY")
        now = settle(session, now, 2.6, "mountain")
        self.assertEqual(session.snapshot(now)["countdown"], "2")
        now = settle(session, now, 2.0, "mountain")
        self.assertIs(session.phase, Phase.TRANSITION)

    def test_the_hold_timer_only_runs_while_the_player_is_in_the_pose(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        target = session.step.pose_id
        now = settle(session, now, session.lesson.transition_s + 0.2, target)
        self.assertIs(session.phase, Phase.HOLDING)

        # Four seconds of doing something else entirely. Not quite zero
        # progress: the smoothed accuracy and the hysteresis together let the
        # timer run for a fraction of a second after somebody leaves the pose,
        # which is deliberate and is what stops it stuttering when they wobble.
        before = session.hold_left
        now = settle(session, now, 4.0, "goddess")
        self.assertGreater(session.hold_left, before - 1.2)
        self.assertFalse(session.snapshot(now)["holding"])
        self.assertTrue(session.feedback.message)

        # Four seconds of doing the pose.
        now = settle(session, now, 4.0, target)
        self.assertLess(session.hold_left, before - 3.0)
        self.assertTrue(session.snapshot(now)["holding"])

    def test_leaving_the_pose_corrects_rather_than_fails_it(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        target = session.step.pose_id
        now = settle(session, now, session.lesson.transition_s + 3.0, target)
        index = session.step_index
        now = settle(session, now, 3.0, "goddess")
        # Still the same pose, still playing, and now saying what to fix.
        self.assertEqual(session.step_index, index)
        self.assertIs(session.state, State.PLAYING)
        self.assertTrue(session.snapshot(now)["feedback"])

    def test_a_pose_nobody_can_find_is_left_behind_rather_than_blocking(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        step = session.step
        deadline = (step.hold_s * syllabus.DEADLINE_FACTOR
                    + syllabus.DEADLINE_ALLOWANCE)
        now = settle(session, now,
                     session.lesson.transition_s + deadline + 1.0, "goddess")
        self.assertGreater(session.step_index, 0)
        self.assertEqual(len(session.results), 1)
        self.assertLess(session.results[0].score, 55)

    def test_the_wrong_side_does_not_earn_hold_time(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        # Wind on to the first pose that has a side to get wrong.
        while session.step is not None and not session.step.pose.side:
            step = session.step
            now = settle(session, now, session.lesson.transition_s
                         + step.hold_s + 0.4, step.pose_id)
        target = session.step.pose_id
        now = settle(session, now, session.lesson.transition_s + 0.2, target)
        before = session.hold_left
        now = settle(session, now, 4.0, target, swap_sides=True)
        self.assertAlmostEqual(session.hold_left, before, delta=0.4)
        self.assertTrue(session.snapshot(now)["wrong_side"])

    def test_pausing_does_not_expire_the_pose_you_paused_in(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        target = session.step.pose_id
        now = settle(session, now, session.lesson.transition_s + 1.0, target)
        remaining = session.phase_until - now
        held = session.hold_left

        self.assertTrue(session.pause(now))
        # Ninety seconds of nobody being there.
        now += 90.0
        self.assertTrue(session.resume(now))
        self.assertAlmostEqual(session.phase_until - now, remaining, delta=0.1)
        self.assertAlmostEqual(session.hold_left, held, delta=0.01)

    def test_the_lesson_clock_is_twenty_minutes_whatever_the_player_does(self):
        for difficulty in DIFFICULTIES:
            with self.subTest(difficulty):
                session, now = ready(difficulty)
                session.start(now)
                started = now
                # Somebody who never finds a single pose: every step runs to
                # its deadline, and the class still stops on time.
                while session.state is State.PLAYING and now - started < 1500:
                    now = settle(session, now, 5.0, "mountain")
                self.assertIs(session.state, State.OVER)
                self.assertLessEqual(now - started, LESSON_SECONDS + 6)

    def test_a_whole_lesson_followed_correctly_scores_and_summarises(self):
        session, now = ready("advanced")
        session.start(now)
        frames = 0
        while session.state is State.PLAYING and frames < 30 * 1400:
            step = session.step
            session.tick_pose(now, body(step.pose_id if step else "mountain"), now)
            now += 1 / 30
            frames += 1

        self.assertIs(session.state, State.OVER)
        summary = session.snapshot(now)["summary"]
        self.assertGreaterEqual(summary["overall"], 90)
        self.assertEqual(summary["band"], "Excellent")
        self.assertGreaterEqual(summary["average_accuracy"], 95)
        self.assertEqual(summary["completed"], summary["total"])
        self.assertGreater(summary["hold_seconds"], 900)
        self.assertIsNotNone(summary["best"])
        self.assertIsNotNone(summary["lowest"])
        self.assertLessEqual(summary["lowest"]["score"], summary["best"]["score"])
        self.assertEqual(len(summary["poses"]), summary["completed"])
        self.assertEqual(session.score, summary["overall"])
        self.assertEqual(session.best, session.score)

    def test_standing_still_for_twenty_minutes_is_not_an_excellent_class(self):
        session, now = ready()
        session.start(now)
        while session.state is State.PLAYING:
            now = settle(session, now, 5.0, "mountain")
        summary = session.snapshot(now)["summary"]
        self.assertLess(summary["overall"], 62)
        self.assertGreater(summary["completed"], 10)

    def test_the_snapshot_carries_everything_the_screen_has_to_show(self):
        session, now = ready("intermediate")
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 1.0, "mountain")
        data = session.snapshot(now)
        self.assertEqual(data["kind"], "yoga")
        for key in ("score", "time_left", "accuracy", "feedback", "holding",
                    "wrong_side", "tracked", "rig", "difficulty"):
            self.assertIn(key, data)
        pose = data["pose"]
        for key in ("name", "sanskrit", "instruction", "cue", "hold",
                    "hold_left", "index", "total", "segment"):
            self.assertIn(key, pose)
        self.assertEqual(pose["index"], 1)
        self.assertEqual(pose["total"], len(session.lesson.steps))
        # Everything must survive the trip to the browser as JSON.
        json.dumps(data)

    def test_the_coach_animation_is_sent_as_angles_to_tween_between(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.1, "mountain")
        rig = session.snapshot(now)["rig"]
        self.assertIn("bones", rig)
        self.assertIn("from_bones", rig)
        self.assertIn("spine", rig["bones"])
        self.assertLess(rig["blend"], 0.4)
        now = settle(session, now, session.lesson.transition_s, "mountain")
        self.assertGreaterEqual(session.snapshot(now)["rig"]["blend"], 0.99)

    def test_a_score_card_appears_and_then_goes_away_on_its_own(self):
        session, now = ready()
        session.start(now)
        now = settle(session, now, COUNTDOWN_SECONDS + 0.2, "mountain")
        step = session.step
        now = settle(session, now,
                     session.lesson.transition_s + step.hold_s + 0.5,
                     step.pose_id)
        self.assertIn("last", session.snapshot(now))
        now = settle(session, now, syllabus.RESULT_SECONDS + 0.5,
                     session.step.pose_id)
        self.assertNotIn("last", session.snapshot(now))

    def test_the_events_are_drained_once(self):
        session, now = ready()
        session.start(now)
        names = {event["name"] for event in session.take_events()}
        self.assertIn("yoga-start", names)
        self.assertEqual(session.take_events(), [])


if __name__ == "__main__":
    unittest.main()
