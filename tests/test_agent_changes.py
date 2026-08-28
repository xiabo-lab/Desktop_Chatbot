"""Changing a file so it can be changed back.

Two properties here can destroy a working device, and neither announces itself.

**The edit must be surgical.** `config/aipi5.yaml` is six hundred lines of which
most are the reasons for the values, and it carries deliberate local deltas —
`call.enabled: true` on the device where the repository says false, and
`agent.enabled` beside it. A round trip through `yaml.safe_load` would drop
every comment, re-quote every string, and turn the diff a person is asked to
approve into the whole file. So one line changes and nothing else does, and
that is asserted rather than assumed.

**A key that does not exist must be refused, not added.** `_from_mapping` in
`aipi5/core/config.py` is hand-written and silently ignores keys it does not
know, so inventing one produces a file that looks changed and a device that
behaves identically — the worst thing to hand back to somebody who just
approved it.

The two-phase gate is tested here too: it is the one guarantee the *helper* can
make about approval without being able to see a person.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "aipi5" / "agent" / "helper"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))

import changes   # noqa: E402
import ops       # noqa: E402
import policy    # noqa: E402

SAMPLE = '''# The screen that comes up when nobody is there.
screensaver:
  # 07:00 because a photograph at six in the morning is a light nobody asked
  # for.
  day_start: "07:00"
  night_start: "21:01"
  timeout_s: 180.0

call:
  # True here and false in the repository. Deliberate: this device is behind
  # tailscale serve, which terminates TLS in front of it.
  enabled: true
  port: 8443

assistant:
  llm_enabled: true
'''


class TestEditingInPlace(unittest.TestCase):

    def test_only_the_one_line_changes(self):
        updated, before, after = changes.edit_setting(
            SAMPLE, "screensaver", "day_start", '"08:00"')
        self.assertEqual('"07:00"', before)
        self.assertEqual('"08:00"', after)

        was, now = SAMPLE.splitlines(), updated.splitlines()
        self.assertEqual(len(was), len(now))
        differing = [i for i, (a, b) in enumerate(zip(was, now)) if a != b]
        self.assertEqual(1, len(differing), "more than one line moved")
        self.assertIn("day_start", now[differing[0]])

    def test_every_comment_survives(self):
        updated, _, _ = changes.edit_setting(SAMPLE, "screensaver",
                                             "day_start", '"08:00"')
        for line in SAMPLE.splitlines():
            if line.strip().startswith("#"):
                with self.subTest(line=line.strip()[:40]):
                    self.assertIn(line, updated)

    def test_the_local_delta_in_another_section_is_untouched(self):
        """The outage this rule exists to prevent: `call.enabled` going false."""
        updated, _, _ = changes.edit_setting(SAMPLE, "screensaver",
                                             "day_start", '"08:00"')
        self.assertIn("  enabled: true", updated)

    def test_a_key_of_the_same_name_in_another_section_is_not_touched(self):
        text = "alpha:\n  value: 1\n\nbeta:\n  value: 2\n"
        updated, before, _ = changes.edit_setting(text, "beta", "value", "9")
        self.assertEqual("2", before)
        self.assertIn("alpha:\n  value: 1", updated)
        self.assertIn("beta:\n  value: 9", updated)

    def test_an_inline_comment_is_kept(self):
        text = 'a:\n  b: 1  # why it is one\n'
        updated, _, _ = changes.edit_setting(text, "a", "b", "2")
        self.assertIn("b: 2", updated)
        self.assertIn("why it is one", updated)

    def test_a_hash_inside_quotes_is_not_a_comment(self):
        text = 'a:\n  b: "the # sign"\n'
        found = changes.find_setting(text, "a", "b")
        self.assertEqual('"the # sign"', found[2])

    def test_the_indentation_is_preserved(self):
        updated, _, _ = changes.edit_setting(SAMPLE, "screensaver",
                                             "timeout_s", "240.0")
        self.assertIn("  timeout_s: 240.0", updated)
        self.assertNotIn("timeout_s: 240.0\n", updated.replace("  timeout_s", "x"))

    def test_a_missing_key_is_refused_rather_than_added(self):
        """A key config.py does not know is a change with no effect."""
        with self.assertRaises(changes.Refused) as caught:
            changes.edit_setting(SAMPLE, "screensaver", "invented_key", "1")
        self.assertIn("does not add new ones", str(caught.exception))

    def test_a_missing_section_is_refused(self):
        with self.assertRaises(changes.Refused):
            changes.edit_setting(SAMPLE, "nonsuch", "day_start", "1")

    def test_a_key_belonging_to_the_next_section_is_not_found(self):
        """Scanning must stop at the next top-level key, or a search for
        screensaver.llm_enabled would find the assistant's."""
        self.assertIsNone(changes.find_setting(SAMPLE, "screensaver",
                                               "llm_enabled"))

    def test_the_quoting_of_the_old_value_is_kept(self):
        """Dropping quotes off a time is a silent, value-dependent disaster.

        Measured with this project's own PyYAML: `08:00` unquoted loads as the
        string "08:00", because a leading zero makes it invalid sexagesimal —
        but `21:01` unquoted loads as the **integer 1261**. So the same edit
        works on one setting and destroys another, and the file still parses
        either way, so nothing downstream notices. The first version of
        `edit_setting` dropped them, and the device showed it.
        """
        for current, new, expected in (
                ('"07:00"', "08:00", '"08:00"'),
                ('"21:01"', "22:00", '"22:00"'),
                ("'07:00'", "08:00", "'08:00'"),
                ('"07:00"', '"08:00"', '"08:00"'),   # already quoted
                ("180.0", "240.0", "240.0"),         # never was quoted
                ("true", "false", "false")):
            with self.subTest(current=current, new=new):
                self.assertEqual(expected, changes._match_quoting(current, new))

    def test_a_time_survives_a_yaml_round_trip_after_editing(self):
        """The property the quoting exists for, checked end to end."""
        yaml = __import__("yaml")
        text = 'screensaver:\n  night_start: "21:01"\n'
        updated, _, _ = changes.edit_setting(text, "screensaver",
                                             "night_start", "22:00")
        loaded = yaml.safe_load(updated)["screensaver"]["night_start"]
        self.assertEqual("22:00", loaded)
        self.assertIsInstance(loaded, str, "it came back as sexagesimal")

    def test_the_diff_shown_for_approval_names_one_line(self):
        diff = changes.diff_line("screensaver", "day_start", '"07:00"', '"08:00"')
        self.assertIn("screensaver.day_start", diff)
        self.assertIn('-  day_start: "07:00"', diff)
        self.assertIn('+  day_start: "08:00"', diff)


