"""The conversational turn, on `/v1/responses`.

The move was not a preference. `gpt-5.6-luna` carries a default reasoning
effort and answers a Chat Completions request that offers it tools with

    400 — Function tools with reasoning_effort are not supported for
    gpt-5.6-luna in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'.

so the assistant started cleanly, reported healthy, and apologised to the first
question that needed a tool. `tests/test_client_negotiation.py` guards the
workaround that kept the old endpoint usable, and the agent loop still relies
on it; this file guards the endpoint the conversation actually runs on now.

Everything here drives fake response objects. The one thing they are careful
about is being *list-shaped*: `response.output` is a list whose contents vary
with the model and the request — a reasoning item, then a search the model ran
itself, then two function calls, then the message — and reading `output[0]`
works right up until the day something is emitted in front of the answer, at
which point the assistant goes quiet with nothing in the log to say why.
"""

from __future__ import annotations

import json
import unittest

from aipi5.core.config import OpenAIConfig
from aipi5.llm.client import OpenAIClient, _output_text, _read_output
from aipi5.llm.conversation import Conversation


# ── fakes ────────────────────────────────────────────────────────────


class Item(dict):
    """An output item. A dict, because that is what a `model_dump`, a replay
    from a log and every fake here all are — and `_item` reads both."""


def message(text: str) -> Item:
    return Item(type="message", role="assistant",
                content=[{"type": "output_text", "text": text}])


def call(name: str, arguments: dict, call_id: str) -> Item:
    return Item(type="function_call", call_id=call_id, name=name,
                arguments=json.dumps(arguments))


def reasoning() -> Item:
    return Item(type="reasoning", summary=[])


def web_search() -> Item:
    return Item(type="web_search_call", status="completed")


class FakeResponse:
    """What the SDK hands back. Deliberately without `output_text`.

    The real object has that convenience and the reader uses it where it
    exists — but a fake that grows one stops exercising the walk, and the walk
    is the part that has to be right when an item arrives in front of the
    answer.
    """

    def __init__(self, *output):
        self.output = list(output)
        self.usage = None


class FakeResponses:
    """`client.responses`, scripted. Records every request it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent: list[dict] = []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        if not self.answers:
            return FakeResponse(message("(nothing left to say)"))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeClient:
    def __init__(self, responses):
        self.responses = responses


class Box:
    """A toolbox that records what was called and answers a fixed string."""

    def __init__(self, tools=None, answer='{"ok": true}'):
        self.tools = tools if tools is not None else [{
            "type": "function", "name": "get_weather", "description": "d",
            "parameters": {"type": "object", "properties": {}, "required": [],
                           "additionalProperties": False},
            "strict": True,
        }]
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def schemas(self):
        return list(self.tools)

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.answer


def client(*answers, retries: int = 1) -> tuple[OpenAIClient, FakeResponses]:
    made = OpenAIClient(OpenAIConfig(max_retries=retries), api_key="")
    responses = FakeResponses(*answers)
    made._client = FakeClient(responses)
    made._error = None
    return made, responses


# ── reading what came back ───────────────────────────────────────────


class TestEveryOutputItemIsRead(unittest.TestCase):

    def test_text_is_found_behind_a_reasoning_item(self):
        """`output[0]` is a reasoning item here. Reading it as the answer is
        an assistant that goes silent, with nothing in the log about why."""
        self.assertEqual("It is 68 degrees.",
                         _output_text(FakeResponse(reasoning(),
                                                   message("It is 68 degrees."))))

    def test_text_split_across_items_is_joined_in_order(self):
        self.assertEqual("one two",
                         _output_text(FakeResponse(message("one "),
                                                   message("two"))))

    def test_the_sdks_own_shortcut_is_used_where_it_exists(self):
        class WithShortcut(FakeResponse):
            output_text = "from the sdk"

        self.assertEqual("from the sdk", _output_text(WithShortcut(message("x"))))

    def test_an_answer_with_no_items_at_all_is_empty_and_not_an_error(self):
        self.assertEqual("", _output_text(FakeResponse()))

    def test_calls_come_back_in_the_shape_the_api_takes_them_back_in(self):
        _, calls, _ = _read_output(FakeResponse(
            reasoning(), call("get_weather", {"when": "now"}, "call_1"),
            message("checking")))
        self.assertEqual([{"type": "function_call", "call_id": "call_1",
                           "name": "get_weather",
                           "arguments": '{"when": "now"}'}], calls)

    def test_several_calls_in_one_answer_are_all_returned(self):
        _, calls, _ = _read_output(FakeResponse(
            call("get_weather", {}, "a"), call("get_current_time", {}, "b")))
        self.assertEqual(["a", "b"], [c["call_id"] for c in calls])

    def test_a_search_the_api_ran_itself_is_recorded_and_not_answered(self):
        """There is no result to send back — the API ran it. It is named so
        the turn's log line says a search happened."""
        _, calls, builtins = _read_output(FakeResponse(web_search(),
                                                       message("Bitcoin is …")))
        self.assertEqual([], calls)
        self.assertEqual(["web_search"], builtins)


