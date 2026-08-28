"""The agent loop, in the ways it can fail to stop.

An agent's characteristic failure is not doing the wrong thing — the helper's
allowlist covers that — it is *never finishing*: looping on a tool that keeps
failing, or reading a log forever, quietly spending money in another room. So
every one of these tests is about an ending.

Three properties are load-bearing and each has bitten a real system somewhere:

**Every ceiling ends in a sentence.** When a budget runs out the loop makes one
last request with no tools offered, so the person gets "I ran out of steps, and
here is what I found" rather than a transcript that stops mid-thought.

**Compaction never splits a tool-call pair.** An assistant message carrying
`tool_calls` and the `tool` messages answering it must travel together, or the
API rejects the request with an error that mentions neither history nor
compaction.

**A refusal is not retried.** Policy said no; asking again gets the same answer
more slowly and burns a step doing it.

No network, no sockets, no model. The clock is injected, so a fifteen-minute
budget is exhausted in microseconds.
"""

from __future__ import annotations

import json
import threading
import unittest

from aipi5.agent.loop import (MAX_RETRIES_PER_TOOL, AgentLoop, Budget,
                              _compact, _safe_boundary)


class FakeStep:
    def __init__(self, text="", tool_calls=(), ok=True, error=""):
        self.text = text
        self.tool_calls = list(tool_calls)
        self.ok = ok
        self.error = error
        self.ms = 1.0
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.message = {"role": "assistant", "content": text or None,
                        "tool_calls": [{"id": c.id} for c in self.tool_calls]}


class FakeCall:
    def __init__(self, name="system_facts", arguments="{}", ident="call-1"):
        self.id = ident
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class FakeClient:
    """Hands back a scripted sequence, and records what it was offered."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.offered: list = []          # the `tools` argument of each call

    def step(self, messages, tools=None, **kwargs):
        self.offered.append(tools)
        if self.steps:
            return self.steps.pop(0)
        return FakeStep(text="done")


class FakeToolBox:
    def __init__(self, answer=None):
        self.answer = answer or json.dumps({"ok": True, "uptime_s": 1})
        self.calls = 0

    def schemas(self):
        return [{"type": "function", "function": {"name": "system_facts"}}]

    def call(self, name, arguments):
        self.calls += 1
        return self.answer


class Clock:
    """Monotonic, and moves only when a test says so."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def build(steps, toolbox=None, budget=None, clock=None):
    events: list[dict] = []
    loop = AgentLoop(FakeClient(steps), toolbox or FakeToolBox(),
                     events.append, "system", budget or Budget(),
                     clock=clock or Clock())
    return loop, events


class TestEndings(unittest.TestCase):

    def test_a_plain_answer_ends_the_run(self):
        loop, events = build([FakeStep(text="Everything is fine.")])
        result = loop.run("r-1", "how are you", threading.Event())
        self.assertTrue(result.ok)
        self.assertEqual("Everything is fine.", result.text)
        self.assertEqual(1, result.steps)
        self.assertEqual("", result.stopped)
        self.assertEqual("agent.done", events[-1]["type"])

    def test_the_step_ceiling_ends_the_run_with_a_sentence(self):
        """The whole point: a budget must not produce silence."""
        forever = [FakeStep(tool_calls=[FakeCall()]) for _ in range(30)]
        loop, events = build(forever, budget=Budget(max_steps=3))
        result = loop.run("r-1", "look at everything", threading.Event())

        self.assertEqual("I reached my limit of 3 steps", result.stopped)
        # The last request was made with no tools, so it had to answer in prose.
        self.assertIsNone(loop.client.offered[-1])
        self.assertIsNotNone(loop.client.offered[0])

    def test_the_tool_call_ceiling_ends_the_run(self):
        calls = [FakeCall(ident=f"c{i}") for i in range(3)]
        steps = [FakeStep(tool_calls=calls) for _ in range(10)]
        loop, _ = build(steps, budget=Budget(max_tool_calls=4))
        result = loop.run("r-1", "check", threading.Event())
        self.assertIn("4 checks", result.stopped)
        self.assertGreaterEqual(result.tool_calls, 4)

    def test_the_clock_ends_the_run(self):
        clock = Clock()

        class Ticking(FakeToolBox):
            def call(self, name, arguments):
                clock.t += 400.0          # each check costs most of the budget
                return super().call(name, arguments)

        steps = [FakeStep(tool_calls=[FakeCall()]) for _ in range(10)]
        loop, _ = build(steps, toolbox=Ticking(),
                        budget=Budget(max_runtime_s=900.0), clock=clock)
        result = loop.run("r-1", "check", threading.Event())
        self.assertIn("ran out of time", result.stopped)

    def test_stopping_ends_the_run_at_the_next_step(self):
        stop = threading.Event()
        stop.set()
        loop, _ = build([FakeStep(text="stopped")])
        result = loop.run("r-1", "check", stop)
        self.assertEqual("you asked me to stop", result.stopped)
        self.assertIsNone(loop.client.offered[-1])

    def test_three_failed_requests_give_up_rather_than_looping(self):
        broken = [FakeStep(ok=False, error="the key is wrong") for _ in range(9)]
        loop, events = build(broken)
        result = loop.run("r-1", "check", threading.Event())
        self.assertFalse(result.ok)
        self.assertEqual("the model could not be reached", result.stopped)
        self.assertEqual(3, sum(1 for e in events if e["type"] == "agent.error"))

    def test_a_failure_that_recovers_does_not_end_the_run(self):
        loop, _ = build([FakeStep(ok=False, error="blip"),
                         FakeStep(text="recovered")])
        result = loop.run("r-1", "check", threading.Event())
        self.assertTrue(result.ok)
        self.assertEqual("recovered", result.text)