class TestTheChangeStore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "aipi5.yaml"
        self.file.write_text(SAMPLE, encoding="utf-8")
        self._saved = policy.CHANGES_DIR
        policy.CHANGES_DIR = self.tmp / "changes"
        policy.CHANGES_DIR.mkdir()
        self.addCleanup(self._restore)

    def _restore(self):
        policy.CHANGES_DIR = self._saved

    def apply_one(self, value='"08:00"'):
        ident = changes.change_id("set_config", {"v": value})
        changes.record(ident, self.file, "set_config", {"value": value}, "r-1")
        updated, _, _ = changes.edit_setting(
            self.file.read_text(encoding="utf-8"), "screensaver",
            "day_start", value)
        changes.write(self.file, updated)
        changes.finish(ident, self.file, True, "")
        return ident

    def test_a_change_can_be_undone(self):
        ident = self.apply_one()
        self.assertIn('day_start: "08:00"', self.file.read_text(encoding="utf-8"))
        changes.restore(ident)
        self.assertEqual(SAMPLE, self.file.read_text(encoding="utf-8"))

    def test_undoing_refuses_when_somebody_else_edited_it_since(self):
        """Not an error — a refusal, so the agent can ask rather than discard
        an edit a person made by hand."""
        ident = self.apply_one()
        self.file.write_text(SAMPLE.replace("21:01", "22:00"), encoding="utf-8")
        with self.assertRaises(changes.Refused) as caught:
            changes.restore(ident)
        self.assertIn("edited since", str(caught.exception))

    def test_force_overrides_that_refusal(self):
        ident = self.apply_one()
        self.file.write_text("clobbered\n", encoding="utf-8")
        changes.restore(ident, force=True)
        self.assertEqual(SAMPLE, self.file.read_text(encoding="utf-8"))

    def test_the_manifest_records_what_was_asked_for(self):
        ident = self.apply_one()
        manifest = changes.read_manifest(ident)
        self.assertEqual("set_config", manifest["op"])
        self.assertEqual(str(self.file), manifest["path"])
        self.assertEqual("r-1", manifest["run"])
        self.assertTrue(manifest["applied"])
        self.assertTrue(manifest["before_sha"])
        self.assertNotEqual(manifest["before_sha"], manifest["after_sha"])

    def test_an_unknown_change_id_is_refused_rather_than_crashing(self):
        with self.assertRaises(changes.Refused):
            changes.read_manifest("nonsuch")
        with self.assertRaises(changes.Refused):
            changes.restore("../../etc/passwd")

    def test_the_listing_is_newest_first(self):
        first = self.apply_one('"08:00"')
        time.sleep(1.05)                    # the id carries whole seconds
        second = self.apply_one('"09:00"')
        listed = [row["id"] for row in changes.listing(10)]
        self.assertEqual([second, first], listed[:2])

    @unittest.skipUnless(os.name == "posix",
                         "Windows has no mode bits to preserve")
    def test_writing_keeps_the_files_mode(self):
        """A 0600 secret must not become world-readable by being edited."""
        os.chmod(self.file, 0o600)
        changes.write(self.file, "replaced\n")
        self.assertEqual(0o600, self.file.stat().st_mode & 0o777)

    def test_writing_leaves_no_temporary_behind(self):
        changes.write(self.file, "replaced\n")
        leftovers = [p.name for p in self.tmp.iterdir()
                     if p.name.startswith(".")]
        self.assertEqual([], leftovers)


