"""Asking before something that rings, sends, or cannot be taken back.

One rule holds this whole file together and it is worth stating before the
tests: **the model never decides that somebody said yes.** It asks the
question; a phrase matcher over the raw transcript, or a finger on a button,
decides the answer. A model that reports "they said yes" is reporting, and this
desk does not read its reports — which is what stops a phone ringing in
somebody's pocket because a half-heard sentence sounded like a request.

The second rule is that everything which is not a clear yes is a no. Silence, a
timeout, "hang on", an expired token, a token already answered. AIA arrived at
that for spoken confirmations and the reasoning is the same here: for something
that cannot be taken back, "I could not tell" has to mean no.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from aipi5.assistant.consent import ConsentDesk

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "aipi5" / "main.py"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestAskingAndAnswering(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.ran: list[str] = []
        self.desk = ConsentDesk(clock=self.clock, ttl_s=60.0)

    def park(self, what="ring the phone"):
        return self.desk.ask(
            what=what, question=f"Shall I {what}?",
            action=lambda: self.ran.append(what) or {"ok": True})

    def test_nothing_happens_until_somebody_says_yes(self):
        pending = self.park()
        self.assertEqual([], self.ran)
        self.desk.answer(pending.token, True)
        self.assertEqual(["ring the phone"], self.ran)

    def test_a_no_runs_nothing_and_is_not_an_error(self):
        pending = self.park()
        answer = self.desk.answer(pending.token, False)
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["allowed"])
        self.assertEqual([], self.ran)

    def test_anything_that_is_not_exactly_true_is_a_no(self):
        """The voice loop's matcher answers True, False or None, and None means
        "I could not tell". A truthy check would make a mumble a yes."""
        for answer in (None, "yes", 1, "sure", object()):
            with self.subTest(answer=answer):
                self.ran.clear()
                pending = self.park()
                self.desk.answer(pending.token, answer)
                self.assertEqual([], self.ran)

    def test_a_second_answer_cannot_run_it_twice(self):
        """The token comes off the desk before the action runs, so a second tap
        during the round trip finds nothing — the difference between one phone
        ringing and two."""
        pending = self.park()
        self.desk.answer(pending.token, True)
        again = self.desk.answer(pending.token, True)
        self.assertFalse(again["ok"])
        self.assertEqual(["ring the phone"], self.ran)

    def test_a_token_that_was_never_issued_is_refused(self):
        self.park()
        self.assertFalse(self.desk.answer("c-invented", True)["ok"])
        self.assertEqual([], self.ran)

    def test_an_expired_question_is_gone_rather_than_answerable(self):
        pending = self.park()
        self.clock.now += 61.0
        answer = self.desk.answer(pending.token, True)
        self.assertFalse(answer["ok"])
        self.assertEqual([], self.ran)

    def test_expiry_is_noticed_without_anybody_asking_about_it(self):
        """`waiting()` is read by the voice loop on every pass, so a question
        nobody answered disappears on its own rather than at the moment
        somebody happens to say something unrelated."""
        self.park()
        self.assertIsNotNone(self.desk.waiting())
        self.clock.now += 61.0
        self.assertIsNone(self.desk.waiting())

    def test_a_second_question_is_refused_rather_than_stacked(self):
        """One room, one voice, one card. Two questions in flight is a yes
        landing on whichever the person was not answering."""
        self.park("ring the phone")
        self.assertIsNone(self.park("delete Mum's birthday"))
        self.assertEqual("ring the phone", self.desk.waiting().what)

    def test_a_question_can_be_asked_once_the_last_one_expired(self):
        self.park("ring the phone")
        self.clock.now += 61.0
        self.assertIsNotNone(self.park("delete Mum's birthday"))

    def test_an_action_that_throws_is_reported_and_not_raised(self):
        """It runs on the voice loop's thread. An exception here is a turn that
        ends in a traceback instead of a sentence."""
        pending = self.desk.ask("explode", "Shall I?",
                                action=lambda: 1 / 0)
        answer = self.desk.answer(pending.token, True)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["allowed"])
        self.assertIn("approved but did not work", answer["error"])

    def test_withdrawing_is_also_a_no(self):
        pending = self.park()
        self.desk.cancel("the person walked away")
        self.assertIsNone(self.desk.waiting())
        self.assertFalse(self.desk.answer(pending.token, True)["ok"])
        self.assertEqual([], self.ran)


class TestWhatThePageIsTold(unittest.TestCase):

    def test_the_snapshot_carries_the_question_and_the_token(self):
        desk = ConsentDesk()
        desk.ask("ring the phone", "Shall I ring your phone?",
                 action=lambda: {"ok": True}, detail="iPhone")
        snapshot = desk.snapshot()
        self.assertEqual("Shall I ring your phone?", snapshot["question"])
        self.assertTrue(snapshot["token"].startswith("c-"))
        self.assertEqual("iPhone", snapshot["detail"])

    def test_there_is_no_snapshot_when_nothing_is_waiting(self):
        self.assertIsNone(ConsentDesk().snapshot())

    def test_a_listener_is_told_when_one_appears_and_when_it_goes(self):
        seen: list = []
        desk = ConsentDesk(on_change=seen.append)
        pending = desk.ask("x", "Shall I?", action=lambda: {"ok": True})
        desk.answer(pending.token, True)
        self.assertEqual([pending, None], seen)

    def test_a_listener_that_throws_does_not_lose_the_question(self):
        def boom(_):
            raise RuntimeError("the page is gone")

        desk = ConsentDesk(on_change=boom)
        pending = desk.ask("x", "Shall I?", action=lambda: {"ok": True})
        self.assertIsNotNone(pending)
        self.assertIsNotNone(desk.waiting())


class TestTheVoiceLoopDecidesItAndNotTheModel(unittest.TestCase):
    """Asserted over the source, because what it guards is an absence.

    There is no failing call to catch here. The failure is somebody adding
    `if outcome.get("confirmed")` to `hear_consent` one afternoon — the model
    saying it heard a yes — and every test still passing, on a device where the
    consequence is a phone that rings without being asked.
    """

    @classmethod
    def setUpClass(cls):
        text = MAIN.read_text(encoding="utf-8")
        cls.body = re.search(r"def hear_consent\(.*?\n\ndef ", text, re.S).group(0)
        cls.code = re.sub(r'"""[\s\S]*?"""', "", cls.body)
        cls.code = re.sub(r"^\s*#.*$", "", cls.code, flags=re.M)

    def test_the_answer_comes_from_the_phrase_matcher(self):
        self.assertIn("is_affirmative(heard.text)", self.code)

    def test_the_model_is_not_asked_anything_here(self):
        for forbidden in ("coordinator", "submit_voice", "answer(text",
                          "llm", "toolbox", "respond("):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, self.code)

    def test_only_an_unambiguous_yes_runs_the_action(self):
        # `is True`, not truthy: the matcher's third answer is None.
        self.assertIn("decision is True", self.code)
        self.assertIn("decision is not True", self.code)

    def test_the_question_is_spoken_before_the_floor_is_held(self):
        """Asking and then returning to idle makes the reply a separate request
        that needs the wake word again, so "确定" arrives as "小艾同学，确定"
        and the confirmation is silently dropped. AIA learned this first."""
        text = MAIN.read_text(encoding="utf-8")
        loop = text[text.index("machine.to(State.SPEAKING)"):]
        loop = loop[:loop.index("except Exception:")]
        self.assertIn("blocking=awaiting is not None", loop)
        speak_at = loop.index("speaker.say(reply")
        hear_at = loop.index("hear_consent(assistant")
        self.assertLess(speak_at, hear_at)

    def test_the_wake_detector_is_not_asked_to_hear_the_answer(self):
        # The reply is an answer to a question the device just asked. Requiring
        # the wake word for it is the failure above.
        self.assertNotIn("detector_wake.detect", self.code)


if __name__ == "__main__":
    unittest.main()
