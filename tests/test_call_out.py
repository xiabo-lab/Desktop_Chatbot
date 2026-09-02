"""Ringing a phone: one implementation, two callers, and a question first.

The sequence — start the signalling, tell the page, send the push, report which
of those worked — used to live inside the HTTP handler, which was fine while
the button on the panel was the only way to ask. "Call my phone" said out loud
would have been a second copy, and the two would have drifted at the first
change. `CallController` is that sequence and nothing else: no HTTP, no request
parsing, no policy about who may ask.

The policy lives with each caller instead. The route's is that it is bound to
loopback. The tool's is that a phone buzzing in somebody's pocket because a
half-heard sentence sounded like a request cannot be undone by saying the
opposite, so it asks first — and the answer is decided by a phrase matcher or a
button, never by the model.
"""

from __future__ import annotations

import json
import unittest

from aipi5.assistant.consent import ConsentDesk
from aipi5.call.controller import CallController
from aipi5.llm.tools import ToolBox


def parse(result: str) -> dict:
    return json.loads(result)


class FakeHub:
    def __init__(self, started=True, why=""):
        self.started = started
        self.why = why
        self.calls: list[str] = []

    def call_out(self, device):
        self.calls.append(device)
        if not self.started:
            return False, "", self.why
        return True, "sess-1", ""


class FakeSubscriptions:
    def __init__(self, names=("iPhone",)):
        self._names = list(names)

    def names(self):
        return list(self._names)


class FakePush:
    def __init__(self, sent=True, detail=""):
        self.sent = sent
        self.detail = detail
        self.rung: list[tuple] = []

    def ring(self, device, payload):
        self.rung.append((device, payload))
        return self.sent, self.detail


def controller(**overrides):
    changed = []
    made = CallController(
        hub=overrides.get("hub") or FakeHub(),
        subscriptions=overrides.get("subscriptions") or FakeSubscriptions(),
        push=overrides.get("push") or FakePush(),
        ice_servers=lambda name: [{"urls": "stun:example"}],
        on_change=lambda: changed.append(True))
    made.changed = changed
    return made


class TestTheController(unittest.TestCase):

    def test_the_happy_path_reports_all_four_things_separately(self):
        made = controller()
        outcome = made.call_out()
        self.assertTrue(outcome.started)
        self.assertTrue(outcome.notified)
        self.assertFalse(outcome.answered)
        self.assertEqual("iPhone", outcome.device)
        self.assertEqual("sess-1", outcome.session)

    def test_answered_is_never_claimed_by_the_thing_that_asked(self):
        """A call is answered by somebody picking up a phone, seconds later.
        Reporting it here would be reporting it on the strength of having
        asked."""
        self.assertFalse(controller().call_out().as_dict()["answered"])

    def test_the_page_is_told_before_the_notification_goes(self):
        """A push that arrived before the session existed would open a phone
        onto a call that is not there, and the phone's answer would be refused
        by a hub that had never heard of it."""
        push = FakePush()
        made = controller(push=push)
        made.call_out()
        self.assertEqual([True], made.changed)
        self.assertEqual(1, len(push.rung))

    def test_a_notification_that_failed_still_leaves_the_call_up(self):
        """The phone may have the app open and see it by polling — but "it rang
        and nothing happened" has no explanation anywhere unless somebody is
        told the notification did not go."""
        made = controller(push=FakePush(sent=False, detail="410 Gone"))
        outcome = made.call_out()
        self.assertTrue(outcome.started)
        self.assertFalse(outcome.notified)
        self.assertEqual("410 Gone", outcome.detail)

    def test_a_call_already_in_progress_is_a_busy_signal(self):
        made = controller(hub=FakeHub(started=False,
                                      why="a call is already in progress"))
        outcome = made.call_out()
        self.assertFalse(outcome)
        self.assertIn("already in progress", outcome.detail)

    def test_no_paired_phone_is_a_sentence_and_rings_nothing(self):
        push = FakePush()
        made = controller(subscriptions=FakeSubscriptions([]), push=push)
        outcome = made.call_out()
        self.assertFalse(outcome)
        self.assertEqual([], push.rung)

    def test_two_phones_and_no_name_asks_rather_than_picking_one(self):
        """Ringing the wrong person's phone is worse than one more question."""
        hub = FakeHub()
        made = controller(subscriptions=FakeSubscriptions(["iPhone", "Pixel"]),
                          hub=hub)
        outcome = made.call_out()
        self.assertFalse(outcome)
        self.assertIn("more than one", outcome.detail)
        self.assertEqual([], hub.calls)

    def test_a_name_that_is_not_paired_is_refused_and_not_looked_up(self):
        hub = FakeHub()
        made = controller(hub=hub)
        outcome = made.call_out("+1 555 0100")
        self.assertFalse(outcome)
        self.assertIn("not a paired phone", outcome.detail)
        self.assertEqual([], hub.calls)

    def test_subscriptions_that_cannot_be_read_are_no_phones_and_not_a_crash(self):
        class Broken:
            def names(self):
                raise OSError("the file is gone")

        made = controller(subscriptions=Broken())
        self.assertEqual([], made.phones())
        self.assertFalse(made.call_out())


