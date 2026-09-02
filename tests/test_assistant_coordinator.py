"""One door in, and the same answer whichever of the four ways it came.

The bug this whole layer exists for has no exception and no log line: the same
sentence reached a different set of tools and a different policy depending on
which of two pages somebody had opened, and neither page said so. So the first
class here asks the same request four ways and asserts it took one path.

The rest is the two things a coordinator owns that nothing else can be trusted
to — that a delegated run's mailbox arrives in the transcript without its tool
results coming with it, and that an approval is never inferred.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.assistant.coordinator import AgentBridge, Coordinator, translate
from aipi5.assistant.events import EventLog


class FakeProxy:
    """`AgentProxy`'s three methods and nothing else, over a scripted mailbox."""

    def __init__(self, batches=None, said=None, state=None):
        self.batches = list(batches or [])
        self.said = said if said is not None else []
        self.state = state or {"run": "", "state": "idle", "busy": False,
                               "pending": None}
        self.polls = 0

    def poll(self, since, timeout=None):
        self.polls += 1
        if self.batches:
            return 200, self.batches.pop(0)
        return 200, {"events": [], "cursor": since, "agent": dict(self.state)}

    def say(self, message):
        self.said.append(message)
        if message.get("type") == "agent.ask":
            return 200, {"ok": True, "run": "r-abc123"}
        return 200, {"ok": True}

    def snapshot(self):
        return dict(self.state)


class DeadProxy(FakeProxy):
    def poll(self, since, timeout=None):
        return 503, {"error": "the agent is not answering"}

    def say(self, message):
        self.said.append(message)
        return 503, {"error": "the agent is not answering"}

    def snapshot(self):
        return None


class TestOneRequestTakesOnePath(unittest.TestCase):
    """The acceptance for this phase, stated as a test.

    Wake voice, touchscreen dictation, keyboard text and phone text all reach
    the same function with the same arguments. Before this, three of the four
    became `agent.ask` and started a 24-step maintenance run.
    """

    def setUp(self):
        self.asked: list[tuple[str, str]] = []
        self.agent = FakeProxy()
        self.coordinator = Coordinator(
            events=EventLog(), agent=self.agent,
            respond=lambda text, language: self.asked.append(
                (text, language)) or "thirty percent")

    def test_all_four_ways_in_reach_the_same_short_turn(self):
        self.coordinator.submit_voice("set the volume to thirty", "en")
        self.coordinator.submit_text("set the volume to thirty", "touch", "en")
        self.coordinator.submit_text("set the volume to thirty", "text", "en")
        # The phone sends text too; it is the same call with the same source.
        self.coordinator.submit_text("set the volume to thirty", "text", "en")
        self.assertEqual([("set the volume to thirty", "en")] * 4, self.asked)

    def test_none_of_them_started_a_maintenance_run(self):
        """The regression this replaces. Typing used to send `agent.ask`
        unconditionally, so "set the volume to thirty" spent a run of the
        daily budget doing something the voice loop does in one call."""
        self.coordinator.submit_text("set the volume to thirty", "text", "en")
        self.assertEqual([], self.agent.said)

    def test_the_source_is_recorded_and_the_answer_is_not_duplicated(self):
        self.coordinator.submit_voice("what time is it", "en")
        rows, _ = self.coordinator.collect(0)
        self.assertEqual([("user", "voice", "what time is it"),
                          ("assistant", "voice", "thirty percent")],
                         [(r["kind"], r["source"], r["text"]) for r in rows])

    def test_an_unknown_source_is_normalised_rather_than_refused(self):
        """A caller that invents one must not lose somebody's sentence."""
        self.coordinator.submit_text("hello", "phone", "en")
        rows, _ = self.coordinator.collect(0)
        self.assertEqual("text", rows[0]["source"])

    def test_an_empty_request_is_refused_without_a_row(self):
        self.assertFalse(self.coordinator.submit_text("   ")["ok"])
        self.assertEqual(([], 0), self.coordinator.collect(0))


