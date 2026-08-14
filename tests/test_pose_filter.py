"""Smoothing, confidence gating and person selection — sections 13 to 15.

Section 46 asks specifically that *low-confidence wrist data must not generate
false cuts*, which is the reason `HandFilter` exists and the reason it is pure:
every case worth testing is about what happened on the previous frame, and none
of them are reachable through a camera.
"""

from __future__ import annotations

import unittest

from aipi5.motion.pose_filter import (HandFilter, PlayerSelector, readiness,
                                      SNAP_DISTANCE)
from aipi5.motion.pose_types import KEYPOINT_NAMES, KeyPoint, PersonPose, PoseFrame


def person(confidence: float = 0.9, box=(0.3, 0.2, 0.4, 0.7), **points):
    """A `PersonPose` with every keypoint present and named ones overridden.

    Keypoints are always complete in a real frame — the model reports all
    seventeen with a confidence rather than omitting the ones it doubts — so a
    helper that produced a partial dictionary would be testing a shape that
    never occurs.
    """
    keypoints = {name: KeyPoint(0.5, 0.5, 0.0) for name in KEYPOINT_NAMES}
    for name, value in points.items():
        keypoints[name] = KeyPoint(*value)
    return PersonPose(confidence=confidence, keypoints=keypoints, box=box)


class TestConfidenceGate(unittest.TestCase):
    """Section 15, and section 46's "must not generate false cuts"."""

    def setUp(self):
        self.filter = HandFilter(confidence=0.5, stale_s=0.25)

    def test_a_confident_wrist_is_accepted(self):
        hands = self.filter.update(person(left_wrist=(0.3, 0.4, 0.9)), now=1.0)
        self.assertTrue(hands["left_wrist"].live)

    def test_an_uncertain_wrist_is_never_accepted_in_the_first_place(self):
        hands = self.filter.update(person(left_wrist=(0.3, 0.4, 0.2)), now=1.0)
        self.assertFalse(hands["left_wrist"].live)
        self.assertEqual(hands["left_wrist"].confidence, 0.0)

    def test_a_low_confidence_jump_cannot_move_a_live_hand(self):
        """The false-cut case, exactly.

        A wrist the model is unsure about is still *reported*, at a position
        that is a guess. If that guess moved the hand, the segment from where
        the hand really was to where the model imagined it would sweep across
        the screen and slice everything on the way.
        """
        self.filter.update(person(left_wrist=(0.20, 0.50, 0.95)), now=1.0)
        hand = self.filter.hands["left_wrist"]
        before = (hand.x, hand.y)

        # A confident-looking position on the far side of the screen, but the
        # model does not believe it.
        self.filter.update(person(left_wrist=(0.90, 0.10, 0.15)), now=1.033)

        self.assertEqual((hand.x, hand.y), before)
        # And the slash segment has collapsed, so it cannot cut along the path
        # of its last real movement either.
        self.assertEqual((hand.previous_x, hand.previous_y), (hand.x, hand.y))
        self.assertEqual(hand.travel, 0.0)

    def test_one_dropped_frame_does_not_blink_the_hand_out(self):
        self.filter.update(person(left_wrist=(0.3, 0.4, 0.9)), now=1.0)
        self.filter.update(person(left_wrist=(0.3, 0.4, 0.1)), now=1.033)
        self.assertTrue(self.filter.hands["left_wrist"].live)

    def test_but_it_does_not_coast_indefinitely(self):
        """Section 15: an old coordinate must not remain forever."""
        self.filter.update(person(left_wrist=(0.3, 0.4, 0.9)), now=1.0)
        self.filter.update(person(left_wrist=(0.3, 0.4, 0.1)), now=1.20)
        self.assertTrue(self.filter.hands["left_wrist"].live)

        self.filter.update(person(left_wrist=(0.3, 0.4, 0.1)), now=1.40)
        self.assertFalse(self.filter.hands["left_wrist"].live)

    def test_no_person_at_all_invalidates_both_hands(self):
        self.filter.update(person(left_wrist=(0.3, 0.4, 0.9),
                                  right_wrist=(0.7, 0.4, 0.9)), now=1.0)
        self.filter.update(None, now=1.5)
        self.assertEqual(self.filter.live_hands, [])

    def test_a_hand_returning_does_not_slash_across_the_screen(self):
        """A reappearing hand must have a zero-length segment.

        Otherwise it draws a blade from where it was lost to where it came
        back, which cuts everything in between — and hands are lost and found
        exactly when somebody is waving them about.
        """
        self.filter.update(person(left_wrist=(0.05, 0.9, 0.95)), now=1.0)
        self.filter.update(None, now=1.5)          # lost, and now stale
        self.filter.update(person(left_wrist=(0.95, 0.1, 0.95)), now=2.0)

        hand = self.filter.hands["left_wrist"]
        self.assertTrue(hand.live)
        self.assertTrue(hand.reappeared)
        self.assertEqual(hand.travel, 0.0)

    def test_reappeared_clears_on_the_next_good_frame(self):
        self.filter.update(person(left_wrist=(0.5, 0.5, 0.9)), now=1.0)
        self.assertTrue(self.filter.hands["left_wrist"].reappeared)
        self.filter.update(person(left_wrist=(0.52, 0.5, 0.9)), now=1.033)
        self.assertFalse(self.filter.hands["left_wrist"].reappeared)


