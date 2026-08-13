"""Which screensaver, and when.

Every case here is a *time*, which is exactly what cannot be checked on the
device: verifying that 21:01 shows the night screen by waiting until 21:01 is
one attempt a day, and moving the Pi's clock to hurry it along would disturb
TLS, Tailscale and the certificates the video call depends on. Section 30 says
so outright and asks for a development clock override instead — which is why
`ScheduleManager.is_day` and `ScreensaverManager.mode` both take a `datetime`
and only fall back to `datetime.now()` when nobody passes one.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from aipi5.core.presence import Presence, PresenceEvent, ScreensaverPolicy
from aipi5.screensaver.manager import Mode, ScreensaverManager
from aipi5.screensaver.schedule import (ScheduleManager, Window, format_hhmm,
                                        parse_hhmm)


def at(clock: str) -> datetime:
    """`"21:01"` on an arbitrary Thursday."""
    hours, minutes = clock.split(":")
    return datetime(2026, 8, 13, int(hours), int(minutes))


def shipped() -> ScheduleManager:
    return ScheduleManager(parse_hhmm("07:00", "07:00"),
                           parse_hhmm("21:01", "21:01"))


class TestParsing(unittest.TestCase):

    def test_reads_hhmm(self):
        self.assertEqual(parse_hhmm("00:00", "07:00"), 0)
        self.assertEqual(parse_hhmm("07:00", "00:00"), 420)
        self.assertEqual(parse_hhmm("21:01", "00:00"), 1261)
        self.assertEqual(parse_hhmm("23:59", "00:00"), 1439)

    def test_accepts_a_single_digit_hour(self):
        # Somebody editing YAML over ssh writes `7:00`, and being strict about
        # it would mean a device that shows the wrong screen all night over a
        # leading zero.
        self.assertEqual(parse_hhmm("7:00", "00:00"), 420)

    def test_nonsense_falls_back(self):
        for bad in ("", None, "noon", "25:00", "07:60", "0700", "7", [1, 2]):
            self.assertEqual(parse_hhmm(bad, "07:00"), 420, bad)

    def test_formats_back(self):
        self.assertEqual(format_hhmm(1261), "21:01")
        self.assertEqual(format_hhmm(0), "00:00")


class TestWindow(unittest.TestCase):
    """The half-open interval, which is what puts the boundaries in the right
    places without a single `+ 1` anywhere."""

    def test_start_is_inclusive_and_end_is_not(self):
        window = Window(420, 1261)          # 07:00 – 21:01
        self.assertTrue(window.contains(420))
        self.assertTrue(window.contains(1260))       # 21:00
        self.assertFalse(window.contains(1261))      # 21:01
        self.assertFalse(window.contains(419))       # 06:59

    def test_crossing_midnight(self):
        # The night, written directly. This is the comparison that a naive
        # `start <= t <= end` gets wrong for every minute of it.
        night = Window(1261, 420)           # 21:01 – 07:00
        for minute in (1261, 1439, 0, 1, 419):
            self.assertTrue(night.contains(minute), minute)
        for minute in (420, 720, 1260):
            self.assertFalse(night.contains(minute), minute)


class TestScheduleBoundaries(unittest.TestCase):
    """Section 24's table, verbatim."""

    def setUp(self):
        self.schedule = shipped()

    def test_the_specifications_boundaries(self):
        expected = {
            "06:59": "night",
            "07:00": "day",
            "20:59": "day",
            "21:00": "day",
            "21:01": "night",
            "23:59": "night",
            "00:00": "night",
        }
        for clock, wanted in expected.items():
            got = "day" if self.schedule.is_day(at(clock)) else "night"
            self.assertEqual(got, wanted, f"at {clock}")

    def test_every_minute_belongs_to_exactly_one_window(self):
        # The failure this catches is a minute that is in neither — which on
        # the device is a screen showing nothing for sixty seconds a day, and
        # is invisible in a test that only checks seven timestamps.
        day = self.schedule.day
        night = Window(self.schedule.night_start, self.schedule.day_start)
        for minute in range(24 * 60):
            self.assertNotEqual(day.contains(minute), night.contains(minute),
                                format_hhmm(minute))

    def test_a_daytime_only_schedule_still_crosses_midnight_safely(self):
        # Somebody who wants photographs from 22:00 to 04:00 — the day window
        # is then the one that wraps, which is the mirror image of the shipped
        # configuration and the case an inverted implementation gets wrong.
        odd = ScheduleManager(parse_hhmm("22:00", "07:00"),
                              parse_hhmm("04:00", "21:01"))
        for clock in ("22:00", "23:59", "00:00", "03:59"):
            self.assertTrue(odd.is_day(at(clock)), clock)
        for clock in ("04:00", "12:00", "21:59"):
            self.assertFalse(odd.is_day(at(clock)), clock)

    def test_describe_names_both_windows(self):
        described = self.schedule.describe()
        self.assertEqual(described["day_start"], "07:00")
        self.assertEqual(described["night_start"], "21:01")
        self.assertIn("07:00", described["day_window"])
        self.assertIn("21:01", described["night_window"])


