"""Powering the device off, and the three seconds in which it can be stopped.

The failure being guarded is not a crash. The newer AIA declares `shutdown`
with `confirm=False` — deliberately, because `poweroff` takes the audio stack
down with it and the last thing this device does is the one thing it cannot
narrate — so a version of this project without a countdown powers the Pi off
the instant it hears the phrase, with nothing on the screen and nothing anybody
can do about it. That version passes every other test in this suite.

The Pi's copy of AIA is older and still says `confirm=True`, which is exactly
why nothing here asserts on that flag: the behaviour has to be the same on both
machines, and it is the command's *name* this project branches on.

So what is checked here is the policy: that the countdown answers only to the
screen, that a touch stops it, and above all that a screen which never says it
is showing anything does not end with the device off.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import Registry
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System

from aipi5.core.shutdown import ShutdownCountdown, countdown_and_run
from aipi5.ui.state import ACTIONS


class FakeResult:
    def __init__(self, text="Shutting down."):
        self.text = text

    def say(self, _language):
        return self.text


class FakeCommand:
    name = "shutdown"

    def __init__(self, ran):
        self.ran = ran
        self.confirm = False

    def handler(self, **arguments):
        self.ran.append(arguments)
        return FakeResult()


class FakeIntent:
    def __init__(self, command):
        self.command = command
        self.arguments = {}


class FakeAssistant:
    def __init__(self, countdown):
        self.countdown = countdown
        self.published = 0

    def publish(self, **_extra):
        self.published += 1


class StubCountdown:
    def __init__(self, outcome: bool):
        self.outcome = outcome
        self.runs = 0

    def run(self, publish) -> bool:
        self.runs += 1
        publish()
        return self.outcome


class TestWhatHappensWhenItIsHeard(unittest.TestCase):
    """`countdown_and_run`, which is the whole of the policy."""

    def run_it(self, outcome: bool, language: str = "en"):
        ran: list = []
        command = FakeCommand(ran)
        countdown = StubCountdown(outcome)
        reply, intent = countdown_and_run(
            FakeAssistant(countdown), FakeIntent(command), language)
        return reply, intent, ran, countdown

    def test_the_countdown_runs_before_anything_else_does(self):
        reply, intent, ran, countdown = self.run_it(True)
        self.assertEqual(countdown.runs, 1)
        self.assertEqual(len(ran), 1)
        self.assertEqual(reply, "Shutting down.")
        self.assertIsNotNone(intent)

    def test_a_cancelled_countdown_does_not_power_anything_off(self):
        reply, intent, ran, _ = self.run_it(False)
        self.assertEqual(ran, [], "the shutdown handler ran after a cancel")
        self.assertEqual(reply, "Cancelled.")
        # None, so the loop lets the music come back: the command did not run,
        # and a turn that reports otherwise leaves a paused player behind.
        self.assertIsNone(intent)

    def test_it_says_so_in_the_language_it_was_asked_in(self):
        reply, _, _, _ = self.run_it(False, language="zh")
        self.assertEqual(reply, "已取消。")

    def test_it_does_not_read_aias_confirm_flag(self):
        # The two copies of AIA here disagree about `shutdown.confirm` — the
        # newer one turned it off when it adopted this countdown, the Pi's is
        # older and still asks out loud. This project must behave the same
        # either way, so the branch is keyed on the command's name.
        ran: list = []
        command = FakeCommand(ran)
        command.confirm = True
        reply, _ = countdown_and_run(
            FakeAssistant(StubCountdown(False)), FakeIntent(command), "en")
        self.assertEqual(ran, [])
        self.assertEqual(reply, "Cancelled.")


class TestWhatTheCommandItselfPromises(unittest.TestCase):
    """AIA's declarations, in the part that is the same in both copies."""

    def specs(self):
        return {command.name: command
                for _, command in Registry([KodamaLite(), System()]).all_commands()}

    def test_reboot_still_asks_out_loud(self):
        # Reboot comes back, so it can afford a question. This is the line
        # between the two policies and it is worth a test of its own.
        self.assertTrue(self.specs()["reboot"].confirm)

    def test_no_button_can_start_it(self):
        for action in ACTIONS:
            self.assertNotIn("shut", action)
            self.assertNotIn("power", action)


