"""Conversation trimming and expiry.

Two failure modes, both silent. History that grows without limit costs money
and latency on every turn and nobody notices until a bill arrives; history
trimmed at the wrong boundary produces a 400 from the API whose message is
about mismatched tool call ids and says nothing about trimming.

The second is the one these tests are really for. Every `function_call` must be
followed by a `function_call_output` carrying the same `call_id`, and an output
whose call has gone is refused just as firmly — so a cut that lands between
them is rejected, and it only happens once the conversation is long enough to
trim, which is to say in the middle of a long evening rather than in any manual
test.

The history is `/v1/responses` input items now. The part that matters for
trimming got *harder*: a tool call used to be a field on an assistant message,
so a cut could only land between two messages, and now it is an item of its own
with its answer in the item after it. What keeps `_trim` correct through that
is that it cuts on user turns and nothing else — a roleless item belongs to
whichever turn it follows and travels with it.
"""

from __future__ import annotations

import unittest

from aipi5.llm.conversation import Conversation, _trim


def turn(question: str, answer: str) -> list[dict]:
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": answer}]


def tool_turn(question: str, call_id: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"type": "function_call", "call_id": call_id, "name": "get_weather",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": "{}"},
        {"role": "assistant", "content": answer},
    ]


class TestTrim(unittest.TestCase):

    def test_keeps_everything_under_the_limit(self):
        messages = turn("a", "1") + turn("b", "2")
        self.assertEqual(_trim(messages, 8), messages)

    def test_drops_the_oldest_turns(self):
        messages = turn("a", "1") + turn("b", "2") + turn("c", "3")
        kept = _trim(messages, 2)
        self.assertEqual([m["content"] for m in kept], ["b", "2", "c", "3"])

    def test_a_tool_call_and_its_result_are_never_separated(self):
        # The failure this whole function exists for. Cutting at message
        # boundaries would leave an assistant message with `tool_calls` whose
        # results have gone, and the API rejects that outright.
        messages = tool_turn("weather?", "call_1", "It's 68.") \
            + tool_turn("and tomorrow?", "call_2", "Rain.")
        kept = _trim(messages, 1)
        self.assertEqual(kept[0]["content"], "and tomorrow?")
        ids = {m["call_id"] for m in kept if m.get("type") == "function_call"}
        results = {m["call_id"] for m in kept
                   if m.get("type") == "function_call_output"}
        self.assertEqual(ids, results, "every call kept must keep its result")
        self.assertEqual({"call_2"}, ids)

    def test_orphans_before_the_first_question_are_dropped(self):
        # Can only be the tail of a turn whose question has already gone.
        messages = [{"role": "assistant", "content": "orphan"}] + turn("a", "1")
        self.assertEqual(_trim(messages, 8), turn("a", "1"))

    def test_a_result_whose_call_has_gone_is_dropped_with_it(self):
        """The one the API refuses outright rather than ignoring.

        A roleless item has no `role` to match on, so the only thing keeping it
        with its call is that `_trim` cuts on user turns — which is easy to
        read past, and the failure is a 400 in the middle of a long evening.
        """
        orphan = [{"type": "function_call_output", "call_id": "gone",
                   "output": "{}"}]
        self.assertEqual(turn("a", "1"), _trim(orphan + turn("a", "1"), 8))

    def test_zero_turns_keeps_nothing(self):
        self.assertEqual(_trim(turn("a", "1"), 0), [])

    def test_no_user_messages_at_all(self):
        self.assertEqual(_trim([{"role": "assistant", "content": "x"}], 4), [])


class TestConversation(unittest.TestCase):

    def test_the_system_prompt_is_not_in_the_history(self):
        """It goes as `instructions`, which is what keeps it out of the thing
        `_trim` cuts. A system prompt in the history is a system prompt that
        can be trimmed away, and the assistant then answers in the wrong
        language with nothing anywhere to say why."""
        conversation = Conversation()
        conversation.user("hello")
        self.assertEqual(len(conversation), 1)
        self.assertEqual([{"role": "user", "content": "hello"}],
                         conversation.items())

    def test_a_tool_call_is_stored_as_the_api_sent_it(self):
        """Rebuilt items get regenerated ids, and the API's answer to a
        mismatch says nothing about where the ids came from."""
        conversation = Conversation()
        conversation.user("weather?")
        call = {"type": "function_call", "call_id": "call_x",
                "name": "get_weather", "arguments": "{}"}
        conversation.assistant_tool_calls([call])
        conversation.tool_result("call_x", '{"ok": true}')
        self.assertEqual(
            [{"role": "user", "content": "weather?"},
             call,
             {"type": "function_call_output", "call_id": "call_x",
              "output": '{"ok": true}'}],
            conversation.items())

    def test_several_calls_in_one_round_are_all_stored_before_any_answer(self):
        """The API rejects a history in which a call is left unanswered as
        firmly as one in which an answer has no call, so they go in together."""
        conversation = Conversation()
        conversation.user("weather and time?")
        conversation.assistant_tool_calls([
            {"type": "function_call", "call_id": "a", "name": "get_weather",
             "arguments": "{}"},
            {"type": "function_call", "call_id": "b", "name": "get_current_time",
             "arguments": "{}"},
        ])
        conversation.tool_result("a", "{}")
        conversation.tool_result("b", "{}")
        items = conversation.items()
        self.assertEqual(["a", "b"], [i["call_id"] for i in items
                                      if i.get("type") == "function_call"])
        self.assertEqual(["a", "b"], [i["call_id"] for i in items
                                      if i.get("type") == "function_call_output"])

    def test_silence_forgets_the_thread(self):
        conversation = Conversation(idle_seconds=600)
        conversation.begin_turn(now=1000.0)
        conversation.user("what's the weather")
        conversation.assistant("It's 68.")

        forgot = conversation.begin_turn(now=1000.0 + 601)
        self.assertTrue(forgot)
        self.assertEqual(len(conversation), 0)

    def test_a_follow_up_within_the_window_keeps_it(self):
        conversation = Conversation(idle_seconds=600)
        conversation.begin_turn(now=1000.0)
        conversation.user("what's the weather")
        conversation.assistant("It's 68.")

        self.assertFalse(conversation.begin_turn(now=1000.0 + 30))
        self.assertEqual(len(conversation), 2)

    def test_trim_is_applied_after_the_turn(self):
        conversation = Conversation(max_turns=2)
        for n in range(5):
            conversation.begin_turn(now=1000.0 + n)
            conversation.user(f"q{n}")
            conversation.assistant(f"a{n}")
            conversation.trim()
        contents = [m["content"] for m in conversation.items()]
        self.assertEqual(contents, ["q3", "a3", "q4", "a4"])

    def test_describe_counts_turns_not_messages(self):
        conversation = Conversation()
        conversation.user("a")
        conversation.assistant("b")
        described = conversation.describe()
        self.assertEqual(described["turns"], 1)
        self.assertEqual(described["messages"], 2)


if __name__ == "__main__":
    unittest.main()
