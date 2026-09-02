"""Nothing starts unless the person asked for it in this sentence.

The model can open a known site and start the music player now, which it could
not do before, and the reason it could not is the whole point of this file.
`execute_kodama_command` used to answer "call open_kodama first" whenever the
player was down, so *any* music request — including one the model inferred from
a half-heard sentence — started an application that resumes its previous queue
on startup and begins playing into the room. It was reported as the player
"starting on its own", which is exactly what it was.

What replaced the old structural guarantee is not the model's restraint and not
a line in the prompt. `begin_turn` reads the raw transcript for a word that
means *open* or *play*, before the model sees the sentence, and the tool is
refused for the whole turn when there is none. A model that has decided
somebody wants music cannot argue its way past a substring check on what they
actually said.

The flag is set by the coordinator rather than by the voice loop, which matters
for the same reason the coordinator exists: the wake word, the Listen button,
the compose box and the phone all pass through one door, so a permission
granted there is granted the same way for all four. A rule that lived in the
voice loop would be a rule the compose box did not have.
"""

from __future__ import annotations

import json
import unittest

from aipi5.assistant import Coordinator, EventLog
from aipi5.llm.tools import ToolBox, asks_to_open

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.plugins.base import Result  # noqa: E402


def parse(result: str) -> dict:
    return json.loads(result)


class FakeLauncher:
    def __init__(self):
        self.opened: list[str] = []

    def open(self, who="something unnamed"):
        self.opened.append(who)
        return Result.done("Opening the music player.", "正在打开音乐播放器。")


class FakeBrowser:
    def __init__(self):
        self.opened: list[str] = []

    def open(self, site, who="something unnamed"):
        self.opened.append(site.name)
        return Result.done(f"Opening {site.label}.", f"正在打开 {site.label}。")


class TestWhatCountsAsAsking(unittest.TestCase):
    """Erring towards *not* matching is the safe direction: a miss means
    somebody says "open YouTube" again and the fast router catches it in nine
    milliseconds, and a false match means an application starting in a living
    room."""

    def test_the_plain_requests(self):
        for said in ("open YouTube", "play some music", "put some music on",
                     "can you bring youtube up", "go to youtube",
                     "start the music player", "show me youtube",
                     "listen to something quiet", "launch youtube"):
            with self.subTest(said=said):
                self.assertTrue(asks_to_open(said))

    def test_the_mandarin_ones(self):
        for said in ("打开YouTube", "打开油管", "播放音乐", "放点轻音乐",
                     "放首歌", "来点音乐", "听一首歌"):
            with self.subTest(said=said):
                self.assertTrue(asks_to_open(said))

    def test_a_sentence_that_only_mentions_it_is_not_asking(self):
        for said in ("do you like music?", "the music is too loud",
                     "what is playing right now", "我喜欢音乐",
                     "音乐太吵了", "is youtube any good",
                     "turn the volume up", "remind me to call Mum"):
            with self.subTest(said=said):
                self.assertFalse(asks_to_open(said))

    def test_particles_that_english_separates_are_still_found(self):
        """"put **some music** on" is what people say; a contiguous "put on"
        matches almost nothing anybody actually utters."""
        self.assertTrue(asks_to_open("put some music on"))
        self.assertTrue(asks_to_open("put the radio on"))

    def test_a_verb_inside_a_longer_word_is_not_a_verb(self):
        """"What's playing" is a question about the player, not a request to
        start one."""
        self.assertFalse(asks_to_open("what's playing"))
        self.assertFalse(asks_to_open("is it still playing"))

    def test_nothing_at_all_is_not_asking(self):
        for said in ("", None, "   "):
            with self.subTest(said=said):
                self.assertFalse(asks_to_open(said))


class GateCase(unittest.TestCase):

    def setUp(self):
        self.launcher = FakeLauncher()
        self.browser = FakeBrowser()
        self.box = ToolBox(launcher=self.launcher, browser=self.browser)

    def open(self, app):
        return parse(self.box.call("open_known_app", json.dumps({"app": app})))


class TestTheGate(GateCase):

    def test_a_toolbox_that_was_never_told_about_a_turn_refuses(self):
        """A permission that defaults to on is a permission nobody set."""
        self.assertFalse(self.open("music")["ok"])
        self.assertEqual([], self.launcher.opened)

    def test_a_sentence_that_did_not_ask_refuses(self):
        self.box.begin_turn("do you like music?")
        answer = self.open("music")
        self.assertFalse(answer["ok"])
        self.assertIn("only open things when", answer["error"])
        self.assertEqual([], self.launcher.opened)

    def test_a_sentence_that_asked_allows_it(self):
        self.box.begin_turn("put some music on")
        self.assertTrue(self.open("music")["ok"])
        self.assertEqual(1, len(self.launcher.opened))

    def test_the_permission_does_not_outlive_its_turn(self):
        self.box.begin_turn("put some music on")
        self.open("music")
        self.box.end_turn()
        self.assertFalse(self.open("music")["ok"])
        self.assertEqual(1, len(self.launcher.opened))

    def test_the_next_turn_replaces_it_rather_than_adding_to_it(self):
        self.box.begin_turn("open youtube")
        self.box.begin_turn("what is the weather")
        self.assertFalse(self.open("youtube")["ok"])
        self.assertEqual([], self.browser.opened)

    def test_the_refusal_tells_the_model_what_to_say_instead(self):
        """Better than a flat no. The person can still get what they wanted by
        saying it, and the fast router answers that in nine milliseconds."""
        self.box.begin_turn("is youtube any good")
        self.assertIn("open YouTube", self.open("youtube")["error"])


