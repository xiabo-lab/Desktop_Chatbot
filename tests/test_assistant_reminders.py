"""Reminders, reached without spending a maintenance run.

`Schedule` lives in `aipi5-agent.service` and stays there: reboot survival,
retry, and the delivery handshake with `Housekeeping` all belong to one owner,
and a second copy on the assistant's side would be a second file of reminders
the phone never hears about.

What is new is the door. The only way in used to be `agent.ask`, so "remind me
at nine to call Mum" spent one of the day's runs and up to 24 model steps
deciding to call `remind_me` — for a request whose entire content is a
timestamp and a sentence. Worse, it made the reminder only as reliable as the
run: one that wandered off, hit its step ceiling, or decided to read the
journal first was a reminder that never got made, with nothing said about it.

So there are three messages that reach `Schedule` directly. No `AgentLoop`, no
budget, no thread, and no model anywhere on the path — which is what makes
"remind me" as dependable as an alarm clock rather than as dependable as a
conversation. These tests are mostly about that absence.
"""

from __future__ import annotations

import json
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aipi5.agent.runtime import AgentService
from aipi5.agent.schedule import Schedule
from aipi5.llm.tools import ToolBox

ROOT = Path(__file__).resolve().parent.parent


def parse(result: str) -> dict:
    return json.loads(result)


class FakeHelper:
    def call(self, name, args, timeout=None):
        return SimpleNamespace(ok=False, result={}, error="no helper here")


class FakeToolBox:
    """Enough of `AgentToolBox` for `AgentService` to be constructed."""

    def __init__(self):
        self.helper = FakeHelper()
        self.schedule = None
        self.notes = None
        self.approvals = None

    def schemas(self):
        return []


def service(tmp: Path) -> AgentService:
    """An `AgentService` whose `Schedule` is a file in a temporary directory.

    Built by hand rather than through `main()`: the point of these three
    messages is that they touch nothing but the schedule, and a fixture that
    stood up a model client would be hiding exactly that.
    """
    cfg = SimpleNamespace(poll_timeout_s=0.1, max_steps=1, max_tool_calls=1,
                          max_runtime_s=1, max_tokens=1, approval_timeout_s=1,
                          daily_run_limit=1)
    made = AgentService.__new__(AgentService)
    made.cfg = cfg
    made.schedule = Schedule(tmp / "reminders.jsonl")
    made._lock = __import__("threading").Lock()
    made._run = ""
    made._state = "idle"
    return made


class ReminderCase(unittest.TestCase):

    def setUp(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = service(Path(directory.name))

    def soon(self, minutes: int = 60) -> str:
        return time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.time() + minutes * 60))


