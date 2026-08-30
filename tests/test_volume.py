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
import unittest
from pathlib import Path
from unittest import mock

from aipi5.core import volume
from aipi5.core.config import load

CONFIG = Path(__file__).resolve().parent.parent / "config" / "aipi5.yaml"


def ran(returncode=0, stdout="", stderr=""):
    return mock.Mock(return_value=subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr))


class TestSettingIt(unittest.TestCase):

    def test_a_percentage_becomes_a_fraction_of_the_default_sink(self):
        with mock.patch("subprocess.run", ran()) as call:
            self.assertTrue(volume.apply(55))
        args = call.call_args[0][0]
        self.assertEqual(args[:3], ["wpctl", "set-volume", volume.SINK])
        self.assertEqual(args[3], "0.55")

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
                self.assertEqual(call.call_args[0][0][3], wanted)


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