class TestManager(unittest.TestCase):
    """Section 27's state machine."""

    def setUp(self):
        self.policy = ScreensaverPolicy(timeout_seconds=60.0, enabled=True)
        self.photos = True
        self.manager = ScreensaverManager(
            self.policy, shipped(),
            photos_ready=lambda: self.photos,
            timezone="America/Los_Angeles")

    def empty_room(self, when: float = 0.0) -> None:
        self.policy.presence_changed(
            PresenceEvent(Presence.PERSON_PRESENT, Presence.PERSON_NOT_PRESENT,
                          when))

    def test_somebody_in_the_room_means_no_screensaver(self):
        self.assertIs(self.manager.mode(at("12:00"), 0.0), Mode.ACTIVE_UI)

    def test_the_idle_timeout_still_belongs_to_the_policy(self):
        self.empty_room(0.0)
        # 59 seconds is not 60. The manager adds a choice of screensaver and
        # must not add a second timer — section 3.
        self.assertIs(self.manager.mode(at("12:00"), 59.0), Mode.ACTIVE_UI)
        self.assertIs(self.manager.mode(at("12:00"), 60.0), Mode.DAY_PHOTOS)

    def test_the_mode_follows_the_clock_while_it_is_up(self):
        self.empty_room(0.0)
        self.assertIs(self.manager.mode(at("20:59"), 100.0), Mode.DAY_PHOTOS)
        # 21:01 with nobody having touched anything: section 2's example, and
        # the transition happens because the mode is recomputed rather than
        # remembered.
        self.assertIs(self.manager.mode(at("21:01"), 200.0), Mode.NIGHT_WEATHER)
        self.assertIs(self.manager.mode(at("07:00"), 300.0), Mode.DAY_PHOTOS)

    def test_activity_takes_it_down_from_either_screen(self):
        self.empty_room(0.0)
        self.assertIs(self.manager.mode(at("12:00"), 100.0), Mode.DAY_PHOTOS)
        self.policy.suppress(now=100.0, person_present=True)
        self.assertIs(self.manager.mode(at("12:00"), 101.0), Mode.ACTIVE_UI)

    def test_a_call_outranks_either_screensaver(self):
        # Section 19. The screensaver is up, a call arrives, and the screen
        # has to be the call's — even though nobody is in front of the camera
        # and the idle policy still believes the room is empty.
        self.empty_room(0.0)
        self.assertIs(self.manager.mode(at("12:00"), 100.0), Mode.DAY_PHOTOS)
        self.manager.hold("a call")
        self.assertIs(self.manager.mode(at("12:00"), 101.0), Mode.ACTIVE_UI)
        self.manager.release("a call")
        self.assertIs(self.manager.mode(at("12:00"), 102.0), Mode.DAY_PHOTOS)

    def test_a_release_from_the_wrong_thing_is_ignored(self):
        self.empty_room(0.0)
        self.manager.hold("a shutdown countdown")
        self.manager.release("a call")
        self.assertIs(self.manager.mode(at("12:00"), 100.0), Mode.ACTIVE_UI)
        self.manager.release("a shutdown countdown")
        self.assertIs(self.manager.mode(at("12:00"), 101.0), Mode.DAY_PHOTOS)

    def test_no_photos_falls_back_to_the_clock_rather_than_an_error(self):
        # Section 18: a fallback screen, not an error page. And it must start
        # showing photographs the moment there are some, without a restart —
        # which is why `photos_ready` is a callable and not a flag.
        self.photos = False
        self.empty_room(0.0)
        self.assertIs(self.manager.mode(at("12:00"), 100.0), Mode.NIGHT_WEATHER)
        self.photos = True
        self.assertIs(self.manager.mode(at("12:00"), 101.0), Mode.DAY_PHOTOS)

    def test_day_mode_clock_never_shows_photos(self):
        manager = ScreensaverManager(self.policy, shipped(),
                                     photos_ready=lambda: True,
                                     day_mode="clock")
        self.empty_room(0.0)
        self.assertIs(manager.mode(at("12:00"), 100.0), Mode.NIGHT_WEATHER)

    def test_a_disabled_screensaver_never_shows_either(self):
        self.policy.enabled = False
        self.empty_room(0.0)
        self.assertIs(self.manager.mode(at("12:00"), 10_000.0), Mode.ACTIVE_UI)

    def test_the_snapshot_carries_both_halves(self):
        self.empty_room(0.0)
        snapshot = self.manager.snapshot(at("22:00"), 100.0)
        # The boolean keeps its old name because the page has always read it;
        # the mode is the new half.
        self.assertTrue(snapshot["showing"])
        self.assertEqual(snapshot["mode"], "night-weather")

    def test_describe_reports_the_timezone(self):
        described = self.manager.describe()
        self.assertEqual(described["timezone"], "America/Los_Angeles")
        self.assertEqual(described["day_window"], "07:00–21:01")