class TestTheTwoPhaseGate(unittest.TestCase):
    """The one thing the helper can guarantee about approval on its own."""

    def setUp(self):
        ops._proposals.clear()
        ops._recent.clear()

    def test_applying_without_proposing_is_refused(self):
        with self.assertRaises(ops.Refused) as caught:
            ops._claim_proposal("nothing-was-proposed", "r-1")
        self.assertIn("not proposed", str(caught.exception))

    def test_a_proposal_is_single_use(self):
        ops._remember_proposal("abc", "r-1", "a diff")
        ops._proposals["abc"]["at"] -= 10.0          # old enough to claim
        ops._claim_proposal("abc", "r-1")
        with self.assertRaises(ops.Refused):
            ops._claim_proposal("abc", "r-1")

    def test_an_instant_reply_is_refused(self):
        """Nobody read a diff in under three seconds."""
        ops._remember_proposal("abc", "r-1", "a diff")
        with self.assertRaises(ops.Refused) as caught:
            ops._claim_proposal("abc", "r-1")
        self.assertIn("faster than a person", str(caught.exception))

    def test_a_stale_proposal_is_refused(self):
        ops._remember_proposal("abc", "r-1", "a diff")
        ops._proposals["abc"]["at"] -= ops.PROPOSAL_MAX_AGE_S + 10
        with self.assertRaises(ops.Refused) as caught:
            ops._claim_proposal("abc", "r-1")
        self.assertIn("ten minutes", str(caught.exception))

    def test_a_proposal_from_another_run_is_refused(self):
        ops._remember_proposal("abc", "r-1", "a diff")
        ops._proposals["abc"]["at"] -= 10.0
        with self.assertRaises(ops.Refused):
            ops._claim_proposal("abc", "r-2")

    def test_the_hash_covers_the_value_so_an_apply_cannot_drift(self):
        """Approving 8 AM must not license applying 3 AM."""
        eight = changes.args_hash("set_config", {"section": "screensaver",
                                                 "key": "day_start",
                                                 "value": '"08:00"'})
        three = changes.args_hash("set_config", {"section": "screensaver",
                                                 "key": "day_start",
                                                 "value": '"03:00"'})
        self.assertNotEqual(eight, three)

    def test_twenty_changes_an_hour_and_then_it_stops(self):
        for _ in range(ops.MUTATIONS_PER_HOUR):
            ops._rate_limit()
        with self.assertRaises(ops.Refused) as caught:
            ops._rate_limit()
        self.assertIn("limit", str(caught.exception))


