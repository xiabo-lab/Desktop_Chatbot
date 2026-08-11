"""The button cooldowns and the audio priority, both without hardware.

These are the two pieces of this change that are wrong *silently*. A cooldown
that does not hold produces a second camera capture, a second news fetch, a
second launch — none of which raises, and all of which look like the device
being slow. Audio priority that unbalances leaves the music paused forever with
nothing in the log to say when or why.

Both are timing rules over a sequence of events, which is exactly the shape
that cannot be checked by pressing a button and watching.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.core.audio_priority import AudioPriority
from aipi5.ui.state import ACTIONS, QUEUE_DEPTH, UNTHROTTLED, UiState


class FakeDucker:
    """AIA's `Ducker`, reduced to what makes its contract observable.

    Including the part that made re-entrancy necessary: `duck()` forgets what
    it previously paused. That is not a quirk of this fake — it is the first
    line of the real method, and it is why two overlapping ducks lose the
    music.
    """

    def __init__(self, playing: bool = True):
        self.playing = playing
        self.paused: list[str] = []
        self.ducks = 0
        self.restores = 0
        self.forgets = 0

    def duck(self) -> bool:
        self.ducks += 1
        self.paused = ["kodamalite"] if self.playing else []
        return bool(self.paused)

    def restore(self) -> None:
        self.restores += 1
        # Restoring resumes what it remembers, which is the whole point: a
        # ducker whose memory was cleared restores nothing and says nothing.
        self.playing = self.playing or bool(self.paused)
        self.resumed = list(self.paused)
        self.paused = []

    def forget(self) -> None:
        self.forgets += 1
        self.paused = []


class TestAudioPriority(unittest.TestCase):

    def setUp(self):
        self.ducker = FakeDucker()
        self.audio = AudioPriority(self.ducker)

    def test_speaking_pauses_the_music_and_gives_it_back(self):
        with self.audio.priority():
            self.assertEqual(self.ducker.ducks, 1)
            self.assertEqual(self.ducker.restores, 0)
        self.assertEqual(self.ducker.restores, 1)
        self.assertEqual(self.ducker.resumed, ["kodamalite"])

    def test_nesting_ducks_once_and_restores_once(self):
        # The bug this class exists for. Two overlapping holders — a button
        # pressed during a turn, a page that speaks while the voice loop has
        # the floor — must not turn into two ducks, because the second one
        # would find nothing playing and forget what the first had paused.
        with self.audio.priority():
            with self.audio.priority():
                pass
            # Still held by the outer one: the music must not come back
            # between two things the assistant is saying.
            self.assertEqual(self.ducker.restores, 0)
        self.assertEqual(self.ducker.ducks, 1)
        self.assertEqual(self.ducker.restores, 1)
        self.assertEqual(self.ducker.resumed, ["kodamalite"])

    def test_the_music_comes_back_even_when_speaking_raises(self):
        with self.assertRaises(RuntimeError):
            with self.audio.priority():
                raise RuntimeError("piper died mid-sentence")
        self.assertEqual(self.ducker.restores, 1)

    def test_nothing_playing_is_not_restored(self):
        # Restoring a player the user had deliberately paused would start
        # music nobody asked for, in a room where somebody just asked a
        # question.
        quiet = FakeDucker(playing=False)
        with AudioPriority(quiet).priority():
            pass
        self.assertEqual(quiet.restores, 0)

    def test_forget_leaves_it_paused_and_still_unwinds(self):
        # "Stop the music" said mid-turn: the command wanted silence, so the
        # end of the turn must not undo it — and the nesting still has to
        # unwind or the next duck would be refused as re-entrant forever.
        self.audio.acquire()
        self.audio.forget()
        self.audio.release()
        self.assertEqual(self.ducker.forgets, 1)
        self.assertEqual(self.ducker.restores, 0)
        self.assertFalse(self.audio.held)

    def test_releasing_too_often_is_survivable(self):
        # This would be a bug upstream, and the response to it is a log line
        # rather than an exception on the path whose whole job is to be the
        # thing that cannot fail.
        self.audio.release()
        self.assertEqual(self.ducker.restores, 0)
        with self.audio.priority():
            pass
        self.assertEqual(self.ducker.ducks, 1)
        self.assertEqual(self.ducker.restores, 1)

    def test_two_threads_holding_at_once_duck_once(self):
        # The case that actually happens: the voice loop holds the floor for a
        # whole turn on its own thread while the HTTP handler serves a button
        # press on another.
        started = threading.Event()
        release = threading.Event()

        def hold():
            with self.audio.priority():
                started.set()
                release.wait(2.0)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(started.wait(2.0))

        with self.audio.priority():
            pass
        # The other thread still has it, so nothing has been given back.
        self.assertEqual(self.ducker.restores, 0)

        release.set()
        thread.join(2.0)
        self.assertEqual(self.ducker.ducks, 1)
        self.assertEqual(self.ducker.restores, 1)


class TestButtonCooldowns(unittest.TestCase):
    """Section 2: ten seconds per button, and per button independently."""

    def setUp(self):
        self.ui = UiState()

    def drain(self):
        """Empty the queue, so the depth-2 limit never masks a cooldown result."""
        while self.ui.take_action() is not None:
            pass

    def test_the_first_press_is_accepted(self):
        self.assertTrue(self.ui.request("camera"))
        self.assertEqual(self.ui.take_action(), "camera")

    def test_a_second_press_within_ten_seconds_is_ignored(self):
        self.assertTrue(self.ui.request("camera"))
        self.drain()
        # The failure being prevented: two camera pages, two captures, two
        # vision requests, and a bill for the second one.
        self.assertFalse(self.ui.request("camera"))
        self.assertIsNone(self.ui.take_action())

    def test_a_burst_of_presses_produces_exactly_one_action(self):
        accepted = [self.ui.request("news") for _ in range(12)]
        self.drain()
        self.assertEqual(accepted.count(True), 1,
                         "an impatient finger must not queue twelve fetches")

    def test_each_button_cools_independently(self):
        self.assertTrue(self.ui.request("camera"))
        self.drain()
        # Pressing Camera must not disable Weather. This is the requirement
        # that rules out a single global lockout, which would be much simpler
        # and would make the device feel broken.
        for other in ("weather", "news", "kodama", "call"):
            with self.subTest(other=other):
                self.assertTrue(self.ui.request(other))
                self.drain()

    def test_the_cooldown_expires(self):
        ui = UiState(cooldown_s=0.05)
        self.assertTrue(ui.request("camera"))
        while ui.take_action() is not None:
            pass
        self.assertFalse(ui.request("camera"))
        time.sleep(0.06)
        self.assertTrue(ui.request("camera"))

    def test_the_remaining_time_is_published(self):
        # The page draws a countdown from this. A button that is merely dead
        # reads as broken, which is what makes somebody press it again.
        self.ui.request("camera")
        self.drain()
        cooldowns = self.ui.snapshot()["cooldowns"]
        self.assertIn("camera", cooldowns)
        self.assertGreater(cooldowns["camera"], 0)
        self.assertLessEqual(cooldowns["camera"], self.ui.cooldown_s)
        self.assertNotIn("weather", cooldowns,
                         "only the cooling actions belong in a twice-a-second poll")

    def test_wake_is_never_rate_limited(self):
        # How a person gets the assistant's attention — from the screensaver,
        # or the Talk page. A device that ignores somebody who tried to talk
        # to it twice is worse than one that listens twice.
        for _ in range(5):
            self.assertTrue(self.ui.request("wake"))
            self.drain()

    def test_every_page_button_in_a_row_gets_through(self):
        # Navigation, not impatience. At the old queue depth of 2 the third
        # press was dropped while its page opened anyway — the page was there
        # and nothing ever happened on it. Found on the device.
        #
        # `QUEUE_DEPTH` is `len(ACTIONS)`, so this also guards the arithmetic:
        # adding a button without the queue growing with it would put the last
        # press of a full row back on the floor.
        pressed = ["talk", "call", "camera", "weather", "news", "kodama"]
        accepted = [self.ui.request(action) for action in pressed]
        self.assertTrue(all(accepted), "a person navigating must not be throttled")
        queued = []
        while (action := self.ui.take_action()) is not None:
            queued.append(action)
        self.assertEqual(queued, pressed)

    def test_a_refused_press_starts_no_cooldown(self):
        # A press the queue dropped because the assistant was busy did no
        # work, so it must not lock the button out of the work it asked for.
        ui = UiState()
        # `wake` is the one action with no cooldown, so it is the only way to
        # fill the queue without the cooldown refusing the press first.
        for _ in range(QUEUE_DEPTH + 2):
            ui.request("wake")
        self.assertFalse(ui.request("camera"), "the queue should be full")
        self.assertEqual(ui.cooling("camera"), 0.0)

    def test_every_page_button_is_a_real_action(self):
        # The page and this tuple have to agree, and the page is HTML that no
        # test can type-check. This is the closest thing to a compiler.
        for action in ("talk", "call", "camera", "weather", "news", "kodama"):
            with self.subTest(action=action):
                self.assertIn(action, ACTIONS)

    def test_the_call_button_is_rate_limited_like_every_other_page(self):
        # `call` is deliberately *not* on the exemption list with `wake`.
        # Starting a call will claim the camera, the microphone and the
        # speaker away from the voice loop, so a repeated press is the most
        # expensive one on the screen, not the least.
        self.assertTrue(self.ui.request("call"))
        self.drain()
        self.assertFalse(self.ui.request("call"))
        self.assertNotIn("call", UNTHROTTLED)

    def test_nothing_destructive_is_reachable_from_a_button(self):
        # The property that makes a UI with buttons on it safe at all: every
        # destructive command is spoken and confirmed out loud, and a button
        # cannot hold that conversation.
        for forbidden in ("shutdown", "reboot", "poweroff", "quit", "close"):
            self.assertNotIn(forbidden, ACTIONS)

    def test_only_wake_is_exempt_from_the_cooldown(self):
        # A guard on the exemption list itself. Adding a page button here
        # would silently undo section 2 for that button.
        self.assertEqual(set(UNTHROTTLED), {"wake"})


class TestCameraDescriptionIds(unittest.TestCase):
    """The camera page needs to tell a new answer from the old one still up."""

    def test_a_new_description_bumps_the_id(self):
        ui = UiState()
        self.assertEqual(ui.snapshot()["camera_description_id"], 0)
        ui.describe_camera("A gray equipment cabinet.")
        self.assertEqual(ui.snapshot()["camera_description_id"], 1)

    def test_the_same_text_twice_is_still_two_answers(self):
        # Two identical descriptions of a room that has not changed are two
        # answers, and the second must re-show and re-start the fade. Comparing
        # the text would treat it as the first one still being on screen.
        ui = UiState()
        ui.describe_camera("Nobody is here.")
        ui.describe_camera("Nobody is here.")
        self.assertEqual(ui.snapshot()["camera_description_id"], 2)

    def test_clearing_does_not_bump(self):
        ui = UiState()
        ui.describe_camera("Something.")
        ui.describe_camera(None)
        self.assertEqual(ui.snapshot()["camera_description_id"], 1)
        self.assertIsNone(ui.snapshot()["camera_description"])


if __name__ == "__main__":
    unittest.main()
