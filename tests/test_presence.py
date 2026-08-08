"""The presence debounce and the screensaver timing.

These are the rules from sections 21, 25 and 26, and every one of them is
about a *sequence* of frames over time — which is exactly what cannot be
checked by standing in front of the camera and watching. Walking out of shot
for eight frames and back in for two is a repeatable test here and an
unrepeatable afternoon on the device.
"""

from __future__ import annotations

import unittest

from aipi5.core.presence import (Presence, PresenceTracker, ScreensaverPolicy)


class TestPresenceTracker(unittest.TestCase):

    def setUp(self):
        # The shipped configuration: quick to notice somebody arriving, slow to
        # decide they have gone.
        self.tracker = PresenceTracker(frames_to_appear=2, frames_to_disappear=8)

    def feed(self, pattern: str):
        """Run a frame sequence. '#' is a person, '.' is not.

        Returns the events, so a test can assert on how many changes a
        sequence produced as well as on where it ended up.
        """
        events = []
        for index, char in enumerate(pattern):
            event = self.tracker.observe(char == "#", now=float(index))
            if event is not None:
                events.append(event)
        return events

    def test_starts_unknown(self):
        # Not "absent". A detector that has seen nothing has not observed an
        # empty room, and starting at absent would begin the screensaver
        # countdown before the first frame.
        self.assertIs(self.tracker.state, Presence.UNKNOWN)

    def test_one_frame_is_not_enough_to_arrive(self):
        self.assertEqual(self.feed("#"), [])
        self.assertIs(self.tracker.state, Presence.UNKNOWN)

    def test_two_frames_arrive(self):
        events = self.feed("##")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].arrived)
        self.assertIs(self.tracker.state, Presence.PERSON_PRESENT)

    def test_a_dropped_frame_does_not_take_the_screen_away(self):
        # The case the whole class exists for. A detector that is right 95% of
        # the time drops one frame in twenty; without the debounce that is a
        # screensaver flicking on and off while somebody sits still.
        events = self.feed("#####.#####")
        self.assertEqual(len(events), 1, "only the arrival should be reported")
        self.assertIs(self.tracker.state, Presence.PERSON_PRESENT)

    def test_leaving_needs_the_full_run(self):
        self.feed("##")
        # Seven is one short of the eight the configuration asks for.
        self.assertEqual(self.feed("......."), [])
        self.assertIs(self.tracker.state, Presence.PERSON_PRESENT)
        events = self.feed(".")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].left)

    def test_the_run_must_be_consecutive(self):
        # Eight absent frames in total, but never eight in a row. The person is
        # still there — this is somebody moving around a room, not leaving it.
        self.feed("##")
        self.feed("....#....#" "...")
        self.assertIs(self.tracker.state, Presence.PERSON_PRESENT)

    def test_no_event_when_the_answer_has_not_changed(self):
        self.feed("##")
        events = self.feed("##########")
        self.assertEqual(events, [], "already present; nothing changed")

    def test_reset_goes_back_to_unknown(self):
        self.feed("##")
        self.tracker.reset()
        self.assertIs(self.tracker.state, Presence.UNKNOWN)
        # And the streak is gone with it: a single frame after a reset must
        # not complete a run that was started before it.
        self.assertEqual(self.feed("#"), [])

    def test_a_state_change_needs_evidence(self):
        with self.assertRaises(ValueError):
            PresenceTracker(frames_to_appear=0, frames_to_disappear=8)


class TestScreensaverPolicy(unittest.TestCase):

    def setUp(self):
        self.policy = ScreensaverPolicy(timeout_seconds=60.0, enabled=True)
        self.tracker = PresenceTracker(2, 2)

    def leave(self, at: float):
        self.tracker.observe(True, now=at)
        self.tracker.observe(True, now=at)
        self.tracker.observe(False, now=at)
        event = self.tracker.observe(False, now=at)
        self.policy.presence_changed(event)
        return event

    def arrive(self, at: float):
        self.tracker.observe(True, now=at)
        event = self.tracker.observe(True, now=at)
        self.policy.presence_changed(event)
        return event

    def test_nothing_shows_before_anybody_has_left(self):
        self.assertFalse(self.policy.should_show(now=10_000))

    def test_the_timeout_is_from_the_moment_presence_was_lost(self):
        self.leave(at=100.0)
        self.assertFalse(self.policy.should_show(now=159.0))
        self.assertTrue(self.policy.should_show(now=160.0))

    def test_returning_takes_it_down_at_once(self):
        self.leave(at=100.0)
        self.assertTrue(self.policy.should_show(now=200.0))
        self.arrive(at=201.0)
        # No timer, no touch, nothing to wait for — section 26.
        self.assertFalse(self.policy.showing)
        self.assertFalse(self.policy.should_show(now=201.0))

    def test_leaving_again_restarts_the_countdown(self):
        self.leave(at=100.0)
        self.policy.should_show(now=200.0)
        self.arrive(at=201.0)
        self.leave(at=202.0)
        self.assertFalse(self.policy.should_show(now=250.0),
                         "the clock should have restarted at 202, not run on from 100")
        self.assertTrue(self.policy.should_show(now=262.0))

    def test_speaking_from_out_of_shot_takes_it_down(self):
        # A voice in the room is proof of a person in it whatever the camera
        # believes, and answering onto a clock face is answering into the
        # wrong screen.
        self.leave(at=100.0)
        self.assertTrue(self.policy.should_show(now=200.0))
        self.policy.suppress(now=200.0)
        self.assertFalse(self.policy.should_show(now=201.0))
        # Not straight back on the next poll, either — the countdown restarts
        # from the activity rather than resuming where it was.
        self.assertFalse(self.policy.should_show(now=250.0))

    def test_it_comes_back_after_activity_in_an_empty_room(self):
        # The defect this pins, found on the device: `suppress` used to clear
        # the countdown outright, which reads as "wait for presence to say the
        # room is empty again" — except presence had already said so, and the
        # tracker only reports *changes*. One spoken command in an empty room
        # therefore took the screensaver away permanently. Verified on the Pi:
        # still showing the full UI to nobody 75 seconds later.
        self.leave(at=100.0)
        self.assertTrue(self.policy.should_show(now=200.0))
        self.policy.suppress(now=200.0)
        self.assertFalse(self.policy.should_show(now=259.0))
        self.assertTrue(self.policy.should_show(now=260.0),
                        "60s after the activity, the empty room sleeps again")

    def test_activity_with_somebody_present_starts_no_countdown(self):
        # The other half. Somebody standing in front of the camera must not
        # have the screensaver appear on them sixty seconds after they spoke.
        self.arrive(at=100.0)
        self.policy.suppress(now=100.0, person_present=True)
        self.assertFalse(self.policy.should_show(now=1000.0))

    def test_disabling_takes_down_one_that_is_already_up(self):
        self.leave(at=100.0)
        self.assertTrue(self.policy.should_show(now=200.0))
        self.policy.enabled = False
        self.assertFalse(self.policy.should_show(now=201.0))


if __name__ == "__main__":
    unittest.main()
