"""Borrowing the camera for hand control, and giving it back.

The feature shipped reading the assistant's own preview and was reported as
unresponsive. It was: timed inside the assistant, a preview frame cost 421 ms,
of which 278 ms was waiting for the camera lock behind the presence detector.
2.4 frames a second, against a sweep of the hand that lasts about one.

So this borrows the device the way a game does. What the tests here are about
is the borrowing, because that is the part that can go wrong quietly: a camera
that is never given back is a device with no presence detection and no video
calls until somebody restarts the assistant.
"""

from __future__ import annotations

import unittest
from unittest import mock

from aipi5.agent.hands import HandFeed, LINGER_S


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeLease:
    """Stands in for `CameraLease`, counting what it was asked to do."""

    instances = []

    def __init__(self, camera, *, borrower=""):
        self.camera = camera
        self.borrower = borrower
        self.acquired = 0
        self.released = 0
        self.fail = None
        FakeLease.instances.append(self)

    def acquire(self):
        if self.fail:
            raise self.fail
        self.acquired += 1
        self.camera.lent = self.borrower

    def release(self):
        self.released += 1
        self.camera.lent = ""

    def preview_jpeg(self, width=480):
        return b"\xff\xd8jpeg"

    def describe(self):
        return {"fps": 30}


class FakeCamera:
    def __init__(self):
        self.lent = ""


class FakeScreen:
    def __init__(self):
        self.held_by = ""
        self.holds = []

    def hold(self, why):
        self.held_by = why
        self.holds.append(("hold", why))

    def release(self, why=""):
        if why and why != self.held_by:
            return
        self.held_by = ""
        self.holds.append(("release", why))


class Fixture(unittest.TestCase):
    def setUp(self):
        FakeLease.instances = []
        self.clock = FakeClock()
        self.camera = FakeCamera()
        self.screen = FakeScreen()
        patcher = mock.patch("aipi5.motion.camera_lease.CameraLease", FakeLease)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.feed = HandFeed(self.camera, screen=self.screen, clock=self.clock)

    def lease(self):
        return FakeLease.instances[-1] if FakeLease.instances else None


class TestTakingAndGivingBack(Fixture):
    def test_nothing_happens_until_a_browser_is_open(self):
        """A camera taken speculatively is a camera taken from the detector."""
        for _tick in range(5):
            self.feed.sync(False)
        self.assertFalse(self.feed.active)
        self.assertEqual(FakeLease.instances, [])
        self.assertEqual(self.camera.lent, "")

    def test_a_browser_takes_the_camera_once(self):
        for _tick in range(4):
            self.feed.sync(True)
        self.assertTrue(self.feed.active)
        self.assertEqual(len(FakeLease.instances), 1,
                         "the camera was re-acquired on a later tick")
        self.assertEqual(self.lease().acquired, 1)

    def test_closing_the_browser_gives_it_back(self):
        self.feed.sync(True)
        self.feed.sync(False)
        self.clock.advance(LINGER_S + 1)
        self.feed.sync(False)
        self.assertFalse(self.feed.active)
        self.assertEqual(self.lease().released, 1)
        self.assertEqual(self.camera.lent, "",
                         "the assistant did not get its camera back")

    def test_it_lingers_between_one_page_and_the_next(self):
        """The agent closing a tab and opening another is ordinary.

        Dropping the camera in that gap costs a second or two of somebody
        waving at a feed that is still starting.
        """
        self.feed.sync(True)
        self.feed.sync(False)
        self.clock.advance(LINGER_S / 2)
        self.feed.sync(False)
        self.assertTrue(self.feed.active, "the camera was dropped too eagerly")
        self.feed.sync(True)
        self.clock.advance(LINGER_S * 3)
        self.feed.sync(True)
        self.assertTrue(self.feed.active)
        self.assertEqual(len(FakeLease.instances), 1)

    def test_close_gives_it_back_immediately(self):
        self.feed.sync(True)
        self.feed.close()
        self.assertFalse(self.feed.active)
        self.assertEqual(self.camera.lent, "")
        self.feed.close()      # idempotent; shutdown calls it from two places