class TestWhatMayBeOpened(GateCase):

    def setUp(self):
        super().setUp()
        self.box.begin_turn("open it")

    def test_a_site_reaches_the_browser_by_name(self):
        answer = self.open("youtube")
        self.assertTrue(answer["ok"])
        self.assertEqual(["youtube"], self.browser.opened)

    def test_the_enum_comes_from_code_and_not_from_yaml(self):
        """A list of things a model may launch, in a file anything that can
        write to the disk may edit, is one careless edit from being a list of
        arbitrary commands."""
        from aipi5.browser.launcher import SITES

        schema = next(t for t in self.box.schemas()
                      if t["name"] == "open_known_app")
        offered = set(schema["parameters"]["properties"]["app"]["enum"])
        self.assertEqual({"music"} | {s.name for s in SITES}, offered)

    def test_there_is_no_way_to_give_it_an_address(self):
        properties = next(t for t in self.box.schemas()
                          if t["name"] == "open_known_app")["parameters"]["properties"]
        self.assertEqual(["app"], list(properties))
        self.assertIn("enum", properties["app"])

    def test_a_name_that_is_not_offered_reaches_no_launcher(self):
        for invented in ("https://example.com", "netflix", "../../bin/sh", ""):
            with self.subTest(app=invented):
                self.assertFalse(self.open(invented)["ok"])
        self.assertEqual([], self.browser.opened)
        self.assertEqual([], self.launcher.opened)

    def test_a_launcher_that_failed_is_reported_and_not_claimed(self):
        class Refuses(FakeLauncher):
            def open(self, who="x"):
                self.opened.append(who)
                return Result.failed("There is no Chromium on this device.",
                                     "这台设备上没有 Chromium。")

        box = ToolBox(launcher=Refuses())
        box.begin_turn("put some music on")
        answer = parse(box.call("open_known_app", '{"app": "music"}'))
        # `ok` is whether it opened, not whether the call returned. This
        # used to read `succeeded` while `ok` said True, which is the shape a
        # model reads first and the transcript draws a tick from — a failure
        # shown to the room as a success.
        self.assertFalse(answer["ok"])
        self.assertIn("no Chromium", answer["error"])

    def test_the_tool_is_not_offered_with_nothing_to_open(self):
        self.assertEqual([], [t for t in ToolBox().schemas()
                              if t.get("name") == "open_known_app"])

    def test_only_what_is_installed_is_offered(self):
        music_only = ToolBox(launcher=self.launcher)
        schema = next(t for t in music_only.schemas()
                      if t["name"] == "open_known_app")
        self.assertEqual(["music"],
                         schema["parameters"]["properties"]["app"]["enum"])


class TestTheCoordinatorSetsIt(unittest.TestCase):
    """One door, so one place the permission is granted. A rule that lived in
    the voice loop would be a rule the compose box did not have."""

    def setUp(self):
        self.launcher = FakeLauncher()
        self.box = ToolBox(launcher=self.launcher)
        self.opened: list[bool] = []

        def respond(text, language):
            self.opened.append(
                parse(self.box.call("open_known_app",
                                    '{"app": "music"}'))["ok"])
            return "ok"

        self.coordinator = Coordinator(
            events=EventLog(), respond=respond,
            on_turn=self.box.begin_turn, after_turn=self.box.end_turn)

    def test_the_wake_word_grants_it_when_the_sentence_asks(self):
        self.coordinator.submit_voice("put some music on", "en")
        self.assertEqual([True], self.opened)

    def test_the_compose_box_grants_it_the_same_way(self):
        self.coordinator.submit_text("put some music on", "text", "en")
        self.assertEqual([True], self.opened)

    def test_neither_grants_it_when_the_sentence_did_not_ask(self):
        self.coordinator.submit_voice("do you like music?", "en")
        self.coordinator.submit_text("do you like music?", "text", "en")
        self.assertEqual([False, False], self.opened)

    def test_it_is_dropped_even_when_the_turn_failed(self):
        """The release is in a `finally`. A permission that leaks out of a
        crashed turn is a standing permission nobody set."""
        def boom(text, language):
            raise RuntimeError("the model exploded")

        coordinator = Coordinator(events=EventLog(), respond=boom,
                                  on_turn=self.box.begin_turn,
                                  after_turn=self.box.end_turn)
        with self.assertRaises(RuntimeError):
            coordinator.submit_voice("put some music on", "en")
        self.assertFalse(parse(self.box.call("open_known_app",
                                             '{"app": "music"}'))["ok"])

    def test_a_hook_that_throws_does_not_lose_the_turn(self):
        def boom(text, source):
            raise RuntimeError("the toolbox is gone")

        answers = []
        coordinator = Coordinator(events=EventLog(), on_turn=boom,
                                  respond=lambda t, l: answers.append(t) or "ok")
        self.assertTrue(coordinator.submit_voice("hello", "en")["ok"])
        self.assertEqual(["hello"], answers)


if __name__ == "__main__":
    unittest.main()
