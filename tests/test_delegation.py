"""Handing long work over, and not handing over anything else.

The acceptance for this phase is one sentence: "why did the screen go blank
last night?" delegates and "set volume to thirty" does not. Both halves matter.
A device that never delegates cannot answer the first at all — three model
rounds is not enough to read a journal, check a unit and compare timestamps. A
device that delegates freely spends one of the day's runs and several minutes
making a phone buzz, and the person standing there gets "I have started looking
into that" when they wanted the volume changed.

The other thing tested here is the shape of the reply. The run answers minutes
later, on the screen, and the model that started it is never told what it
found — because a model handed a plausible-looking summary will describe
findings that do not exist yet, out loud, to somebody who has no way to know.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from aipi5.assistant import Coordinator, EventLog
from aipi5.assistant.coordinator import AgentBridge
from aipi5.llm.tools import ToolBox

ROOT = Path(__file__).resolve().parent.parent


def parse(result: str) -> dict:
    return json.loads(result)


class FakeProxy:
    def __init__(self, batches=None, ok=True):
        self.batches = list(batches or [])
        self.ok = ok
        self.said: list[dict] = []

    def poll(self, since, timeout=None):
        if self.batches:
            return 200, self.batches.pop(0)
        return 200, {"events": [], "cursor": since,
                     "agent": {"run": "", "state": "idle", "busy": False,
                               "pending": None}}

    def say(self, message):
        self.said.append(message)
        if not self.ok:
            return 200, {"ok": False, "error": "that is 6 runs today, which is "
                                               "the limit. It resets at midnight."}
        return 200, {"ok": True, "run": "r-abc123"}

    def snapshot(self):
        return {"run": "", "state": "idle", "busy": False, "pending": None}


class DelegationCase(unittest.TestCase):

    def setUp(self):
        self.proxy = FakeProxy()
        self.spoken: list[tuple[str, str]] = []
        self.events = EventLog()
        self.coordinator = Coordinator(
            events=self.events, agent=self.proxy,
            respond=lambda text, language: "ok",
            speak=lambda text, run: self.spoken.append((text, run)),
            bridge=AgentBridge(self.proxy, self.events, poll_s=0.05,
                               drain_s=0.05))
        self.addCleanup(self.coordinator.close)
        self.box = ToolBox(delegate=self.coordinator.delegate)

    def delegate(self, **args):
        return parse(self.box.call("delegate_agent_task", json.dumps(args)))


class TestTheTool(DelegationCase):

    def test_it_starts_a_run_and_comes_straight_back(self):
        answer = self.delegate(task="why did the screen go blank last night")
        self.assertTrue(answer["ok"])
        self.assertEqual("r-abc123", answer["run"])
        self.assertEqual({"type": "agent.ask",
                          "text": "why did the screen go blank last night"},
                         self.proxy.said[0])

    def test_the_model_is_told_nothing_it_could_turn_into_findings(self):
        """A model handed a plausible-looking summary will describe things the
        run has not found yet, out loud, to somebody who cannot check."""
        answer = self.delegate(task="why did the screen restart")
        self.assertIn("nothing about what it might find", answer["instruction"])
        self.assertNotIn("result", answer)
        self.assertNotIn("findings", answer)

    def test_an_empty_task_starts_nothing(self):
        self.assertFalse(self.delegate(task="  ")["ok"])
        self.assertEqual([], self.proxy.said)

    def test_a_refusal_from_the_agent_becomes_a_sentence(self):
        box = ToolBox(delegate=Coordinator(
            events=EventLog(), agent=FakeProxy(ok=False)).delegate)
        answer = parse(box.call("delegate_agent_task",
                                json.dumps({"task": "look into it"})))
        self.assertFalse(answer["ok"])
        self.assertIn("runs today", answer["error"])

    def test_the_transcript_says_it_was_handed_over(self):
        self.delegate(task="why did the screen restart",
                      reason="I have started looking into that.")
        rows, _ = self.events.collect(0)
        self.assertEqual(["tool"], [r["kind"] for r in rows])
        self.assertEqual("delegate_agent_task", rows[0]["tool"])
        self.assertEqual("I have started looking into that.", rows[0]["text"])

    def test_it_is_not_offered_without_an_agent(self):
        self.assertEqual([], [t for t in ToolBox().schemas()
                              if t.get("name") == "delegate_agent_task"])

    def test_the_schema_names_what_belongs_here_and_what_does_not(self):
        """The acceptance for this phase, written where the model reads it."""
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "delegate_agent_task")
        self.assertIn("why did the screen go blank last night",
                      schema["description"])
        self.assertIn("Not for anything you can do here", schema["description"])
        for cheap in ("volume", "calendar", "picture", "weather", "reminder"):
            with self.subTest(cheap=cheap):
                self.assertIn(cheap, schema["description"])

    def test_the_prompt_does_not_send_anybody_back_to_the_device(self):
        """The other half of the same fault, and the half that was giving the
        advice. The prompt said: you cannot close an application, "say that
        they should ask the device directly". So asked to close the browser it
        answered "please say close the browser to the device" -- to somebody
        who had just said exactly that, to the device.

        Written when Talk and Agent were two things. There is one now, and the
        model is it.
        """
        from aipi5.llm import prompts

        page = prompts.BASE
        self.assertNotIn("ask the device directly", page)
        self.assertIn("you are it", page)

    def test_the_prompt_keeps_shut_down_and_close_apart(self):
        """`shutdown` is a real spoken command with a confirmation, so
        offering it to somebody who asked to close a browser offers to turn the
        machine off instead.

        Closing is only a command for the *music player* -- `kodama.quit`,
        "close kodama", 退出软件. The prompt said there was no such command for
        anything, which is wrong in the one place it is right to say so; the
        browser is the one with nothing behind it.
        """
        from aipi5.llm import prompts

        closing = prompts.BASE[prompts.BASE.index("Closing an application"):]
        closing = closing[:closing.index("- **Never tell")]
        self.assertIn("music player", closing)
        self.assertIn("such command for the browser", closing)
        self.assertIn("maintenance agent", closing)

    def test_the_schema_covers_doing_and_not_only_asking(self):
        """Asked to close the browser, the model answered "I can't close
        applications -- say 'Shut down' and the device will confirm". Wrong
        twice: the request had already been said to the device, and *shut
        down* is not what somebody asking to close a browser wants.

        It reads a description about long *questions*, finds closing a browser
        is not one, and invents advice rather than handing over a thing the
        agent can plainly do. `browser_close` and `restart_service` are two of
        its tools.
        """
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "delegate_agent_task")
        for doing in ("closing the browser", "restarting a service",
                      "changing a setting"):
            with self.subTest(doing=doing):
                self.assertIn(doing, schema["description"])
        self.assertIn("you are the device", schema["description"])

    def test_the_task_carries_what_the_person_said(self):
        """The agent has none of this conversation — it is a different process
        with its own history — so a task of "check that" is a run that has
        nothing to check."""
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "delegate_agent_task")
        self.assertIn("the agent has none of this conversation",
                      schema["parameters"]["properties"]["task"]["description"])


class TestFollowingTheRun(DelegationCase):

    def test_the_checks_and_the_answer_land_in_the_same_transcript(self):
        proxy = FakeProxy([{"cursor": 4, "events": [
            {"type": "agent.tool", "name": "read_journal", "ok": True,
             "summary": "200 lines from the display unit"},
            {"type": "agent.say", "run": "r-1",
             "text": "The display unit restarted at 02:14."},
            {"type": "agent.done", "run": "r-1", "ok": True, "steps": 4,
             "seconds": 61},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        events = EventLog()
        bridge = AgentBridge(proxy, events)
        bridge.follow("r-1")
        bridge.pump(timeout=0)
        rows, _ = events.collect(0)
        self.assertEqual(["tool", "assistant", "done"],
                         [r["kind"] for r in rows])
        self.assertTrue(all(r["source"] == "agent" for r in rows))

    def test_the_final_answer_is_offered_once_the_run_is_done(self):
        """At `agent.done`, not at every `agent.say`. A run emits several as it
        works something out, and reading each aloud is the assistant narrating
        a twelve-minute investigation into a room where somebody asked one
        question."""
        spoken: list[tuple[str, str]] = []
        proxy = FakeProxy([{"cursor": 5, "events": [
            {"type": "agent.say", "run": "r-1", "text": "Checking the journal."},
            {"type": "agent.say", "run": "r-1", "text": "It restarted at 02:14."},
            {"type": "agent.done", "run": "r-1", "ok": True},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog(),
                             on_answer=lambda text, run: spoken.append((text, run)))
        bridge.follow("r-1")
        bridge.pump(timeout=0)
        self.assertEqual([("It restarted at 02:14.", "r-1")], spoken)

    def test_a_run_that_said_nothing_offers_nothing(self):
        spoken = []
        proxy = FakeProxy([{"cursor": 1, "events": [
            {"type": "agent.done", "run": "r-1", "ok": False,
             "stopped": "you asked me to stop"},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog(),
                             on_answer=lambda text, run: spoken.append(text))
        bridge.follow("r-1")
        bridge.pump(timeout=0)
        self.assertEqual([], spoken)

    def test_a_listener_that_throws_does_not_take_the_bridge_down(self):
        proxy = FakeProxy([{"cursor": 2, "events": [
            {"type": "agent.say", "run": "r-1", "text": "done"},
            {"type": "agent.done", "run": "r-1", "ok": True},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        events = EventLog()
        bridge = AgentBridge(proxy, events,
                             on_answer=lambda text, run: 1 / 0)
        bridge.follow("r-1")
        self.assertEqual(2, bridge.pump(timeout=0))

    def test_the_last_words_do_not_carry_into_the_next_run(self):
        spoken = []
        proxy = FakeProxy(
            [{"cursor": 2, "events": [
                {"type": "agent.say", "run": "r-1", "text": "the first answer"},
                {"type": "agent.done", "run": "r-1", "ok": True},
            ], "agent": {"run": "r-1", "state": "idle"}},
             {"cursor": 3, "events": [
                 {"type": "agent.done", "run": "r-2", "ok": True},
             ], "agent": {"run": "r-2", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog(),
                             on_answer=lambda text, run: spoken.append(text))
        bridge.follow("r-1")
        bridge.pump(timeout=0)
        bridge.follow("r-2")
        bridge.pump(timeout=0)
        self.assertEqual(["the first answer"], spoken)

    def test_cancelling_reaches_the_run(self):
        self.delegate(task="look into it")
        self.coordinator.cancel()
        self.assertEqual({"type": "agent.stop", "run": "r-abc123"},
                         self.proxy.said[-1])


class TestWhenTheAnswerIsSpokenAloud(unittest.TestCase):
    """Asserted over `main.py`, because all three conditions are about the
    room rather than about the run, and every one of them exists because the
    failure it prevents is easy to walk into."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "aipi5" / "main.py").read_text(encoding="utf-8")
        cls.body = re.search(r"def speak_delegated_answer\(.*?\n    def ",
                             text, re.S).group(0)

    def test_a_long_answer_is_left_on_the_screen(self):
        """A run that read a journal comes back with paragraphs. Spoken, that
        is a minute during which the music is ducked and nobody in the room can
        interrupt."""
        self.assertIn("DELEGATED_SPEAK_CHARS", self.body)
        self.assertIn("leaving it on the", self.body)

    def test_an_empty_room_is_not_spoken_to(self):
        """A run started twelve minutes ago finished into an empty kitchen more
        often than not, and a device that talks to an empty room at eleven at
        night is a device people unplug."""
        self.assertIn("Presence.PERSON_PRESENT", self.body)

    def test_a_call_or_a_game_owns_the_speaker(self):
        self.assertIn("self.call.hub.live()", self.body)
        self.assertIn("self.games.active", self.body)

    def test_every_refusal_says_why_in_the_log(self):
        """A silent decision not to speak is indistinguishable from a run that
        produced nothing, which is the report that came in the first time."""
        self.assertEqual(4, self.body.count("leaving it on the"))

    def test_it_is_recorded_even_when_it_is_spoken(self):
        """The 24-hour audible log is what was said in the room, and this was.

        Recorded by `say`, which is the only thing that should: it records and
        speaks together "so the two cannot drift apart", and this method used
        to call `history.record` as well — writing every spoken delegated
        answer into the log twice, which reads as an assistant that said the
        same thing back to back.
        """
        self.assertIn("say(self, text, language)", self.body)
        self.assertNotIn('self.history.record("aia", text, language)',
                         self.body)
        source = (ROOT / "aipi5" / "main.py").read_text(encoding="utf-8")
        body = source[source.index("def say(assistant"):]
        body = body[:body.index(chr(10) + chr(10) + "def ")]
        self.assertIn('assistant.history.record("aia", text, language)',
                      body)


if __name__ == "__main__":
    unittest.main()
