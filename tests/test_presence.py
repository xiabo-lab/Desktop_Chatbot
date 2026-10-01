"""The presence debounce and the screensaver timing.

These are the rules from sections 21, 25 and 26, and every one of them is
about a *sequence* of frames over time — which is exactly what cannot be
checked by standing in front of the camera and watching. Walking out of shot
for eight frames and back in for two is a repeatable test here and an
unrepeatable afternoon on the device.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.core.presence import (Presence, PresenceEvent, PresenceTracker,
                                 ScreensaverPolicy)
from aipi5.vision.person_detection import PresenceWatcher


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


class TestStagedIdlePolicy(unittest.TestCase):
    """Camera, idle screen, and monitor use one activity timestamp."""

    def setUp(self):
        self.policy = ScreensaverPolicy(
            timeout_seconds=600.0,
            camera_timeout_seconds=60.0,
            display_off_seconds=1800.0,
            wake_grace_seconds=600.0,
        )
        self.policy.presence_changed(
            PresenceEvent(Presence.PERSON_PRESENT,
                          Presence.PERSON_NOT_PRESENT, 100.0))

    def test_the_three_edges_are_1_10_and_30_minutes(self):
        self.assertFalse(self.policy.should_release_camera(now=159.9))
        self.assertTrue(self.policy.should_release_camera(now=160.0))
        self.assertFalse(self.policy.should_show(now=699.9))
        self.assertTrue(self.policy.should_show(now=700.0))
        self.assertFalse(self.policy.should_power_off_display(now=1899.9))
        self.assertTrue(self.policy.should_power_off_display(now=1900.0))

    def test_activity_resets_all_three_edges(self):
        self.policy.should_show(now=700.0)
        self.policy.suppress(now=1000.0)
        self.assertFalse(self.policy.should_release_camera(now=1059.9))
        self.assertTrue(self.policy.should_release_camera(now=1060.0))
        self.assertFalse(self.policy.should_show(now=1599.9))
        self.assertTrue(self.policy.should_show(now=1600.0))
        self.assertFalse(self.policy.should_power_off_display(now=2799.9))
        self.assertTrue(self.policy.should_power_off_display(now=2800.0))

    def test_known_presence_cancels_every_idle_stage(self):
        self.policy.presence_changed(
            PresenceEvent(Presence.PERSON_NOT_PRESENT,
                          Presence.PERSON_PRESENT, 150.0))
        self.assertFalse(self.policy.should_release_camera(now=10_000.0))
        self.assertFalse(self.policy.should_show(now=10_000.0))
        self.assertFalse(self.policy.should_power_off_display(now=10_000.0))


class _CountingCamera:
    """A camera that only counts how often it was read."""

    def __init__(self):
        self.reads = 0
        self.first = threading.Event()

    def frame(self):
        self.reads += 1
        self.first.set()
        return object()


class _QuietDetector:
    name = "fake"

    def available(self):
        return True

    def detect(self, frame):
        return False, 0.0

    def close(self):
        pass


class TestParkingTheWatcher(unittest.TestCase):
    """The detector loop stopping, so the camera can be released.

    Threaded, and there is no way around that: the promise being checked is that
    `pause` does not return while a read is in flight, and "in flight" is a
    property of a thread. It is the reason the camera can be closed at all — a
    frame fetched a moment after the release would reopen the device, because
    `Camera.frame` deliberately wakes a sleeping camera.
    """

    def setUp(self):
        self.camera = _CountingCamera()
        self.tracker = PresenceTracker(2, 2)
        self.watcher = PresenceWatcher(self.camera, _QuietDetector(),
                                       self.tracker, interval_ms=5)
        self.addCleanup(self.watcher.stop)

    def run_until_reading(self):
        self.watcher.start()
        self.assertTrue(self.camera.first.wait(2.0), "the loop never read a frame")

    def wait_for_reads_above(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.camera.reads > count:
                return True
            time.sleep(0.005)
        return False

    def test_nothing_is_read_once_pause_has_returned(self):
        self.run_until_reading()
        self.watcher.pause("the screen is asleep")
        settled = self.camera.reads
        # Twenty intervals. If a read were still in flight, or the loop looked
        # once more before noticing, it would land inside this window.
        time.sleep(0.1)
        self.assertEqual(self.camera.reads, settled)
        self.assertTrue(self.watcher.paused)

    def test_resuming_looks_again(self):
        self.run_until_reading()
        self.watcher.pause("the screen is asleep")
        settled = self.camera.reads
        self.watcher.resume()
        self.assertTrue(self.wait_for_reads_above(settled),
                        "an unparked watcher has to start reading again")
        self.assertFalse(self.watcher.paused)

    def test_resuming_forgets_what_the_empty_room_looked_like(self):
        """Otherwise the first person to walk up produces no *change*.

        The tracker only reports transitions. Coming back from a park with
        `PERSON_NOT_PRESENT` still standing means somebody arriving is a change
        from absent to present — which is fine — but coming back with
        `PERSON_PRESENT` standing, from a park that began while somebody was
        there, means the empty room they left behind is never reported.
        """
        self.tracker.observe(True, now=0.0)
        self.tracker.observe(True, now=0.1)
        self.assertIs(self.tracker.state, Presence.PERSON_PRESENT)
        self.watcher.pause("the screen is asleep")
        self.watcher.resume()
        self.assertIs(self.tracker.state, Presence.UNKNOWN)

    def test_pausing_twice_is_harmless(self):
        self.watcher.pause("once")
        self.watcher.pause("twice")
        self.assertTrue(self.watcher.paused)

    def test_a_parked_watcher_says_so(self):
        self.watcher.pause("the screen is asleep and nobody is there")
        described = self.watcher.describe()
        self.assertTrue(described["paused"])
        self.assertEqual(described["paused_because"],
                         "the screen is asleep and nobody is there")

    def test_stopping_a_parked_watcher_does_not_wait_it_out(self):
        self.run_until_reading()
        self.watcher.pause("the screen is asleep")
        started = time.monotonic()
        self.watcher.stop()
        self.assertLess(time.monotonic() - started, self.watcher.PAUSED_POLL_S,
                        "stop must release the parked loop rather than wait for "
                        "its next look-up")


class TestTheGraceAfterATouch(unittest.TestCase):
    """The second countdown, and why it cannot be the first one.

    This exists because the screensaver now takes the camera with it. Waking is
    a touch, the camera reopens, and the detector has to be given long enough to
    find somebody before its silence is allowed to put the screen back to sleep.
    Five minutes against sixty seconds, and the two are not interchangeable.
    """

    def setUp(self):
        self.policy = ScreensaverPolicy(timeout_seconds=60.0, enabled=True,
                                        wake_grace_seconds=300.0)
        self.tracker = PresenceTracker(2, 8)

    def observe(self, seen: bool, times: int, at: float):
        """Feed the tracker and forward whatever it decides to the policy."""
        for _ in range(times):
            event = self.tracker.observe(seen, now=at)
            if event is not None:
                self.policy.presence_changed(event)

    def empty_room_at_boot(self, at: float = 10.0):
        self.observe(False, 8, at)

    def test_a_boot_into_an_empty_room_uses_the_ordinary_timeout(self):
        """Nobody has touched anything, so there is nobody to keep waiting."""
        self.empty_room_at_boot(at=10.0)
        self.assertFalse(self.policy.should_show(now=69.0))
        self.assertTrue(self.policy.should_show(now=70.0))

    def test_a_touch_buys_the_full_grace(self):
        self.empty_room_at_boot(at=10.0)
        self.assertTrue(self.policy.should_show(now=100.0))
        self.policy.suppress(now=100.0)
        self.assertFalse(self.policy.should_show(now=399.0))
        self.assertTrue(self.policy.should_show(now=400.0))

    def test_the_detectors_first_verdict_does_not_cut_the_grace_short(self):
        """The defect the grace was added and then immediately lost to.

        A touch wakes the screen; the watcher is unparked and its tracker reset
        to UNKNOWN; four seconds later it reports its first opinion, which in an
        empty room is "nobody". That is not somebody leaving, and treating it as
        one replaced the five minutes with sixty seconds — so the screen went
        dark a minute after being touched, in front of whoever touched it.
        """
        self.empty_room_at_boot(at=10.0)
        self.policy.should_show(now=100.0)
        self.policy.suppress(now=100.0)

        # The unpark: back to UNKNOWN, then eight frames of nobody.
        self.tracker.reset()
        self.observe(False, 8, at=104.0)

        self.assertFalse(self.policy.should_show(now=200.0),
                         "sixty seconds after the touch the screen is still up")
        self.assertFalse(self.policy.should_show(now=399.0))
        self.assertTrue(self.policy.should_show(now=400.0),
                        "five minutes after the touch, and not before")

    def test_somebody_found_during_the_grace_stops_the_countdown(self):
        self.empty_room_at_boot(at=10.0)
        self.policy.should_show(now=100.0)
        self.policy.suppress(now=100.0)
        self.tracker.reset()
        self.observe(True, 2, at=105.0)
        self.assertFalse(self.policy.should_show(now=10_000.0))

    def test_and_leaving_afterwards_is_the_short_countdown_again(self):
        """The grace is for the uncertainty, not for the room."""
        self.empty_room_at_boot(at=10.0)
        self.policy.suppress(now=100.0)
        self.tracker.reset()
        self.observe(True, 2, at=105.0)
        self.observe(False, 8, at=200.0)
        self.assertFalse(self.policy.should_show(now=259.0))
        self.assertTrue(self.policy.should_show(now=260.0),
                        "they were seen leaving, so sixty seconds is enough")

    def test_the_grace_defaults_to_the_timeout(self):
        """A deployment that never sets it behaves as it always did."""
        policy = ScreensaverPolicy(timeout_seconds=45.0)
        self.assertEqual(policy.wake_grace_seconds, 45.0)


if __name__ == "__main__":
    unittest.main()
