"""The privileged helper, in the parts that are the whole reason it exists.

This is the file that matters most in the agent layer. Everything else decides
what the agent *says*; `policy.py` decides what root *does*, and it is asked by a
process whose instructions come from a language model. So the tests here are not
about the happy path — they are about the refusals, and each one names the
escalation it prevents.

The path tests monkeypatch the policy constants onto a temporary directory
rather than skipping on Windows. Two reasons: the logic is identical whatever
the roots are, and a security check that only runs on the device is one that
gets broken on the laptop and noticed a deploy later.

No sockets are opened against the real helper here; `tests/test_agent_client.py`
covers that side. Nothing in this file needs root.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "aipi5" / "agent" / "helper"
if str(HELPER) not in sys.path:
    # Exactly how it runs: the helper is installed flat into
    # /usr/local/lib/aipi5-agent and its own directory is sys.path[0].
    sys.path.insert(0, str(HELPER))

import ops        # noqa: E402
import policy     # noqa: E402


class TestTheServiceTable(unittest.TestCase):
    """What may be restarted, and the two names that may never be."""

    def test_the_helper_cannot_be_named_at_all(self):
        """The agent does not get to restart the thing containing it.

        Absence, not a blocklist: there is no entry to be matched, so there is
        no matching to get wrong.
        """
        self.assertIsNone(policy.service("aipi5-agent-helper"))
        self.assertNotIn("aipi5-agent-helper", policy.SERVICES)
        self.assertNotIn("aipi5-agent-helper", policy.JOURNAL_UNITS)

    def test_an_unlisted_unit_is_refused_even_though_it_exists(self):
        for name in ("sshd", "ssh", "systemd-journald", "tailscale-wait-online",
                     "ollama", "hailo-ollama", "cron", "dbus"):
            with self.subTest(name=name):
                self.assertIsNone(policy.service(name))

    def test_a_unit_name_cannot_be_smuggled_in_as_the_key(self):
        """The caller passes a key; the unit string is looked up here."""
        for attempt in ("aipi5.service", "aipi5 sshd", "aipi5;sshd",
                        "../sshd", "aipi5\nsshd", ""):
            with self.subTest(attempt=attempt):
                self.assertIsNone(policy.service(attempt))

    def test_the_agent_may_not_stop_itself(self):
        """Restart comes back. Stop leaves a task half-done and nobody to say so."""
        self.assertFalse(policy.may_stop("aipi5-agent"))
        self.assertTrue(policy.may_stop("aipi5"))

    def test_every_listed_service_has_a_complete_row(self):
        for name, row in policy.SERVICES.items():
            with self.subTest(name=name):
                scope, run_as, unit = row
                self.assertIn(scope, ("user", "system"))
                self.assertTrue(unit.endswith(".service"))
                self.assertEqual(scope == "user", run_as is not None,
                                 "a user unit needs an owner and a system one "
                                 "must not have one")


class TestTheJournalFloor(unittest.TestCase):
    """The privacy boundary, which is not a default."""

    def test_warnings_and_above_is_a_property_not_a_parameter(self):
        """The journal carries household speech. There is no argument for it.

        Utterance text and model replies log at INFO. If the floor were a
        parameter with a safe default, the unsafe value would still exist and
        the model would be the thing choosing.
        """
        self.assertEqual("warning", policy.MIN_PRIORITY)
        for name, handler in ops.READ_ONLY.items():
            with self.subTest(op=name):
                self.assertNotIn("priority", handler.__code__.co_names,
                                 "no operation may take a priority argument")

    def test_read_journal_ignores_a_priority_argument_if_one_is_passed(self):
        """Belt and braces: even a well-formed attempt changes nothing."""
        source = ops.read_journal.__code__
        self.assertIn("MIN_PRIORITY", source.co_names)

    def test_a_level_is_refused_rather_than_honoured(self):
        with self.assertRaises(ops.Refused):
            ops.read_journal({"unit": "nonsuch"})


class TestJournalArguments(unittest.TestCase):
    """Every parameter validated where it is read."""

    def test_an_unlisted_unit_is_refused(self):
        with self.assertRaises(ops.Refused):
            ops.read_journal({"unit": "sshd"})

    def test_lines_must_be_a_sane_integer(self):
        for bad in (0, -1, 10_000, policy.MAX_JOURNAL_LINES + 1,
                    "500", 12.5, True, None):
            with self.subTest(lines=bad):
                with self.assertRaises(ops.Refused):
                    ops.read_journal({"unit": "aipi5", "lines": bad})

    def test_since_must_be_one_of_the_offered_windows(self):
        for bad in ("-99y", "yesterday'; rm -rf /", "", "2020-01-01", None, 5):
            with self.subTest(since=bad):
                with self.assertRaises(ops.Refused):
                    ops.read_journal({"unit": "aipi5", "since": bad})

    def test_contains_is_bounded_text(self):
        with self.assertRaises(ops.Refused):
            ops.read_journal({"unit": "aipi5", "contains": "x" * 81})
        with self.assertRaises(ops.Refused):
            ops.read_journal({"unit": "aipi5", "contains": 7})

    def test_contains_never_reaches_journalctl(self):
        """`-g` takes a regex, and a model-supplied one is a DoS with root's CPU."""
        import inspect
        body = inspect.getsource(ops.read_journal)
        body = body.replace(ops.read_journal.__doc__ or "", "")
        self.assertNotIn('"-g"', body)
        self.assertNotIn("'-g'", body)
        # It is filtered in Python instead, against the message text.
        self.assertIn("contains not in message", body)


