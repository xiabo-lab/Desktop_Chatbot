"""Letting the agent edit code, and the four places it must never reach.

This is the largest permission in the project. A configuration value is bounded
and is checked by `config.load()`; a line of Python is arbitrary code that will
run as `fuwenxu`, who carries `NOPASSWD: ALL`. So the allowlist is not the gate
here — the test suite is, and these tests are about the things the suite cannot
check about itself.

Four exclusions carry the weight, and each is excluded **by name** rather than
by happening to sit outside a directory somebody might widen later:

    aipi5/agent/**   its own runtime — the code that asks for approval
    scripts/**       the installer, which puts root-owned code in place
    systemd/**       what starts at boot, before anybody is watching
    /usr/local/lib/aipi5-agent   the policy that decides all of this

And one property that is about autonomy rather than paths: **nothing starts a
run except a person asking.** There is no scheduler for agent runs and there
must not be one, so a source change cannot happen while nobody is awake.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "aipi5" / "agent" / "helper"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))

import ops       # noqa: E402
import policy    # noqa: E402


class TestWhatMayBeWritten(unittest.TestCase):
    """Built on a temporary tree, so it runs on the laptop as well as the Pi."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.repo = self.home / "AIPI5"
        for part in ("config", "aipi5/ui/web", "aipi5/agent/helper",
                     "aipi5/games/yoga", "scripts", "systemd", ".ssh",
                     ".config/systemd/user"):
            (self.home / "AIPI5" / part if not part.startswith(".")
             else self.home / part).mkdir(parents=True, exist_ok=True)

        self._saved = (policy.OWNER_HOME, policy.REPO, policy.WRITABLE,
                       policy.NEVER)
        policy.OWNER_HOME, policy.REPO = self.home, self.repo
        policy.WRITABLE = (
            (self.repo / "config", ("*.yaml",), False),
            (self.repo / "aipi5", ("*.py", "*.html", "*.js", "*.css", "*.md"),
             True),
        )
        policy.NEVER = (
            self.repo / "aipi5" / "agent",
            self.repo / "scripts",
            self.repo / "systemd",
            self.home / ".ssh",
            self.home / ".config" / "systemd",
        )
        self.addCleanup(self._restore)

    def _restore(self):
        (policy.OWNER_HOME, policy.REPO, policy.WRITABLE,
         policy.NEVER) = self._saved

    def writable(self, relative):
        return policy.resolve_write(str(self.repo / relative)) is not None

    # ── what the stage is for ───────────────────────────────────────

    def test_source_anywhere_in_the_tree_may_be_changed(self):
        """Nested, because `aipi5/ui/server.py` is not a direct child."""
        for relative in ("aipi5/main.py", "aipi5/ui/server.py",
                         "aipi5/games/yoga/game.py",
                         "aipi5/ui/web/index.html", "config/aipi5.yaml"):
            with self.subTest(path=relative):
                self.assertTrue(self.writable(relative))

    # ── the four exclusions ─────────────────────────────────────────

    def test_it_cannot_edit_its_own_runtime(self):
        """An agent that can rewrite the code that asks for approval has
        nothing left to ask."""
        for relative in ("aipi5/agent/tools.py", "aipi5/agent/approvals.py",
                         "aipi5/agent/runtime.py",
                         "aipi5/agent/helper/policy.py",
                         "aipi5/agent/helper/ops.py"):
            with self.subTest(path=relative):
                self.assertFalse(self.writable(relative))

    def test_it_cannot_edit_the_installer(self):
        for relative in ("scripts/install-agent.sh", "scripts/deploy.sh"):
            with self.subTest(path=relative):
                self.assertFalse(self.writable(relative))

    def test_it_cannot_edit_what_starts_at_boot(self):
        for relative in ("systemd/aipi5.service",
                         "systemd/aipi5-agent-helper.service"):
            with self.subTest(path=relative):
                self.assertFalse(self.writable(relative))

    def test_it_cannot_reach_the_shell_that_grants_fuwenxu(self):
        for absolute in (self.home / ".bashrc", self.home / ".ssh" / "x.py",
                         self.home / ".config" / "systemd" / "user" / "x.py"):
            with self.subTest(path=str(absolute)):
                self.assertIsNone(policy.resolve_write(str(absolute)))

    def test_a_traversal_out_of_the_source_tree_is_refused(self):
        """The tree check is on the resolved path, or `aipi5/../scripts` works."""
        for escape in ("aipi5/../scripts/deploy.sh",
                       "aipi5/ui/../../systemd/aipi5.service",
                       "aipi5/../../.ssh/authorized_keys"):
            with self.subTest(path=escape):
                self.assertFalse(self.writable(escape))

    def test_only_source_extensions_are_writable(self):
        """A `.sh` inside the tree would be a script somebody runs."""
        for relative in ("aipi5/run.sh", "aipi5/x.service", "aipi5/notes.txt",
                         "aipi5/data.yaml", "config/main.py"):
            with self.subTest(path=relative):
                self.assertFalse(self.writable(relative))


class TestTheProductionPolicy(unittest.TestCase):
    """The real values, not a temporary tree. These ship."""

    def test_the_four_exclusions_are_named_in_never(self):
        wanted = {policy.REPO / "aipi5" / "agent",
                  policy.REPO / "scripts",
                  policy.REPO / "systemd",
                  Path("/usr/local/lib/aipi5-agent")}
        self.assertTrue(wanted.issubset(set(policy.NEVER)),
                        f"missing: {sorted(str(p) for p in wanted - set(policy.NEVER))}")

    def test_the_whole_source_tree_is_no_longer_blanket_forbidden(self):
        """It was, before this stage. If it goes back, patching is dead."""
        self.assertNotIn(policy.REPO / "aipi5", policy.NEVER)

    def test_writable_still_excludes_shell_scripts_and_units(self):
        patterns = {g for _, globs, _ in policy.WRITABLE for g in globs}
        for forbidden in ("*.sh", "*.service", "*.socket", "*"):
            with self.subTest(pattern=forbidden):
                self.assertNotIn(forbidden, patterns)


