"""One body, two camera shapes, and the same answer from all three consumers.

Boxing, Yoga and the crossed-arms gesture all measure *shapes* — signed angles
between bones, and distances divided by a shoulder span. Every threshold they
use was measured through a 16:9 camera, and normalised camera coordinates carry
the frame's aspect ratio inside them: a bone at a true 45 degrees reads as 60.6
degrees in a 16:9 frame and 53.1 degrees in a 4:3 one.

Nothing noticed while there was one camera mode. The moment a 4:3 mode was
worth having, that 7.5 degrees became a systematic error on every diagonal limb
— most of a yoga tolerance, and enough to mark Triangle Pose wrong for somebody
doing it correctly. `PersonPose.shaped()` is the fix and this is the test of it:
the same room, photographed two ways, has to measure the same.
"""

from __future__ import annotations

import unittest

from aipi5.games.boxing.motion import BoxingMotionAnalyzer
from aipi5.games.yoga.rig import measure, player_joints
from aipi5.motion import geometry
from aipi5.motion.gestures import arms_crossed
from aipi5.motion.pose_types import KEYPOINT_NAMES, KeyPoint, PersonPose

#: What the camera in use does when asked for 4:3: it crops the 16:9 field
#: horizontally rather than rescaling it, keeping 75% of the width and all of
#: the height. Verified on the device by photographing one scene at both sizes.
CROP = 0.75


def wide(points: dict, confidence: float = 0.97) -> PersonPose:
    """A person as a 16:9 camera reports them."""
    keypoints = {name: KeyPoint(0.5, 0.7, 0.02) for name in KEYPOINT_NAMES}
    for name, (x, y) in points.items():
        keypoints[name] = KeyPoint(x, y, confidence)
    return PersonPose(0.97, keypoints, (0.3, 0.1, 0.4, 0.8), 1,
                      aspect=16 / 9)


def cropped(person: PersonPose) -> PersonPose:
    """The same room through the 4:3 mode: narrower field, same height.

    x is re-expressed as a fraction of a horizontal field three quarters the
    size, about the centre of the frame — which is what a centre crop is.
    """
    keypoints = {name: KeyPoint(0.5 + (point.x - 0.5) / CROP, point.y,
                                point.confidence)
                 for name, point in person.keypoints.items()}
    return PersonPose(person.confidence, keypoints, person.box,
                      person.track_id, aspect=4 / 3)


STANDING = {
    "nose": (0.50, 0.22),
    "left_shoulder": (0.43, 0.34), "right_shoulder": (0.57, 0.34),
    "left_elbow": (0.40, 0.47), "right_elbow": (0.60, 0.47),
    "left_wrist": (0.38, 0.59), "right_wrist": (0.62, 0.59),
    "left_hip": (0.45, 0.60), "right_hip": (0.55, 0.60),
    "left_knee": (0.44, 0.76), "right_knee": (0.56, 0.76),
    "left_ankle": (0.43, 0.92), "right_ankle": (0.57, 0.92),
}

#: Deliberately full of diagonals — a triangle-ish stance, where the aspect
#: error is largest. A pose made only of horizontal and vertical bones would
#: pass this suite without the fix and prove nothing.
DIAGONAL = {
    "nose": (0.44, 0.26),
    "left_shoulder": (0.40, 0.36), "right_shoulder": (0.55, 0.40),
    "left_elbow": (0.31, 0.25), "right_elbow": (0.63, 0.52),
    "left_wrist": (0.25, 0.14), "right_wrist": (0.70, 0.66),
    "left_hip": (0.44, 0.60), "right_hip": (0.56, 0.62),
    "left_knee": (0.36, 0.75), "right_knee": (0.66, 0.77),
    "left_ankle": (0.30, 0.92), "right_ankle": (0.74, 0.90),
}


