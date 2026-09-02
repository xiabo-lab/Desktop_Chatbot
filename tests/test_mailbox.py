"""The mailbox, in the parts a working call does not prove.

`Mailbox` came out of `SignalingHub` so a second caller — the phone's agent
console — could have one without inheriting a call state machine. Lifting code
that already worked is exactly the change that looks free and is not, so the
properties the hub was relying on are pinned here rather than left to
`tests/test_call.py`, which exercises them only incidentally and only for a
call.

Four things, each of which can be wrong while a call still connects:

**The cursor moves on an empty batch.** A poll that times out must hand back a
cursor its caller can ask with again. Getting this wrong gives a peer that
re-reads the same tail forever, which looks like a message arriving repeatedly.

**A blocked reader wakes on a post.** The whole point of a held GET. If it
misses the notification it waits out its full twenty-five seconds and the call
setup that should take milliseconds takes half a minute.

**Overflow drops the oldest.** Unbounded growth is a phone that vanished
mid-handshake holding memory forever.

**A reset does not rewind the sequence.** A peer that has not noticed the reset
must ask for something that never arrives, rather than being handed the next
session's messages as the tail of its own.

No network, no threads beyond the two this file starts itself.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.call.mailbox import MAX_MAILBOX, POLL_TIMEOUT_S, Mailbox

SEATS = ("pi", "phone")


class TestDelivery(unittest.TestCase):
    """Ordering, addressing, and the cursor."""

    def setUp(self):
        self.box = Mailbox(SEATS, poll_timeout_s=0.05)

    def test_a_message_reaches_only_the_seat_it_was_addressed_to(self):
        self.box.post("pi", {"type": "offer"})
        messages, _ = self.box.collect("pi", 0, timeout=0)
        self.assertEqual([{"type": "offer"}], messages)
        self.assertEqual(([], 0), self.box.collect("phone", 0, timeout=0))

    def test_messages_come_back_in_the_order_they_were_posted(self):
        for n in range(5):
            self.box.post("pi", {"n": n})
        messages, _ = self.box.collect("pi", 0, timeout=0)
        self.assertEqual([0, 1, 2, 3, 4], [m["n"] for m in messages])

    def test_the_cursor_consumes(self):
        self.box.post("pi", {"n": 1})
        _, cursor = self.box.collect("pi", 0, timeout=0)
        self.box.post("pi", {"n": 2})
        messages, _ = self.box.collect("pi", cursor, timeout=0)
        self.assertEqual([{"n": 2}], messages)

    def test_an_empty_batch_returns_the_cursor_it_was_given(self):
        """Unchanged, because nothing was consumed — but never a rewind."""
        self.box.post("pi", {"n": 1})
        _, cursor = self.box.collect("pi", 0, timeout=0)
        messages, again = self.box.collect("pi", cursor, timeout=0)
        self.assertEqual([], messages)
        self.assertEqual(cursor, again)

    def test_the_sequence_is_shared_across_seats(self):
        """So a cursor means something on its own, whoever handed it over."""
        self.box.post("pi", {"n": 1})
        self.box.post("phone", {"n": 2})
        _, pi_cursor = self.box.collect("pi", 0, timeout=0)
        _, phone_cursor = self.box.collect("phone", 0, timeout=0)
        self.assertNotEqual(pi_cursor, phone_cursor)

    def test_an_unknown_seat_is_refused_rather_than_created(self):
        self.assertFalse(self.box.post("stranger", {"type": "offer"}))
        self.assertEqual(([], 7), self.box.collect("stranger", 7, timeout=0))


class TestWaiting(unittest.TestCase):
    """The held GET."""

    def test_a_blocked_reader_wakes_when_a_message_is_posted(self):
        box = Mailbox(SEATS)
        got: list = []

        def read():
            got.append(box.collect("pi", 0, timeout=5.0))

        reader = threading.Thread(target=read, daemon=True)
        started = time.monotonic()
        reader.start()
        # Long enough that the reader is certainly parked on the condition.
        time.sleep(0.05)
        box.post("pi", {"type": "bye"})
        reader.join(timeout=3.0)
        self.assertFalse(reader.is_alive(), "the reader never woke")
        self.assertLess(time.monotonic() - started, 2.0,
                        "the reader waited out its timeout instead of waking")
        self.assertEqual([{"type": "bye"}], got[0][0])

    def test_a_reader_with_nothing_to_read_returns_after_its_timeout(self):
        box = Mailbox(SEATS)
        started = time.monotonic()
        messages, cursor = box.collect("pi", 0, timeout=0.1)
        self.assertEqual(([], 0), (messages, cursor))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_the_default_timeout_is_the_modules(self):
        box = Mailbox(SEATS)
        self.assertEqual(POLL_TIMEOUT_S, box.poll_timeout_s)


class TestPresence(unittest.TestCase):
    """`last_seen`, which is how the hub notices a phone that walked away."""

    def test_a_seat_that_has_never_collected_has_not_been_seen(self):
        box = Mailbox(SEATS)
        self.assertIsNone(box.last_seen("phone"))

    def test_collecting_marks_the_seat_seen_even_with_nothing_to_read(self):
        """On entry, not on return — a poll that waits is still presence."""
        box = Mailbox(SEATS)
        box.collect("phone", 0, timeout=0)
        self.assertIsNotNone(box.last_seen("phone"))

    def test_a_reset_forgets_who_was_asking(self):
        box = Mailbox(SEATS)
        box.collect("phone", 0, timeout=0)
        box.reset()
        self.assertIsNone(box.last_seen("phone"))


class TestBounds(unittest.TestCase):
    """Overflow and reset."""

    def test_an_uncollected_box_drops_the_oldest_rather_than_growing(self):
        box = Mailbox(SEATS, max_depth=4)
        for n in range(10):
            box.post("pi", {"n": n})
        self.assertEqual(4, box.depth("pi"))
        messages, _ = box.collect("pi", 0, timeout=0)
        self.assertEqual([6, 7, 8, 9], [m["n"] for m in messages])

    def test_the_default_depth_is_the_modules(self):
        box = Mailbox(SEATS)
        self.assertEqual(MAX_MAILBOX, box.max_depth)

    def test_an_eviction_is_counted_rather_than_silent(self):
        """A reader that lost the front of its transcript must be able to say so."""
        box = Mailbox(SEATS, max_depth=4)
        self.assertEqual(0, box.dropped("pi"))
        for n in range(10):
            box.post("pi", {"n": n})
        self.assertEqual(6, box.dropped("pi"))
        # And only for the seat that overflowed.
        self.assertEqual(0, box.dropped("phone"))

    def test_the_drop_count_survives_being_collected(self):
        """Reading what is left does not un-lose what was dropped."""
        box = Mailbox(SEATS, max_depth=2)
        for n in range(5):
            box.post("pi", {"n": n})
        box.collect("pi", 0, timeout=0)
        self.assertEqual(3, box.dropped("pi"))

    def test_a_reset_clears_the_drop_count(self):
        box = Mailbox(SEATS, max_depth=1)
        box.post("pi", {"n": 1})
        box.post("pi", {"n": 2})
        self.assertEqual(1, box.dropped("pi"))
        box.reset()
        self.assertEqual(0, box.dropped("pi"))

    def test_a_reset_empties_every_seat(self):
        box = Mailbox(SEATS)
        box.post("pi", {"n": 1})
        box.post("phone", {"n": 2})
        box.reset()
        self.assertEqual(0, box.depth("pi"))
        self.assertEqual(0, box.depth("phone"))

    def test_a_reset_does_not_rewind_the_sequence(self):
        """A stale cursor must ask for nothing, never for the new session."""
        box = Mailbox(SEATS)
        box.post("pi", {"session": "old"})
        _, stale = box.collect("pi", 0, timeout=0)
        box.reset()
        box.post("pi", {"session": "new"})
        messages, _ = box.collect("pi", stale, timeout=0)
        self.assertEqual([{"session": "new"}], messages,
                         "a cursor from before the reset should still advance")
        # And the reverse: a cursor from before the reset must not re-deliver
        # what the old session had already consumed.
        self.assertNotIn({"session": "old"}, messages)


if __name__ == "__main__":
    unittest.main()


class TestAReaderThatOutlivedThisProcess(unittest.TestCase):
    """The sequence begins at 1 again when the service does. The readers do
    not restart with it.

    `AgentBridge` keeps one number for the life of the *voice* process, and
    `aipi5-agent.service` is a separate unit that is restarted on its own
    whenever agent code is deployed. So the bridge could be holding a cursor of
    a hundred against a mailbox that has issued three — and it would ignore the
    first hundred messages of the next run, which is most of one, before
    catching up. Silently: nothing errors, the transcript is simply missing its
    beginning.

    The same asymmetry in the other direction made the kiosk go deaf across an
    assistant restart; that one is in `EventLog.collect`.
    """

    def test_a_cursor_beyond_anything_issued_gets_everything(self):
        box = Mailbox(["agent"])
        box.post("agent", {"type": "agent.say", "text": "first"})
        box.post("agent", {"type": "agent.say", "text": "second"})
        messages, cursor = box.collect("agent", 100, timeout=0)
        self.assertEqual(["first", "second"], [m["text"] for m in messages])
        self.assertEqual(2, cursor)

    def test_it_does_not_wait_out_the_poll_first(self):
        """A reader in this state would otherwise block for the whole timeout
        on every pass while messages sat unread in front of it."""
        box = Mailbox(["agent"])
        box.post("agent", {"type": "agent.say", "text": "here"})
        started = time.monotonic()
        messages, _ = box.collect("agent", 100, timeout=5.0)
        self.assertEqual(1, len(messages))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_cursor_at_the_high_water_mark_is_not_a_restart(self):
        """Ordinary "nothing new since I last asked". It must keep waiting
        rather than being handed the whole box again."""
        box = Mailbox(["agent"])
        box.post("agent", {"type": "agent.say", "text": "here"})
        self.assertEqual(([], 1), box.collect("agent", 1, timeout=0))

    def test_eviction_does_not_look_like_a_restart(self):
        """The counter must be the highest ever *issued*, not the newest still
        held — otherwise a mailbox that has dropped its front would resend
        everything to a reader that is merely behind."""
        box = Mailbox(["agent"], max_depth=2)
        for n in range(5):
            box.post("agent", {"type": "agent.say", "text": str(n)})
        messages, cursor = box.collect("agent", 4, timeout=0)
        self.assertEqual(["4"], [m["text"] for m in messages])
        self.assertEqual(5, cursor)
