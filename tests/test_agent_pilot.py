"""Hand control read off the accelerator instead of out of a browser.

The gesture rules are the same ones the browser version ended up with, tuned on
the device against real sweeps. What is different is where they run, and that
is the point: no video leaves Python, so none of the five faults that came out
of streaming the camera to a page can happen at all.

These tests drive `_read` with made-up pose frames, which is exactly what the
pose thread does thirty times a second. No camera, no accelerator, no socket.
"""

from __future__ import annotations

import unittest

from aipi5.agent.pilot import (DWELL_S, HandPilot, LOST_S, REACH,
                               SWEEP_SPEED, SWEEP_TRAVEL_X, SWEEP_TRAVEL_Y)


class Point:
    def __init__(self, x, y, confidence=0.9):
        self.x, self.y, self.confidence = x, y, confidence


class Person:
    """A body with both wrists up unless told otherwise."""

    def __init__(self, wrist=(0.5, 0.3), confidence=0.9, hips=0.7):
        self.keypoints = {
            "left_wrist": Point(*wrist, confidence),
            "right_wrist": Point(9.0, 9.0, 0.0),      # not believable
            "left_hip": Point(0.5, hips),
            "right_hip": Point(0.5, hips),
        }


class Frame:
    def __init__(self, person):
        self.person = person


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1 / 30):
        self.now += seconds


