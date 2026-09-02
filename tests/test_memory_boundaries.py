"""Four stores, four lifetimes, and the one that must not become a fifth.

Unifying the *page* was the point of this work. Unifying the **records** would
have been a mistake, and it is the kind of mistake that looks like tidying up:
there are four things that could be called "what the assistant remembers", they
overlap, and merging them into one transcript would be fewer moving parts.

They are not the same thing, and what separates them is who is allowed to see
each one and for how long:

    ConversationLog   what was said out loud in the room, 24 hours, on disk,
                      role-filtered, and swept by `Retention`
    Conversation      the model's short context, a handful of turns, in memory,
                      forgotten after a silence
    Schedule / Notes  the agent's own durable records, written only when
                      somebody explicitly asked for a reminder or said to
                      remember something
    EventLog          the presentation index this project added — a bounded
                      ring of rows in the order a person would have watched
                      them happen

The fourth is the one to watch. A page needs the rows in order; nothing needs
them tomorrow. If it ever grows a file it becomes a permanent, unfiltered
record of everything said in a living room, kept beside a 24-hour record that
was designed to be filtered and swept — and nobody would notice, because the
page would look exactly the same.

So these tests are mostly about absences: no writes, a hard cap, and references
between the stores by id rather than copies of their contents.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from aipi5.assistant.coordinator import AgentBridge, Coordinator
from aipi5.assistant.events import DEPTH, EventLog
from aipi5.llm.conversation import Conversation

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "aipi5" / "assistant" / "events.py"
COORDINATOR = ROOT / "aipi5" / "assistant" / "coordinator.py"


class TestTheIndexIsNotARecord(unittest.TestCase):

    def test_it_writes_nothing_anywhere(self):
        """Asserted over the source, because what it guards is an absence.

        A `path` argument added here one afternoon turns a presentation index
        into a permanent unfiltered log of everything said in a living room —
        kept beside a 24-hour record that was deliberately filtered and swept,
        and looking identical from the page.
        """
        source = EVENTS.read_text(encoding="utf-8")
        for forbidden in ("open(", "Path(", "write_text", "os.replace",
                          "sqlite3", "json.dump", "pickle"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, source)

    def test_it_imports_nothing_that_could_persist(self):
        tree = ast.parse(EVENTS.read_text(encoding="utf-8"), str(EVENTS))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(set(), imported & {"pathlib", "os", "shutil", "json",
                                            "sqlite3", "tempfile"})

    def test_it_is_bounded_and_the_bound_is_small(self):
        """Small enough that it cannot become a day's transcript by accident.
        A run posts a couple of dozen rows; a day of conversation is a few
        hundred."""
        self.assertLessEqual(DEPTH, 1000)
        log = EventLog(depth=DEPTH)
        for n in range(DEPTH + 50):
            log.publish("user", "voice", text=str(n))
        rows, _ = log.collect(0)
        self.assertEqual(DEPTH, len(rows))
        self.assertEqual(50, log.dropped())

    def test_what_it_dropped_is_counted_rather_than_hidden(self):
        """A reader that lost the front of a transcript and is not told has a
        hole it cannot see. Counting is what lets a page say so."""
        log = EventLog(depth=4)
        for n in range(10):
            log.publish("user", "voice", text=str(n))
        self.assertEqual(6, log.snapshot()["dropped"])

    def test_a_restart_starts_it_empty(self):
        """There is no file to read back, which is the point of the two tests
        above stated as behaviour."""
        self.assertEqual(([], 0), EventLog().collect(0))


class TestTheStoresReferenceRatherThanCopy(unittest.TestCase):

    def test_a_delegated_run_is_tied_to_its_rows_by_id(self):
        """The agent's mailbox stays in `aipi5-agent.service` and is not copied
        here. What crosses is a run id, which is enough to find the run and not
        enough to be a second copy of it."""
        class Proxy:
            def poll(self, since, timeout=None):
                return 200, {"events": [], "cursor": since, "agent": {}}

            def say(self, message):
                return 200, {"ok": True, "run": "r-abc123"}

            def snapshot(self):
                return {"run": "", "state": "idle", "busy": False,
                        "pending": None}

        events = EventLog()
        proxy = Proxy()
        coordinator = Coordinator(events=events, agent=proxy,
                                  bridge=AgentBridge(proxy, events))
        self.addCleanup(coordinator.close)
        coordinator.delegate("why did the screen restart")
        rows, _ = events.collect(0)
        self.assertEqual("r-abc123", rows[0]["run"])

    def test_a_tool_row_carries_a_name_and_a_summary_and_not_a_result(self):
        """`translate` takes the agent's own one-line summary. The result
        itself never leaves `aipi5-agent.service`, which is what keeps the
        contents of a config file out of a living room."""
        from aipi5.assistant.coordinator import translate

        row = translate({"type": "agent.tool", "name": "read_config_file",
                         "ok": True, "summary": "read config/aipi5.yaml",
                         "result": {"text": "openai:\\n  model: …"}})
        self.assertEqual("read config/aipi5.yaml", row["text"])
        self.assertNotIn("result", row)
        self.assertNotIn("result", row.get("meta", {}))


class TestTheShortContextIsStillShort(unittest.TestCase):
    """Unifying the page must not have quietly unbounded the model's memory.

    Both limits are still there and still mean what they meant: turns, because
    that is what a person perceives, and a silence, because somebody walking up
    an hour later is starting a new conversation.
    """

    def test_a_silence_still_forgets_the_thread(self):
        conversation = Conversation(idle_seconds=600)
        conversation.begin_turn(now=1000.0)
        conversation.user("what's the weather")
        conversation.assistant("It's 68.")
        self.assertTrue(conversation.begin_turn(now=1000.0 + 601))
        self.assertEqual(0, len(conversation))

    def test_the_turn_count_still_bounds_it(self):
        conversation = Conversation(max_turns=2)
        for n in range(6):
            conversation.begin_turn(now=1000.0 + n)
            conversation.user(f"q{n}")
            conversation.assistant(f"a{n}")
            conversation.trim()
        self.assertEqual(4, len(conversation))

    def test_the_event_log_is_not_the_model_context(self):
        """They are different objects with different lifetimes, and nothing
        feeds one from the other. A coordinator that handed its transcript to
        the model would have made the short context unbounded."""
        tree = ast.parse(COORDINATOR.read_text(encoding="utf-8"),
                         str(COORDINATOR))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(a.name for a in node.names)
        self.assertNotIn("Conversation", names)
        self.assertNotIn("conversation.user",
                         COORDINATOR.read_text(encoding="utf-8"))


class TestTheAudibleRecordIsUntouched(unittest.TestCase):
    """The 24-hour log is AIA's, it is on disk, it is role-filtered and it is
    swept. None of that changed, and the assertion is that the new layer did
    not start writing to it or reading from it."""

    def test_the_assistant_package_does_not_touch_the_conversation_log(self):
        """Checked as imports, not as text. Both names appear in prose in
        `events.py`, explaining what it is *not* — and a substring check calls
        that a dependency."""
        for name in ("events.py", "coordinator.py", "consent.py"):
            path = ROOT / "aipi5" / "assistant" / name
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
            with self.subTest(module=name):
                self.assertEqual(set(), names & {"ConversationLog", "Retention"})

    def test_the_voice_loop_still_records_what_was_said_out_loud(self):
        main = (ROOT / "aipi5" / "main.py").read_text(encoding="utf-8")
        self.assertIn('assistant.history.record("user", text, language)', main)
        self.assertIn('assistant.history.record("aia", reply, language)', main)

    def test_a_spoken_delegated_answer_is_recorded_and_a_silent_one_is_not(self):
        """It was said in the room, so it belongs in the record of what was
        said in the room. One that was only shown on the screen was not."""
        main = (ROOT / "aipi5" / "main.py").read_text(encoding="utf-8")
        body = main[main.index("def speak_delegated_answer"):]
        body = body[:body.index("\n    def ")]
        record = body.index('self.history.record("aia", text, language)')
        for refusal in ("leaving it on the screen",):
            self.assertLess(body.index(refusal), record)


if __name__ == "__main__":
    unittest.main()
