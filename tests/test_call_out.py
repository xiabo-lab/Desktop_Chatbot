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

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "aipi5" / "main.py"


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
        # The trailing None is what makes `device` genuinely optional: the
        # type says ["string", "null"] and JSON Schema applies the enum too,
        # so without it "leave it out" was not expressible. The point of this
        # test is that the phones come from the paired list.
        self.assertEqual(["iPhone", None],
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


class TestNothingRingsBeforeEverythingThatCanFail(unittest.TestCase):
    """`ice_servers()` used to be evaluated last, inside the constructor call
    that builds the successful outcome — after the session was up and the push
    was away.

    So if it raised, the consent desk caught the exception and reported "it was
    approved but did not work" while the phone was ringing in somebody's
    pocket. The worst version of a wrong report: the person is told nothing
    happened, by a device they can hear happening.
    """

    def test_a_failure_working_out_the_connection_rings_nothing(self):
        hub, push = FakeHub(), FakePush()

        def broken(name):
            raise RuntimeError("no TURN credentials")

        made = CallController(hub=hub, subscriptions=FakeSubscriptions(),
                              push=push, ice_servers=broken,
                              on_change=lambda: None)
        outcome = made.call_out("iPhone")
        self.assertFalse(outcome.started)
        self.assertEqual([], hub.calls)
        self.assertEqual([], push.rung)

    def test_it_says_what_went_wrong_rather_than_raising(self):
        made = CallController(
            hub=FakeHub(), subscriptions=FakeSubscriptions(), push=FakePush(),
            ice_servers=lambda name: (_ for _ in ()).throw(RuntimeError("nope")),
            on_change=lambda: None)
        self.assertIn("nope", made.call_out("iPhone").detail)

    def test_the_ordinary_path_still_carries_the_connection_details(self):
        made = controller()
        outcome = made.call_out("iPhone")
        self.assertTrue(outcome.started)
        self.assertEqual([{"urls": "stun:example"}], outcome.ice_servers)


class TestWhenTheNotificationDoesNotArrive(unittest.TestCase):
    """The ring stays up — an open app sees it by polling — but somebody
    watching a silent phone must not be told "Done."."""

    def test_the_outcome_records_that_it_did_not_go(self):
        made = controller(push=FakePush(sent=False, detail="subscription gone"))
        outcome = made.call_out("iPhone")
        self.assertTrue(outcome.started)
        self.assertIs(False, outcome.notified)

    def test_the_spoken_reply_distinguishes_the_two(self):
        """Asserted over `main.py`: the generic "Done." for a call whose
        notification failed is the assistant reporting a success the person can
        see is not one."""
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn("CONSENT_DONE_QUIETLY", main)
        self.assertIn('result.get("notified") is False', main)


class TestCallingOffMeansTheToolIsGone(unittest.TestCase):
    """`call.enabled: false` stops the server listening. It does not empty the
    push subscriptions on disk, so `phones()` still names whatever was paired
    when calling was on — and the toolbox was handed the controller on the
    rollout switch alone.

    A device with calling deliberately disabled would still have offered the
    model `call_phone`, and a stale subscription would have been rung.
    """

    def test_both_flags_are_required_in_the_wiring(self):
        main = MAIN.read_text(encoding="utf-8")
        wiring = main[main.index("self.toolbox.calls = "):]
        wiring = wiring[:wiring.index("\n\n")]
        self.assertIn("settings.call.enabled", wiring)
        self.assertIn('settings.assistant.tool("call")', wiring)

    def test_a_toolbox_without_the_controller_does_not_offer_it(self):
        box = ToolBox(consent=ConsentDesk())
        self.assertNotIn("call_phone", {t["name"] for t in box.schemas()})
        answer = json.loads(box.call("call_phone", "{}"))
        self.assertFalse(answer["ok"])
        self.assertNotIn("unexpectedly", answer["error"])


class TestThePhoneGoesThroughTheSameDoor(unittest.TestCase):
    """The fourth way in, which was not going through it.

    `aipi5/assistant/coordinator.py` says all four — the wake word, the Listen
    button, the compose box and the phone — reach the same function with the
    same tools and the same policy. Three of them did. The phone's compose box
    sent `agent.ask` unconditionally, so "set the volume to thirty" said into
    it spent one of the day's six maintenance runs and up to 24 model steps
    doing what the voice loop does in one call, while the same sentence typed
    on the panel three feet away took the short path.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path as _Path

        from aipi5.assistant import Coordinator, EventLog
        from aipi5.call.server import CallServer
        from aipi5.call.signaling import SignalingHub
        from aipi5.call.tokens import TrustedDevices
        from aipi5.core import config as config_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = _Path(self.tmp.name)
        devices = TrustedDevices(root / "devices.json")
        self.token = devices.pair("a phone")

        self.asked: list[tuple] = []
        self.agent = _RecordingProxy()
        self.coordinator = Coordinator(
            events=EventLog(), agent=self.agent,
            respond=lambda text, language: self.asked.append(
                (text, language)) or "The volume is thirty percent.")
        self.addCleanup(self.coordinator.close)

        cfg = config_mod.CallConfig(enabled=True, host="127.0.0.1", port=0,
                                    tls=False, devices=root / "devices.json")
        self.server = CallServer(cfg, hub=SignalingHub(), devices=devices,
                                 coordinator=self.coordinator)
        self.assertTrue(self.server.start(), self.server.error)
        self.addCleanup(self.server.stop)
        self.port = self.server._server.server_address[1]

    def ask(self, body, token=True):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        try:
            head = {"Content-Type": "application/json"}
            if token:
                head["Authorization"] = f"Bearer {self.token}"
            connection.request("POST", "/assistant/v1/ask",
                               json.dumps(body), head)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_a_sentence_takes_the_short_path(self):
        status, answer = self.ask({"text": "set the volume to thirty"})
        self.assertEqual(200, status)
        self.assertEqual("The volume is thirty percent.", answer["text"])
        self.assertEqual([("set the volume to thirty", "en")], self.asked)

    def test_it_does_not_spend_a_maintenance_run(self):
        """The regression, stated plainly. This is what `agent.ask` cost."""
        self.ask({"text": "set the volume to thirty"})
        self.assertEqual([], self.agent.said)

    def test_the_phone_is_still_a_paired_device_and_not_the_public(self):
        """Unlike the panel, this server is reachable from the whole tailnet.
        Every route here authenticates, and this one is no different."""
        status, _ = self.ask({"text": "set the volume to thirty"}, token=False)
        self.assertEqual(401, status)
        self.assertEqual([], self.asked)

    def test_a_build_with_no_coordinator_says_so(self):
        self.server.coordinator = None
        status, answer = self.ask({"text": "hello"})
        self.assertEqual(503, status)
        self.assertIn("coordinator", answer["error"])

    def test_the_page_sends_there_and_not_to_the_agent(self):
        page = (ROOT / "aipi5" / "call" / "web" / "phone.html").read_text(
            encoding="utf-8")
        compose = page[page.index('el("agent-compose").addEventListener'):]
        compose = compose[:compose.index("});")]
        self.assertIn("assistantAsk(text)", compose)
        self.assertNotIn("agent.ask", compose)


class _RecordingProxy:
    """`AgentProxy`'s three methods. Records anything sent to the agent."""

    def __init__(self):
        self.said: list[dict] = []

    def poll(self, since, timeout=None):
        return 200, {"events": [], "cursor": since,
                     "agent": {"run": "", "state": "idle"}}

    def say(self, message):
        self.said.append(message)
        return 200, {"ok": True, "run": "r-1"}

    def snapshot(self):
        return {"run": "", "state": "idle", "busy": False, "pending": None}