class TestWithNothingBehindIt(unittest.TestCase):
    """A device with no API key and no agent still answers, and says why."""

    def test_no_conversation_is_a_sentence_and_not_an_exception(self):
        coordinator = Coordinator(events=EventLog())
        answer = coordinator.submit_voice("tell me a story", "en")
        self.assertFalse(answer["ok"])
        self.assertIn("not available", answer["error"])
        rows, _ = coordinator.collect(0)
        self.assertEqual(["user", "error"], [r["kind"] for r in rows])

    def test_delegating_with_no_agent_installed_says_so(self):
        coordinator = Coordinator(events=EventLog(), respond=lambda t, l: "ok")
        answer = coordinator.delegate("find out why the screen restarted")
        self.assertFalse(answer["ok"])
        self.assertIn("not installed", answer["error"])

    def test_an_unreachable_agent_is_reported_rather_than_raised(self):
        coordinator = Coordinator(events=EventLog(), agent=DeadProxy(),
                                  respond=lambda t, l: "ok")
        answer = coordinator.delegate("look into it")
        self.assertFalse(answer["ok"])
        rows, _ = coordinator.collect(0)
        self.assertEqual([("error", "agent")],
                         [(r["kind"], r["source"]) for r in rows])

    def test_the_snapshot_survives_an_agent_that_will_not_answer(self):
        coordinator = Coordinator(events=EventLog(), agent=DeadProxy())
        state = coordinator.snapshot()
        self.assertTrue(state["agent"])
        self.assertIsNone(state["pending"])


class TestOneTurnAtATime(unittest.TestCase):
    """Two submissions share a `Conversation`. Interleaving them produces a
    history in which the model answers one question with another's tool
    results — and nothing about that looks like a failure afterwards."""

    def test_a_second_request_waits_for_the_first(self):
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def slow(text, language):
            order.append("in:" + text)
            started.set()
            release.wait(5.0)
            order.append("out:" + text)
            return text

        coordinator = Coordinator(events=EventLog(), respond=slow)
        first = threading.Thread(
            target=lambda: coordinator.submit_voice("one", "en"), daemon=True)
        first.start()
        self.assertTrue(started.wait(5.0))
        second = threading.Thread(
            target=lambda: coordinator.submit_text("two", "text", "en"),
            daemon=True)
        second.start()
        time.sleep(0.1)
        self.assertEqual(["in:one"], order)
        release.set()
        first.join(5.0)
        second.join(5.0)
        self.assertEqual(["in:one", "out:one", "in:two", "out:two"], order)

    def test_a_request_that_waited_too_long_is_told_so(self):
        release = threading.Event()
        coordinator = Coordinator(
            events=EventLog(), turn_wait_s=0.05,
            respond=lambda text, language: (release.wait(5.0), "done")[1])
        busy = threading.Thread(
            target=lambda: coordinator.submit_voice("one", "en"), daemon=True)
        busy.start()
        time.sleep(0.1)
        answer = coordinator.submit_text("two", "text", "en")
        release.set()
        busy.join(5.0)
        self.assertFalse(answer["ok"])
        self.assertIn("still working", answer["error"])


class TestTheAgentMailboxBecomesTranscript(unittest.TestCase):

    def test_a_running_tool_line_is_not_a_row(self):
        """It is a state, replaced by the finished row a moment later. Both
        would be a transcript that says everything twice."""
        self.assertIsNone(translate({"type": "agent.tool", "state": "running",
                                     "name": "read_journal"}))

    def test_a_state_change_is_not_a_row(self):
        self.assertIsNone(translate({"type": "agent.state", "state": "finishing"}))

    def test_an_unknown_type_is_ignored_rather_than_failing(self):
        """The agent and the assistant are separate deployments and one can be
        newer than the other."""
        self.assertIsNone(translate({"type": "agent.something.new"}))

    def test_a_finished_tool_carries_its_summary_and_never_its_result(self):
        row = translate({"type": "agent.tool", "name": "read_journal", "ok": True,
                         "summary": "200 lines from the display unit",
                         "result": {"lines": ["secret", "secret"]}})
        self.assertEqual("tool", row["kind"])
        self.assertEqual("read_journal", row["tool"])
        self.assertEqual("200 lines from the display unit", row["text"])
        self.assertNotIn("result", row)
        self.assertNotIn("result", row.get("meta", {}))

    def test_the_question_and_the_answer_are_ordinary_conversation(self):
        self.assertEqual("user", translate({"type": "agent.ask",
                                            "text": "why?"})["kind"])
        self.assertEqual("assistant", translate({"type": "agent.say",
                                                 "text": "because"})["kind"])

    def test_an_approval_carries_its_token(self):
        row = translate({"type": "agent.approval", "token": "t-1",
                         "what": "Change the display timeout?",
                         "detail": "screensaver.timeout_seconds: 60 -> 90"})
        self.assertEqual("approval", row["kind"])
        self.assertEqual("t-1", row["meta"]["token"])

    def test_a_finished_run_carries_what_it_cost(self):
        row = translate({"type": "agent.done", "run": "r-1", "ok": True,
                         "steps": 6, "tool_calls": 11, "seconds": 92.4})
        self.assertEqual("done", row["kind"])
        self.assertEqual(6, row["meta"]["steps"])

    def test_every_translated_row_is_publishable(self):
        """The two halves must agree. A row `translate` produces that
        `EventLog` refuses is an exception on the bridge thread, at the moment
        a run is trying to report itself."""
        log = EventLog()
        for message in ({"type": "agent.ask", "text": "why?"},
                        {"type": "agent.say", "text": "because"},
                        {"type": "agent.tool", "name": "n", "ok": True,
                         "summary": "s"},
                        {"type": "agent.approval", "token": "t", "what": "w"},
                        {"type": "agent.approval.gone", "outcome": "timeout"},
                        {"type": "agent.error", "error": "boom"},
                        {"type": "agent.done", "steps": 1, "seconds": 1}):
            row = translate(message)
            with self.subTest(type=message["type"]):
                self.assertIsNotNone(row)
                log.publish(row.pop("kind"), "agent", **row)