class Fixture(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sent = []
        self.pilot = HandPilot(object(), object(),
                               send=lambda g, at: self.sent.append((g, at)),
                               clock=self.clock)

    def wave(self, path, seconds=0.5, confidence=0.9):
        """Move the wrist along `path` over `seconds`, at 30 fps."""
        steps = max(2, int(seconds * 30))
        for i in range(steps + 1):
            t = i / steps
            x = path[0][0] + (path[1][0] - path[0][0]) * t
            y = path[0][1] + (path[1][1] - path[0][1]) * t
            self.pilot._read(Frame(Person((x, y), confidence)))
            self.clock.tick(seconds / steps)

    def hold(self, where, seconds):
        steps = max(2, int(seconds * 30))
        for _ in range(steps):
            self.pilot._read(Frame(Person(where)))
            self.clock.tick(seconds / steps)

    def gestures(self):
        return [g for g, _ in self.sent if g not in ("move", "hide")]


class TestSweeps(Fixture):
    """The four directions, from a wrist rather than from fingers."""

    def test_a_quick_sweep_up_scrolls_up(self):
        # Mirrored x, camera y grows downward: moving up the frame is a
        # falling y, and the page should move the way the hand does.
        self.wave([(0.5, 0.7), (0.5, 0.25)], seconds=0.35)
        self.assertIn("scroll_up", self.gestures())

    def test_a_quick_sweep_down_scrolls_down(self):
        self.wave([(0.5, 0.25), (0.5, 0.7)], seconds=0.35)
        self.assertIn("scroll_down", self.gestures())

    def test_sideways_goes_back_and_forward(self):
        """**Pose keypoints arrive mirrored**, so x already reads as the person
        does: growing x is their hand moving to their right.

        `HailoPose._person` applies `geometry.mirror` while decoding, and
        boxing.js warns in as many words against "applying a second mirror and
        swapping it". This module did exactly that, and left and right came out
        backwards -- which is what the test below now pins.
        """
        self.wave([(0.2, 0.4), (0.8, 0.4)], seconds=0.35)
        self.assertIn("forward", self.gestures(),
                      "hand moving to their right should go forward")

        self.setUp()
        self.wave([(0.8, 0.4), (0.2, 0.4)], seconds=0.35)
        self.assertIn("back", self.gestures(),
                      "hand moving to their left should go back")

    def test_the_pointer_follows_the_hand_rather_than_opposing_it(self):
        """The same double mirror moved the pointer the wrong way too, which is
        the half of the report that says "tracking motion"."""
        self.wave([(0.25, 0.4), (0.75, 0.4)], seconds=1.2)
        moves = [at for g, at in self.sent if g == "move"]
        self.assertGreater(len(moves), 3)
        self.assertGreater(moves[-1]["x"], moves[0]["x"],
                           "the pointer went the opposite way to the hand")

    def test_the_same_movement_done_slowly_is_not_a_sweep(self):
        """Aiming at a link crosses the same distance, given long enough.

        Distance alone cannot tell them apart, and the version that tried
        fired Back on somebody trying to point at something.
        """
        self.wave([(0.2, 0.4), (0.8, 0.4)], seconds=4.0)
        self.assertEqual(self.gestures(), [])

    def test_a_small_flick_is_not_a_sweep(self):
        self.wave([(0.48, 0.4), (0.55, 0.4)], seconds=0.2)
        self.assertEqual(self.gestures(), [])

    def test_sideways_is_held_to_a_higher_bar_than_up_and_down(self):
        """A scroll nobody meant is undone by scrolling back. A Back nobody
        meant loses the page they were reading."""
        self.assertGreater(SWEEP_TRAVEL_X, SWEEP_TRAVEL_Y)


class TestTheReturnStroke(Fixture):
    """The fault that made repeating a gesture impossible.

    An arm that has swept one way comes back, and the way back is a sweep in
    the other direction. A fixed lockout was tried and is the wrong shape: too
    long for a flick's return, far too short for somebody deliberately lifting
    their hand to the top of the frame to scroll down again.
    """

    def test_the_stroke_back_does_not_fire_the_opposite_gesture(self):
        self.wave([(0.5, 0.25), (0.5, 0.7)], seconds=0.35)     # scroll down
        self.wave([(0.5, 0.7), (0.5, 0.25)], seconds=0.35)     # arm returning
        self.assertEqual(self.gestures(), ["scroll_down"],
                         "the return stroke fired a gesture of its own")

    def test_and_a_deliberate_second_sweep_still_works(self):
        self.wave([(0.5, 0.25), (0.5, 0.7)], seconds=0.35)
        self.wave([(0.5, 0.7), (0.5, 0.25)], seconds=0.35)     # reposition
        self.hold((0.5, 0.25), 0.5)                            # and settle
        self.wave([(0.5, 0.25), (0.5, 0.7)], seconds=0.35)
        self.assertEqual(self.gestures(), ["scroll_down", "scroll_down"])


class TestDwellClicking(Fixture):
    """There is no fist, and the docstring in `pilot.py` says why.

    `hand_landmark_lite` is published for Hailo-8 only, every HAILO10H URL in
    the model zoo answers 403, and loading the Hailo-8 build on this chip
    returns HAILO_NOT_IMPLEMENTED -- measured, not assumed. Wrists are what the
    accelerator can see, so holding still is what a click has to be.
    """

    def test_holding_still_clicks(self):
        self.hold((0.5, 0.4), DWELL_S + 0.3)
        self.assertIn("click", self.gestures())

    def test_a_click_lands_where_the_hand_was(self):
        self.hold((0.35, 0.45), DWELL_S + 0.3)
        placed = [at for g, at in self.sent if g == "click"]
        self.assertTrue(placed)
        self.assertTrue(0.0 <= placed[0]["x"] <= 1.0)
        self.assertTrue(0.0 <= placed[0]["y"] <= 1.0)

    def test_a_hand_passing_through_does_not_click(self):
        self.wave([(0.2, 0.4), (0.8, 0.4)], seconds=DWELL_S * 2)
        self.assertNotIn("click", self.gestures())

    def test_a_click_is_visible_before_it_lands(self):
        """`hold` rides along with the pointer so the marker can close towards
        a fist as the dwell builds. A click nobody saw coming feels like a
        misfire even when it was aimed."""
        self.hold((0.5, 0.4), DWELL_S * 0.8)
        holds = [at.get("hold", 0) for g, at in self.sent if g == "move"]
        self.assertTrue(holds)
        self.assertGreater(max(holds), 0.4,
                           "the dwell built with no warning on the marker")
        self.assertLessEqual(max(holds), 1.0)

    def test_a_hand_left_where_it_clicked_does_not_click_again(self):
        """It has to go somewhere first. Resting is not choosing again."""
        self.hold((0.5, 0.4), DWELL_S + 0.3)
        self.assertEqual(self.gestures().count("click"), 1)
        self.hold((0.5, 0.4), DWELL_S * 3)
        self.assertEqual(self.gestures().count("click"), 1)

    def test_but_moving_away_and_settling_clicks_again(self):
        self.hold((0.5, 0.4), DWELL_S + 0.3)
        self.wave([(0.5, 0.4), (0.75, 0.6)], seconds=0.9)
        self.hold((0.75, 0.6), DWELL_S + 0.5)
        self.assertEqual(self.gestures().count("click"), 2)

    def test_dwell_is_long_enough_not_to_fire_on_a_pause(self):
        """The first version used nine hundred milliseconds and fired on a
        hand that had merely stopped on its way past something."""
        self.assertGreaterEqual(DWELL_S, 1.2)

    def test_resting_on_a_button_clicks_once(self):
        """Not four times. The hand has to leave and come back."""
        self.hold((0.5, 0.4), DWELL_S * 3)
        self.assertEqual(self.gestures().count("click"), 1)


class TestSteadiness(Fixture):
    """A wrist keypoint moves every frame even from a hand held still.

    The model re-estimates it from scratch thirty times a second, so an
    unfiltered pointer twitches constantly -- reported as the marker "jumping
    around itself". The browser version smoothed by exactly this much and that
    is the line that did not get ported.
    """

    def jitter(self, spread, seconds=2.0):
        """A hand held still, plus the noise a real keypoint carries."""
        import random

        rng = random.Random(7)
        steps = int(seconds * 30)
        for _ in range(steps):
            self.pilot._read(Frame(Person((0.5 + rng.uniform(-spread, spread),
                                           0.4 + rng.uniform(-spread, spread)))))
            self.clock.tick()

    def test_a_still_hand_gives_a_still_pointer(self):
        self.jitter(0.012)
        moves = [at for g, at in self.sent if g == "move"]
        if len(moves) < 2:
            return                                  # steadier than the test
        spread = max(abs(a["x"] - b["x"]) for a, b in zip(moves, moves[1:]))
        self.assertLess(spread, 0.03,
                        "the pointer jumped about under keypoint noise")

    def test_and_the_hand_is_still_followed(self):
        """Smoothing that hides a real movement is worse than the jitter."""
        self.wave([(0.3, 0.4), (0.7, 0.4)], seconds=1.5)
        moves = [at for g, at in self.sent if g == "move"]
        self.assertGreater(moves[-1]["x"] - moves[0]["x"], 0.35,
                           "smoothing swallowed most of the movement")

    def test_sweeps_are_measured_on_the_raw_trail(self):
        """Smoothing a fast stroke shortens it, and a sweep is judged on how
        far and how quickly it went. The trail stays unfiltered for that."""
        self.wave([(0.5, 0.25), (0.5, 0.7)], seconds=0.35)
        self.assertIn("scroll_down", self.gestures())


class TestThePointer(Fixture):
    def test_a_moving_hand_moves_the_pointer(self):
        self.wave([(0.35, 0.35), (0.6, 0.55)], seconds=1.0)
        moves = [at for g, at in self.sent if g == "move"]
        self.assertGreater(len(moves), 3)
        self.assertNotEqual((moves[0]["x"], moves[0]["y"]),
                            (moves[-1]["x"], moves[-1]["y"]))

    def test_the_pointer_never_leaves_the_page(self):
        self.wave([(-0.4, -0.4), (1.6, 1.6)], seconds=2.0)
        for _, at in self.sent:
            if at:
                self.assertTrue(0.0 <= at["x"] <= 1.0)
                self.assertTrue(0.0 <= at["y"] <= 1.0)

    def test_most_of_the_frame_is_usable(self):
        """The browser version trimmed 22% a side, so a hand in the outer
        margin clamped and the pointer froze while the arm kept moving --
        reported three times before it was found."""
        self.assertLessEqual(REACH, 0.12)

    def test_the_pointer_is_taken_away_when_the_hand_goes(self):
        self.hold((0.5, 0.4), 0.4)
        self.assertTrue([g for g, _ in self.sent if g == "move"])
        self.sent.clear()
        for _ in range(int((LOST_S + 0.3) * 30)):
            self.pilot._read(Frame(None))
            self.clock.tick()
        self.assertIn("hide", [g for g, _ in self.sent])

    def test_and_not_taken_away_twice(self):
        self.hold((0.5, 0.4), 0.4)
        for _ in range(int((LOST_S + 2.0) * 30)):
            self.pilot._read(Frame(None))
            self.clock.tick()
        self.assertEqual([g for g, _ in self.sent].count("hide"), 1)


class TestWhatIsNotDrivingAnything(Fixture):
    def test_an_arm_hanging_at_a_side_is_ignored(self):
        """Otherwise the pointer wanders whenever somebody walks past."""
        for _ in range(30):
            self.pilot._read(Frame(Person((0.5, 0.9), hips=0.7)))
            self.clock.tick()
        self.assertEqual(self.sent, [])

    def test_a_wrist_the_model_doubts_is_ignored(self):
        for _ in range(30):
            self.pilot._read(Frame(Person((0.5, 0.3), confidence=0.1)))
            self.clock.tick()
        self.assertEqual(self.sent, [])

    def test_no_person_is_not_a_crash(self):
        for _ in range(5):
            self.pilot._read(Frame(None))
            self.clock.tick()
        self.assertEqual(self.sent, [])

    def test_a_sender_that_throws_does_not_kill_the_pose_thread(self):
        """`_saw` is the accelerator's callback. An exception escaping it
        would take down the thread the motion games share."""
        def explode(gesture, at):
            raise RuntimeError("no")

        pilot = HandPilot(object(), object(), send=explode, clock=self.clock)
        for _ in range(40):
            pilot._saw(Frame(Person((0.5, 0.3))))
            self.clock.tick()


class TestWhatItSendsIsWhatTheAgentAccepts(unittest.TestCase):
    def test_every_gesture_it_can_send_is_one_the_runtime_knows(self):
        """The pilot is upstream of the same named-operation table the browser
        version used; a name it invents would be refused at the far end."""
        from aipi5.agent.runtime import AgentService

        known = set(AgentService.GESTURES)
        for name in ("scroll_up", "scroll_down", "back", "forward",
                     "click", "move", "hide"):
            with self.subTest(name=name):
                self.assertIn(name, known)


if __name__ == "__main__":
    unittest.main()
