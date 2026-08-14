"""The periodic maintenance, and the microphone it must not depend on.

The bug these pin down was measured on the device and is worth restating,
because it is not the kind a unit test finds by accident: the voice loop is
`while not stopping: frame = next(frames)`, and its idle branch was the only
caller of four unrelated maintenance jobs. A microphone that stopped delivering
frames therefore stopped the camera retry, the weather refresh, the call sweep
and — worst — `publish()`, which freezes the entire UI snapshot including the
screensaver decision.

The tests below drive `tick()` directly rather than the thread, because what
matters is *what gets called and in what order*, not the scheduling. One test
does run the thread, to prove the microphone is genuinely not in the path.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.core.housekeeping import Housekeeping

#: Far enough ahead of any injected clock that a timer pinned to it never
#: fires. Needed because `Housekeeping` seeds its timers from
#: `time.monotonic()`, and **that value's magnitude is platform-specific**:
#: seconds since boot on Linux, something much larger on Windows. A test that
#: injects `now=1000.0` is therefore in the future on a freshly booted Pi and
#: in the distant past on a desktop — which is exactly how these tests came to
#: pass on one machine and fail on the other. Pin both timers, always.
_NEVER = 1e12


class FakeAssistant:
    """Records what housekeeping asked of it. No microphone anywhere."""

    def __init__(self):
        self.calls = []
        self.raise_on = set()
        #: What `refresh_weather` reports back. False is "no reading".
        self.weather_available = True

        outer = self

        class Hub:
            def sweep(self):
                outer._note("sweep")

        class Camera:
            def retry_reclaim(self):
                outer._note("retry_reclaim")

        self.call_hub = Hub()
        self.camera = Camera()

    def _note(self, what):
        if what in self.raise_on:
            self.calls.append(what + ":raised")
            raise RuntimeError(f"{what} is broken")
        self.calls.append(what)

    def refresh_weather(self):
        self._note("refresh_weather")
        return self.weather_available

    def recheck_degraded(self):
        self._note("recheck_degraded")

    def on_call_change(self):
        self._note("on_call_change")


class TestTick(unittest.TestCase):

    def setUp(self):
        self.assistant = FakeAssistant()
        # A weather interval of 0 so every tick refreshes; the cache behind
        # `refresh_weather` is what really rate-limits it in production.
        self.keeper = Housekeeping(self.assistant, weather_seconds=0.0)
        self.keeper._last_recheck = _NEVER
        # `_last_weather` starts at `time.monotonic()`, so the first refresh is
        # due one interval after startup — correct, because `start()` has
        # already fetched once. These tests inject their own clock, so it has
        # to be moved back out of the way.
        self.keeper._last_weather = 0.0

    def test_one_tick_does_all_four_jobs(self):
        self.keeper.tick(now=1000.0)
        self.assertEqual(self.assistant.calls,
                         ["sweep", "retry_reclaim", "refresh_weather",
                          "on_call_change"])

    def test_publishing_happens_last(self):
        # `on_call_change` publishes, so it has to carry whatever the three
        # before it changed.
        self.keeper.tick(now=1000.0)
        self.assertEqual(self.assistant.calls[-1], "on_call_change")

    def test_the_weather_respects_its_interval(self):
        keeper = Housekeeping(self.assistant, weather_seconds=600.0)
        keeper._last_recheck = _NEVER
        keeper._last_weather = 1000.0
        # One tick to establish that the weather works — until then the short
        # retry interval applies, because nothing has checked.
        keeper.tick(now=1100.0)
        self.assistant.calls.clear()
        keeper.tick(now=1200.0)
        self.assertNotIn("refresh_weather", self.assistant.calls)
        keeper.tick(now=1800.0)
        self.assertIn("refresh_weather", self.assistant.calls)

    def test_the_camera_is_retried_every_tick(self):
        # Not rate limited here on purpose: `Camera.retry_reclaim` does its own
        # limiting and knows the difference between a lost camera and a
        # borrowed one. Housekeeping's job is only to keep asking.
        for tick in range(3):
            self.keeper.tick(now=1000.0 + tick)
        self.assertEqual(self.assistant.calls.count("retry_reclaim"), 3)


class TestWeatherRetriesFasterAfterAFailure(unittest.TestCase):
    """The bug: a device that boots before its network shows no temperature
    for a full cache interval after the link comes back.

    `weather.cache_seconds` answers "how stale may a good reading be". It was
    also being used for "how long after a failure before trying again", and
    those are different questions. Measured on the device: the network blocked
    at startup left `/api/state` with `weather: null`, and the ten-minute cache
    interval meant it stayed null for ten minutes after the network returned —
    a night screensaver with no temperature and nothing explaining it.
    """

    def setUp(self):
        from aipi5.core.housekeeping import WEATHER_RETRY_S
        self.retry = WEATHER_RETRY_S
        self.assistant = FakeAssistant()
        self.keeper = Housekeeping(self.assistant, weather_seconds=600.0)
        self.keeper._last_weather = 0.0
        self.keeper._last_recheck = _NEVER

    def refreshes(self):
        return self.assistant.calls.count("refresh_weather")

    def test_the_first_check_is_due_on_the_short_interval(self):
        """Because nothing has verified the startup fetch worked.

        The first version of this fix started optimistic, and a Pi that booted
        without a network then waited the full ten-minute cache interval
        before its first retry — verified on the device, still no weather 75
        seconds after the link came back.
        """
        fresh = Housekeeping(self.assistant, weather_seconds=600.0)
        fresh._last_recheck = _NEVER
        fresh._last_weather = 1000.0
        fresh.tick(now=1000.0 + self.retry + 5)
        self.assertEqual(self.refreshes(), 1,
                         "the first weather check waited for the cache "
                         "interval instead of the retry interval")

    def test_a_good_reading_waits_the_full_cache_interval(self):
        # The first tick is the one that establishes the weather works; from
        # then on the slow interval applies.
        self.keeper.tick(now=1000.0)
        self.assertEqual(self.refreshes(), 1)
        self.keeper.tick(now=1000.0 + self.retry + 5)
        self.assertEqual(self.refreshes(), 1, "retried early despite success")
        self.keeper.tick(now=1000.0 + 601)
        self.assertEqual(self.refreshes(), 2)

    def test_a_failure_retries_on_the_short_interval(self):
        self.assistant.weather_available = False
        self.keeper.tick(now=1000.0)
        self.assertEqual(self.refreshes(), 1)
        # Well before the 600 s cache interval would allow.
        self.keeper.tick(now=1000.0 + self.retry + 5)
        self.assertEqual(self.refreshes(), 2)

    def test_it_goes_back_to_the_slow_interval_once_it_recovers(self):
        self.assistant.weather_available = False
        self.keeper.tick(now=1000.0)
        self.assistant.weather_available = True
        self.keeper.tick(now=1000.0 + self.retry + 5)      # recovers here
        before = self.refreshes()
        self.keeper.tick(now=1000.0 + 2 * self.retry + 20)
        self.assertEqual(self.refreshes(), before,
                         "still retrying fast after the weather came back")

    def test_recovery_is_announced_once(self):
        self.assistant.weather_available = False
        with self.assertLogs("aipi5.core.housekeeping", level="INFO") as first:
            self.keeper.tick(now=1000.0)
        self.assertTrue(any("no weather right now" in l for l in first.output))

        self.assistant.weather_available = True
        with self.assertLogs("aipi5.core.housekeeping", level="INFO") as caught:
            self.keeper.tick(now=1000.0 + self.retry + 5)
        self.assertTrue(any("available again" in l for l in caught.output))

    def test_a_working_startup_fetch_is_not_announced(self):
        # `start()` already fetched; the first tick merely confirms it. A line
        # saying the weather "is available again" when it never went away is
        # noise in a journal people read to find real faults.
        with self.assertNoLogs("aipi5.core.housekeeping", level="INFO"):
            self.keeper.tick(now=1000.0)


class TestDegradedSubsystemsAreRechecked(unittest.TestCase):
    """The banner across the top of the screen must stop lying.

    `report` is built once at boot, so "OpenAI unavailable" survived the model
    becoming reachable again and stayed on screen until a restart.
    """

    def setUp(self):
        from aipi5.core.housekeeping import RECHECK_S
        self.every = RECHECK_S
        self.assistant = FakeAssistant()
        self.keeper = Housekeeping(self.assistant, weather_seconds=1e9)
        self.keeper._last_weather = _NEVER

    def test_not_on_every_tick(self):
        self.keeper._last_recheck = 1000.0
        self.keeper.tick(now=1000.0)
        self.assertNotIn("recheck_degraded", self.assistant.calls)

    def test_rechecked_on_its_own_interval(self):
        self.keeper._last_recheck = 0.0
        self.keeper.tick(now=self.every + 10)
        self.assertIn("recheck_degraded", self.assistant.calls)


class TestOneFailureDoesNotStopTheRest(unittest.TestCase):
    """The fix must not reproduce the bug inside itself.

    The original failure was four jobs sharing one point of failure. Guarding
    them as a group rather than individually would be the same mistake with a
    different cause.
    """

    def setUp(self):
        self.assistant = FakeAssistant()
        self.keeper = Housekeeping(self.assistant, weather_seconds=0.0)
        self.keeper._last_weather = 0.0
        self.keeper._last_recheck = _NEVER

    def test_a_broken_sweep_still_leaves_the_camera_retried(self):
        self.assistant.raise_on = {"sweep"}
        with self.assertLogs("aipi5.core.housekeeping", level="ERROR"):
            self.keeper.tick(now=1000.0)
        self.assertIn("retry_reclaim", self.assistant.calls)
        self.assertIn("on_call_change", self.assistant.calls)

    def test_a_broken_camera_still_leaves_state_published(self):
        self.assistant.raise_on = {"retry_reclaim"}
        with self.assertLogs("aipi5.core.housekeeping", level="ERROR"):
            self.keeper.tick(now=1000.0)
        self.assertIn("on_call_change", self.assistant.calls)

    def test_everything_broken_still_ticks_again(self):
        self.assistant.raise_on = {"sweep", "retry_reclaim",
                                   "refresh_weather", "on_call_change"}
        with self.assertLogs("aipi5.core.housekeeping", level="ERROR"):
            for tick in range(3):
                self.keeper.tick(now=1000.0 + tick)
        self.assertEqual(self.keeper._ticks, 3)

    def test_repeated_failures_stop_flooding_the_journal(self):
        # A tick failing every second for a month is a journal nobody reads.
        self.assistant.raise_on = {"sweep"}
        with self.assertLogs("aipi5.core.housekeeping", level="ERROR") as caught:
            for tick in range(12):
                self.keeper.tick(now=1000.0 + tick)
        traces = [line for line in caught.output if "Traceback" in line]
        self.assertLess(len(traces), 12)
        self.assertTrue(any("suppressed" in line for line in caught.output))


class TestItRunsWithoutAMicrophone(unittest.TestCase):
    """The regression itself, stated as plainly as it can be.

    `FakeAssistant` has no microphone, no audio, and nothing that yields
    frames. If the maintenance still runs, it is not reachable only through
    the voice loop — which is the entire point of the module.
    """

    def test_the_thread_ticks_on_its_own_clock(self):
        assistant = FakeAssistant()
        keeper = Housekeeping(assistant, weather_seconds=1e9, tick_s=0.02)
        keeper.start()
        self.addCleanup(keeper.stop)

        deadline = time.monotonic() + 3.0
        while assistant.calls.count("retry_reclaim") < 3:
            if time.monotonic() > deadline:
                self.fail("housekeeping did not tick without a microphone")
            time.sleep(0.02)

        self.assertTrue(keeper.running)
        self.assertGreaterEqual(assistant.calls.count("on_call_change"), 3)

    def test_stop_ends_the_thread(self):
        assistant = FakeAssistant()
        keeper = Housekeeping(assistant, weather_seconds=1e9, tick_s=0.02)
        keeper.start()
        self.assertTrue(keeper.running)
        keeper.stop()
        self.assertFalse(keeper.running)
        self.assertNotIn("aipi5-housekeeping",
                         [t.name for t in threading.enumerate()])

    def test_describe_reports_honestly(self):
        assistant = FakeAssistant()
        keeper = Housekeeping(assistant, weather_seconds=1e9)
        self.assertFalse(keeper.describe()["running"])
        keeper.tick(now=1.0)
        self.assertEqual(keeper.describe()["ticks"], 1)
        self.assertEqual(keeper.describe()["failures"], 0)


class TestTheVoiceLoopNoLongerOwnsIt(unittest.TestCase):
    """A source check, because the fix is a *removal* and removals regress.

    Somebody restoring "just the camera retry" to the idle branch would
    reintroduce the coupling for that one job without anything failing.
    """

    def test_the_idle_branch_does_not_do_maintenance(self):
        # Scoped to the idle branch only. `call_hub.sweep()` and
        # `on_call_change()` still belong in the *in-call* fast path above it:
        # during a live call the loop is demonstrably running, and housekeeping
        # covers that case too.
        from pathlib import Path
        source = Path(__file__).resolve().parent.parent / "aipi5" / "main.py"
        text = source.read_text(encoding="utf-8")
        start = text.index("if not woke and requested is None:")
        branch = text[start:text.index("if woke and", start)]
        for forbidden in ("camera.retry_reclaim()", "call_hub.sweep()",
                          "refresh_weather()", "on_call_change()"):
            self.assertNotIn(
                forbidden, branch,
                f"{forbidden} is back in the voice loop's idle branch, where "
                f"a dead microphone stops it running")


if __name__ == "__main__":
    unittest.main()
