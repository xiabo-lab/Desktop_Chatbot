"""The phone's agent console, and the units that stand the runtime up.

Assertions on file text, following `tests/test_ui_assets.py`'s precedent. That
is a blunt instrument and it is the right one here, because everything it
guards is a thing that is invisible until somebody is standing in a kitchen
with a phone that will not let them type.

The iOS ones especially. `user-select: none` on `html, body` is correct for the
rest of this page and makes a textarea unusable; a font under 16px makes iOS
zoom the viewport on focus and the `maximum-scale` in the viewport meta has
been ignored since iOS 10; `100vh` is the height with the keyboard absent, so a
composer sized with it sits underneath the keyboard. None of the three produces
an error anywhere — they produce a page somebody quietly cannot use.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHONE = ROOT / "aipi5" / "call" / "web" / "phone.html"
SERVER = ROOT / "aipi5" / "call" / "server.py"
UNITS = ROOT / "systemd"


class TestTheConsoleExists(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.page = PHONE.read_text(encoding="utf-8")

    def test_every_piece_the_script_addresses_is_in_the_markup(self):
        """`el()` on a missing id returns null and the failure is a blank screen."""
        for ident in ("agent", "agent-log", "agent-state", "agent-text",
                      "agent-send", "agent-compose", "agent-back", "agent-stop",
                      "agent-badge", "open-agent"):
            with self.subTest(id=ident):
                self.assertIn(f'id="{ident}"', self.page)

    def test_the_screen_is_a_peer_of_the_others(self):
        for ident in ("home", "files", "incoming", "call", "agent"):
            with self.subTest(id=ident):
                self.assertRegex(self.page, rf'<div id="{ident}"')


class TestTheIosTraps(unittest.TestCase):
    """Three fixes, none of which announces itself when it is missing."""

    @classmethod
    def setUpClass(cls):
        cls.page = PHONE.read_text(encoding="utf-8")
        block = re.search(r"#agent-text\s*\{(.*?)\}", cls.page, re.S)
        assert block, "the #agent-text rule has gone"
        cls.rule = block.group(1)

    def test_the_textarea_undoes_the_pages_user_select_none(self):
        self.assertIn("user-select: text", self.rule)
        self.assertIn("-webkit-user-select: text", self.rule)
        # And the page-wide rule it is undoing is still there, or this test is
        # guarding something that no longer exists.
        self.assertIn("user-select: none", self.page)

    def test_the_font_is_large_enough_that_ios_does_not_zoom(self):
        found = re.search(r"font-size:\s*(\d+)px", self.rule)
        self.assertIsNotNone(found, "no font-size on the composer")
        self.assertGreaterEqual(int(found.group(1)), 16)

    def test_the_console_is_sized_with_the_visual_viewport(self):
        agent = re.search(r"#agent\s*\{(.*?)\}", self.page, re.S).group(1)
        self.assertIn("100dvh", agent)
        self.assertNotIn("100vh;", agent)

    def test_the_transcript_scrolls_and_the_composer_does_not(self):
        log = re.search(r"#agent-log\s*\{(.*?)\}", self.page, re.S).group(1)
        self.assertIn("overflow-y: auto", log)
        self.assertIn("min-height: 0", log)      # or flex refuses to shrink it
        compose = re.search(r"#agent-compose\s*\{(.*?)\}", self.page, re.S).group(1)
        self.assertIn("flex: 0 0 auto", compose)


class TestTheWireMatches(unittest.TestCase):
    """Every path the page calls is a route the server declares."""

    @classmethod
    def setUpClass(cls):
        cls.page = PHONE.read_text(encoding="utf-8")
        cls.server = SERVER.read_text(encoding="utf-8")

    def test_the_pages_agent_routes_all_exist(self):
        called = set(re.findall(r'"(/agent/v1/[a-z]+)', self.page))
        self.assertTrue(called, "the page calls no agent routes at all")
        for path in sorted(called):
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', self.server)

    def test_the_console_is_refused_when_the_agent_is_not_installed(self):
        self.assertIn("_agent_missing", self.server)
        self.assertIn("the agent is not installed on this device", self.server)

    def test_every_agent_route_authenticates_the_phone_first(self):
        """The same `_device()` gate every other route on this server uses."""
        for handler in ("_agent_poll", "_agent_state", "_agent_say"):
            with self.subTest(handler=handler):
                body = re.search(rf"def {handler}\(self.*?\n\n",
                                 self.server, re.S).group(0)
                self.assertIn("self._device()", body)

    def test_nothing_is_drawn_with_innerhtml(self):
        """A journal line is a place an attacker's string can end up."""
        console = self.page[self.page.index("function drawEvent"):]
        console = console[:console.index("async function agentSay")]
        self.assertNotIn("innerHTML", console)
        self.assertIn("textContent", console)