class TestTheScaleItself(unittest.TestCase):

    def test_a_wider_field_is_a_smaller_factor(self):
        self.assertAlmostEqual(geometry.shape_scale(16 / 9), 1.0)
        self.assertAlmostEqual(geometry.shape_scale(4 / 3), CROP)

    def test_a_nonsense_aspect_changes_nothing(self):
        """A frame of zero height must not turn every joint into a nan."""
        self.assertEqual(geometry.shape_scale(0.0), 1.0)
        self.assertEqual(geometry.shape_scale(-2.0), 1.0)

    def test_shaping_a_reference_pose_is_free(self):
        """The common case returns the same object, not a rebuilt one."""
        person = wide(STANDING)
        self.assertIs(person.shaped(), person)

    def test_shaping_is_idempotent(self):
        once = cropped(wide(STANDING)).shaped()
        self.assertIs(once.shaped(), once)


class TestYogaMeasuresTheSameBody(unittest.TestCase):

    def metrics(self, person):
        return measure(player_joints(person, 0.4))

    def test_every_angle_survives_the_crop(self):
        wide_metrics = self.metrics(wide(DIAGONAL))
        crop_metrics = self.metrics(cropped(wide(DIAGONAL)))
        self.assertTrue(wide_metrics)
        for name, value in wide_metrics.items():
            with self.subTest(metric=name):
                self.assertAlmostEqual(value, crop_metrics[name], places=6)

    def test_the_crop_really_would_have_moved_them(self):
        """Guards the test above: without `shaped()` this is a real error.

        Comparing the shaped 4:3 body against the *raw* 4:3 body shows what was
        being corrected. If that difference were negligible there would be
        nothing here worth testing.
        """
        raw = cropped(wide(DIAGONAL))
        # Same joints, but claiming to be a reference-shape camera, so
        # `player_joints` leaves them alone.
        lying = PersonPose(raw.confidence, raw.keypoints, raw.box,
                           raw.track_id, aspect=16 / 9)
        good = self.metrics(raw)
        bad = self.metrics(lying)
        worst = max(abs(good[name] - bad[name]) for name in good)
        self.assertGreater(worst, 5.0)


class TestBoxingMeasuresTheSameBody(unittest.TestCase):

    def run_both(self, frames):
        """Play the same frames through a 16:9 analyzer and a 4:3 one."""
        wide_motion, crop_motion = BoxingMotionAnalyzer(), BoxingMotionAnalyzer()
        wide_result = crop_result = None
        for at, points in frames:
            person = wide(points)
            wide_result = wide_motion.update(person, at)
            crop_result = crop_motion.update(cropped(person), at)
        return wide_result, crop_result

    def test_the_torso_angle_and_span_are_the_camera_independent_ones(self):
        wide_result, crop_result = self.run_both([(1.0, DIAGONAL)])
        self.assertAlmostEqual(wide_result.posture["torso_angle"],
                               crop_result.posture["torso_angle"], places=1)
        self.assertAlmostEqual(wide_result.posture["shoulder_width"],
                               crop_result.posture["shoulder_width"], places=4)

    def test_a_duck_is_a_duck_in_either_camera(self):
        """The measure most exposed to the crop: vertical over horizontal."""
        ducked = dict(STANDING)
        ducked.update({"nose": (0.50, 0.30),
                       "left_shoulder": (0.43, 0.42),
                       "right_shoulder": (0.57, 0.42)})
        wide_result, crop_result = self.run_both(
            [(1.0, STANDING), (1.5, ducked)])
        self.assertIn("duck", wide_result.names)
        self.assertIn("duck", crop_result.names)


class TestTheGestureSurvivesTheCrop(unittest.TestCase):

    #: Arms folded into an X, wrists past the opposite shoulders and above
    #: their own elbows. Close enough to the limits that a 25% squeeze on one
    #: axis would change the answer.
    CROSSED = {
        "left_shoulder": (0.40, 0.36), "right_shoulder": (0.60, 0.36),
        "left_elbow": (0.36, 0.52), "right_elbow": (0.64, 0.52),
        "left_wrist": (0.62, 0.42), "right_wrist": (0.38, 0.42),
    }

    def test_crossed_arms_read_the_same_at_either_aspect(self):
        person = wide(self.CROSSED)
        self.assertTrue(arms_crossed(person))
        self.assertTrue(arms_crossed(cropped(person)))

    def test_arms_merely_down_are_refused_at_either_aspect(self):
        person = wide(STANDING)
        self.assertFalse(arms_crossed(person))
        self.assertFalse(arms_crossed(cropped(person)))


if __name__ == "__main__":
    unittest.main()