class TestTheToolAsksFirst(unittest.TestCase):

    def setUp(self):
        self.hub = FakeHub()
        self.push = FakePush()
        self.controller = controller(hub=self.hub, push=self.push)
        self.desk = ConsentDesk()
        self.box = ToolBox(calls=self.controller, consent=self.desk)

    def call(self, **args):
        return parse(self.box.call("call_phone", json.dumps(args)))

    def test_the_call_itself_rings_nothing(self):
        answer = self.call()
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["asked"])
        self.assertFalse(answer["ringing"])
        self.assertEqual([], self.hub.calls)
        self.assertEqual([], self.push.rung)

    def test_a_yes_is_what_rings_it(self):
        self.call()
        outcome = self.desk.answer(self.desk.waiting().token, True)
        self.assertTrue(outcome["allowed"])
        self.assertEqual(["iPhone"], self.hub.calls)
        self.assertEqual(1, len(self.push.rung))

    def test_a_no_rings_nothing(self):
        self.call()
        self.desk.answer(self.desk.waiting().token, False)
        self.assertEqual([], self.hub.calls)

    def test_silence_rings_nothing(self):
        """The matcher's third answer is None — "I could not tell" — and for
        something that cannot be undone that has to be a no."""
        self.call()
        self.desk.answer(self.desk.waiting().token, None)
        self.assertEqual([], self.hub.calls)

    def test_the_question_names_the_phone(self):
        self.call()
        self.assertEqual("Shall I ring iPhone?", self.desk.waiting().question)

    def test_the_model_is_told_not_to_claim_it_is_ringing(self):
        self.assertIn("must not say it is", self.call()["instruction"])

    def test_a_phone_number_is_not_something_this_tool_takes(self):
        answer = self.call(device="+1 555 0100")
        self.assertFalse(answer["ok"])
        self.assertIn("not a paired phone", answer["error"])
        self.assertIsNone(self.desk.waiting())

    def test_the_enum_is_built_from_the_paired_list(self):
        """A model cannot name a phone that does not exist, and could not do
        anything with one if it did — the controller checks again."""
        schema = next(t for t in self.box.schemas() if t["name"] == "call_phone")
        self.assertEqual(["iPhone"],
                         schema["parameters"]["properties"]["device"]["enum"])

    def test_two_phones_and_no_name_asks_which(self):
        box = ToolBox(consent=self.desk,
                      calls=controller(subscriptions=FakeSubscriptions(
                          ["iPhone", "Pixel"]), hub=self.hub))
        answer = parse(box.call("call_phone", "{}"))
        self.assertFalse(answer["ok"])
        self.assertIn("Ask which one", answer["error"])
        self.assertIsNone(self.desk.waiting())

    def test_the_tool_is_not_offered_when_there_is_nothing_to_ring(self):
        """Better than a tool the model discovers is useless at runtime, in
        front of somebody who has just asked for it."""
        box = ToolBox(consent=self.desk,
                      calls=controller(subscriptions=FakeSubscriptions([])))
        self.assertEqual([], [t for t in box.schemas()
                              if t.get("name") == "call_phone"])

    def test_with_no_desk_at_all_nothing_rings(self):
        box = ToolBox(calls=self.controller)
        answer = parse(box.call("call_phone", "{}"))
        self.assertFalse(answer["ok"])
        self.assertEqual([], self.hub.calls)

    def test_what_comes_back_after_a_yes_says_what_actually_happened(self):
        box = ToolBox(consent=self.desk,
                      calls=controller(hub=self.hub,
                                       push=FakePush(sent=False,
                                                     detail="410 Gone")))
        box.call("call_phone", "{}")
        outcome = self.desk.answer(self.desk.waiting().token, True)
        result = outcome["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["notified"])
        self.assertFalse(result["answered"])


class TestTheRouteUsesTheSameCode(unittest.TestCase):
    """Asserted over the source. The failure this guards is somebody restoring
    the sequence inline for a quick fix, leaving two implementations that agree
    only until the next change."""

    def test_the_handler_forwards_rather_than_ringing(self):
        import re
        from pathlib import Path

        server = (Path(__file__).resolve().parent.parent / "aipi5" / "ui"
                  / "server.py").read_text(encoding="utf-8")
        branch = server[server.index('if path == "/api/call/out":'):]
        branch = branch[:branch.index('elif path == "/api/call/answer":')]
        self.assertIn("controller.call_out(", branch)
        for forbidden in ("push.ring(", "hub.call_out(", "subscriptions.names("):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, branch)
        self.assertIsNotNone(re.search(r"controller", branch))

    def test_the_call_server_holds_one(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "aipi5" / "call"
                  / "server.py").read_text(encoding="utf-8")
        self.assertIn("self.controller = call_controller.CallController(", source)


if __name__ == "__main__":
    unittest.main()
