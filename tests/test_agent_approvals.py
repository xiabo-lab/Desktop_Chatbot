"""Asking a person, and the four ways that can go wrong.

`ApprovalDesk` is `ShutdownCountdown` with the polarity inverted, so these
tests are `tests/test_shutdown.py`'s read backwards. The token discipline is
copied exactly and tested exactly, because the failure it prevents is identical:
an answer about a prompt that has already gone must never be read as an answer
about the one that replaced it.

What flips is which direction is permissive. In the countdown, being loose about
a *cancel* was safe, because the safe outcome was a device that stayed on. Here
the dangerous direction is *approve*, so that is the strict one — and `deny` is
allowed to arrive late and still be honoured.

The clock is not injected: these use short real timeouts, because what is being
tested is a `threading.Event` wait and faking that would test nothing.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.agent.approvals import ApprovalDesk


class TestAsking(unittest.TestCase):

    def setUp(self):
        self.events: list[dict] = []
        self.desk = ApprovalDesk(self.events.append, timeout_s=0.4)

    def answer_soon(self, allow, delay=0.05, token=None):
        def later():
            time.sleep(delay)
            pending = self.desk.pending()
            self.desk.answer(token if token is not None
                             else (pending.token if pending else ""), allow)
        threading.Thread(target=later, daemon=True).start()

    def test_an_approval_lets_it_through(self):
        self.answer_soon(True)
        allowed, why = self.desk.ask("r-1", "set_config", "Change a thing")
        self.assertTrue(allowed)
        self.assertEqual("you approved it", why)

    def test_a_refusal_stops_it(self):
        self.answer_soon(False)
        allowed, why = self.desk.ask("r-1", "set_config", "Change a thing")
        self.assertFalse(allowed)
        self.assertEqual("you said no", why)

    def test_a_timeout_is_a_refusal_and_says_so(self):
        """The inversion, and the reason this class exists.

        The countdown proceeds when nobody answers because the safe outcome is
        a device that powers off as asked. Here the safe outcome is a device
        nobody changed, so silence means no.
        """
        started = time.monotonic()
        allowed, why = self.desk.ask("r-1", "set_config", "Change a thing")
        self.assertFalse(allowed)
        self.assertIn("nobody answered", why)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_the_card_reaches_the_phone_before_the_wait_begins(self):
        self.answer_soon(True)
        self.desk.ask("r-1", "set_config", "Change a thing", "a diff", "a warning")
        card = self.events[0]
        self.assertEqual("agent.approval", card["type"])
        self.assertEqual("Change a thing", card["what"])
        self.assertEqual("a diff", card["detail"])
        self.assertEqual("a warning", card["warning"])
        self.assertTrue(card["token"])

    def test_the_card_is_taken_down_however_it_ends(self):
        for allow, outcome in ((True, "approved"), (False, "denied")):
            with self.subTest(outcome=outcome):
                self.events.clear()
                self.answer_soon(allow)
                self.desk.ask("r-1", "set_config", "x")
                gone = [e for e in self.events
                        if e["type"] == "agent.approval.gone"]
                self.assertEqual([outcome], [e["outcome"] for e in gone])

    def test_a_timeout_also_takes_the_card_down(self):
        self.desk.ask("r-1", "set_config", "x")
        gone = [e for e in self.events if e["type"] == "agent.approval.gone"]
        self.assertEqual(["timeout"], [e["outcome"] for e in gone])


class TestTheToken(unittest.TestCase):
    """Copied from the countdown, and asymmetric on purpose."""

    def setUp(self):
        self.events: list[dict] = []
        self.desk = ApprovalDesk(self.events.append, timeout_s=0.4)

    def test_an_approval_for_a_stale_token_is_refused(self):
        """The direction that matters. A tap on a notification for a prompt
        that has gone must not approve the one now on screen."""
        def stale():
            time.sleep(0.05)
            outcome = self.desk.answer("r-1.99.deadbe", True)
            self.assertFalse(outcome["ok"])
            self.assertIn("different request", outcome["error"])
            # And the real one still works.
            pending = self.desk.pending()
            self.desk.answer(pending.token, True)

        threading.Thread(target=stale, daemon=True).start()
        allowed, _ = self.desk.ask("r-1", "set_config", "x")
        self.assertTrue(allowed)

    def test_a_denial_with_a_stale_token_is_honoured(self):
        """The safe direction is allowed to be late. A 'no' about anything is
        a reason not to proceed."""
        def stale():
            time.sleep(0.05)
            self.desk.answer("r-1.99.deadbe", False)

        threading.Thread(target=stale, daemon=True).start()
        allowed, why = self.desk.ask("r-1", "set_config", "x")
        self.assertFalse(allowed)
        self.assertEqual("you said no", why)

    def test_every_token_is_different(self):
        seen = set()
        for _ in range(4):
            self.answer_later()
            self.desk.ask("r-1", "set_config", "x")
            seen.add(self.events[-2]["token"])
        self.assertEqual(4, len(seen))

    def answer_later(self):
        def later():
            time.sleep(0.03)
            pending = self.desk.pending()
            if pending:
                self.desk.answer(pending.token, True)
        threading.Thread(target=later, daemon=True).start()

    def test_answering_when_nothing_is_pending_is_refused(self):
        outcome = self.desk.answer("anything", True)
        self.assertFalse(outcome["ok"])
        self.assertIn("nothing waiting", outcome["error"])


class TestOneAtATime(unittest.TestCase):

    def test_a_second_question_is_refused_rather_than_queued(self):
        events: list[dict] = []
        desk = ApprovalDesk(events.append, timeout_s=0.4)
        results: list = []

        def first():
            results.append(desk.ask("r-1", "set_config", "the first"))

        thread = threading.Thread(target=first, daemon=True)
        thread.start()
        time.sleep(0.05)
        allowed, why = desk.ask("r-1", "restart_service", "the second")
        self.assertFalse(allowed)
        self.assertIn("already waiting", why)
        thread.join(timeout=2.0)

    def test_cancelling_releases_a_waiting_run(self):
        """A run that ends must take its card with it, or the next thing
        somebody taps approves work that stopped happening."""
        events: list[dict] = []
        desk = ApprovalDesk(events.append, timeout_s=5.0)
        results: list = []

        def asking():
            results.append(desk.ask("r-1", "set_config", "x"))

        thread = threading.Thread(target=asking, daemon=True)
        thread.start()
        time.sleep(0.05)
        started = time.monotonic()
        desk.cancel("r-1")
        thread.join(timeout=2.0)

        self.assertLess(time.monotonic() - started, 1.0,
                        "cancel did not release the waiting thread")
        self.assertEqual([(False, "you said no")], results)
        self.assertIn("cancelled", [e.get("outcome") for e in events])

    def test_cancelling_a_different_run_leaves_it_alone(self):
        events: list[dict] = []
        desk = ApprovalDesk(events.append, timeout_s=0.3)

        def asking():
            desk.ask("r-1", "set_config", "x")

        thread = threading.Thread(target=asking, daemon=True)
        thread.start()
        time.sleep(0.05)
        desk.cancel("r-2")
        self.assertIsNotNone(desk.pending())
        thread.join(timeout=2.0)


class TestWhatTheAssistantWatches(unittest.TestCase):
    """`snapshot()` is how a push notification knows to fire."""

    def test_nothing_pending_is_none_not_an_empty_dict(self):
        desk = ApprovalDesk(lambda event: None)
        self.assertIsNone(desk.snapshot())

    def test_a_pending_question_is_visible_to_the_pusher(self):
        events: list[dict] = []
        desk = ApprovalDesk(events.append, timeout_s=0.4)

        def asking():
            desk.ask("r-1", "set_config", "Change the slideshow time",
                     "a diff", "a warning")

        thread = threading.Thread(target=asking, daemon=True)
        thread.start()
        time.sleep(0.05)
        snapshot = desk.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual("set_config", snapshot["op"])
        self.assertEqual("Change the slideshow time", snapshot["what"])
        self.assertGreater(snapshot["expires_at"], time.time())
        thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