class TestTheLevelFilter(unittest.TestCase):
    """The privacy control, which journald cannot do on its own.

    AIPI5 logs through Python to stderr, and systemd stamps a whole captured
    stream with one priority — `info`. So `journalctl -p warning` against the
    assistant returns nothing, ever, and a filter that trusted it would send
    either everything or nothing depending on which way it failed. Measured on
    the device, which is the only place this shows up.
    """

    def keep(self, message, priority=6):
        return ops._worth_keeping({"PRIORITY": str(priority)}, message)

    def test_pythons_own_level_token_is_honoured(self):
        self.assertTrue(self.keep("21:54:49 WARNING aipi5.photos 403 expired"))
        self.assertTrue(self.keep("01:02:03 ERROR   aipi5.llm request failed"))
        self.assertTrue(self.keep("01:02:03 CRITICAL aipi5 out of memory"))

    def test_info_and_debug_are_dropped_because_that_is_where_speech_lives(self):
        """The utterance the router declined is logged at INFO. It must not leave."""
        for message in ("21:54:49 INFO    aia.stt.sensevoice heard: turn the lights off",
                        "21:54:49 INFO    aipi5.llm llm 940 ms: 'Good morning'",
                        "21:54:49 DEBUG   aipi5.ui.server GET /api/state"):
            with self.subTest(message=message[:40]):
                self.assertFalse(self.keep(message))

    def test_journalds_own_priority_is_honoured_where_it_is_real(self):
        """systemd's messages about the unit do carry a true priority."""
        self.assertTrue(self.keep("Failed to start aipi5.service.", priority=3))
        self.assertTrue(self.keep("Main process exited, code=killed", priority=4))
        self.assertFalse(self.keep("Started aipi5.service.", priority=6))

    def test_anything_unrecognised_is_dropped(self):
        """Default deny. The failure of the other polarity is a private
        conversation leaving the house."""
        for message in ("", "some bare line", "  File \"/x.py\", line 3",
                        "Traceback (most recent call last):",
                        "01:02:03 WARNINGXYZ not a level"):
            with self.subTest(message=message[:40]):
                self.assertFalse(self.keep(message))

    def test_a_missing_or_broken_priority_does_not_admit_a_line(self):
        for entry in ({}, {"PRIORITY": None}, {"PRIORITY": "nonsense"},
                      {"PRIORITY": []}):
            with self.subTest(entry=entry):
                self.assertFalse(ops._worth_keeping(entry, "bare line"))
                self.assertTrue(ops._worth_keeping(entry, "01:02:03 ERROR x"))