class TestTheCountdown(unittest.TestCase):

    def setUp(self):
        self.published = 0

    def publish(self) -> None:
        self.published += 1

    def screen(self, countdown: ShutdownCountdown, answer: str | None,
               delay: float = 0.02) -> threading.Thread:
        """A page that reacts the way the real one does, or does not react."""

        def react():
            time.sleep(delay)
            if answer is None:
                return
            countdown.showing(countdown.payload()["token"])
            if answer == "cancel":
                time.sleep(delay)
                countdown.cancel()

        thread = threading.Thread(target=react)
        thread.start()
        self.addCleanup(thread.join)
        return thread

    def test_a_countdown_nobody_answers_does_not_power_off(self):
        # The one that matters. A dead Chromium, a crashed page and a person
        # deciding not to cancel must not look the same from here.
        countdown = ShutdownCountdown(seconds=1)
        countdown.ACK_TIMEOUT_S = 0.1
        started = time.monotonic()
        self.assertFalse(countdown.run(self.publish))
        # It gave up on the acknowledgement rather than sitting through the
        # whole countdown first.
        self.assertLess(time.monotonic() - started, 0.9)

    def test_a_countdown_left_alone_powers_off(self):
        countdown = ShutdownCountdown(seconds=0.2)
        self.screen(countdown, "showing")
        self.assertTrue(countdown.run(self.publish))

    def test_a_touch_stops_it(self):
        countdown = ShutdownCountdown(seconds=5)
        self.screen(countdown, "cancel")
        started = time.monotonic()
        self.assertFalse(countdown.run(self.publish))
        # And stopped it *then*, rather than letting the five seconds run out
        # and returning the same answer for a different reason.
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_screen_learns_at_both_ends(self):
        countdown = ShutdownCountdown(seconds=0.1)
        self.screen(countdown, "showing")
        countdown.run(self.publish)
        # Once when it appears and once when it is over: a countdown that is
        # published only on the way in stays on the screen afterwards.
        self.assertEqual(self.published, 2)

    def test_nothing_is_published_when_nothing_is_counting(self):
        self.assertIsNone(ShutdownCountdown().payload())

    def test_what_the_screen_is_told_is_enough_to_draw(self):
        countdown = ShutdownCountdown(seconds=3)
        seen = {}

        def capture():
            time.sleep(0.02)                  # let `run` publish it first
            seen.update(countdown.payload() or {})
            countdown.showing(seen.get("token", 0))
            countdown.cancel()

        thread = threading.Thread(target=capture)
        thread.start()
        self.addCleanup(thread.join)
        countdown.run(self.publish)
        self.assertEqual(seen.get("seconds"), 3)
        self.assertGreater(seen.get("token", 0), 0)

    def test_an_answer_about_an_older_countdown_is_ignored(self):
        # A page reloaded mid-count, or a request that arrived late. Accepting
        # it as this countdown's acknowledgement would mean powering off on the
        # strength of a screen that showed something else.
        countdown = ShutdownCountdown(seconds=0.5)
        countdown.ACK_TIMEOUT_S = 0.2
        stale = threading.Thread(target=lambda: countdown.showing(0))
        stale.start()
        self.addCleanup(stale.join)
        self.assertFalse(countdown.run(self.publish))

    def test_a_cancel_is_taken_whatever_it_claims_to_be_about(self):
        # The opposite rule to `showing`, and on purpose: a stale token is a
        # reason to keep the device on, never a reason to carry on towards
        # powering it off.
        countdown = ShutdownCountdown(seconds=5)

        def react():
            time.sleep(0.02)
            countdown.showing(countdown.payload()["token"])
            time.sleep(0.02)
            countdown.cancel(999)

        thread = threading.Thread(target=react)
        thread.start()
        self.addCleanup(thread.join)
        self.assertFalse(countdown.run(self.publish))

    def test_a_touch_when_nothing_is_counting_does_nothing(self):
        self.assertFalse(ShutdownCountdown().cancel())
        self.assertFalse(ShutdownCountdown().showing(1))


if __name__ == "__main__":
    unittest.main()
