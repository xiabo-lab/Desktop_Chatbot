"""The gate between the language model and the device.

The most important tests in this project. Everything else here is about the
assistant working; these are about it not doing something nobody asked for.

Four properties, checked against the *real* AIA command declarations rather
than against a mock, because the whole design rests on those declarations being
the source of truth:

1. No `confirm=True` command is reachable from the model. Shutdown, reboot and
   closing the music player are spoken commands, confirmed out loud, and the
   model is not part of that conversation.
2. Nothing from the `system` plugin is offered at all.
3. A tool name or a command name the model invents is refused, not guessed at.
4. The model cannot *start* the music player. It may drive one that is already
   running; opening it is a person's decision, made with the Music button or
   out loud. This one is here because it was violated: the player was reported
   as starting on its own, and `open_kodama` was how.

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

from aipi5.llm.prompts import with_facts
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
        # `/v1/responses` shape: the name and the parameters sit on the tool
        # rather than inside a nested `function` object. The agent's toolbox
        # still speaks the other spelling — see `_schema` in
        # `aipi5/agent/tools.py` for why the two are no longer shared.
        self.kodama = next(t for t in self.tools
                           if t["name"] == "execute_kodama_command")
        self.offered = set(self.kodama["parameters"]
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
        names = {t["name"] for t in self.tools}
        self.assertNotIn("get_weather", names)
        self.assertNotIn("describe_camera_image", names)

    def test_the_model_is_never_offered_a_way_to_start_the_player(self):
        # Property 4. Not "with this configuration": there is no argument that
        # puts a launcher into a ToolBox any more, so there is no arrangement of
        # services that brings this tool back by accident.
        names = {t["name"] for t in self.tools}
        self.assertNotIn("open_kodama", names)
        self.assertNotIn("open_kodama", ToolBox()._handlers)

    def test_a_toolbox_cannot_be_given_a_launcher(self):
        # The structural half, and the reason this is a guarantee rather than a
        # removed line somebody could put back without weighing it. A ToolBox
        # holds no launcher, so no tool has an object to call `open()` on.
        with self.assertRaises(TypeError):
            ToolBox(launcher=object())

    def test_every_schema_refuses_extra_properties(self):
        for tool in self.tools:
            self.assertFalse(
                tool["parameters"]["additionalProperties"],
                f"{tool['name']} would silently accept invented fields")

    def test_every_schema_is_strict(self):
        """Strict mode is what makes the schema a guarantee and not a hint.

        Without it the model may omit a required field, send a string where a
        number belongs, or add one that is silently dropped — and every one of
        those arrives at a handler as an argument nobody validated.
        """
        for tool in self.tools:
            with self.subTest(name=tool["name"]):
                self.assertIs(True, tool["strict"])

    def test_an_optional_argument_is_nullable_and_still_required(self):
        """The price of strict mode, and the shape that pays it.

        Strict mode admits no optional argument: every property must be in
        `required`. So one that is genuinely optional is declared nullable and
        listed anyway, and the model sends `null` when it has nothing to say.
        Declaring it optional instead is a schema the API refuses outright, at
        the moment somebody asks a question — not at startup.
        """
        argument = self.kodama["parameters"]["properties"]["argument"]
        self.assertIn("null", argument["type"])
        self.assertIn("argument", self.kodama["parameters"]["required"])
        # And a genuinely required one is not made nullable.
        command = self.kodama["parameters"]["properties"]["command"]
        self.assertEqual("string", command["type"])

    def test_an_explicit_null_reads_as_no_argument(self):
        """What strict mode actually sends. `args.get("argument", "")` returns
        None here, not "" — which reached `.strip()` as an AttributeError in
        the middle of a turn."""
        box = ToolBox(registry=self.registry, clock=object())
        answer = parse(box.call("execute_kodama_command",
                                json.dumps({"command": "pause",
                                            "argument": None})))
        # Refused because the player is not running, not because it raised.
        self.assertIn("error", answer)

    def test_the_web_search_tool_is_off_unless_it_is_asked_for(self):
        """A model given both will search the web for weather this device has
        cached from a forecast API three hundred metres away."""
        self.assertNotIn("web_search", [t.get("type") for t in self.tools])
        with_search = ToolBox(registry=self.registry, web_search=True).schemas()
        self.assertEqual("web_search", with_search[-1]["type"])


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
        # And the message sends the model to the person, not to a tool. It used
        # to say "call open_kodama first", which is how a music request the
        # model merely inferred became an app starting up and playing its last
        # queue into the room.
        self.assertNotIn("open_kodama", result["error"])
        self.assertIn("Music button", result["error"])

    def test_a_tool_that_throws_becomes_an_error_not_an_exception(self):
        class Exploding:
            def current(self, force=False):
                raise RuntimeError("boom")

        box = ToolBox(weather=Exploding())
        result = parse(box.call("get_weather", "{}"))
        self.assertFalse(result["ok"])


class TestThePromptDoesNotInviteALaunch(unittest.TestCase):
    """The tool list is half the boundary; the prompt is the other half.

    Removing `open_kodama` while the standing facts still said "it can be
    started with the open_kodama tool" would leave the model being told, every
    turn the player was closed, to reach for something that no longer exists —
    which is a model that answers "I'll start it" and then does not.
    """

    def facts_when_closed(self) -> str:
        return with_facts("Anytown", "en", {"kodama_running": False})

    def test_the_standing_facts_never_name_a_launch_tool(self):
        self.assertNotIn("open_kodama", self.facts_when_closed())

    def test_a_closed_player_is_reported_with_the_way_to_open_it(self):
        # The fact stays — the model needs it to answer "why isn't it playing?"
        # — but it now points at the person rather than at itself.
        self.assertIn("no way to start it", self.facts_when_closed())

    def test_a_running_player_is_told_none_of_this(self):
        # Asserted on the standing-facts line rather than on the whole prompt:
        # the fixed part of the prompt says "you cannot start it" always, and
        # should. It is the per-turn fact that must appear only when true.
        facts = with_facts("Anytown", "en", {"kodama_running": True})
        self.assertNotIn("no way to start it", facts)


if __name__ == "__main__":
    unittest.main()
