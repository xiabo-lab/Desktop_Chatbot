"""Changing one setting in a file whose comments are most of its content.

`config/aipi5.yaml` is nine hundred lines of which the majority explain why
each number is what it is. Every test here is really the same test asked from
a different angle: **did anything change that nobody asked to change.**

The sexagesimal case below is the one that justifies the module existing
separately from a two-line `yaml.safe_dump`.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from aipi5.core import yaml_edit

SAMPLE = """\
# The screen.
display:
  width: 1280
  height: 800

audio:
  volume: 70  # room level
  gain: 10

screensaver:
  enabled: true
  day_start: "07:00"
  night_start: "21:01"
  day_mode: photos

games:
  enabled: true
"""


class TestFindingASetting(unittest.TestCase):

    def test_it_finds_the_value_the_indent_and_the_comment(self):
        index, indent, value, comment = yaml_edit.find_setting(
            SAMPLE, "audio", "volume")
        self.assertEqual("70", value)
        self.assertEqual("  ", indent)
        self.assertEqual("room level", comment)
        self.assertEqual("  volume: 70  # room level", SAMPLE.splitlines()[index])

    def test_a_key_is_scoped_to_its_own_section(self):
        """`enabled` is under both `screensaver` and `games`. Reaching the
        wrong one would turn the screensaver off when somebody asked about the
        games, and the file would still parse."""
        _, _, screensaver, _ = yaml_edit.find_setting(
            SAMPLE, "screensaver", "enabled")
        line, _, _, _ = yaml_edit.find_setting(SAMPLE, "games", "enabled")
        self.assertEqual("true", screensaver)
        self.assertEqual("  enabled: true", SAMPLE.splitlines()[line])
        self.assertGreater(line, SAMPLE.splitlines().index("games:"))

    def test_a_missing_section_and_a_missing_key_are_both_none(self):
        self.assertIsNone(yaml_edit.find_setting(SAMPLE, "nothing", "volume"))
        self.assertIsNone(yaml_edit.find_setting(SAMPLE, "audio", "nothing"))


class TestTheCommentScan(unittest.TestCase):
    """A `#` inside quotes is not a comment. The obvious test — are the quotes
    balanced — answers a different question, and the first version of this
    returned `"the` as the value."""

    def test_a_hash_inside_quotes_is_part_of_the_value(self):
        self.assertEqual(('"the # sign"', ""),
                         yaml_edit.split_comment('"the # sign"'))

    def test_a_real_comment_is_separated(self):
        self.assertEqual(("70", "room level"),
                         yaml_edit.split_comment("70  # room level"))

    def test_a_value_with_no_comment(self):
        self.assertEqual(("70", ""), yaml_edit.split_comment("70"))


class TestQuotingIsPreserved(unittest.TestCase):
    """**The reason this module is not `yaml.safe_dump`.**

    Measured with this project's own PyYAML: `08:00` unquoted loads as the
    string "08:00", but `21:01` unquoted loads as the *integer* 1261 — YAML 1.1
    reads a colon-separated number as sexagesimal. Writing a time without its
    quotes therefore works for some values and silently destroys others, and
    leaves a file that parses either way so nothing downstream notices.
    """

    def test_the_language_really_does_this(self):
        """Asserted rather than asserted-about, so the day PyYAML changes its
        mind this test says so instead of the schedule quietly breaking."""
        self.assertEqual("21:01", yaml.safe_load('night_start: "21:01"')["night_start"])
        self.assertEqual(1261, yaml.safe_load("night_start: 21:01")["night_start"])

    def test_a_time_written_back_still_reads_as_a_time(self):
        updated, _, _ = yaml_edit.set_scalar(
            SAMPLE, "screensaver", "night_start", "22:30")
        self.assertEqual("22:30",
                         yaml.safe_load(updated)["screensaver"]["night_start"])

    def test_the_worst_value_specifically(self):
        """21:01 is the shipped default and the one that breaks."""
        updated, _, _ = yaml_edit.set_scalar(
            SAMPLE, "screensaver", "day_start", "21:01")
        self.assertIn('day_start: "21:01"', updated)
        self.assertEqual("21:01",
                         yaml.safe_load(updated)["screensaver"]["day_start"])

    def test_an_unquoted_value_stays_unquoted(self):
        updated, _, _ = yaml_edit.set_scalar(SAMPLE, "audio", "volume", 42)
        self.assertIn("volume: 42", updated)
        self.assertNotIn('volume: "42"', updated)
        self.assertEqual(42, yaml.safe_load(updated)["audio"]["volume"])

    def test_a_value_already_quoted_is_left_alone(self):
        self.assertEqual('"07:30"', yaml_edit.match_quoting('"07:00"', '"07:30"'))

    def test_a_value_containing_the_quote_uses_the_other_one(self):
        self.assertEqual("'say \"go\"'", yaml_edit.match_quoting('"x"', 'say "go"'))


class TestNothingElseMoves(unittest.TestCase):

    def test_the_comment_on_the_line_survives(self):
        updated, _, _ = yaml_edit.set_scalar(SAMPLE, "audio", "volume", 42)
        self.assertIn("  volume: 42  # room level", updated)

    def test_every_other_line_is_untouched(self):
        updated, _, _ = yaml_edit.set_scalar(SAMPLE, "audio", "volume", 42)
        before, after = SAMPLE.splitlines(), updated.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(1, len(differing))
        self.assertIn("volume", before[differing[0]])

    def test_it_reports_what_it_replaced(self):
        _, previous, written = yaml_edit.set_scalar(SAMPLE, "audio", "volume", 42)
        self.assertEqual("70", previous)
        self.assertEqual("42", written)

    def test_writing_the_same_value_changes_no_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "aipi5.yaml"
            path.write_text(SAMPLE, encoding="utf-8")
            before = path.read_bytes()
            yaml_edit.write_scalar(path, "audio", "volume", 70)
            self.assertEqual(before, path.read_bytes())


class TestAKeyThatIsNotThere(unittest.TestCase):
    """Refused rather than added. `_from_mapping` in `aipi5/core/config.py` is
    hand-written and ignores keys it does not know, so inventing one produces a
    file that looks changed and a device that behaves exactly as it did — the
    worst answer to give somebody who just pressed a button."""

    def test_it_raises(self):
        with self.assertRaises(yaml_edit.SettingMissing):
            yaml_edit.set_scalar(SAMPLE, "audio", "balance", 5)

    def test_it_is_an_oserror_so_existing_rollbacks_catch_it(self):
        """`VolumeControl.set` rolls the audible level back on `OSError`. A new
        exception type would have walked straight past that."""
        self.assertTrue(issubclass(yaml_edit.SettingMissing, OSError))

    def test_the_file_is_not_touched(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "aipi5.yaml"
            path.write_text(SAMPLE, encoding="utf-8")
            with self.assertRaises(yaml_edit.SettingMissing):
                yaml_edit.write_scalar(path, "audio", "balance", 5)
            self.assertEqual(SAMPLE, path.read_text(encoding="utf-8"))


class TestTheWriteItself(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "aipi5.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def test_it_writes_and_returns_the_previous_value(self):
        self.assertEqual("70", yaml_edit.write_scalar(
            self.path, "audio", "volume", 42))
        self.assertEqual(42, yaml.safe_load(
            self.path.read_text(encoding="utf-8"))["audio"]["volume"])

    def test_it_leaves_no_temporary_file_behind(self):
        yaml_edit.write_scalar(self.path, "audio", "volume", 42)
        self.assertEqual(["aipi5.yaml"],
                         [p.name for p in Path(self.folder.name).iterdir()])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_file_keeps_its_mode(self):
        """This runs while the assistant is up and the file is read at every
        start; a config that became unreadable would be a device that will not
        boot."""
        os.chmod(self.path, 0o640)
        yaml_edit.write_scalar(self.path, "audio", "volume", 42)
        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))


class TestVolumeStillGoesThroughIt(unittest.TestCase):
    """One implementation, not two. The quoting rule above is exactly the kind
    of detail that gets fixed once and reintroduced by a copy."""

    def test_persist_is_a_wrapper_and_not_a_second_editor(self):
        import inspect

        from aipi5.core import volume

        body = inspect.getsource(volume._persist)
        self.assertIn("yaml_edit.write_scalar", body)
        self.assertNotIn("splitlines", body)

    def test_the_voice_side_does_not_reach_into_the_root_helper(self):
        """`aipi5/agent/helper/changes.py` solved this first and stays where it
        is: it is root's code, deliberately not a package, and the boundary
        tests forbid importing it. Twenty similar lines is the cheaper
        mistake."""
        source = (Path(__file__).resolve().parent.parent / "aipi5" / "core"
                  / "yaml_edit.py").read_text(encoding="utf-8")
        self.assertNotIn("from aipi5.agent", source)
        self.assertNotIn("import changes", source)


if __name__ == "__main__":
    unittest.main()