class TestWhatOutranksAHand(Fixture):
    def test_a_camera_already_lent_is_left_alone(self):
        """A game or a call outranks a hand.

        One has somebody playing it and the other has somebody on the other
        end; hand control does without and says why.
        """
        self.camera.lent = "an AI Motion game"
        self.feed.sync(True)
        self.assertFalse(self.feed.active)
        self.assertEqual(FakeLease.instances, [],
                         "it tried to take a camera that was in use")
        self.assertIn("another feature", self.feed.error)

    def test_no_camera_at_all_is_not_a_crash(self):
        feed = HandFeed(None, screen=self.screen, clock=self.clock)
        feed.sync(True)
        self.assertFalse(feed.active)
        self.assertIn("no camera", feed.error)
        self.assertIsNone(feed.preview_jpeg())

    def test_a_lease_that_will_not_start_leaves_nothing_behind(self):
        """The failure that matters: a camera taken and then not given back."""
        from aipi5.motion.camera_lease import CameraLeaseError

        original = FakeLease.__init__

        def failing(self, camera, *, borrower=""):
            original(self, camera, borrower=borrower)
            self.fail = CameraLeaseError("the camera would not open")

        with mock.patch.object(FakeLease, "__init__", failing):
            self.feed.sync(True)
        self.assertFalse(self.feed.active)
        self.assertIn("would not open", self.feed.error)
        self.assertEqual(self.screen.held_by, "",
                         "the screensaver was held off for a feed that failed")


class TestTheScreensaver(Fixture):
    def test_the_screen_is_held_while_a_hand_is_driving(self):
        """Holding the camera stops presence detection.

        So without this the idle timer sees a room that has gone quiet and
        blanks the screen on somebody standing in front of it reading a page.
        """
        self.feed.sync(True)
        self.assertEqual(self.screen.held_by, "hand control")

    def test_and_released_afterwards(self):
        self.feed.sync(True)
        self.feed.close()
        self.assertEqual(self.screen.held_by, "")

    def test_the_hold_is_named_so_it_cannot_release_someone_elses(self):
        """`ScreensaverManager.release` checks the name for exactly this."""
        self.feed.sync(True)
        self.screen.held_by = "a call"          # a call started meanwhile
        self.feed.close()
        self.assertEqual(self.screen.held_by, "a call",
                         "it took the screensaver off hold for a live call")


class TestTheGeneration(Fixture):
    """How the page knows its video stream has died.

    Guessing from the pixels was tried and does not work: a 16x16 hash averages
    sensor noise away, so a live feed of a room where nobody is moving is
    byte-identical frame to frame and reads as frozen. That produced a
    reconnect every four seconds and, because the reader returns early when it
    believes the feed is dead, five minutes of complete silence with the camera
    still held -- which looked exactly like a camera that could not see a hand.
    """

    def test_it_starts_at_zero_and_counts_acquisitions(self):
        self.assertEqual(self.feed.describe()["generation"], 0)
        self.feed.sync(True)
        self.assertEqual(self.feed.describe()["generation"], 1)

    def test_a_release_and_retake_is_a_new_generation(self):
        """The case that broke a test round: the camera was handed back and
        taken again four seconds later, and every stream opened before it was
        dead with nothing to say so."""
        self.feed.sync(True)
        first = self.feed.describe()["generation"]
        self.feed.close()
        self.feed.sync(True)
        self.assertGreater(self.feed.describe()["generation"], first)

    def test_it_does_not_move_while_the_feed_stays_up(self):
        """Otherwise the page reconnects for no reason, once a second."""
        self.feed.sync(True)
        first = self.feed.describe()["generation"]
        for _tick in range(10):
            self.feed.sync(True)
        self.assertEqual(self.feed.describe()["generation"], first)

    def test_a_failed_acquisition_is_not_a_generation(self):
        """Nothing was taken, so no stream was invalidated."""
        self.camera.lent = "an AI Motion game"
        self.feed.sync(True)
        self.assertEqual(self.feed.describe()["generation"], 0)


class TestServingFrames(Fixture):
    def test_no_frames_when_it_is_not_running(self):
        self.assertIsNone(self.feed.preview_jpeg())

    def test_frames_come_from_the_lease(self):
        self.feed.sync(True)
        self.assertEqual(self.feed.preview_jpeg(), b"\xff\xd8jpeg")

    def test_a_frame_that_fails_is_not_an_exception_in_an_http_handler(self):
        self.feed.sync(True)
        with mock.patch.object(FakeLease, "preview_jpeg",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(self.feed.preview_jpeg())

    def test_describe_says_whether_it_is_running(self):
        self.assertFalse(self.feed.describe()["active"])
        self.feed.sync(True)
        described = self.feed.describe()
        self.assertTrue(described["active"])
        self.assertEqual(described["fps"], 30)


if __name__ == "__main__":
    unittest.main()