class TestSmoothing(unittest.TestCase):
    """Section 14: enough to stop the shiver, not enough to feel."""

    def test_small_movements_are_smoothed(self):
        filt = HandFilter(confidence=0.5, smoothing=0.5)
        filt.update(person(left_wrist=(0.500, 0.5, 0.9)), now=1.0)
        filt.update(person(left_wrist=(0.520, 0.5, 0.9)), now=1.033)
        # Halfway, because alpha is 0.5.
        self.assertAlmostEqual(filt.hands["left_wrist"].x, 0.510)

    def test_smoothing_converges_rather_than_lagging_forever(self):
        filt = HandFilter(confidence=0.5, smoothing=0.55)
        filt.update(person(left_wrist=(0.50, 0.5, 0.9)), now=1.0)
        now = 1.0
        for _ in range(6):
            now += 0.033
            filt.update(person(left_wrist=(0.55, 0.5, 0.9)), now=now)
        self.assertAlmostEqual(filt.hands["left_wrist"].x, 0.55, places=2)

    def test_a_real_slash_is_not_rounded_off(self):
        """Fast movement snaps instead of averaging. Section 14's warning.

        Smoothing a genuine slash with where the hand used to be shortens
        every fast movement — which is every movement the game is about.
        """
        filt = HandFilter(confidence=0.5, smoothing=0.55)
        filt.update(person(left_wrist=(0.20, 0.5, 0.9)), now=1.0)
        filt.update(person(left_wrist=(0.60, 0.5, 0.9)), now=1.033)

        hand = filt.hands["left_wrist"]
        self.assertAlmostEqual(hand.x, 0.60)
        self.assertAlmostEqual(hand.previous_x, 0.20)
        self.assertGreater(hand.travel, SNAP_DISTANCE)

    def test_speed_is_per_second_not_per_frame(self):
        filt = HandFilter(confidence=0.5)
        filt.update(person(left_wrist=(0.2, 0.5, 0.9)), now=1.0)
        filt.update(person(left_wrist=(0.5, 0.5, 0.9)), now=1.1)
        hand = filt.hands["left_wrist"]
        self.assertAlmostEqual(hand.speed(0.1), 3.0, places=5)

    def test_speed_of_a_zero_interval_is_not_a_division_by_zero(self):
        filt = HandFilter(confidence=0.5)
        filt.update(person(left_wrist=(0.2, 0.5, 0.9)), now=1.0)
        self.assertEqual(filt.hands["left_wrist"].speed(0.0), 0.0)