class TestTheServiceSideDoesNotRunAnything(ReminderCase):

    def test_a_reminder_is_made_by_one_call_with_no_run(self):
        answer = self.service.reminder_create(self.soon(), "call Mum")
        self.assertTrue(answer["ok"])
        self.assertEqual("call Mum", answer["reminder"]["text"])
        # Nothing was started. `_state` is what `ask()` moves off "idle", and
        # a run here would spend one of the day's budget on a file write.
        self.assertEqual("idle", self.service._state)
        self.assertEqual("", self.service._run)

    def test_the_time_is_resolved_and_read_back_as_a_human_one(self):
        """An epoch read out loud is not a confirmation of anything."""
        when = self.soon(120)
        answer = self.service.reminder_create(when, "call Mum")
        self.assertEqual(when, answer["reminder"]["when"])
        self.assertGreater(answer["reminder"]["in_seconds"], 0)

    def test_words_instead_of_a_timestamp_are_refused_and_not_guessed(self):
        """`parse_when` is a validator, not a natural-language parser. "next
        Tuesday" means something different depending on the day it is asked,
        and a parser that guesses is a reminder that arrives on the wrong day
        for reasons nobody can reconstruct afterwards."""
        answer = self.service.reminder_create("tomorrow morning", "call Mum")
        self.assertFalse(answer["ok"])
        self.assertIn("could not read", answer["error"])

    def test_a_time_that_has_already_passed_is_refused(self):
        past = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3600))
        answer = self.service.reminder_create(past, "call Mum")
        self.assertFalse(answer["ok"])
        self.assertIn("already passed", answer["error"])

    def test_a_year_away_is_the_limit_so_a_mistyped_year_is_caught(self):
        answer = self.service.reminder_create("2099-01-01 09:00", "call Mum")
        self.assertFalse(answer["ok"])
        self.assertIn("more than a year", answer["error"])

    def test_an_empty_reminder_is_refused(self):
        self.assertFalse(self.service.reminder_create(self.soon(), "  ")["ok"])

    def test_a_delivery_channel_this_device_does_not_have_is_refused(self):
        answer = self.service.reminder_create(self.soon(), "call Mum",
                                              deliver="carrier pigeon")
        self.assertFalse(answer["ok"])

    def test_listing_shows_what_is_waiting(self):
        self.service.reminder_create(self.soon(), "call Mum")
        listing = self.service.reminder_list()
        self.assertEqual(1, len(listing["reminders"]))
        self.assertEqual("pending", listing["reminders"][0]["state"])

    def test_cancelling_takes_it_out_of_what_is_due(self):
        made = self.service.reminder_create(self.soon(-0), "call Mum")
        self.service.reminder_cancel(made["reminder"]["id"])
        self.assertEqual([], self.service.schedule.pending())

    def test_cancelling_something_that_is_not_there_says_so(self):
        answer = self.service.reminder_cancel("m-nonesuch")
        self.assertFalse(answer["ok"])
        self.assertIn("no reminder", answer["error"])

    def test_it_survives_the_process(self):
        """The whole reason `Schedule` is a file. A reminder that does not
        outlive a restart is a reminder that does not outlive the agent being
        asked to restart the assistant, which is the commonest thing it does."""
        self.service.reminder_create(self.soon(), "call Mum")
        reopened = Schedule(self.service.schedule.path)
        self.assertEqual(1, len(reopened.pending()))


class FakeAgent:
    """`AgentProxy`'s three reminder methods, over a real `Schedule`."""

    def __init__(self, service, status=200):
        self.service = service
        self.status = status
        self.sent: list[tuple] = []

    def reminder_create(self, when, text, deliver="push"):
        self.sent.append(("create", when, text, deliver))
        if self.status != 200:
            return self.status, {"error": "the agent is not answering"}
        return 200, self.service.reminder_create(when, text, deliver)

    def reminder_list(self, limit=20):
        if self.status != 200:
            return self.status, {"error": "the agent is not answering"}
        return 200, self.service.reminder_list(limit)

    def reminder_cancel(self, ident):
        if self.status != 200:
            return self.status, {"error": "the agent is not answering"}
        return 200, self.service.reminder_cancel(ident)


class TestTheToolSide(ReminderCase):

    def setUp(self):
        super().setUp()
        self.agent = FakeAgent(self.service)
        self.box = ToolBox(agent=self.agent)

    def call(self, tool, **args):
        return parse(self.box.call(tool, json.dumps(args)))

    def test_it_forwards_and_reads_the_resolved_time_back(self):
        when = self.soon(90)
        answer = self.call("create_reminder", when=when, text="call Mum")
        self.assertTrue(answer["ok"])
        self.assertEqual(when, answer["when"])
        self.assertEqual("push", answer["deliver"])

    def test_what_comes_back_is_the_record_and_not_the_request(self):
        """Reading the request back would be the assistant confirming a time
        the device may have refused, adjusted, or read in a different zone."""
        source = (ROOT / "aipi5" / "llm" / "tools.py").read_text(encoding="utf-8")
        body = re.search(r"def _create_reminder\(.*?\n    def ", source,
                         re.S).group(0)
        self.assertIn('_ok(**answer["reminder"])', body)

    def test_a_null_channel_means_the_phone(self):
        """Strict mode sends an explicit null for an optional argument, and
        `str(None)` is the string "None", which no schedule accepts."""
        answer = self.call("create_reminder", when=self.soon(), text="x",
                           deliver=None)
        self.assertTrue(answer["ok"])
        self.assertEqual("push", answer["deliver"])

    def test_a_missing_time_is_a_question_rather_than_a_guess(self):
        answer = self.call("create_reminder", when="", text="call Mum")
        self.assertFalse(answer["ok"])
        self.assertIn("Ask the person when", answer["error"])
        self.assertEqual([], self.agent.sent)

    def test_a_refusal_from_the_far_side_becomes_a_sentence(self):
        answer = self.call("create_reminder", when="tomorrow", text="call Mum")
        self.assertFalse(answer["ok"])
        self.assertIn("could not read", answer["error"])

    def test_an_agent_that_is_not_answering_is_reported_and_not_raised(self):
        box = ToolBox(agent=FakeAgent(self.service, status=503))
        answer = parse(box.call("create_reminder",
                                json.dumps({"when": self.soon(), "text": "x"})))
        self.assertFalse(answer["ok"])
        self.assertIn("not answering", answer["error"])

    def test_reading_and_cancelling_go_through_the_same_door(self):
        made = self.call("create_reminder", when=self.soon(), text="call Mum")
        listing = self.call("read_reminders")
        self.assertEqual(1, listing["count"])
        cancelled = self.call("delete_reminder", id=made["id"])
        self.assertTrue(cancelled["ok"])
        self.assertEqual([], self.service.schedule.pending())

    def test_cancelling_with_no_id_asks_rather_than_guessing(self):
        self.call("create_reminder", when=self.soon(), text="call Mum")
        answer = self.call("delete_reminder", id="")
        self.assertFalse(answer["ok"])
        self.assertIn("read_reminders", answer["error"])

    def test_the_tools_are_not_offered_without_an_agent(self):
        names = {t["name"] for t in ToolBox().schemas()}
        for tool in ("create_reminder", "read_reminders", "delete_reminder"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, names)

    def test_the_schema_refuses_words_and_says_where_to_get_the_time(self):
        schema = next(t for t in self.box.schemas()
                      if t["name"] == "create_reminder")
        self.assertIn("absolute local date and time", schema["description"])
        self.assertIn("Never send 'tomorrow'", schema["description"])
        self.assertIn("ask them when", schema["description"])