class TestReportingToThePhone(unittest.TestCase):

    def test_a_tool_is_announced_before_it_runs_not_after(self):
        """A slow check should say what it is doing while it does it."""
        loop, events = build([FakeStep(tool_calls=[FakeCall()]),
                              FakeStep(text="done")])
        loop.run("r-1", "check", threading.Event())
        tools = [e for e in events if e["type"] == "agent.tool"]
        self.assertEqual(["running", "done"], [e["state"] for e in tools])

    def test_the_models_reasoning_is_never_emitted(self):
        loop, events = build([FakeStep(tool_calls=[FakeCall()]),
                              FakeStep(text="answer")])
        loop.run("r-1", "check", threading.Event())
        kinds = {e["type"] for e in events}
        self.assertEqual(set(), kinds - {"agent.ask", "agent.say", "agent.state",
                                         "agent.tool", "agent.error",
                                         "agent.done"})

    def test_the_summary_names_what_is_being_checked_not_how(self):
        call = FakeCall("read_journal", json.dumps({"unit": "kodama-lite"}))
        loop, events = build([FakeStep(tool_calls=[call]), FakeStep(text="ok")])
        loop.run("r-1", "check", threading.Event())
        running = next(e for e in events
                       if e["type"] == "agent.tool" and e["state"] == "running")
        self.assertEqual("the kodama-lite log", running["summary"])

    def test_a_malformed_argument_blob_does_not_break_the_summary(self):
        call = FakeCall("read_journal", "{not json")
        loop, events = build([FakeStep(tool_calls=[call]), FakeStep(text="ok")])
        loop.run("r-1", "check", threading.Event())
        self.assertTrue(any(e["type"] == "agent.tool" for e in events))


class TestRetries(unittest.TestCase):

    def test_a_refusal_is_not_retried(self):
        """Policy said no. Asking again is the same answer, more slowly."""
        refused = json.dumps({"ok": False, "error": "not on the list",
                              "retryable": False})
        box = FakeToolBox(refused)
        loop, _ = build([FakeStep(tool_calls=[FakeCall()]), FakeStep(text="ok")],
                        toolbox=box)
        loop.run("r-1", "check", threading.Event())
        self.assertEqual(1, box.calls)

    def test_a_transient_failure_is_retried(self):
        transient = json.dumps({"ok": False, "error": "helper not answering",
                                "retryable": True})
        box = FakeToolBox(transient)
        loop, _ = build([FakeStep(tool_calls=[FakeCall()]), FakeStep(text="ok")],
                        toolbox=box)
        loop.run("r-1", "check", threading.Event())
        # One attempt plus MAX_RETRIES_PER_TOOL of them.
        self.assertEqual(1 + MAX_RETRIES_PER_TOOL, box.calls)


class TestFollowUp(unittest.TestCase):

    def test_something_said_mid_run_joins_the_run(self):
        """"Wait, check the camera instead" must not need starting again."""
        loop, events = build([FakeStep(tool_calls=[FakeCall()]),
                              FakeStep(text="ok")])
        loop.push("actually check the camera")
        loop.run("r-1", "check the disk", threading.Event())
        echoed = [e for e in events
                  if e["type"] == "agent.ask" and e.get("echo")]
        self.assertEqual(1, len(echoed))
        self.assertEqual("actually check the camera", echoed[0]["text"])

    def test_empty_text_is_not_pushed(self):
        loop, _ = build([FakeStep(text="ok")])
        self.assertFalse(loop.push("   "))


class TestCompaction(unittest.TestCase):
    """The one that produces an API error naming nothing useful."""

    def test_a_tool_call_is_never_separated_from_its_result(self):
        messages = [{"role": "system", "content": "s"}]
        for n in range(20):
            messages.append({"role": "user", "content": f"u{n}"})
            messages.append({"role": "assistant",
                             "tool_calls": [{"id": f"c{n}"}]})
            messages.append({"role": "tool", "tool_call_id": f"c{n}",
                             "content": "{}"})

        compacted = _compact(messages)
        pending = None
        for message in compacted:
            if message.get("tool_calls"):
                pending = {c["id"] for c in message["tool_calls"]}
            elif message.get("role") == "tool":
                self.assertIsNotNone(
                    pending, "a tool result arrived with no call before it")
                pending.discard(message["tool_call_id"])
            elif pending:
                self.assertEqual(set(), pending,
                                 "a tool call was left unanswered")
                pending = None

    def test_the_system_message_survives(self):
        messages = [{"role": "system", "content": "s"}]
        messages += [{"role": "user", "content": str(n)} for n in range(40)]
        self.assertEqual("system", _compact(messages)[0]["role"])

    def test_it_actually_shortens(self):
        messages = [{"role": "system", "content": "s"}]
        messages += [{"role": "user", "content": str(n)} for n in range(40)]
        self.assertLess(len(_compact(messages)), len(messages))

    def test_history_with_no_safe_cut_is_left_alone(self):
        """Better a long request than one the API will reject."""
        messages = [{"role": "system", "content": "s"},
                    {"role": "assistant", "tool_calls": [{"id": "c"}]}]
        messages += [{"role": "tool", "tool_call_id": "c", "content": "{}"}
                     for _ in range(30)]
        self.assertEqual(messages, _compact(messages))

    def test_what_counts_as_a_safe_boundary(self):
        self.assertTrue(_safe_boundary({"role": "user"}))
        self.assertTrue(_safe_boundary({"role": "assistant", "content": "x"}))
        self.assertFalse(_safe_boundary({"role": "assistant",
                                         "tool_calls": [{"id": "c"}]}))
        self.assertFalse(_safe_boundary({"role": "tool", "content": "{}"}))


if __name__ == "__main__":
    unittest.main()
