"""The four routes the unified page will be built on.

The page still reads `/api/feed` and `/api/agent/poll`; these are the contract
it moves to, and they exist first so the move is a change to one file rather
than to two at once. What is asserted here is mostly the thing that will be
easy to get wrong when the page does move: a row must arrive once. Both old
routes carry the same rows at a different level, so a page reading a new one
and an old one together draws the whole conversation twice.
"""

from __future__ import annotations

import http.client
import json
import unittest
from types import SimpleNamespace

from aipi5.assistant import Coordinator, EventLog
from aipi5.ui.state import UiState
from aipi5.ui.server import WebUI


class FakeProxy:
    """`AgentProxy`'s three methods, over nothing."""

    def __init__(self):
        self.said: list[dict] = []

    def poll(self, since, timeout=None):
        return 200, {"events": [], "cursor": since,
                     "agent": {"run": "", "state": "idle", "busy": False,
                               "pending": None}}

    def say(self, message):
        self.said.append(message)
        if message.get("type") == "agent.ask":
            return 200, {"ok": True, "run": "r-1"}
        return 200, {"ok": True}

    def snapshot(self):
        return {"run": "", "state": "idle", "busy": False, "pending": None}


class RouteCase(unittest.TestCase):

    def setUp(self):
        cfg = SimpleNamespace(host="127.0.0.1", port=0, url="http://127.0.0.1:0")
        self.asked: list[tuple] = []
        self.proxy = FakeProxy()
        self.coordinator = Coordinator(
            events=EventLog(), agent=self.proxy,
            respond=lambda text, language: self.asked.append(
                (text, language)) or "the volume is thirty percent")
        self.addCleanup(self.coordinator.close)
        self.web = WebUI(cfg, state=UiState(), history=None, info=lambda: {},
                         coordinator=self.coordinator)
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.port = self.web._server.server_address[1]

    def call(self, method, path, body=None, timeout=5):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=timeout)
        try:
            if body is None:
                connection.request(method, path)
            else:
                connection.request(method, path, json.dumps(body),
                                   {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()


class TestTheFourRoutes(RouteCase):

    def test_asking_reaches_the_short_turn_and_not_the_agent(self):
        """The regression the whole contract exists for: this used to be
        `agent.ask` unconditionally, so typing "set the volume to thirty"
        started a maintenance run with a 24-step budget."""
        status, answer = self.call("POST", "/api/assistant/ask",
                                   {"text": "set the volume to thirty"})
        self.assertEqual(200, status)
        self.assertTrue(answer["ok"])
        self.assertEqual([("set the volume to thirty", "en")], self.asked)
        self.assertEqual([], self.proxy.said)

    def test_the_answer_is_in_the_transcript_once(self):
        self.call("POST", "/api/assistant/ask", {"text": "hello"})
        status, batch = self.call("GET", "/api/assistant/events?since=0&wait=0")
        self.assertEqual(200, status)
        self.assertEqual([("user", "hello"),
                          ("assistant", "the volume is thirty percent")],
                         [(r["kind"], r["text"]) for r in batch["events"]])

    def test_a_reader_that_has_seen_everything_is_handed_nothing_again(self):
        """A row arrives once. The failure this guards is a page that draws
        the whole conversation a second time every time it polls."""
        self.call("POST", "/api/assistant/ask", {"text": "hello"})
        _, first = self.call("GET", "/api/assistant/events?since=0&wait=0")
        _, second = self.call("GET",
                              f"/api/assistant/events?since={first['cursor']}&wait=0")
        self.assertEqual([], second["events"])
        self.assertEqual(first["cursor"], second["cursor"])

    def test_a_bad_cursor_is_read_as_the_beginning(self):
        self.call("POST", "/api/assistant/ask", {"text": "hello"})
        _, batch = self.call("GET", "/api/assistant/events?since=banana&wait=0")
        self.assertEqual(2, len(batch["events"]))

    def test_the_snapshot_says_what_the_page_needs_to_draw_itself(self):
        _, batch = self.call("GET", "/api/assistant/events?since=0&wait=0")
        state = batch["assistant"]
        self.assertIn("cursor", state)
        self.assertIn("pending", state)
        self.assertTrue(state["agent"])
        self.assertTrue(state["conversation"])

    def test_cancel_reaches_the_agent_as_a_stop(self):
        status, _ = self.call("POST", "/api/assistant/cancel", {"run": "r-1"})
        self.assertEqual(200, status)
        self.assertEqual({"type": "agent.stop", "run": "r-1"},
                         self.proxy.said[0])

    def test_an_approval_is_forwarded_as_a_boolean(self):
        self.call("POST", "/api/assistant/approval",
                  {"token": "t-1", "allow": "sure"})
        self.assertEqual({"type": "agent.answer", "token": "t-1", "allow": True},
                         self.proxy.said[0])

    def test_an_approval_with_no_token_never_reaches_the_agent(self):
        _, answer = self.call("POST", "/api/assistant/approval", {"allow": True})
        self.assertFalse(answer["ok"])
        self.assertEqual([], self.proxy.said)

    def test_wait_zero_answers_at_once_with_nothing_to_say(self):
        """A page opening wants the backlog and the snapshot together. On a
        device idle since breakfast the transcript is empty, and the default
        long poll would leave the panel blank for twenty seconds before it
        could draw so much as the run state."""
        import time as _time

        started = _time.monotonic()
        status, batch = self.call("GET", "/api/assistant/events?since=0&wait=0")
        self.assertEqual(200, status)
        self.assertEqual([], batch["events"])
        self.assertLess(_time.monotonic() - started, 3.0)

    def test_the_wait_is_bounded_by_the_servers_own_number(self):
        """A caller asking for an hour would hold a handler thread for an
        hour. Clamped rather than refused: the request is reasonable, the
        number is not."""
        started = __import__("time").monotonic()
        self.call("POST", "/api/assistant/ask", {"text": "hello"})
        self.call("GET", "/api/assistant/events?since=0&wait=99999")
        self.assertLess(__import__("time").monotonic() - started, 20.0)
        server = (__import__("pathlib").Path(__file__).resolve().parent.parent
                  / "aipi5" / "ui" / "server.py").read_text(encoding="utf-8")
        self.assertIn("min(wait, ASSISTANT_POLL_S)", server)

    def test_an_unknown_assistant_route_is_a_404_the_caller_can_read(self):
        """Refused *after* the body is drained.

        Answering and closing with a body still in the receive queue is an RST
        on Windows, so the caller sees a reset connection rather than the 404 —
        and a page posting to a route that has been renamed reports "the
        assistant is not answering", which sends whoever is debugging it to
        look at the wrong service entirely. Caught here as a flake: this test
        failed about one run in six before the drain.
        """
        for _ in range(5):
            status, payload = self.call("POST", "/api/assistant/anything",
                                        {"text": "x" * 2000})
            self.assertEqual(404, status)
            self.assertEqual("not found", payload["error"])


class TestWithNoCoordinator(unittest.TestCase):
    """A build without one still serves every other page rather than failing
    to start — the same rule the camera, the agent and the volume follow."""

    def setUp(self):
        cfg = SimpleNamespace(host="127.0.0.1", port=0, url="http://127.0.0.1:0")
        self.web = WebUI(cfg, state=UiState(), history=None, info=lambda: {})
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.port = self.web._server.server_address[1]

    def test_the_routes_say_so_rather_than_raising(self):
        for method, path in (("GET", "/api/assistant/events?since=0&wait=0"),
                             ("POST", "/api/assistant/ask")):
            with self.subTest(path=path):
                connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                        timeout=5)
                try:
                    if method == "GET":
                        connection.request(method, path)
                    else:
                        connection.request(method, path, "{}",
                                           {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    payload = json.loads(response.read() or b"{}")
                finally:
                    connection.close()
                self.assertEqual(503, response.status)
                self.assertIn("coordinator", payload["error"])


class TestTheOldRoutesAreStillThere(RouteCase):
    """Compatibility, until the phone moves. `aipi5/call/web/phone.html` is on
    `/api/agent/poll` and `/api/agent/say`, and removing them here would take
    the phone's console down for a page change on the panel."""

    def test_the_agent_routes_did_not_go_away(self):
        from aipi5.ui.server import AGENT_MESSAGES

        self.assertEqual({"agent.ask", "agent.stop", "agent.answer"},
                         AGENT_MESSAGES)
        server = (__import__("pathlib").Path(__file__).resolve().parent.parent
                  / "aipi5" / "ui" / "server.py").read_text(encoding="utf-8")
        for path in ("/api/agent/poll", "/api/agent/say", "/api/feed"):
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', server)

    def test_the_two_levels_are_documented_as_mutually_exclusive(self):
        """Both carry the same rows at a different level. The page that reads
        one must not also read the other, and the only place that can be said
        is beside the routes."""
        server = (__import__("pathlib").Path(__file__).resolve().parent.parent
                  / "aipi5" / "ui" / "server.py").read_text(encoding="utf-8")
        block = server[server.index("# ── the unified assistant contract"):]
        block = block[:block.index("def _assistant_events")]
        self.assertIn("draws every delegated row", block)
        self.assertIn("/api/feed", block)


if __name__ == "__main__":
    unittest.main()