class TestTheTwoToolTablesStayApart(unittest.TestCase):
    """The agent already has `remind_me`, `list_reminders`, `cancel_reminder`.

    Nothing is broken by two names for one store — they reach the same
    `Schedule` on purpose. What would be broken is the invariant in
    `tests/test_agent_boundary.py` that the two dispatch tables are disjoint,
    which is what makes "nothing the agent can do is reachable from a spoken
    sentence" checkable at all. So the assistant's three are named differently,
    and that is a decision rather than a coincidence.
    """

    def test_the_names_do_not_collide(self):
        from aipi5.agent.tools import AgentToolBox

        assistant = set(ToolBox()._handlers)
        agent = set(AgentToolBox()._handlers)
        self.assertEqual(set(), assistant & agent)
        self.assertLessEqual({"create_reminder", "read_reminders",
                              "delete_reminder"}, assistant)
        self.assertLessEqual({"remind_me", "list_reminders", "cancel_reminder"},
                             agent)


class TestTheMessagesAreNotRunsInDisguise(unittest.TestCase):
    """Asserted over the source. What this guards is that nobody ever routes
    these three back through `ask()` for tidiness — which would restore the
    24-step budget, the daily limit and the model, on the one path where the
    person is promised something will happen at nine."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "aipi5" / "agent" / "runtime.py").read_text(encoding="utf-8")
        cls.block = text[text.index("def reminder_create"):
                         text.index("def open_page")]

    def test_they_call_the_schedule_and_nothing_else(self):
        self.assertIn("self.schedule.add(", self.block)
        for forbidden in ("self.ask(", "self.loop", "AgentLoop", "_work",
                          "threading.Thread", "daily_run_limit"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, self.block)

    def test_the_handler_accepts_all_three(self):
        text = (ROOT / "aipi5" / "agent" / "runtime.py").read_text(encoding="utf-8")
        for kind in ("assistant.reminder.create", "assistant.reminder.list",
                     "assistant.reminder.cancel"):
            with self.subTest(kind=kind):
                self.assertIn(f'kind == "{kind}"', text)

    def test_the_bodies_are_bounded_before_they_reach_the_schedule(self):
        text = (ROOT / "aipi5" / "agent" / "runtime.py").read_text(encoding="utf-8")
        handler = text[text.index('if kind == "assistant.reminder.create"'):]
        handler = handler[:handler.index("return self._json(400")]
        self.assertIn("[:64]", handler)
        self.assertIn("schedule_mod.MAX_TEXT", handler)


if __name__ == "__main__":
    unittest.main()
