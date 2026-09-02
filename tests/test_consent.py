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



class TestOneChannelDoesNotAnswerAnother(unittest.TestCase):
    """A question belongs to the turn that asked it.

    The spoken loop reads the desk at the end of every turn and, if something
    is waiting, holds the floor for a yes or a no. It used to read it globally.
    So: somebody types "call my phone" on the panel, the tool parks the
    question, and within the minute somebody in the room says "what's the
    weather". The forecast is read out, the floor is held in silence for an
    answer to a question the room never heard, and anything affirmative in the
    next sentence rings the phone.

    Nothing about that is visible from either channel on its own, which is why
    both channels' tests passed.
    """

    def setUp(self):
        self.desk = ConsentDesk()
        self.rang: list = []
        self.pending = self.desk.ask(
            "ring Fuwen iPhone", "Shall I ring it?",
            action=lambda: self.rang.append(1) or {"ok": True},
            turn="t-typed-one", source="text")

    def test_a_later_spoken_turn_does_not_pick_it_up(self):
        self.assertIsNone(self.desk.waiting_for("t-spoken-two"))

    def test_a_turn_that_parked_nothing_hears_nothing(self):
        """The fast path. It never reaches the model, so it parks nothing and
        carries no turn id -- and must not inherit somebody else's."""
        self.assertIsNone(self.desk.waiting_for(""))

    def test_the_turn_that_asked_still_hears_it(self):
        self.assertIs(self.pending, self.desk.waiting_for("t-typed-one"))

    def test_the_card_still_sees_it_whoever_asked(self):
        """`waiting()` is the right question for drawing a card: the panel
        shows what is waiting anywhere, and the token decides who may answer.
        Only holding a microphone open needs the narrower one."""
        self.assertIsNotNone(self.desk.waiting())
        self.assertEqual("Shall I ring it?", self.desk.snapshot()["question"])

    def test_the_token_answers_it_from_any_channel(self):
        """Binding the *listening* to a turn must not bind the answering: the
        approval card on the panel and the phone both answer by token, and a
        question asked out loud can still be settled with a finger."""
        self.desk.answer(self.pending.token, True)
        self.assertEqual([1], self.rang)

    def test_the_source_is_recorded(self):
        self.assertEqual("text", self.pending.source)

    def test_an_expired_question_is_not_this_turns_either(self):
        clock = Clock()
        desk = ConsentDesk(clock=lambda: clock.now)
        desk.ask("x", "Shall I?", action=lambda: {"ok": True}, turn="t-1")
        clock.now += 3600
        self.assertIsNone(desk.waiting_for("t-1"))


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



class TestWhatTheTranscriptSaysHappened(unittest.TestCase):
    """A row once it has happened, and not a moment before.

    `delete_birthday` returns ok when the *question* was asked, so the row the
    toolbox publishes says it asked. What actually happened is known here,
    after somebody answered, and is published from here -- found on the device,
    where the page read "removed a birthday" while the assistant was still
    saying "shall I?" and the entry was still in the calendar.
    """

    def setUp(self):
        self.done: list[tuple] = []
        self.desk = ConsentDesk(on_done=lambda *row: self.done.append(row))

    def ask(self, action=lambda: {"ok": True}):
        return self.desk.ask("delete the birthday for Ann",
                             "Shall I remove it?", action=action,
                             said="removed a birthday")

    def test_a_yes_is_reported_after_the_action_ran(self):
        ran: list = []
        pending = self.ask(action=lambda: ran.append(1) or {"ok": True})
        self.desk.answer(pending.token, True)
        self.assertEqual([1], ran)
        self.assertEqual([("removed a birthday", True, True)], self.done)

    def test_a_no_is_reported_as_not_allowed(self):
        pending = self.ask()
        self.desk.answer(pending.token, False)
        self.assertEqual([("removed a birthday", False, True)], self.done)

    def test_an_unclear_answer_is_reported_as_not_allowed(self):
        """None means the matcher could not tell, which is a no."""
        pending = self.ask()
        self.desk.answer(pending.token, None)
        self.assertEqual(False, self.done[0][1])

    def test_an_action_that_threw_is_reported_as_not_ok(self):
        def boom():
            raise RuntimeError("the calendar is read-only")

        pending = self.ask(action=boom)
        self.desk.answer(pending.token, True)
        self.assertEqual([("removed a birthday", True, False)], self.done)

    def test_a_question_nobody_answered_reports_nothing(self):
        """`on_change(None)` fires for a timeout too. Nothing happened, so
        there is nothing to say happened."""
        clock = Clock()
        desk = ConsentDesk(clock=lambda: clock.now,
                           on_done=lambda *row: self.done.append(row))
        desk.ask("x", "Shall I?", action=lambda: {"ok": True},
                 said="removed a birthday")
        clock.now += 3600
        self.assertIsNone(desk.waiting())
        self.assertEqual([], self.done)

    def test_a_listener_that_throws_does_not_break_the_answer(self):
        def boom(*row):
            raise RuntimeError("the page is gone")

        desk = ConsentDesk(on_done=boom)
        pending = desk.ask("x", "Shall I?", action=lambda: {"ok": True},
                           said="removed a birthday")
        self.assertTrue(desk.answer(pending.token, True)["allowed"])



class TestTheTurnIdReachesTheDesk(unittest.TestCase):
    """End to end: the coordinator mints it, `begin_turn` carries it, the
    handler stamps it, and the spoken loop reads with it."""

    def test_a_parked_question_carries_the_turn_that_parked_it(self):
        from aipi5.assistant import Coordinator, EventLog
        from aipi5.llm.tools import ToolBox

        desk = ConsentDesk()
        box = ToolBox(consent=desk, calls=_OnePhone())

        def respond(text, language):
            box.call("call_phone", "{}")
            return "Shall I ring it?"

        coordinator = Coordinator(events=EventLog(), respond=respond,
                                  consent=desk, on_turn=box.begin_turn,
                                  after_turn=box.end_turn)
        answer = coordinator.submit_voice("call my phone", "en")
        self.assertTrue(answer["turn"].startswith("t-"))
        self.assertIsNotNone(desk.waiting_for(answer["turn"]))
        self.assertIsNone(desk.waiting_for("t-somebody-else"))

    def test_two_turns_get_two_ids(self):
        from aipi5.assistant import Coordinator, EventLog

        coordinator = Coordinator(events=EventLog(),
                                  respond=lambda text, language: "ok")
        first = coordinator.submit_text("hello")["turn"]
        second = coordinator.submit_text("hello again")["turn"]
        self.assertNotEqual(first, second)

    def test_the_spoken_loop_asks_for_its_own_turn(self):
        """Asserted over the source: `waiting()` there is the bug, and it is
        one character away from being back."""
        loop = MAIN.read_text(encoding="utf-8")
        self.assertIn("consent.waiting_for(spoken_turn)", loop)
        self.assertNotIn("awaiting = assistant.consent.waiting()", loop)


class _OnePhone:
    def phones(self):
        return ["Fuwen iPhone"]

    def available(self):
        return True

    def call_out(self, device=""):
        raise AssertionError("nothing may ring before somebody says yes")


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