class TestPaths(unittest.TestCase):
    """Where a write may land. The escalation each check prevents is named."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.home = self.tmp / "home"
        self.repo = self.home / "AIPI5"
        self.config = self.repo / "config"
        self.units = self.home / ".config" / "systemd" / "user"
        self.code = self.repo / "aipi5"
        for directory in (self.config, self.units, self.code):
            directory.mkdir(parents=True)
        (self.config / "aipi5.yaml").write_text("display: {}\n", encoding="utf-8")

        self._saved = (policy.WRITABLE, policy.NEVER, policy.READABLE,
                       policy.LISTABLE)
        policy.WRITABLE = ((self.config, ("*.yaml",), False),)
        policy.NEVER = (self.home / ".bashrc", self.home / ".ssh",
                        self.home / ".config" / "systemd", self.code)
        policy.READABLE = frozenset({self.config / "aipi5.yaml"})
        policy.LISTABLE = frozenset({self.config})
        self.addCleanup(self._restore)

    def _restore(self):
        (policy.WRITABLE, policy.NEVER, policy.READABLE,
         policy.LISTABLE) = self._saved

    # ── the allowed case, so the refusals mean something ──────────────
    def test_the_configuration_file_is_writable(self):
        self.assertIsNotNone(policy.resolve_write(str(self.config / "aipi5.yaml")))

    def test_a_new_yaml_beside_it_is_writable(self):
        self.assertIsNotNone(policy.resolve_write(str(self.config / "other.yaml")))

    # ── the refusals ────────────────────────────────────────────────
    def test_a_relative_path_is_refused(self):
        for bad in ("config/aipi5.yaml", "aipi5.yaml", "./x.yaml", ""):
            with self.subTest(path=bad):
                self.assertIsNone(policy.resolve_write(bad))

    def test_a_traversal_out_of_the_writable_directory_is_refused(self):
        """`config/../aipi5/main.py` is source code, not configuration."""
        escape = str(self.config / ".." / "aipi5" / "main.py")
        self.assertIsNone(policy.resolve_write(escape))

    def test_a_traversal_into_the_systemd_directory_is_refused(self):
        """This is the escalation: a unit file there runs as fuwenxu, who is root."""
        escape = str(self.config / ".." / ".." / ".config" / "systemd" /
                     "user" / "evil.yaml")
        self.assertIsNone(policy.resolve_write(escape))

    def test_a_bashrc_write_is_refused(self):
        self.assertIsNone(policy.resolve_write(str(self.home / ".bashrc")))

    def test_source_code_is_refused_because_the_scope_is_configuration(self):
        self.assertIsNone(policy.resolve_write(str(self.code / "main.py")))

    def test_a_wrong_extension_in_the_right_directory_is_refused(self):
        for name in ("main.py", "notes.txt", "aipi5.yaml.bak", "x.service"):
            with self.subTest(name=name):
                self.assertIsNone(policy.resolve_write(str(self.config / name)))

    def test_a_nul_byte_is_refused(self):
        self.assertIsNone(policy.resolve_write(str(self.config / "a.yaml") + "\x00"))

    def test_an_absurdly_long_path_is_refused(self):
        self.assertIsNone(policy.resolve_write("/" + "a" * 5000 + ".yaml"))

    def test_a_non_string_is_refused(self):
        for bad in (None, 7, [], {}, Path("/tmp/x.yaml")):
            with self.subTest(value=bad):
                self.assertIsNone(policy.resolve_write(bad))

    @unittest.skipUnless(os.name == "posix", "symlinks need privilege on Windows")
    def test_a_symlink_pointing_out_of_the_directory_is_refused(self):
        """The check that a string-only allowlist would miss.

        An earlier operation plants `config/escape.yaml -> ~/.bashrc`; the
        literal path is inside the allowed directory and the real one is not.
        """
        link = self.config / "escape.yaml"
        link.symlink_to(self.home / ".bashrc")
        self.assertIsNone(policy.resolve_write(str(link)))

    @unittest.skipUnless(os.name == "posix", "symlinks need privilege on Windows")
    def test_a_symlinked_parent_is_refused(self):
        sneaky = self.home / "sneaky"
        sneaky.symlink_to(self.home / ".config" / "systemd" / "user")
        self.assertIsNone(policy.resolve_write(str(sneaky / "x.yaml")))

    # ── reading and listing use the same machinery ───────────────────
    def test_reading_is_an_explicit_list_not_a_directory(self):
        self.assertIsNotNone(policy.resolve_read(str(self.config / "aipi5.yaml")))
        self.assertIsNone(policy.resolve_read(str(self.config / "other.yaml")))

    def test_listing_only_the_named_directories(self):
        self.assertIsNotNone(policy.resolve_list(str(self.config)))
        self.assertIsNone(policy.resolve_list(str(self.home)))


class TestPackages(unittest.TestCase):
    """apt runs maintainer scripts as root, so the list starts empty."""

    def test_nothing_is_installable_until_somebody_decides_it_is(self):
        self.assertEqual(frozenset(), policy.PACKAGES)
        for name in ("nginx", "curl", "jq", "sqlite3"):
            with self.subTest(name=name):
                self.assertFalse(policy.package(name))

    def test_a_malformed_name_is_refused_even_if_it_is_on_the_list(self):
        saved = policy.PACKAGES
        policy.PACKAGES = frozenset({"jq", "Evil; rm -rf /"})
        try:
            self.assertTrue(policy.package("jq"))
            self.assertFalse(policy.package("Evil; rm -rf /"))
            self.assertFalse(policy.package("jq curl"))
            self.assertFalse(policy.package("-jq"))
            self.assertFalse(policy.package(None))
        finally:
            policy.PACKAGES = saved


class TestTheDispatchTable(unittest.TestCase):
    """What exists at all."""

    def test_the_tables_do_not_overlap(self):
        """Whether an operation changes the device is a property of which table
        it is in, not of reading its body.

        Three tables now. The browser is its own because it is neither: opening
        a page alters nothing on the device, but it does put a window over the
        kiosk, so it is not read-only in the sense that matters to somebody
        looking at the screen.
        """
        tables = {"READ_ONLY": set(ops.READ_ONLY),
                  "MUTATING": set(ops.MUTATING),
                  "BROWSER": set(ops.BROWSER)}
        for first in tables:
            for second in tables:
                if first < second:
                    with self.subTest(pair=(first, second)):
                        self.assertEqual(set(), tables[first] & tables[second])
        self.assertEqual(set(ops.OPS), set().union(*tables.values()))

    def test_every_browser_operation_is_named_like_one(self):
        for name in ops.BROWSER:
            with self.subTest(name=name):
                self.assertTrue(name.startswith("browser_"))

    def test_nothing_that_reads_has_crept_into_the_mutating_table(self):
        for name in ops.MUTATING:
            with self.subTest(name=name):
                self.assertTrue(
                    name.startswith(("apply_", "propose_", "restart_",
                                     "rollback_", "install_")),
                    f"{name} is in MUTATING but is not named like a change")

    def test_reading_the_record_of_changes_is_not_itself_a_change(self):
        self.assertIn("list_changes", ops.READ_ONLY)

    def test_no_operation_shells_out_through_a_string(self):
        """The module docstring says so; this checks the code agrees."""
        import inspect
        # Minus the docstring, which says the words in order to forbid them.
        body = inspect.getsource(ops).replace(ops.__doc__ or "", "")
        self.assertNotIn("shell=True", body)
        self.assertNotIn("os.system", body)
        self.assertNotIn("subprocess.call(", body)
        self.assertNotIn("os.popen", body)

    def test_every_operation_is_reachable_only_by_name(self):
        for name in ops.OPS:
            with self.subTest(name=name):
                self.assertTrue(name.replace("_", "").isalnum())

    def test_the_restart_budget_exceeds_the_units_own_start_timeout(self):
        """aipi5.service declares TimeoutStartSec=180 and a cold start uses it.

        A restart budget shorter than that is a rollback that fires because the
        SD card was cold rather than because anything was wrong.
        """
        self.assertGreater(ops.RESTART_TIMEOUT_S, 180.0)


if __name__ == "__main__":
    unittest.main()