class TestTheScreensaverIsVisualOnly(unittest.TestCase):
    """Section 20: music must not stop because the screen went away.

    Stated as a test on the source rather than on behaviour because the way
    this requirement gets broken is by a well-meaning line being *added* — the
    screensaver coming up looks like a moment to release the audio floor, and
    it is not one. `AudioPriority` counts holders and a stray release would
    unpause Kodama-Lite in the middle of a call.
    """

    def test_nothing_here_touches_audio_or_the_player(self):
        from pathlib import Path
        package = Path(__file__).resolve().parent.parent / "aipi5" / "screensaver"
        for source in package.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for forbidden in ("AudioPriority", "Ducker", "KodamaLite",
                              "audio.acquire", "audio.release", "player."):
                self.assertNotIn(forbidden, text, f"{source.name}: {forbidden}")

    def test_waking_is_the_idle_policy_and_nothing_else(self):
        # The only thing a wake does here is take the screensaver down. If this
        # object ever grows a side effect, it has to be a deliberate change to
        # this list rather than something that arrived with a feature.
        public = sorted(name for name in dir(ScreensaverManager)
                        if not name.startswith("_"))
        self.assertEqual(public, ["describe", "held_by", "hold", "log_startup",
                                  "mode", "release", "snapshot"])


class TestReboot(unittest.TestCase):
    """Section 23: the mode comes from the clock, never from having been
    running when the boundary went past."""

    def make(self):
        policy = ScreensaverPolicy(timeout_seconds=60.0, enabled=True)
        manager = ScreensaverManager(policy, shipped(),
                                     photos_ready=lambda: True)
        policy.presence_changed(
            PresenceEvent(Presence.PERSON_PRESENT,
                          Presence.PERSON_NOT_PRESENT, 0.0))
        return manager

    def test_booting_at_eight_in_the_morning(self):
        self.assertIs(self.make().mode(at("08:00"), 100.0), Mode.DAY_PHOTOS)

    def test_booting_at_eleven_at_night(self):
        # A process that has never seen 21:01 go past. A manager that waited
        # for the transition would show photographs to a dark room until dawn.
        self.assertIs(self.make().mode(at("23:00"), 100.0), Mode.NIGHT_WEATHER)

    def test_booting_at_three_in_the_morning(self):
        self.assertIs(self.make().mode(at("03:00"), 100.0), Mode.NIGHT_WEATHER)


if __name__ == "__main__":
    unittest.main()