class TestValidatingTheArguments(unittest.TestCase):

    def test_a_section_or_key_that_is_not_a_name_is_refused(self):
        for bad in ({"section": "../etc", "key": "a", "value": 1},
                    {"section": "a b", "key": "a", "value": 1},
                    {"section": "a", "key": "b: c", "value": 1},
                    {"section": "", "key": "a", "value": 1},
                    {"section": "A", "key": "a", "value": 1},
                    {"section": None, "key": "a", "value": 1}):
            with self.subTest(args=bad):
                with self.assertRaises(ops.Refused):
                    ops._config_target(bad)

    def test_a_value_with_a_line_break_is_refused(self):
        """Otherwise one setting becomes two lines of somebody else's file.

        **Pointed at a path the policy actually allows**, which the first
        version of this test was not. It passed on Windows because
        `/home/fuwenxu/...` does not resolve there, so the *path* check
        refused first and the value check was never reached -- and the
        value check was wrong. It read `"\r\n" in value`, so a
        bare newline sailed through and one setting could become two
        lines of YAML. The device found it; the laptop never could.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / 'aipi5.yaml').write_text('a:\n  b: 1\n',
                                        encoding='utf-8')
        saved = policy.WRITABLE
        policy.WRITABLE = ((tmp, ('*.yaml',), False),)
        self.addCleanup(lambda: setattr(policy, 'WRITABLE', saved))
        target = str(tmp / 'aipi5.yaml')

        # Every line ending, not one spelling of one.
        for ending in ('\n', '\r', '\r\n'):
            with self.subTest(ending=repr(ending)):
                with self.assertRaises(ops.Refused):
                    ops._config_target({'section': 'a', 'key': 'b',
                                        'path': target,
                                        'value': '1' + ending + 'rogue: true'})

        # And an ordinary value must still be accepted through the same
        # path, or the assertions above would pass with a check that
        # refuses everything.
        _, _, _, value = ops._config_target(
            {'section': 'a', 'key': 'b', 'value': '08:00',
             'path': target})
        self.assertEqual('08:00', value)

    def test_a_value_of_the_wrong_shape_is_refused(self):
        for bad in ([], {}, None):
            with self.subTest(value=bad):
                with self.assertRaises(ops.Refused):
                    ops._config_target({"section": "a", "key": "b", "value": bad})

    def test_values_are_written_as_yaml_not_as_python(self):
        """`True` is what Python prints; `true` is what YAML reads.

        Pointed at a temporary directory rather than the real repository, so
        this runs on the machine the code is written on and not only on the Pi.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "aipi5.yaml").write_text("a:\n  b: 1\n", encoding="utf-8")
        saved = policy.WRITABLE
        policy.WRITABLE = ((tmp, ("*.yaml",), False),)
        self.addCleanup(lambda: setattr(policy, "WRITABLE", saved))

        target = str(tmp / "aipi5.yaml")
        for given, expected in ((True, "true"), (False, "false"),
                                (8, "8"), (1.5, "1.5"), ("07:00", "07:00")):
            with self.subTest(value=given):
                _, _, _, value = ops._config_target(
                    {"section": "a", "key": "b", "value": given,
                     "path": target})
                self.assertEqual(expected, value)

    def test_a_path_outside_the_writable_directories_is_refused(self):
        """`aipi5/main.py` is deliberately absent: stage 6 made source
        writable. The exclusions are tested in `test_agent_source.py`,
        against a temporary tree so they hold on both machines."""
        for bad in ("/etc/passwd", "/home/fuwenxu/.bashrc",
                    "/home/fuwenxu/AIPI5/scripts/deploy.sh",
                    "/home/fuwenxu/AIPI5/aipi5/agent/tools.py"):
            with self.subTest(path=bad):
                with self.assertRaises(ops.Refused):
                    ops._config_target({"section": "a", "key": "b",
                                        "value": 1, "path": bad})


if __name__ == "__main__":
    unittest.main()
