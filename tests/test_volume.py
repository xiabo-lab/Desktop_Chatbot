"""The one number that decides how loud the device is.

Added because the agent could not turn it down: there was no volume control
anywhere in this project, so "make it quieter" had nothing to change. It is a
configuration key rather than a new privileged operation because the write path
for a key already exists -- propose, approve, edit in place, validate, restart,
roll back if it will not load -- and a number that fits on one line does not
justify a second way in.
"""

from __future__ import annotations

import subprocess
import json
import http.client
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aipi5.core import volume
from aipi5.core.config import load
from aipi5.ui.server import WebUI
from aipi5.ui.state import UiState

CONFIG = Path(__file__).resolve().parent.parent / "config" / "aipi5.yaml"


def ran(returncode=0, stdout="", stderr=""):
    return mock.Mock(return_value=subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr))


class TestSettingIt(unittest.TestCase):

    def test_a_percentage_becomes_a_fraction_of_the_default_sink(self):
        with mock.patch("subprocess.run", ran()) as call:
            self.assertTrue(volume.apply(55))
        args = call.call_args_list[0].args[0]
        self.assertEqual(args[:3], ["wpctl", "set-volume", volume.SINK])
        self.assertEqual(args[3], "0.55")
        self.assertEqual(["wpctl", "set-mute", volume.SINK, "0"],
                         call.call_args_list[1].args[0])

    def test_the_sink_is_named_by_role_rather_than_by_card(self):
        """`@DEFAULT_AUDIO_SINK@` follows the output. A card and a control --
        which is what `amixer` would need -- would have to be told which, and
        would be wrong the day the device plays through something else."""
        self.assertEqual(volume.SINK, "@DEFAULT_AUDIO_SINK@")

    def test_out_of_range_is_clamped_at_both_ends(self):
        for asked, wanted in ((150, "1.00"), (-20, "0.00"), (0, "0.00"),
                              (100, "1.00")):
            with self.subTest(asked=asked):
                with mock.patch("subprocess.run", ran()) as call:
                    volume.apply(asked)
                self.assertEqual(call.call_args_list[0].args[0][3], wanted)


