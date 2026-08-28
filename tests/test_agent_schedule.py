"""Reminders, and the ways one silently fails to arrive.

A reminder is a promise about a moment in the future, so almost everything that
can go wrong with it goes wrong *later*, somewhere nobody is watching. That
shapes these tests: they are about persistence, about times being read the way
the person meant, and about a failure to deliver being retried rather than
either forgotten or repeated forever.

The clock is injected — the pattern `aipi5/screensaver/schedule.py` established
for the same reason. Moving the Pi's system clock to test a time-of-day
behaviour disturbs TLS, Tailscale and the call certificates; passing `now`
disturbs nothing and checks a reminder eight days out in a millisecond.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from aipi5.agent.schedule import (CANCELLED, FAILED, MAX_ATTEMPTS, PENDING,
                                  SENT, Schedule, parse_when)


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "reminders.jsonl"
        self.clock = Clock()
        self.schedule = Schedule(self.path, clock=self.clock)


class TestSettingOne(Base):

    def test_a_reminder_is_due_only_when_its_moment_arrives(self):
        self.schedule.add(self.clock.now + 3600, "call John")
        self.assertEqual([], self.schedule.due())
        self.clock.now += 3599
        self.assertEqual([], self.schedule.due())
        self.clock.now += 2
        self.assertEqual(["call John"], [r.text for r in self.schedule.due()])

    def test_a_time_in_the_past_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.schedule.add(self.clock.now - 3600, "too late")
        self.assertIn("already passed", str(caught.exception))

    def test_a_minute_of_slack_lets_remind_me_in_a_moment_work(self):
        self.schedule.add(self.clock.now - 10, "in a moment")
        self.assertEqual(1, len(self.schedule.due()))

    def test_a_year_away_is_refused_because_it_is_usually_a_typo(self):
        with self.assertRaises(ValueError):
            self.schedule.add(self.clock.now + 400 * 86400, "far future")

    def test_empty_text_is_refused(self):
        for bad in ("", "   ", None):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    self.schedule.add(self.clock.now + 60, bad)

    def test_an_unknown_delivery_is_refused(self):
        with self.assertRaises(ValueError):
            self.schedule.add(self.clock.now + 60, "x", deliver="carrier pigeon")

    def test_there_is_a_ceiling_on_how_many_can_wait(self):
        from aipi5.agent.schedule import MAX_PENDING
        for n in range(MAX_PENDING):
            self.schedule.add(self.clock.now + 60 + n, f"one {n}")
        with self.assertRaises(ValueError) as caught:
            self.schedule.add(self.clock.now + 99999, "one too many")
        self.assertIn("limit", str(caught.exception))


class TestSurvivingARestart(Base):
    """The property the whole feature rests on."""

    def test_a_reminder_outlives_the_process_that_set_it(self):
        self.schedule.add(self.clock.now + 3600, "bring the laptop")
        # A different Schedule over the same file is what a reboot looks like.
        later = Schedule(self.path, clock=Clock(self.clock.now + 7200))
        self.assertEqual(["bring the laptop"], [r.text for r in later.due()])

    def test_one_corrupt_line_does_not_lose_the_others(self):
        """This file is meant to be fixable by hand, and a hand can mistype."""
        self.schedule.add(self.clock.now + 60, "first")
        self.schedule.add(self.clock.now + 120, "second")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("{this is not json\n")
        recovered = Schedule(self.path, clock=self.clock)
        self.assertEqual({"first", "second"},
                         {r.text for r in recovered.pending()})

    def test_a_missing_file_is_an_empty_schedule_not_a_crash(self):
        fresh = Schedule(self.tmp / "nothing-here.jsonl", clock=self.clock)
        self.assertEqual([], fresh.pending())

    def test_the_file_is_replaced_atomically(self):
        self.schedule.add(self.clock.now + 60, "one")
        leftovers = [p.name for p in self.tmp.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_what_is_written_is_readable_json_lines(self):
        self.schedule.add(self.clock.now + 60, "one")
        for line in self.path.read_text(encoding="utf-8").splitlines():
            with self.subTest(line=line[:40]):
                self.assertIn("text", json.loads(line))


class TestDelivery(Base):

    def setUp(self):
        super().setUp()
        self.item = self.schedule.add(self.clock.now + 10, "call John")
        self.clock.now += 20

    def test_a_delivered_reminder_stops_being_due(self):
        self.assertEqual(1, len(self.schedule.due()))
        self.schedule.delivered(self.item.id, True, "sent")
        self.assertEqual([], self.schedule.due())
        self.assertEqual(SENT, self.schedule.listing()[0]["state"])

    def test_a_failed_delivery_is_tried_again_but_not_immediately(self):
        """A phone that is off must not burn every attempt in five seconds."""
        self.schedule.delivered(self.item.id, False, "no subscription")
        self.assertEqual([], self.schedule.due(), "it retried at once")
        self.clock.now += 61
        self.assertEqual(1, len(self.schedule.due()))

    def test_it_gives_up_rather_than_retrying_for_ever(self):
        """A phone that has been factory reset is not a reason to send the
        same notification until the device is unplugged."""
        for _ in range(MAX_ATTEMPTS):
            self.schedule.delivered(self.item.id, False, "gone")
            self.clock.now += 61
        self.assertEqual([], self.schedule.due())
        self.assertEqual(FAILED, self.schedule.listing()[0]["state"])

    def test_reporting_delivery_of_something_unknown_is_refused(self):
        self.assertFalse(self.schedule.delivered("m-nonsuch", True))

    def test_a_second_report_for_the_same_one_is_refused(self):
        """Two assistants, or one that retried its own POST."""
        self.assertTrue(self.schedule.delivered(self.item.id, True, ""))
        self.assertFalse(self.schedule.delivered(self.item.id, True, ""))


class TestCancelling(Base):

    def test_a_cancelled_reminder_never_fires(self):
        item = self.schedule.add(self.clock.now + 10, "call John")
        self.schedule.cancel(item.id)
        self.clock.now += 20
        self.assertEqual([], self.schedule.due())
        self.assertEqual(CANCELLED, self.schedule.listing()[0]["state"])

    def test_cancelling_something_already_sent_does_nothing(self):
        item = self.schedule.add(self.clock.now + 10, "x")
        self.clock.now += 20
        self.schedule.delivered(item.id, True, "")
        self.assertIsNone(self.schedule.cancel(item.id))

    def test_cancelling_an_unknown_id_does_nothing(self):
        self.assertIsNone(self.schedule.cancel("m-nonsuch"))


class TestReadingTimes(unittest.TestCase):
    """`parse_when` is a validator, not a natural-language parser.

    The model is given the current time and asked for an absolute one, because
    "next Tuesday" means a different day depending on when it is read — and a
    parser that guesses produces a reminder on the wrong day for a reason
    nobody can reconstruct afterwards.
    """

    def test_an_iso_timestamp_is_read_in_the_devices_own_timezone(self):
        moment = parse_when("2026-08-28 09:00")
        self.assertEqual((2026, 8, 28, 9, 0),
                         time.localtime(moment)[:5])

    def test_the_t_separator_works_too(self):
        self.assertEqual(parse_when("2026-08-28 09:00"),
                         parse_when("2026-08-28T09:00"))

    def test_an_explicit_offset_is_honoured(self):
        self.assertEqual(parse_when("2026-08-28T09:00+00:00"),
                         parse_when("2026-08-28T09:00Z"))

    def test_words_are_refused_rather_than_guessed(self):
        for bad in ("tomorrow", "next tuesday", "in an hour", "9am",
                    "", "   ", None, 7):
            with self.subTest(when=bad):
                with self.assertRaises(ValueError):
                    parse_when(bad)

    def test_the_refusal_says_what_would_work(self):
        with self.assertRaises(ValueError) as caught:
            parse_when("tomorrow")
        self.assertIn("2026-08-28 09:00", str(caught.exception))


class TestListing(Base):

    def test_waiting_ones_come_first_and_soonest_first(self):
        self.schedule.add(self.clock.now + 300, "later")
        self.schedule.add(self.clock.now + 100, "sooner")
        listed = self.schedule.listing()
        self.assertEqual(["sooner", "later"], [r["text"] for r in listed])

    def test_a_reminder_describes_itself_in_words_not_epochs(self):
        item = self.schedule.add(self.clock.now + 3600, "call John")
        described = item.describe(self.clock.now)
        self.assertRegex(described["when"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(3600, described["in_seconds"])
        self.assertEqual(PENDING, described["state"])

    def test_pruning_keeps_everything_still_waiting(self):
        for n in range(5):
            item = self.schedule.add(self.clock.now + 10 + n, f"done {n}")
            self.clock.now += 20
            self.schedule.delivered(item.id, True, "")
        waiting = self.schedule.add(self.clock.now + 9999, "still waiting")
        self.schedule.prune(keep=2)
        self.assertIn(waiting.id, [r.id for r in self.schedule.pending()])
        self.assertLessEqual(
            len([r for r in self.schedule.listing(50)
                 if r["state"] == SENT]), 2)


if __name__ == "__main__":
    unittest.main()