class TestThePatchItself(unittest.TestCase):
    """Exact replacement, because the alternatives are worse.

    A unified diff needs fuzzy context matching, which is a way to apply a
    change somewhere the author did not mean. A whole-file replacement makes
    the approval card useless — nobody reads six hundred lines on a phone.
    """

    def test_text_that_is_not_there_is_refused(self):
        with self.assertRaises(ops.Refused) as caught:
            ops._apply_patch("a b c", "missing", "x")
        self.assertIn("not in the file", str(caught.exception))

    def test_text_that_appears_twice_is_refused(self):
        with self.assertRaises(ops.Refused) as caught:
            ops._apply_patch("a b a", "a", "x")
        self.assertIn("appears 2 times", str(caught.exception))

    def test_a_unique_match_is_replaced_and_nothing_else_moves(self):
        self.assertEqual("one TWO three",
                         ops._apply_patch("one two three", "two", "TWO"))

    def test_a_no_op_patch_is_refused(self):
        with self.assertRaises(ops.Refused):
            ops._patch_target({"path": "x", "old": "same", "new": "same"})

    def test_an_enormous_patch_is_refused(self):
        with self.assertRaises(ops.Refused) as caught:
            ops._patch_target({"path": "x", "old": "y",
                               "new": "z" * (ops.MAX_PATCH_BYTES + 1)})
        self.assertIn("review on a phone", str(caught.exception))

    def test_the_preview_shows_both_sides(self):
        preview = ops._preview(Path("/x/server.py"), "old line", "new line")
        self.assertIn("server.py", preview)
        self.assertIn("- old line", preview)
        self.assertIn("+ new line", preview)

    def test_a_source_change_restarts_the_assistant(self):
        """Including a page: `WebUI.page()` caches index.html for the life of
        the process, so restarting the browser reloads the same stale bytes."""
        self.assertEqual("aipi5", ops._restart_for(
            policy.REPO / "aipi5" / "ui" / "web" / "index.html"))
        self.assertEqual("aipi5", ops._restart_for(
            policy.REPO / "aipi5" / "main.py"))


class TestTheSuiteIsTheGate(unittest.TestCase):

    def test_a_python_change_is_validated_by_running_the_tests(self):
        import inspect
        source = inspect.getsource(ops._validate)
        self.assertIn("_suite_passes", source)
        self.assertIn("py_compile", source)

    def test_the_suite_runs_as_the_owner_and_not_as_root(self):
        """A suite that passes as root has proved something about root."""
        import inspect
        self.assertIn("as_owner=True", inspect.getsource(ops._suite_passes))

    def test_the_one_known_pre_existing_error_is_tolerated_by_name(self):
        """`test_yoga_coach3d` imports pytest, which is not on the device.

        Named explicitly rather than trusting the exit code, so tolerating it
        does not tolerate everything else that might fail alongside it.
        """
        import inspect
        source = inspect.getsource(ops._suite_passes)
        self.assertIn("test_yoga_coach3d", source)
        self.assertNotIn("returncode == 0", source)


class TestNothingStartsARunOnItsOwn(unittest.TestCase):
    """Autonomy, as chosen: the agent acts only in a conversation you started.

    So there must be no scheduler, no timer and no housekeeping job that begins
    a run. The reminder scheduler fires *notifications*, which is a different
    thing — it never calls `ask`.
    """

    #: Read as text rather than imported: `runtime` binds `socket.AF_UNIX` at
    #: class-definition time, which does not exist on Windows, and a security
    #: property that only holds on the device is one that gets broken on the
    #: laptop and noticed a deploy later.
    SOURCE = (ROOT / "aipi5" / "agent" / "runtime.py").read_text(
        encoding="utf-8")

    def test_only_an_incoming_message_begins_a_run(self):
        # `ask` is what starts the loop thread. It must be reached from the
        # HTTP handler and from nowhere else.
        callers = [line.strip() for line in self.SOURCE.splitlines()
                   if ".ask(" in line and "def ask" not in line]
        self.assertTrue(callers, "nothing calls ask; the test is watching "
                                 "the wrong name")
        for line in callers:
            with self.subTest(line=line):
                self.assertIn("service.ask", line,
                              "something other than an incoming message "
                              "starts a run")

    def test_the_runtime_has_no_timer_that_could_start_one(self):
        for forbidden in ("threading.Timer", "sched.scheduler",
                          "croniter", "APScheduler"):
            with self.subTest(pattern=forbidden):
                self.assertNotIn(forbidden, self.SOURCE)

    def test_the_assistant_never_asks_the_agent_anything(self):
        """`Housekeeping` polls the agent to deliver reminders and to ring the
        phone about approvals. It must never send `agent.ask`, or the device
        would be starting its own conversations."""
        keeper = (ROOT / "aipi5" / "core" / "housekeeping.py").read_text(
            encoding="utf-8")
        self.assertIn("agent.delivered", keeper)
        self.assertNotIn("agent.ask", keeper)

    def test_the_reminder_scheduler_notifies_and_never_asks(self):
        import inspect

        from aipi5.agent import schedule
        self.assertNotIn("ask", inspect.getsource(schedule.Schedule))


if __name__ == "__main__":
    unittest.main()
