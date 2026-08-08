"""The gate between the language model and the device.

The most important tests in this project. Everything else here is about the
assistant working; these are about it not doing something nobody asked for.

Three properties, checked against the *real* AIA command declarations rather
than against a mock, because the whole design rests on those declarations being
the source of truth:

1. No `confirm=True` command is reachable from the model. Shutdown, reboot and
   closing the music player are spoken commands, confirmed out loud, and the
   model is not part of that conversation.
2. Nothing from the `system` plugin is offered at all.
3. A tool name or a command name the model invents is refused, not guessed at.

The filter is on the `confirm` flag and not on a list of names, which is what
makes property 1 survive AIA growing a new destructive command — the test below
asserts the mechanism as well as today's outcome.
"""

from __future__ import annotations

import json
import unittest

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import CommandSpec, Plugin, Registry, Result
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System

from aipi5.llm.tools import ToolBox


class FakePlayer(Plugin):
    """A Kodama plugin that answers without a session bus.

    AIA's real one shells out to `playerctl` through `os.getuid()`, which does
    not exist off a POSIX machine — so the paths that ask whether the player is
    running need a stand-in here. The command declarations under test are still
    the real ones; only `available()` is faked.
    """

    name = "kodama"
    description = "Music player (Kodama-Lite)"

    def __init__(self, running: bool = True):
        self.running = running
        self.calls: list[tuple[str, dict]] = []

    def available(self) -> bool:
        return self.running

    def _record(self, name: str, **kwargs) -> Result:
        self.calls.append((name, kwargs))
        return Result.done("done", "完成")

    def commands(self) -> list[CommandSpec]:
        return [
            CommandSpec(name="pause", description="Pause playback",
                        handler=lambda: self._record("pause")),
            CommandSpec(name="play", description="Search for a song and play it",
                        handler=lambda query: self._record("play", query=query),
                        params={"query": "song, artist or album"}),
            CommandSpec(name="quit", description="Close Kodama-Lite",
                        handler=lambda: self._record("quit"),
                        confirm=True),
        ]


def parse(result: str) -> dict:
    return json.loads(result)


class TestWhatIsOffered(unittest.TestCase):
    """Against AIA's real declarations."""

    def setUp(self):
        self.registry = Registry([KodamaLite(), System()])
        self.box = ToolBox(registry=self.registry)
        self.tools = self.box.schemas()
        self.kodama = next(t for t in self.tools
                           if t["function"]["name"] == "execute_kodama_command")
        self.offered = set(self.kodama["function"]["parameters"]
                           ["properties"]["command"]["enum"])

    def test_ordinary_commands_are_offered(self):
        for name in ("pause", "next", "play", "search", "volume", "karaoke"):
            self.assertIn(name, self.offered)

    def test_closing_the_player_is_not_offered(self):
        # `quit` carries confirm=True in AIA's declaration and is answered out
        # loud before it runs. A model cannot hold that conversation.
        self.assertNotIn("quit", self.offered)

    def test_nothing_from_the_system_plugin_is_offered(self):
        # Not even the harmless ones. The plugin's own docstring says that if
        # it ever grows a command taking free text that is the moment to stop
        # and reconsider — so the model is kept out of the whole plugin rather
        # than out of two named commands in it.
        for name in ("shutdown", "reboot", "network"):
            self.assertNotIn(name, self.offered)

    def test_the_filter_is_the_confirm_flag_not_a_name_list(self):
        # The mechanism, not just today's outcome. A destructive command added
        # to AIA tomorrow must be excluded the moment it is declared, with
        # nothing here to remember to update.
        confirmable = {c.name for p, c in self.registry.all_commands()
                       if p.name == "kodama" and c.confirm}
        self.assertTrue(confirmable, "AIA should still declare at least one")
        self.assertFalse(self.offered & confirmable)

    def test_a_tool_with_no_service_behind_it_is_not_offered(self):
        # Better than offering one that always errors and letting the model
        # discover that at runtime.
        names = {t["function"]["name"] for t in self.tools}
        self.assertNotIn("get_weather", names)
        self.assertNotIn("describe_camera_image", names)
        self.assertNotIn("open_kodama", names)

    def test_every_schema_refuses_extra_properties(self):
        for tool in self.tools:
            self.assertFalse(
                tool["function"]["parameters"]["additionalProperties"],
                f"{tool['function']['name']} would silently accept invented fields")


class TestDispatch(unittest.TestCase):

    def setUp(self):
        self.player = FakePlayer()
        self.box = ToolBox(registry=Registry([self.player]))

    def test_an_invented_tool_name_is_refused(self):
        result = parse(self.box.call("run_shell_command", '{"cmd": "rm -rf /"}'))
        self.assertFalse(result["ok"])
        self.assertIn("no tool called", result["error"])

    def test_an_invented_command_name_is_refused(self):
        result = parse(self.box.call("execute_kodama_command",
                                     '{"command": "poweroff"}'))
        self.assertFalse(result["ok"])
        self.assertEqual(self.player.calls, [])

    def test_a_confirmable_command_is_refused_even_when_named(self):
        # The model cannot reach `quit` by asking for it directly, because it
        # is not in the table the lookup uses — the same table it was shown.
        result = parse(self.box.call("execute_kodama_command",
                                     '{"command": "quit"}'))
        self.assertFalse(result["ok"])
        self.assertEqual(self.player.calls, [])

    def test_unparseable_arguments_do_not_raise(self):
        # Models emit malformed JSON. It must cost the turn nothing worse than
        # a refusal.
        result = parse(self.box.call("execute_kodama_command", "{not json"))
        self.assertFalse(result["ok"])

    def test_a_real_command_runs(self):
        result = parse(self.box.call("execute_kodama_command",
                                     '{"command": "pause"}'))
        self.assertTrue(result["ok"])
        self.assertEqual(self.player.calls, [("pause", {})])

    def test_the_argument_name_comes_from_the_declaration(self):
        # Not from the model. Whatever it calls the field, the handler is
        # invoked with the parameter its own CommandSpec declares.
        parse(self.box.call("execute_kodama_command",
                            '{"command": "play", "argument": "五月天"}'))
        self.assertEqual(self.player.calls, [("play", {"query": "五月天"})])

    def test_extra_fields_are_ignored_not_passed_through(self):
        parse(self.box.call("execute_kodama_command",
                            '{"command": "pause", "shell": "rm -rf /", "sudo": true}'))
        self.assertEqual(self.player.calls, [("pause", {})])

    def test_a_command_that_needs_an_argument_refuses_without_one(self):
        result = parse(self.box.call("execute_kodama_command",
                                     '{"command": "play"}'))
        self.assertFalse(result["ok"])
        self.assertEqual(self.player.calls, [])

    def test_a_closed_player_is_reported_rather_than_driven(self):
        box = ToolBox(registry=Registry([FakePlayer(running=False)]))
        result = parse(box.call("execute_kodama_command", '{"command": "pause"}'))
        self.assertFalse(result["ok"])
        self.assertIn("open_kodama", result["error"])

    def test_a_tool_that_throws_becomes_an_error_not_an_exception(self):
        class Exploding:
            def current(self, force=False):
                raise RuntimeError("boom")

        box = ToolBox(weather=Exploding())
        result = parse(box.call("get_weather", "{}"))
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
