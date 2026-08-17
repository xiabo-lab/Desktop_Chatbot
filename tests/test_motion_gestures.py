"""The arms-crossed X that starts and restarts motion games."""

from __future__ import annotations

import unittest

from aipi5.motion.gestures import CrossedArmsGesture, arms_crossed
from aipi5.motion.pose_types import KEYPOINT_NAMES, KeyPoint, PersonPose


def person(**overrides) -> PersonPose:
    points = {name: KeyPoint(0.5, 0.5, 0.0) for name in KEYPOINT_NAMES}
    pose = {
        "left_shoulder": (0.35, 0.30, 0.9),
        "right_shoulder": (0.65, 0.30, 0.9),
        "left_elbow": (0.27, 0.53, 0.9),
        "right_elbow": (0.73, 0.53, 0.9),
        "left_wrist": (0.62, 0.39, 0.9),
        "right_wrist": (0.38, 0.39, 0.9),
    }
    pose.update(overrides)
    for name, value in pose.items():
        points[name] = KeyPoint(*value)
    return PersonPose(confidence=0.9, keypoints=points)


class TestCrossedArmsGeometry(unittest.TestCase):

    def test_an_x_across_the_chest_is_recognised(self):
        self.assertTrue(arms_crossed(person()))

    def test_ordinary_raised_arms_are_not_an_x(self):
        subject = person(left_wrist=(0.24, 0.39, 0.9),
                         right_wrist=(0.76, 0.39, 0.9))
        self.assertFalse(arms_crossed(subject))

    def test_clasped_hands_in_the_middle_are_not_an_x(self):
        subject = person(left_wrist=(0.51, 0.40, 0.9),
                         right_wrist=(0.49, 0.40, 0.9))
        self.assertFalse(arms_crossed(subject))

    def test_low_crossed_hands_between_swings_are_not_an_x(self):
        subject = person(left_wrist=(0.62, 0.76, 0.9),
                         right_wrist=(0.38, 0.76, 0.9))
        self.assertFalse(arms_crossed(subject))

    def test_one_uncertain_joint_refuses_the_gesture(self):
        self.assertFalse(arms_crossed(
            person(left_elbow=(0.27, 0.53, 0.2))))

    def test_mirroring_does_not_change_the_answer(self):
        mirrored = {}
        for name, point in person().keypoints.items():
            mirrored[name] = KeyPoint(1.0 - point.x, point.y, point.confidence)
        self.assertTrue(arms_crossed(
            PersonPose(confidence=0.9, keypoints=mirrored)))


class TestCrossedArmsLatch(unittest.TestCase):

    def setUp(self):
        self.gesture = CrossedArmsGesture(hold_s=0.6, release_s=0.2,
                                          lost_grace_s=0.15)
        self.crossed = person()
        self.neutral = person(left_wrist=(0.24, 0.55, 0.9),
                              right_wrist=(0.76, 0.55, 0.9))

    def test_start_requires_a_continuous_hold(self):
        self.assertFalse(self.gesture.update(self.crossed, "ready", 1.0))
        self.assertFalse(self.gesture.update(self.crossed, "ready", 1.59))
        self.assertTrue(self.gesture.update(self.crossed, "ready", 1.61))
        self.assertFalse(self.gesture.update(self.crossed, "ready", 2.0),
                         "one held pose must fire only once")

    def test_one_short_keypoint_dropout_does_not_erase_the_hold(self):
        self.gesture.update(self.crossed, "ready", 1.0)
        self.gesture.update(None, "ready", 1.10)
        self.assertTrue(self.gesture.update(self.crossed, "ready", 1.61))

    def test_a_real_loss_resets_the_hold(self):
        self.gesture.update(self.crossed, "ready", 1.0)
        self.gesture.update(None, "ready", 1.20)
        self.assertFalse(self.gesture.update(self.crossed, "ready", 1.61))
        self.assertTrue(self.gesture.update(self.crossed, "ready", 2.22))

    def test_game_over_does_not_accept_an_x_held_from_play(self):
        self.gesture.update(self.neutral, "ready", 1.0)
        self.gesture.update(self.crossed, "playing", 2.0)
        self.assertFalse(self.gesture.update(self.crossed, "over", 3.0))
        self.assertFalse(self.gesture.update(self.crossed, "over", 4.0))

        # Lower arms briefly, then make a fresh X.
        self.gesture.update(self.neutral, "over", 4.1)
        self.gesture.update(self.neutral, "over", 4.31)
        self.gesture.update(self.crossed, "over", 4.4)
        self.assertTrue(self.gesture.update(self.crossed, "over", 5.01))

    def test_status_reports_hold_progress(self):
        self.gesture.update(self.crossed, "ready", 10.0)
        self.gesture.update(self.crossed, "ready", 10.3)
        detail = self.gesture.describe(10.3)
        self.assertEqual(detail["name"], "arms-crossed-x")
        self.assertTrue(detail["crossed"])
        self.assertAlmostEqual(detail["progress"], 0.5)


if __name__ == "__main__":
    unittest.main()
