"""Slash-versus-fruit — section 46's collision requirement.

The headline case is the one section 16 names: *"slash passes through fruit"
even if neither endpoint is inside the fruit*. At 30 Hz a hand crosses a
watermelon between samples more often than not, so a point test would make a
hard, accurate swing score nothing — the worst possible failure for a game
about swinging hard.
"""

from __future__ import annotations

import math
import unittest

from aipi5.games.fruit_ninja import collision


class TestSegmentThroughCircle(unittest.TestCase):

    def test_slash_passes_clean_through(self):
        """Section 46, stated exactly. Neither end is inside the fruit."""
        # Fruit of radius 50 at (500, 400). The hand goes from well left to
        # well right of it, straight through the middle.
        self.assertTrue(collision.segment_hits_circle(
            300, 400, 700, 400, 500, 400, 50))

        # And neither endpoint is anywhere near it, which is the whole point.
        self.assertGreater(math.hypot(300 - 500, 400 - 400), 50)
        self.assertGreater(math.hypot(700 - 500, 400 - 400), 50)

    def test_a_fast_hand_still_connects(self):
        """250 px between samples, a 58 px watermelon, a hit that must count."""
        self.assertTrue(collision.segment_hits_circle(
            400, 300, 650, 300, 525, 300, 58))

    def test_a_point_test_would_have_missed_that(self):
        """Proof the segment test is earning its keep, not just passing."""
        for x, y in ((400, 300), (650, 300)):
            self.assertGreater(math.hypot(x - 525, y - 300), 58)

    def test_a_miss_is_a_miss(self):
        self.assertFalse(collision.segment_hits_circle(
            300, 100, 700, 100, 500, 400, 50))

    def test_just_grazing_counts(self):
        # Closest approach exactly equals the radius.
        self.assertTrue(collision.segment_hits_circle(
            0, 350, 1000, 350, 500, 400, 50))

    def test_just_outside_does_not(self):
        self.assertFalse(collision.segment_hits_circle(
            0, 349, 1000, 349, 500, 400, 50))

    def test_the_segment_is_not_an_infinite_line(self):
        """A slash at the top must not cut a fruit at the bottom.

        The line through this segment passes exactly through the fruit; the
        segment stops 200 px short of it. Using the line instead of the
        segment would look like the game cutting things for you.
        """
        self.assertFalse(collision.segment_hits_circle(
            100, 400, 300, 400, 500, 400, 50))
        # ... and the same slash extended does hit, which shows the fruit was
        # on the line all along.
        self.assertTrue(collision.segment_hits_circle(
            100, 400, 600, 400, 500, 400, 50))

    def test_a_stationary_hand_inside_the_fruit_hits(self):
        self.assertTrue(collision.segment_hits_circle(
            500, 400, 500, 400, 500, 400, 50))

    def test_a_stationary_hand_outside_does_not(self):
        self.assertFalse(collision.segment_hits_circle(
            100, 100, 100, 100, 500, 400, 50))

    def test_a_diagonal_slash(self):
        self.assertTrue(collision.segment_hits_circle(
            400, 300, 600, 500, 500, 400, 30))

    def test_starting_inside_and_leaving(self):
        self.assertTrue(collision.segment_hits_circle(
            500, 400, 900, 400, 500, 400, 50))


class TestClosestDistance(unittest.TestCase):

    def test_perpendicular_distance_when_the_foot_is_on_the_segment(self):
        self.assertAlmostEqual(
            collision.closest_distance(0, 0, 100, 0, 50, 30), 30.0)

    def test_clamps_to_the_near_end(self):
        self.assertAlmostEqual(
            collision.closest_distance(0, 0, 100, 0, -30, 0), 30.0)

    def test_clamps_to_the_far_end(self):
        self.assertAlmostEqual(
            collision.closest_distance(0, 0, 100, 0, 130, 0), 30.0)

    def test_zero_length_segment_is_a_point(self):
        self.assertAlmostEqual(
            collision.closest_distance(10, 10, 10, 10, 13, 14), 5.0)


class TestBothMoving(unittest.TestCase):
    """The fruit moves too, so the honest test is path against path."""

    def test_hand_and_fruit_crossing(self):
        """The satisfying shot: a fruit rising into a hand coming down.

        At neither end of the interval are the two in the same place — only in
        between. Treating the fruit as stationary at either instant misses it.
        """
        hand_from, hand_to = (500, 200), (500, 600)
        fruit_from, fruit_to = (300, 400), (700, 400)

        self.assertTrue(collision.slash_hits_fruit(
            hand_from, hand_to, fruit_from, fruit_to, 40))

    def test_that_case_would_be_missed_if_the_fruit_were_treated_as_still(self):
        hand_from, hand_to = (500, 200), (500, 600)
        for fruit in ((300, 400), (700, 400)):
            self.assertFalse(collision.segment_hits_circle(
                *hand_from, *hand_to, *fruit, 40))

    def test_travelling_together_never_touches(self):
        """A hand keeping pace beside a fruit does not slice it."""
        self.assertFalse(collision.slash_hits_fruit(
            (100, 100), (500, 500), (300, 100), (700, 500), 40))

    def test_a_fruit_that_is_not_moving_behaves_like_the_simple_case(self):
        self.assertTrue(collision.slash_hits_fruit(
            (300, 400), (700, 400), (500, 400), (500, 400), 50))

    def test_a_clear_miss_with_both_moving(self):
        self.assertFalse(collision.slash_hits_fruit(
            (0, 0), (100, 0), (600, 600), (700, 600), 40))


class TestSlashAngle(unittest.TestCase):

    def test_horizontal(self):
        self.assertAlmostEqual(collision.slash_angle((0, 0), (10, 0)), 0.0)

    def test_vertical(self):
        self.assertAlmostEqual(collision.slash_angle((0, 0), (0, 10)),
                               math.pi / 2)

    def test_a_hand_that_did_not_move_is_not_an_error(self):
        self.assertEqual(collision.slash_angle((5, 5), (5, 5)), 0.0)


if __name__ == "__main__":
    unittest.main()