class TestTheUnits(unittest.TestCase):
    """What actually contains the runtime."""

    @classmethod
    def setUpClass(cls):
        cls.service = (UNITS / "aipi5-agent.service").read_text(encoding="utf-8")
        cls.socket = (UNITS / "aipi5-agent.socket").read_text(encoding="utf-8")
        cls.helper = (UNITS / "aipi5-agent-helper.service").read_text(encoding="utf-8")

    def test_the_runtime_is_not_fuwenxu(self):
        """fuwenxu carries NOPASSWD: ALL. Running as that user is running as root."""
        self.assertIn("User=aipi5-agent", self.service)
        self.assertNotIn("User=fuwenxu", self.service)

    def test_the_home_directory_is_hidden_by_the_kernel(self):
        """A mount namespace, not a rule somebody has to remember to check.

        Without it the agent can write ~/.config/systemd/user/ or ~/.bashrc and
        become fuwenxu, which is root.
        """
        self.assertIn("ProtectHome=tmpfs", self.service)
        self.assertIn("BindReadOnlyPaths=/home/fuwenxu/AIPI5", self.service)

    def test_it_cannot_gain_privilege(self):
        for directive in ("NoNewPrivileges=yes", "CapabilityBoundingSet=",
                          "ProtectSystem=strict", "RestrictSUIDSGID=yes"):
            with self.subTest(directive=directive):
                self.assertIn(directive, self.service)

    def test_the_key_is_handed_over_rather_than_fetched(self):
        self.assertIn("LoadCredential=openai:", self.service)

    def test_there_is_no_ordering_edge_onto_the_assistant(self):
        """systemd breaks a cycle by deleting a job, and this project has
        already lost a boot that way -- see systemd/aipi5.service."""
        for line in self.service.splitlines():
            if line.startswith(("After=", "Before=", "Requires=", "Wants=",
                                "PartOf=", "BindsTo=")):
                with self.subTest(line=line):
                    self.assertNotIn("aipi5.service", line)
                    self.assertNotIn("aipi5-ui", line)
                    self.assertNotIn("kodama", line)

    def test_the_sockets_are_not_world_reachable(self):
        self.assertIn("SocketMode=0660", self.socket)
        self.assertIn("SocketUser=aipi5-agent", self.socket)
        self.assertIn("SocketGroup=fuwenxu", self.socket)

    def test_the_two_services_do_not_share_a_state_directory(self):
        """They did, and it was an escalation path.

        systemd hands a `StateDirectory=` to the user its unit runs as. Both
        units named `aipi5-agent`, so the *runtime* — which runs as that user —
        took ownership of the directory the **helper** keeps rollback backups
        in. Verified on the device: `aipi5-agent` could append to a saved
        "before" copy, and then ask to be rolled back onto it, writing content
        as root with nobody asked.

        Two identical `StateDirectory=` lines read as agreement. They were not.
        """
        import re
        runtime = re.findall(r"^StateDirectory=(\S+)", self.service, re.M)
        helper = re.findall(r"^StateDirectory=(\S+)", self.helper, re.M)
        self.assertTrue(runtime and helper, "a StateDirectory has gone missing")
        self.assertEqual(set(), set(runtime) & set(helper))

    def test_the_backups_are_not_under_the_runtimes_directory(self):
        """Wherever the change store moves, it must not be somewhere the agent
        owns — the whole value of a backup is that the thing being guarded
        against cannot edit it."""
        import sys
        helper_dir = ROOT / "aipi5" / "agent" / "helper"
        if str(helper_dir) not in sys.path:
            sys.path.insert(0, str(helper_dir))
        import policy
        runtime_state = "/var/lib/aipi5-agent"
        self.assertFalse(str(policy.CHANGES_DIR).replace("\\", "/")
                         .startswith(runtime_state + "/"),
                         "the change store is inside the runtime's own "
                         "StateDirectory, which the agent owns")

    def test_the_helper_runs_the_system_interpreter_not_the_venv(self):
        """So `pip install` can never change what root runs."""
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/aipi5-agent/main.py",
                      self.helper)
        self.assertNotIn(".venv", self.helper)


if __name__ == "__main__":
    unittest.main()
