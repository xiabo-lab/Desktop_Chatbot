"""The two request parameters that have to be negotiated against the model.

Both were found on the device rather than reasoned about, and both fail in a
way that is invisible until the exact request shape that triggers them is sent.

**`reasoning_effort` with tools.** `gpt-5.6-luna` accepts a plain completion
with no `reasoning_effort` and rejects the *same* request the moment a `tools`
array is added:

    400 — Function tools with reasoning_effort are not supported for
    gpt-5.6-luna in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'.

So the startup probe passed, the assistant reported healthy, and the first
question that needed a tool came back "Sorry, I couldn't work that one out."
That is the failure these tests exist to keep out.

**The token parameter.** `max_completion_tokens` against `max_tokens`, each
rejected by the models that want the other.

Neither is guessed from the model name — a rename would break that — so both
are tried and remembered. These tests drive a fake client that reproduces the
real API's complaints.
"""

from __future__ import annotations

import unittest

from aipi5.core.config import OpenAIConfig
from aipi5.llm.client import OpenAIClient, _is_reasoning_effort_error, _is_token_param_error


class FakeBadRequest(Exception):
    status_code = 400


class FakeCompletions:
    """Stands in for `client.chat.completions`, rejecting what the API rejects.

    `accepts` names the request shape this fake model tolerates, so one class
    covers "wants reasoning_effort=none", "has no such parameter", and the two
    token spellings.
    """

    def __init__(self, *, token_param="max_completion_tokens",
                 tool_effort="none", has_effort_param=True):
        self.token_param = token_param
        self.tool_effort = tool_effort
        self.has_effort_param = has_effort_param
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        if self.token_param not in kwargs:
            raise FakeBadRequest(
                f"Unsupported parameter: use '{self.token_param}' instead")

        if kwargs.get("tools"):
            effort = kwargs.get("reasoning_effort")
            if not self.has_effort_param and effort is not None:
                raise FakeBadRequest(
                    "Unrecognized request argument supplied: reasoning_effort")
            if self.has_effort_param and effort != self.tool_effort:
                raise FakeBadRequest(
                    "Function tools with reasoning_effort are not supported for "
                    "this model in /v1/chat/completions. To use function tools, "
                    "use /v1/responses or set reasoning_effort to 'none'.")

        return _Response("ok")


class _Response:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {
            "content": text, "tool_calls": None})()})()]


def client_with(completions) -> OpenAIClient:
    client = OpenAIClient(OpenAIConfig(max_retries=0), api_key="sk-test")
    # The real constructor built a real `OpenAI`; swap in the fake below it.
    client._client = type("F", (), {"chat": type("C", (), {
        "completions": completions})()})()
    return client


class TestErrorRecognisers(unittest.TestCase):
    """Both matchers must be narrow enough not to swallow real 400s."""

    def test_recognises_the_real_luna_message(self):
        real = ("Error code: 400 - {'error': {'message': \"Function tools with "
                "reasoning_effort are not supported for gpt-5.6-luna in "
                "/v1/chat/completions. To use function tools, use /v1/responses "
                "or set reasoning_effort to 'none'.\"}}")
        self.assertTrue(_is_reasoning_effort_error(Exception(real)))

    def test_recognises_an_unknown_parameter(self):
        self.assertTrue(_is_reasoning_effort_error(
            Exception("Unrecognized request argument supplied: reasoning_effort")))

    def test_ignores_unrelated_four_hundreds(self):
        for message in ("Invalid schema for function 'get_weather'",
                        "Incorrect API key provided",
                        "This model does not exist"):
            with self.subTest(message=message):
                self.assertFalse(_is_reasoning_effort_error(Exception(message)))
                self.assertFalse(_is_token_param_error(Exception(message)))

    def test_the_two_matchers_do_not_overlap(self):
        # Each must claim only its own failure, or the negotiation retries the
        # wrong parameter and reports the wrong cause.
        effort = Exception("Function tools with reasoning_effort are not supported")
        tokens = Exception("Unsupported parameter: use 'max_completion_tokens' instead")
        self.assertTrue(_is_reasoning_effort_error(effort))
        self.assertFalse(_is_token_param_error(effort))
        self.assertTrue(_is_token_param_error(tokens))
        self.assertFalse(_is_reasoning_effort_error(tokens))


class TestNegotiation(unittest.TestCase):

    def test_a_plain_completion_sends_no_reasoning_effort(self):
        # The shape that made this bug invisible: the startup probe has no
        # tools, so it passed while every tool call was failing.
        fake = FakeCompletions()
        client = client_with(fake)
        client._request(messages=[], tools=None, max_tokens=16)
        self.assertNotIn("reasoning_effort", fake.calls[0])

    def test_tools_get_reasoning_effort_none(self):
        fake = FakeCompletions(tool_effort="none")
        client = client_with(fake)
        client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        self.assertEqual(fake.calls[0]["reasoning_effort"], "none")

    def test_a_model_without_the_parameter_falls_back_to_omitting_it(self):
        fake = FakeCompletions(has_effort_param=False)
        client = client_with(fake)
        client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        self.assertEqual(len(fake.calls), 2, "one rejected attempt, then success")
        self.assertNotIn("reasoning_effort", fake.calls[1])

    def test_the_answer_is_remembered(self):
        # Negotiated once per process, not once per request. Without this every
        # tool call pays an extra round trip, which on the slow path is the
        # whole of what a person waits for.
        fake = FakeCompletions(has_effort_param=False)
        client = client_with(fake)
        client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        self.assertEqual(len(fake.calls), 3, "2 for the first call, 1 for the second")

    def test_the_token_parameter_is_negotiated_too(self):
        fake = FakeCompletions(token_param="max_tokens")
        client = client_with(fake)
        client._request(messages=[], tools=None, max_tokens=16)
        self.assertIn("max_tokens", fake.calls[-1])
        self.assertNotIn("max_completion_tokens", fake.calls[-1])

    def test_both_can_be_wrong_at_once(self):
        # An older model that wants max_tokens and has no reasoning_effort.
        # The variants are a product, so this resolves without special-casing.
        fake = FakeCompletions(token_param="max_tokens", has_effort_param=False)
        client = client_with(fake)
        client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        final = fake.calls[-1]
        self.assertIn("max_tokens", final)
        self.assertNotIn("reasoning_effort", final)

    def test_an_unrelated_error_is_raised_not_retried(self):
        class Always:
            calls: list = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                raise FakeBadRequest("Invalid schema for function 'get_weather'")

        fake = Always()
        client = client_with(fake)
        with self.assertRaises(FakeBadRequest):
            client._request(messages=[], tools=[{"x": 1}], max_tokens=16)
        self.assertEqual(len(fake.calls), 1, "a real error must not be retried")


if __name__ == "__main__":
    unittest.main()