class TestPlayerSelection(unittest.TestCase):
    """Section 13: pick a player and do not keep changing your mind."""

    def frame(self, *boxes):
        return PoseFrame(timestamp=0.0,
                         persons=tuple(person(box=box) for box in boxes))

    def test_the_largest_person_is_the_player(self):
        selector = PlayerSelector()
        chosen = selector.choose(self.frame((0.0, 0.0, 0.2, 0.3),
                                            (0.5, 0.1, 0.4, 0.7)))
        self.assertAlmostEqual(chosen.box[2], 0.4)

    def test_nobody_in_shot_is_nobody(self):
        self.assertIsNone(PlayerSelector().choose(PoseFrame(timestamp=0.0)))

    def test_a_bystander_does_not_steal_the_game(self):
        """A slightly bigger passer-by must not take the sword mid-slash."""
        selector = PlayerSelector()
        player = (0.4, 0.1, 0.35, 0.7)
        selector.choose(self.frame(player))

        for _ in range(10):
            chosen = selector.choose(self.frame(player, (0.05, 0.1, 0.37, 0.72)))
            self.assertAlmostEqual(chosen.box[0], 0.4)

    def test_a_decisively_bigger_person_eventually_takes_over(self):
        selector = PlayerSelector()
        player = (0.4, 0.1, 0.20, 0.40)
        challenger = (0.05, 0.05, 0.45, 0.85)
        selector.choose(self.frame(player))

        for _ in range(30):
            chosen = selector.choose(self.frame(player, challenger))
        self.assertAlmostEqual(chosen.box[0], 0.05)

    def test_a_player_lost_for_one_frame_is_not_forgotten(self):
        selector = PlayerSelector()
        player = (0.4, 0.1, 0.35, 0.7)
        selector.choose(self.frame(player))
        selector.choose(PoseFrame(timestamp=0.0))
        chosen = selector.choose(self.frame(player, (0.05, 0.1, 0.36, 0.71)))
        self.assertAlmostEqual(chosen.box[0], 0.4)

    def test_a_player_gone_for_a_long_time_is_forgotten(self):
        selector = PlayerSelector()
        selector.choose(self.frame((0.4, 0.1, 0.35, 0.7)))
        for _ in range(20):
            selector.choose(PoseFrame(timestamp=0.0))
        chosen = selector.choose(self.frame((0.05, 0.1, 0.30, 0.60),
                                            (0.5, 0.1, 0.31, 0.61)))
        self.assertIsNotNone(chosen)


class TestReadiness(unittest.TestCase):
    """Section 20 and 21: what the start screen says, and when START lights."""

    def ready_person(self):
        return person(box=(0.3, 0.2, 0.35, 0.6),
                      left_shoulder=(0.4, 0.35, 0.9),
                      right_shoulder=(0.6, 0.35, 0.9),
                      left_wrist=(0.35, 0.5, 0.9),
                      right_wrist=(0.65, 0.5, 0.9))

    def test_nobody_there(self):
        state = readiness(None, 0.45)
        self.assertFalse(state["ready"])
        self.assertFalse(state["player"])
        self.assertIn("camera", state["advice"])

    def test_a_complete_upper_body_is_ready(self):
        state = readiness(self.ready_person(), 0.45)
        self.assertTrue(state["ready"])

    def test_missing_shoulders_asks_them_to_move_back(self):
        subject = person(box=(0.3, 0.2, 0.35, 0.6),
                         left_shoulder=(0.4, 0.35, 0.1),
                         right_shoulder=(0.6, 0.35, 0.1),
                         left_wrist=(0.35, 0.5, 0.9),
                         right_wrist=(0.65, 0.5, 0.9))
        state = readiness(subject, 0.45)
        self.assertFalse(state["ready"])
        self.assertIn("back", state["advice"])

    def test_one_hand_hidden_says_which(self):
        subject = person(box=(0.3, 0.2, 0.35, 0.6),
                         left_shoulder=(0.4, 0.35, 0.9),
                         right_shoulder=(0.6, 0.35, 0.9),
                         left_wrist=(0.35, 0.5, 0.9),
                         right_wrist=(0.65, 0.5, 0.1))
        state = readiness(subject, 0.45)
        self.assertFalse(state["ready"])
        self.assertIn("other hand", state["advice"])

    def test_no_hands_at_all_says_both(self):
        subject = person(box=(0.3, 0.2, 0.35, 0.6),
                         left_shoulder=(0.4, 0.35, 0.9),
                         right_shoulder=(0.6, 0.35, 0.9),
                         left_wrist=(0.35, 0.5, 0.1),
                         right_wrist=(0.65, 0.5, 0.1))
        self.assertIn("Both hands", readiness(subject, 0.45)["advice"])

    def test_standing_too_close_leaves_no_room_to_swing(self):
        subject = person(box=(0.05, 0.02, 0.9, 0.95),
                         left_shoulder=(0.3, 0.3, 0.9),
                         right_shoulder=(0.7, 0.3, 0.9),
                         left_wrist=(0.2, 0.6, 0.9),
                         right_wrist=(0.8, 0.6, 0.9))
        state = readiness(subject, 0.45)
        self.assertFalse(state["ready"])
        self.assertIn("room", state["advice"])


if __name__ == "__main__":
    unittest.main()
