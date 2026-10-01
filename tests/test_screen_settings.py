"""Setting the screensaver schedule from the screen.

The settings page used to show these four values read-only, and said why: they
"live in one place in the YAML ... and a second way to set them is a second
thing that can disagree." That objection is about a second **store**, and it is
right about one. It is not an argument against a second **editor** of the one
store, which this device already has — the volume slider writes `audio.volume`
through the same code.

So the tests below are mostly about the ways the two could come apart: a value
applied but not saved, a value saved but not applied, and a value the file
would not accept leaving the screen showing something the device is not doing.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import yaml

from aipi5.core import yaml_edit
from aipi5.core.presence import ScreensaverPolicy
from aipi5.core.screen_settings import ScheduleError, ScreensaverSettings
from aipi5.screensaver.manager import Mode, ScreensaverManager
from aipi5.screensaver.schedule import ScheduleManager

SAMPLE = """\
display:
  width: 1280

screensaver:
  enabled: true
  timeout_seconds: 60
  day_start: "07:00"
  night_start: "21:01"  # the night begins a minute after nine
  day_mode: photos
  night_mode: weather

games:
  enabled: true
"""


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 2, hour, minute)


class ScheduleCase(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "aipi5.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        self.manager = ScreensaverManager(
            ScreensaverPolicy(),
            ScheduleManager(7 * 60, 21 * 60 + 1),
            photos_ready=lambda: True,
            day_mode="photos", night_mode="weather")
        self.settings = ScreensaverSettings(self.manager, self.path)

    def saved(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))["screensaver"]


class TestItTakesEffectAtOnce(ScheduleCase):
    """No restart and no watcher: `mode()` re-reads the schedule every time it
    is called, and the page asks twice a second."""

    def test_moving_the_boundary_changes_what_the_clock_calls_for(self):
        self.assertIs(Mode.NIGHT_WEATHER, self.manager._scheduled(at(6, 30)))
        self.settings.set(day_start="06:00")
        self.assertIs(Mode.DAY_PHOTOS, self.manager._scheduled(at(6, 30)))

    def test_changing_what_a_half_shows(self):
        self.assertIs(Mode.NIGHT_WEATHER, self.manager._scheduled(at(23, 0)))
        self.settings.set(night_mode="photos")
        self.assertIs(Mode.DAY_PHOTOS, self.manager._scheduled(at(23, 0)))

    def test_only_what_was_sent_is_touched(self):
        """The page changes one thing per tap and must not have to restate the
        other three — that is how two taps in flight overwrite one another."""
        self.settings.set(night_start="22:00")
        after = self.saved()
        self.assertEqual("22:00", after["night_start"])
        self.assertEqual("07:00", after["day_start"])
        self.assertEqual("photos", after["day_mode"])
        self.assertEqual("weather", after["night_mode"])


class TestItSurvivesARestart(ScheduleCase):

    def test_the_file_is_written(self):
        self.settings.set(day_start="06:30", night_start="22:15")
        self.assertEqual("06:30", self.saved()["day_start"])
        self.assertEqual("22:15", self.saved()["night_start"])

    def test_a_time_is_still_a_time_after_the_round_trip(self):
        """**The one that would have gone unnoticed.** `21:01` unquoted loads
        as the integer 1261 — YAML 1.1 sexagesimal — so a writer that dropped
        the quotes would work for `06:30` and destroy `22:01`, leaving a file
        that still parses."""
        self.settings.set(night_start="22:01")
        self.assertEqual("22:01", self.saved()["night_start"])
        self.assertIn('night_start: "22:01"',
                      self.path.read_text(encoding="utf-8"))

    def test_the_comments_are_still_there(self):
        self.settings.set(night_start="22:00")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# the night begins a minute after nine", text)
        self.assertIn("display:\n  width: 1280", text)
        self.assertIn("games:\n  enabled: true", text)

    def test_a_device_with_no_file_still_changes_and_says_so(self):
        """Defaults, or a test. The change applies; it simply does not
        survive, and `describe` reports that rather than implying it saved."""
        loose = ScreensaverSettings(self.manager, None)
        answer = loose.set(day_start="05:00")
        self.assertIs(False, answer["persistent"])
        self.assertEqual("05:00", answer["day_start"])


class TestWhatItRefuses(ScheduleCase):
    """`parse_hhmm` substitutes the specification's time for anything it
    cannot read and logs a warning. That is right for a config file read at
    boot and wrong for a button somebody just pressed."""

    def test_a_time_that_is_not_a_time(self):
        for bad in ("25:00", "07:60", "seven", "", "7"):
            with self.subTest(sent=bad), self.assertRaises(ScheduleError):
                self.settings.set(day_start=bad)

    def test_nothing_is_saved_when_it_refuses(self):
        with self.assertRaises(ScheduleError):
            self.settings.set(day_start="25:00")
        self.assertEqual("07:00", self.saved()["day_start"])
        self.assertEqual("07:00", self.manager.describe()["day_start"])

    def test_both_halves_starting_at_once(self):
        """`Window.contains` reads that as "always day" and the night screen
        never appears. The manager only warns; a person pressing a button
        deserves to be told."""
        with self.assertRaises(ScheduleError) as raised:
            self.settings.set(day_start="21:01")
        self.assertIn("never shows", str(raised.exception))

    def test_it_notices_the_clash_across_two_fields_in_one_request(self):
        with self.assertRaises(ScheduleError):
            self.settings.set(day_start="09:00", night_start="09:00")

    def test_a_mode_the_device_does_not_have(self):
        with self.assertRaises(ScheduleError):
            self.settings.set(day_mode="lava lamp")

    def test_the_night_cannot_be_told_to_show_the_clock(self):
        """`clock` and `weather` are the same screen — the manager only ever
        branches on `photos` — so the night offers two choices and `clock` is
        not one of the words it uses for them."""
        self.assertEqual(("photos", "weather"), ScreensaverManager.NIGHT_MODES)
        with self.assertRaises(ScheduleError):
            self.settings.set(night_mode="clock")


class TestWhenTheFileWillNotTakeIt(ScheduleCase):
    """A change that lasts until the next restart is a misleading success."""

    def test_the_running_schedule_is_put_back(self):
        with mock.patch.object(yaml_edit, "write_scalar",
                               side_effect=OSError("read-only filesystem")):
            with self.assertRaises(ScheduleError):
                self.settings.set(day_start="05:30")
        self.assertEqual("07:00", self.manager.describe()["day_start"])
        self.assertIs(Mode.NIGHT_WEATHER, self.manager._scheduled(at(6, 0)))

    def test_the_mode_is_put_back_too(self):
        with mock.patch.object(yaml_edit, "write_scalar",
                               side_effect=OSError("nope")):
            with self.assertRaises(ScheduleError):
                self.settings.set(night_mode="photos")
        self.assertEqual("weather", self.manager.describe()["night_mode"])

    def test_the_error_says_what_happened(self):
        with mock.patch.object(yaml_edit, "write_scalar",
                               side_effect=OSError("read-only filesystem")):
            with self.assertRaises(ScheduleError) as raised:
                self.settings.set(day_start="05:30")
        self.assertIn("could not be saved", str(raised.exception))

    def test_a_missing_key_is_reported_rather_than_added(self):
        """`_from_mapping` ignores keys it does not know, so inventing one
        gives a file that looks changed and a device that behaves the same."""
        self.path.write_text("screensaver:\n  enabled: true\n", encoding="utf-8")
        with self.assertRaises(ScheduleError):
            self.settings.set(day_start="05:30")


class TestWhatThePageIsTold(ScheduleCase):

    def test_it_offers_the_choices_rather_than_the_page_knowing_them(self):
        answer = self.settings.describe()
        self.assertEqual(["photos", "clock"], answer["day_modes"])
        self.assertEqual(["photos", "weather"], answer["night_modes"])

    def test_it_carries_the_running_windows(self):
        answer = self.settings.set(day_start="06:00")
        self.assertEqual("06:00", answer["day_start"])
        self.assertEqual("06:00–21:01", answer["day_window"])

    def test_the_page_reads_the_running_value_and_not_the_file(self):
        """Somebody who edits the YAML over ssh while the service is up sees
        the running value, which is the true one, and their edit takes effect
        at the next start. Exactly what the volume does today."""
        self.path.write_text(
            SAMPLE.replace('day_start: "07:00"', 'day_start: "04:00"'),
            encoding="utf-8")
        self.assertEqual("07:00", self.settings.describe()["day_start"])


if __name__ == "__main__":
    unittest.main()


class TestTheRoute(unittest.TestCase):
    """`POST /api/screensaver`. Reading it back is `/api/system`'s existing
    `screensaver` block, so there is no GET here."""

    def setUp(self):
        import http.client
        import json as json_mod
        from types import SimpleNamespace

        from aipi5.ui.server import WebUI
        from aipi5.ui.state import UiState

        self.http, self.json_mod = http.client, json_mod
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "aipi5.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        manager = ScreensaverManager(
            ScreensaverPolicy(), ScheduleManager(7 * 60, 21 * 60 + 1),
            photos_ready=lambda: True, day_mode="photos", night_mode="weather")
        self.settings = ScreensaverSettings(manager, self.path)

        cfg = SimpleNamespace(host="127.0.0.1", port=0, url="http://127.0.0.1:0")
        self.web = WebUI(cfg, state=UiState(), history=None, info=lambda: {},
                         screen_settings=self.settings)
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.port = self.web._server.server_address[1]

    def post(self, body, path="/api/screensaver"):
        connection = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", path, self.json_mod.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, self.json_mod.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_it_moves_the_boundary(self):
        status, answer = self.post({"day_start": "06:30"})
        self.assertEqual(200, status)
        self.assertTrue(answer["ok"])
        self.assertEqual("06:30", answer["day_start"])

    def test_a_bad_time_is_four_hundred_and_a_sentence(self):
        status, answer = self.post({"day_start": "25:00"})
        self.assertEqual(400, status)
        self.assertIn("no such time", answer["error"])

    def test_an_empty_request_changes_nothing(self):
        status, answer = self.post({})
        self.assertEqual(400, status)
        self.assertIn("nothing to change", answer["error"])

    def test_fields_it_does_not_know_are_ignored_rather_than_obeyed(self):
        """A caller inventing `timeout_seconds` must not set it here."""
        status, _ = self.post({"day_start": "06:00", "timeout_seconds": 5})
        self.assertEqual(200, status)
        self.assertEqual(600, self.settings.describe()["timeout_s"])

    def test_a_build_with_no_screensaver_says_so(self):
        self.web.screen_settings = None
        status, answer = self.post({"day_start": "06:00"})
        self.assertEqual(503, status)
        self.assertIn("screensaver", answer["error"])
