"""Conversation trimming and expiry.

Two failure modes, both silent. History that grows without limit costs money
and latency on every turn and nobody notices until a bill arrives; history
trimmed at the wrong boundary produces a 400 from the API whose message is
about mismatched tool call ids and says nothing about trimming.

The second is the one these tests are really for. An assistant message
carrying `tool_calls` must be followed by a `tool` result for every one of
them, so a cut that lands between them is rejected — and it only happens once
the conversation is long enough to trim, which is to say in the middle of a
long evening rather than in any manual test.
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
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "{}"},
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
        ids = {c["id"] for m in kept if m.get("tool_calls")
               for c in m["tool_calls"]}
        results = {m["tool_call_id"] for m in kept if m["role"] == "tool"}
        self.assertEqual(ids, results, "every call kept must keep its result")

    def test_orphans_before_the_first_question_are_dropped(self):
        # Can only be the tail of a turn whose question has already gone.
        messages = [{"role": "assistant", "content": "orphan"}] + turn("a", "1")
        self.assertEqual(_trim(messages, 8), turn("a", "1"))

    def test_zero_turns_keeps_nothing(self):
        self.assertEqual(_trim(turn("a", "1"), 0), [])

    def test_no_user_messages_at_all(self):
        self.assertEqual(_trim([{"role": "assistant", "content": "x"}], 4), [])


class TestConversation(unittest.TestCase):

    def test_the_system_prompt_is_not_stored(self):
        # It is rebuilt every request because it carries the current time and
        # the language of the utterance, both of which change turn to turn.
        conversation = Conversation()
        conversation.user("hello")
        self.assertEqual(len(conversation), 1)
        self.assertEqual(conversation.messages("SYSTEM")[0],
                         {"role": "system", "content": "SYSTEM"})

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
        contents = [m["content"] for m in conversation.messages("S")[1:]]
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