class TestWhenItCannotBeSet(unittest.TestCase):
    """A device that will not set its volume is slightly too loud. It must not
    be a device that will not start, so nothing here raises."""

    def test_a_missing_tool_is_reported_not_raised(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(volume.apply(50))

    def test_a_refusal_is_reported_not_raised(self):
        with mock.patch("subprocess.run", ran(1, stderr="no such sink")):
            self.assertFalse(volume.apply(50))

    def test_a_hang_is_reported_not_raised(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("wpctl", 5)):
            self.assertFalse(volume.apply(50))

    def test_reading_it_back_survives_the_same_things(self):
        for boom in (FileNotFoundError, OSError,
                     subprocess.TimeoutExpired("wpctl", 5)):
            with self.subTest(boom=boom):
                with mock.patch("subprocess.run", side_effect=boom):
                    self.assertIsNone(volume.read())


class TestReadingItBack(unittest.TestCase):
    def test_a_level_comes_back_as_a_percentage(self):
        with mock.patch("subprocess.run", ran(stdout="Volume: 0.55\n")):
            self.assertEqual(volume.read(), 55)

    def test_a_muted_sink_still_reports_its_level(self):
        with mock.patch("subprocess.run",
                        ran(stdout="Volume: 0.30 [MUTED]\n")):
            self.assertEqual(volume.read(), 30)

    def test_something_unparseable_is_None_rather_than_a_guess(self):
        with mock.patch("subprocess.run", ran(stdout="Volume: loud\n")):
            self.assertIsNone(volume.read())

    def test_levels_above_the_supported_range_are_clamped(self):
        with mock.patch("subprocess.run", ran(stdout="Volume: 1.25\n")):
            self.assertEqual(volume.read(), 100)


class TestMasterControl(unittest.TestCase):
    def _config(self, folder: str, text: str = "audio:\n  volume: 100\n") -> Path:
        path = Path(folder) / "aipi5.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_one_change_applies_to_the_sink_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._config(folder)
            control = volume.VolumeControl(100, path)
            with (mock.patch.object(volume, "read", return_value=100),
                  mock.patch.object(volume, "apply", return_value=True) as apply):
                self.assertTrue(control.set(37))
            apply.assert_called_once_with(37)
            self.assertEqual(load(path).audio.volume, 37)

    def test_persistence_keeps_comments_and_the_rest_of_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._config(
                folder, "display:\n  width: 1280\n\naudio:\n"
                        "  volume: 70  # room level\n\nagent:\n  enabled: false\n")
            control = volume.VolumeControl(70, path)
            with (mock.patch.object(volume, "read", return_value=70),
                  mock.patch.object(volume, "apply", return_value=True)):
                self.assertTrue(control.set(42))
            text = path.read_text(encoding="utf-8")
            self.assertIn("  volume: 42  # room level", text)
            self.assertIn("display:\n  width: 1280", text)
            self.assertIn("agent:\n  enabled: false", text)

    def test_a_save_failure_rolls_the_audible_level_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._config(folder, "audio:\n  gain: 10\n")
            control = volume.VolumeControl(40, path)
            with (mock.patch.object(volume, "read", return_value=40),
                  mock.patch.object(volume, "apply", return_value=True) as apply):
                self.assertFalse(control.set(60))
            self.assertEqual([mock.call(60), mock.call(40)], apply.call_args_list)

    def test_description_names_every_stream_behind_the_master_sink(self):
        control = volume.VolumeControl(55)
        with mock.patch.object(volume, "read", return_value=55):
            info = control.describe()
        self.assertEqual(55, info["level"])
        self.assertEqual(["Game", "Call", "Kodama-Lite", "Browser",
                          "Agent Talk"], info["controls"])


class TestVolumeAPI(unittest.TestCase):
    class Control:
        def __init__(self):
            self.level = 64

        def set(self, level):
            self.level = level
            return True

        def describe(self):
            return {"level": self.level, "available": True,
                    "persistent": True, "error": "", "controls": []}

    def setUp(self):
        cfg = SimpleNamespace(host="127.0.0.1", port=0,
                              url="http://127.0.0.1:0")
        self.control = self.Control()
        self.web = WebUI(cfg, state=UiState(), history=None, info=lambda: {},
                         volume=self.control)
        self.assertTrue(self.web.start())
        self.port = self.web._server.server_address[1]

    def tearDown(self):
        self.web.stop()

    def test_the_local_endpoint_sets_and_reads_the_master_level(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        body = json.dumps({"level": 23})
        connection.request("POST", "/api/volume", body,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        answer = json.loads(response.read())
        connection.close()
        self.assertEqual(200, response.status)
        self.assertTrue(answer["ok"])
        self.assertEqual(23, answer["level"])
        self.assertEqual(23, self.control.level)

    def test_out_of_range_input_is_refused(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        body = json.dumps({"level": 101})
        connection.request("POST", "/api/volume", body,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(400, response.status)
        self.assertEqual(64, self.control.level)


class TestTheSetting(unittest.TestCase):
    def test_the_shipped_file_has_the_key(self):
        """`edit_setting` refuses a key that is not already in the file --
        deliberately, so the agent can only change what somebody put there.
        A volume the agent cannot reach is the bug this closes.

        The *value* is deliberately not asserted. This file carries local
        deltas on the device -- `call.enabled: true` is the long-standing one,
        and a volume somebody has turned down is now another. Pinning the
        number made this fail on the one machine where it matters, which is
        the opposite of what a test is for.
        """
        self.assertIn("volume:", CONFIG.read_text(encoding="utf-8"))
        level = load(CONFIG).audio.volume
        self.assertIsInstance(level, int)
        self.assertTrue(0 <= level <= 100)

    def test_it_is_under_a_section_of_its_own(self):
        """`set_config` takes a section and a key, so the section has to exist
        and has to be the one somebody would name out loud."""
        text = CONFIG.read_text(encoding="utf-8")
        self.assertIn("\naudio:\n", text)

    def test_a_silly_number_in_the_file_does_not_stop_the_device(self):
        import re
        import tempfile

        original = CONFIG.read_text(encoding="utf-8")
        # Whatever the volume happens to be. This file is edited on the device
        # -- by hand and by the agent -- so the number is not the test's to
        # know, and pinning it is what made this fail on the one machine where
        # it matters.
        pattern = re.compile(r"(\naudio:\n\s+volume:\s*)(-?\d+)")
        self.assertIsNotNone(pattern.search(original),
                             "audio.volume is not where this test expects it")
        for silly, wanted in (("500", 100), ("-3", 0)):
            with self.subTest(silly=silly):
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "aipi5.yaml"
                    path.write_text(pattern.sub(r"\g<1>" + silly, original, 1),
                                    encoding="utf-8")
                    self.assertEqual(load(path).audio.volume, wanted)


if __name__ == "__main__":
    unittest.main()


class TestSayingItOutLoud(unittest.TestCase):
    """"Turn the volume to thirty", said in the room, must move the room.

    It did not. AIA's only volume command belongs to the Kodama-Lite plugin and
    sets the *music player's* own level, and its phrases are the ones people
    say for the house -- `volume {level}`, `音量调到{level}`. The fast router
    matches before the model is ever asked, so every spoken volume request went
    to the music player: nothing else in the house changed, and when the player
    was not running the answer was "the music player is not running".

    Typing the same words went elsewhere. The compose box does not run the
    router, so it reached the model, and the model has `set_master_volume`.
    Two ways of asking, two different volumes.
    """

    def setUp(self):
        self.control = _Recording()
        self.plugin = volume.SystemVolume(self.control)
        self.command = next(c for c in self.plugin.commands()
                            if c.name == "set_volume")

    def run_it(self, said: str):
        return self.command.handler(level=said)

    def test_it_sets_the_shared_level(self):
        self.assertTrue(self.run_it("30").ok)
        self.assertEqual([30], self.control.asked)

    def test_it_reads_back_what_the_device_took_and_not_what_was_asked(self):
        """`VolumeControl.set` rolls back when it cannot persist, so a reply
        built from the request would announce a change already undone."""
        self.control.reports = 42          # what the sink says afterwards
        answer = self.run_it("30")
        self.assertIn("42", answer.say("en"))
        self.assertNotIn("30", answer.say("en"))

    def test_mandarin_numerals_arrive_as_words_and_are_understood(self):
        """The router captures the argument as it was said, so this handler is
        given 二十 rather than 20."""
        self.assertTrue(self.run_it("二十").ok)
        self.assertEqual([20], self.control.asked)

    def test_a_percentage_in_either_language(self):
        for said, wanted in (("fifty percent", 50), ("百分之五十", 50)):
            with self.subTest(said=said):
                self.control.asked.clear()
                self.assertTrue(self.run_it(said).ok)
                self.assertEqual([wanted], self.control.asked)

    def test_a_request_with_no_number_asks_rather_than_guessing(self):
        answer = self.run_it("")
        self.assertFalse(answer.ok)
        self.assertEqual([], self.control.asked)
        self.assertIn("What volume", answer.say("en"))

    def test_a_device_whose_audio_has_gone_says_so(self):
        self.control.accepts = False
        answer = self.run_it("30")
        self.assertFalse(answer.ok)
        self.assertIn("could not", answer.say("en"))

    def test_the_reply_is_spoken(self):
        """The confirmation arrives *at* the new level, which is the only
        feedback somebody across the room gets."""
        self.assertTrue(self.command.speaks)

    def test_the_plugin_is_never_reported_as_not_running(self):
        """`main.py` refuses a whole chain when a plugin says it is
        unavailable, as "<description> is not currently running" -- which for a
        volume control is untrue and unhelpful."""
        self.assertTrue(self.plugin.available())


class TestOnlyOneThingAnswersAVolumeRequest(unittest.TestCase):
    """The router ranks candidates by score, and two commands with identical
    phrases score identically -- so which volume changed would have come down
    to the order `main.py` built a list in. Kodama's is withheld instead."""

    def test_the_player_no_longer_offers_a_volume_command(self):
        from aipi5.kodama.launcher import AIPI5Player

        self.assertNotIn("volume",
                         {c.name for c in AIPI5Player().commands()})

    def test_everything_else_the_player_does_is_untouched(self):
        from aia.plugins.kodama import KodamaLite
        from aipi5.kodama.launcher import AIPI5Player

        theirs = {c.name for c in KodamaLite().commands()}
        ours = {c.name for c in AIPI5Player().commands()}
        self.assertEqual({"volume"}, theirs - ours)
        self.assertTrue(len(ours) > 5)

    def test_the_phrases_kodama_used_now_reach_the_shared_level(self):
        """The exact spellings that used to change the music player."""
        spoken = {p for c in volume.SystemVolume(_Recording()).commands()
                  for p in c.phrases["en"] + c.phrases["zh"]}
        for phrase in ("volume {level}", "set volume to {level}",
                       "turn the volume to {level}", "音量调到{level}",
                       "把音量调到{level}", "声音调到{level}"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, spoken)

    def test_the_registry_offers_exactly_one_volume_command(self):
        """Asserted over `main.py`, which is where the two are put together."""
        main = (Path(__file__).resolve().parent.parent
                / "aipi5" / "main.py").read_text(encoding="utf-8")
        self.assertIn("volume_control.SystemVolume(self.volume)", main)
        self.assertIn("AIPI5Player()", main)
        self.assertNotIn("self.player = KodamaLite()", main)


class _Recording:
    """`VolumeControl`'s two methods, and what was asked of them."""

    def __init__(self, level: int = 30, accepts: bool = True):
        self.level = level
        self.accepts = accepts
        self.asked: list[int] = []
        #: What the sink reports afterwards, when that differs from what was
        #: asked -- a rolled-back write, or a device that clamped it.
        self.reports: int | None = None

    def set(self, percent: int) -> bool:
        self.asked.append(percent)
        if not self.accepts:
            return False
        self.level = percent
        return True

    def describe(self) -> dict:
        level = self.level if self.reports is None else self.reports
        return {"level": level, "configured": self.level,
                "available": self.accepts, "persistent": True,
                "error": "" if self.accepts else "the sink is gone",
                "controls": []}