class TestTheBridge(unittest.TestCase):

    def test_one_pump_draws_a_batch_and_advances_the_cursor(self):
        proxy = FakeProxy([{"cursor": 7, "events": [
            {"type": "agent.ask", "run": "r-1", "text": "why?"},
            {"type": "agent.tool", "name": "read_journal", "ok": True,
             "summary": "200 lines"},
            {"type": "agent.say", "run": "r-1", "text": "the display unit restarted"},
        ], "agent": {"run": "r-1", "state": "running"}}])
        log = EventLog()
        bridge = AgentBridge(proxy, log)
        self.assertEqual(3, bridge.pump(timeout=0))
        self.assertEqual(7, bridge._cursor)
        rows, _ = log.collect(0)
        self.assertEqual(["user", "tool", "assistant"],
                         [r["kind"] for r in rows])
        self.assertTrue(all(r["source"] == "agent" for r in rows))

    def test_a_finished_run_stops_the_bridge(self):
        """A bridge that outlived its run is a long poll per delegated task,
        forever, against a service that has nothing to say.

        `follow` rather than `start`: this is the pump under test, and a thread
        pumping the same scripted mailbox alongside it would race for the batch.
        """
        proxy = FakeProxy([{"cursor": 2, "events": [
            {"type": "agent.done", "run": "r-1", "ok": True, "steps": 3,
             "seconds": 4},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog())
        bridge.follow("r-1")
        self.assertTrue(bridge.busy())
        bridge.pump(timeout=0)
        self.assertFalse(bridge.busy())

    def test_an_unreachable_agent_publishes_one_row_and_gives_up(self):
        """One row, not one per poll.

        The first version published on every failed poll, and a socket that is
        simply not there filled the whole 400-row ring with the same sentence
        inside a second — so the transcript a person came back to read was
        gone, replaced by the news that it was gone.
        """
        log = EventLog()
        bridge = AgentBridge(DeadProxy(), log)
        bridge.follow("r-1")
        self.assertEqual(0, bridge.pump(timeout=0))
        self.assertEqual(0, bridge.pump(timeout=0))
        self.assertEqual(0, bridge.pump(timeout=0))
        self.assertFalse(bridge.busy())
        rows, _ = log.collect(0)
        self.assertEqual(1, len(rows))
        self.assertEqual("error", rows[0]["kind"])

    def test_starting_twice_does_not_draw_every_row_twice(self):
        """The agent runs one at a time and appends a follow-up to the run in
        flight, so "start" arriving twice means one conversation."""
        bridge = AgentBridge(FakeProxy(), EventLog(), poll_s=0.05, drain_s=0.05)
        bridge.start("r-1")
        try:
            first = bridge._thread
            bridge.start("r-2")
            self.assertEqual("r-2", bridge.run)
            self.assertIs(first, bridge._thread)
        finally:
            bridge.stop()

    def test_the_thread_ends_when_the_run_does(self):
        proxy = FakeProxy([{"cursor": 1, "events": [
            {"type": "agent.done", "run": "r-1", "ok": True},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog(), poll_s=0.05, drain_s=0.05)
        bridge.start("r-1")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and bridge.alive():
            time.sleep(0.02)
        self.assertFalse(bridge.alive())

    def test_the_loop_does_not_spin_against_a_proxy_that_answers_at_once(self):
        """`pump` hands its timeout to the far side, and the loop is written
        assuming a poll with nothing to report blocks out there for twenty
        seconds. That assumption belongs to `aipi5-agent.service`, not to this
        file — and when it failed, two bridges took a core each and the damage
        showed up as an unrelated socket test timing out three files later."""
        proxy = FakeProxy()
        bridge = AgentBridge(proxy, EventLog(), poll_s=1.0, drain_s=0.05)
        bridge.start("r-1")
        try:
            time.sleep(0.6)
        finally:
            bridge.stop()
        # A floor of 0.25 s means at most three or four passes in 0.6 s. The
        # unbounded version managed tens of thousands.
        self.assertLessEqual(proxy.polls, 6)

    def test_the_drain_does_not_spin_against_a_proxy_that_answers_at_once(self):
        """`pump` hands its timeout to the *proxy*. A real long poll waits out
        there; a socket that is gone, or a stub, answers instantly — so a drain
        written as a polling loop is a busy loop on a Pi core."""
        proxy = FakeProxy([{"cursor": 1, "events": [
            {"type": "agent.done", "run": "r-1", "ok": True},
        ], "agent": {"run": "r-1", "state": "idle"}}])
        bridge = AgentBridge(proxy, EventLog(), poll_s=0.05, drain_s=0.2)
        bridge.start("r-1")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and bridge.alive():
            time.sleep(0.02)
        self.assertFalse(bridge.alive())
        self.assertLessEqual(proxy.polls, 4)


class TestDelegation(unittest.TestCase):

    def setUp(self):
        self.proxy = FakeProxy()
        self.coordinator = Coordinator(
            events=EventLog(), agent=self.proxy,
            respond=lambda text, language: "ok",
            bridge=AgentBridge(self.proxy, EventLog(), poll_s=0.05,
                               drain_s=0.05))
        # `delegate` starts a real thread against a proxy that answers at once.
        # Left running, that is a poll loop per test for the rest of the suite
        # — which is how this was found: two of them starved the process
        # enough that a socket test three files later timed out.
        self.addCleanup(self.coordinator.close)

    def test_it_sends_one_ask_and_returns_the_run_id(self):
        answer = self.coordinator.delegate("why did the screen restart",
                                           "I started checking that.")
        self.assertEqual({"type": "agent.ask",
                          "text": "why did the screen restart"},
                         self.proxy.said[0])
        self.assertEqual("r-abc123", answer["run"])

    def test_the_transcript_says_it_was_handed_over(self):
        self.coordinator.delegate("why did the screen restart",
                                  "I started checking that.")
        rows, _ = self.coordinator.collect(0)
        self.assertEqual(1, len(rows))
        self.assertEqual("delegate_agent_task", rows[0]["tool"])
        self.assertEqual("I started checking that.", rows[0]["text"])

    def test_an_empty_task_is_refused_before_a_run_is_spent(self):
        self.assertFalse(self.coordinator.delegate("  ")["ok"])
        self.assertEqual([], self.proxy.said)


class TestApprovalIsNeverInferred(unittest.TestCase):
    """Silence, a timeout, an unclear answer and a stale token all mean no.

    None of that is decided here — the desk in `aipi5-agent.service` binds an
    answer to the token it issued. What this side must not do is invent a
    token, or turn something that is not a button press into one.
    """

    def setUp(self):
        self.proxy = FakeProxy()
        self.coordinator = Coordinator(events=EventLog(), agent=self.proxy)

    def test_a_missing_token_is_refused_without_reaching_the_agent(self):
        self.assertFalse(self.coordinator.answer_approval("", True)["ok"])
        self.assertEqual([], self.proxy.said)

    def test_the_token_is_forwarded_verbatim_and_the_answer_is_a_boolean(self):
        self.coordinator.answer_approval("t-1", "yes please")
        self.assertEqual({"type": "agent.answer", "token": "t-1", "allow": True},
                         self.proxy.said[0])

    def test_a_denial_is_sent_as_a_denial(self):
        self.coordinator.answer_approval("t-1", False)
        self.assertIs(False, self.proxy.said[0]["allow"])

    def test_cancelling_with_no_agent_is_a_sentence(self):
        self.assertFalse(Coordinator(events=EventLog()).cancel("r-1")["ok"])


if __name__ == "__main__":
    unittest.main()