# ── the turn ─────────────────────────────────────────────────────────


class TestTheTurn(unittest.TestCase):

    def test_an_ordinary_answer_needs_one_request(self):
        made, api = client(FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        reply = made.respond(conversation, "SYSTEM", Box())
        self.assertTrue(reply)
        self.assertEqual("Hello.", reply.text)
        self.assertEqual(1, len(api.sent))

    def test_the_system_prompt_goes_as_instructions_and_not_as_history(self):
        made, api = client(FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        made.respond(conversation, "SYSTEM", Box())
        self.assertEqual("SYSTEM", api.sent[0]["instructions"])
        self.assertEqual([{"role": "user", "content": "hello"}],
                         api.sent[0]["input"])

    def test_tools_are_offered_with_tool_choice_auto(self):
        made, api = client(FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        made.respond(conversation, "S", Box())
        self.assertEqual("auto", api.sent[0]["tool_choice"])
        self.assertEqual("get_weather", api.sent[0]["tools"][0]["name"])

    def test_nothing_is_stored_on_the_api_side(self):
        """The conversation is held here, bounded and self-expiring. A stored
        response is a transcript of a living room sitting on somebody else's
        disk with no expiry this code controls."""
        made, api = client(FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        made.respond(conversation, "S", Box())
        self.assertIs(False, api.sent[0]["store"])

    def test_a_tool_call_is_run_and_its_result_is_sent_back_paired(self):
        made, api = client(
            FakeResponse(call("get_weather", {"when": "now"}, "call_1")),
            FakeResponse(message("It is 68 degrees.")))
        box = Box()
        conversation = Conversation()
        conversation.user("weather?")
        reply = made.respond(conversation, "S", box)

        self.assertEqual("It is 68 degrees.", reply.text)
        self.assertEqual([("get_weather", '{"when": "now"}')], box.calls)
        self.assertEqual(["get_weather"], reply.tool_calls)
        # The second request carries the call and its answer, in order, with
        # the same id. A mismatch here is a 400 whose message says nothing
        # about where the ids came from.
        second = api.sent[1]["input"]
        self.assertEqual("function_call", second[1]["type"])
        self.assertEqual("function_call_output", second[2]["type"])
        self.assertEqual(second[1]["call_id"], second[2]["call_id"])
        self.assertEqual('{"ok": true}', second[2]["output"])

    def test_two_calls_in_one_round_are_both_answered(self):
        made, api = client(
            FakeResponse(call("get_weather", {}, "a"),
                         call("get_current_time", {}, "b")),
            FakeResponse(message("68 degrees, and it is four o'clock.")))
        box = Box()
        conversation = Conversation()
        conversation.user("weather and time?")
        made.respond(conversation, "S", box)
        second = api.sent[1]["input"]
        outputs = [i["call_id"] for i in second
                   if i.get("type") == "function_call_output"]
        self.assertEqual(["a", "b"], outputs)

    def test_a_failing_tool_still_ends_the_turn_in_a_sentence(self):
        """A tool that returns an error leaves the model able to say "I can't
        check the news right now", which is what the person needs to hear."""
        made, _ = client(
            FakeResponse(call("get_weather", {}, "call_1")),
            FakeResponse(message("I could not reach the weather service.")))
        box = Box(answer='{"ok": false, "error": "the service did not answer"}')
        conversation = Conversation()
        conversation.user("weather?")
        reply = made.respond(conversation, "S", box)
        self.assertTrue(reply)
        self.assertIn("could not reach", reply.text)

    def test_a_search_the_api_ran_is_named_in_the_turns_tool_list(self):
        made, _ = client(FakeResponse(web_search(),
                                      message("About $61,000.")))
        conversation = Conversation()
        conversation.user("bitcoin price?")
        reply = made.respond(conversation, "S", Box())
        self.assertEqual("About $61,000.", reply.text)
        self.assertEqual(["web_search"], reply.tool_calls)


class TestTheLoopIsBounded(unittest.TestCase):

    def test_the_last_round_is_asked_with_no_tools(self):
        """Without it a tool-happy model spends the whole budget calling
        things and never says anything, and the turn ends in silence."""
        made, api = client(
            FakeResponse(call("get_weather", {}, "a")),
            FakeResponse(call("get_weather", {}, "b")),
            FakeResponse(call("get_weather", {}, "c")),
            FakeResponse(message("It is 68 degrees.")))
        conversation = Conversation()
        conversation.user("weather?")
        reply = made.respond(conversation, "S", Box())
        self.assertEqual(4, len(api.sent))
        self.assertIn("tools", api.sent[2])
        self.assertNotIn("tools", api.sent[3])
        self.assertEqual("It is 68 degrees.", reply.text)

    def test_a_model_that_says_nothing_at_all_is_reported_and_not_spoken(self):
        made, _ = client(FakeResponse(reasoning()))
        conversation = Conversation()
        conversation.user("hello")
        reply = made.respond(conversation, "S", Box())
        self.assertFalse(reply)
        self.assertEqual("the model returned nothing", reply.error)

    def test_a_tool_call_with_no_toolbox_does_not_hang_the_turn(self):
        made, _ = client(FakeResponse(call("get_weather", {}, "a")))
        conversation = Conversation()
        conversation.user("weather?")
        reply = made.respond(conversation, "S", None)
        self.assertFalse(reply)
        self.assertIn("does not exist here", reply.error)


class TestFailures(unittest.TestCase):

    class Boom(Exception):
        status_code = 500

    class Down(Exception):
        pass

    def test_an_api_error_becomes_a_sentence_rather_than_a_traceback(self):
        made, _ = client(self.Boom("upstream exploded"), retries=0)
        conversation = Conversation()
        conversation.user("hello")
        reply = made.respond(conversation, "S", Box())
        self.assertFalse(reply)
        self.assertEqual("the OpenAI API returned a server error", reply.error)

    def test_a_dropped_link_is_retried_once_and_then_reported(self):
        made, api = client(TimeoutError("timed out"),
                           FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        reply = made.respond(conversation, "S", Box())
        self.assertTrue(reply)
        self.assertEqual(2, len(api.sent))

    def test_a_four_hundred_is_not_retried(self):
        """It fails identically the second time, and the retry costs the
        person in the room another few seconds of silence."""
        class Bad(Exception):
            status_code = 400

        made, api = client(Bad("malformed"), FakeResponse(message("Hello.")))
        conversation = Conversation()
        conversation.user("hello")
        made.respond(conversation, "S", Box())
        self.assertEqual(1, len(api.sent))

    def test_no_client_at_all_answers_rather_than_raising(self):
        made = OpenAIClient(OpenAIConfig(), api_key="")
        reply = made.respond(Conversation(), "S", Box())
        self.assertFalse(reply)
        self.assertIn("no OPENAI_API_KEY", reply.error)


class TestTheStartupProbe(unittest.TestCase):
    """The failure it exists to catch is a model that takes a plain request,
    reports healthy at boot, and refuses every request that offers it a tool.
    A probe without tools passed that day too."""

    def test_it_sends_the_same_request_shape_a_real_question_does(self):
        made, api = client(FakeResponse(message("ready")))
        ok, detail = made.probe()
        self.assertTrue(ok)
        self.assertIn("answered in", detail)
        self.assertEqual("auto", api.sent[0]["tool_choice"])
        self.assertEqual("startup_check", api.sent[0]["tools"][0]["name"])
        self.assertIs(True, api.sent[0]["tools"][0]["strict"])

    def test_a_model_that_refuses_tools_fails_the_probe(self):
        class Refuses(Exception):
            status_code = 400

        made, _ = client(Refuses("Function tools are not supported"), retries=0)
        ok, detail = made.probe()
        self.assertFalse(ok)
        self.assertIn("Function tools", detail)

    def test_an_unknown_model_names_the_setting_to_change(self):
        class Missing(Exception):
            status_code = 404

        made, _ = client(Missing("model_not_found"), retries=0)
        ok, detail = made.probe()
        self.assertFalse(ok)
        self.assertIn("openai.model in config/aipi5.yaml", detail)


class TestVision(unittest.TestCase):

    def test_the_picture_goes_as_an_input_image_and_the_prompt_beside_it(self):
        made, api = client(FakeResponse(message("A kitchen.")))
        reply = made.describe_image("data:image/jpeg;base64,AAAA", "INSTRUCTION",
                                    "what room is this?")
        self.assertEqual("A kitchen.", reply.text)
        content = api.sent[0]["input"][0]["content"]
        self.assertEqual("input_text", content[0]["type"])
        self.assertEqual("what room is this?", content[0]["text"])
        self.assertEqual("input_image", content[1]["type"])
        self.assertEqual("data:image/jpeg;base64,AAAA", content[1]["image_url"])
        self.assertEqual("INSTRUCTION", api.sent[0]["instructions"])

    def test_the_picture_is_not_added_to_the_conversation(self):
        """Carrying a base64 JPEG forward into every later request multiplies
        the cost of the rest of the conversation for no benefit. The
        *description* is what the caller puts in the history, as ordinary
        text."""
        made, _ = client(FakeResponse(message("A kitchen.")))
        conversation = Conversation()
        made.describe_image("data:image/jpeg;base64,AAAA", "I")
        self.assertEqual([], conversation.items())

    def test_no_question_asks_the_open_one(self):
        made, api = client(FakeResponse(message("A kitchen.")))
        made.describe_image("data:image/jpeg;base64,AAAA", "I", "   ")
        self.assertEqual("What do you see?",
                         api.sent[0]["input"][0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
