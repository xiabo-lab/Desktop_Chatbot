"""The shared transcript, and the one rule about what may go in it.

Most of this file is about that rule. `AssistantEvent` is rendered onto a
screen in a living room and served to anything on loopback, so the difference
between "the journal had 200 lines about the display" and the 200 lines is the
difference between a transcript and a leak — and the leak has no error message.
It is enforced in `_meta` and `_validate` rather than asked for in a comment,
and this is where that enforcement is checked.

The rest is the cursor. Two readers hold their own, rows are never reused, and
what was evicted before anybody read it is counted rather than silently lost.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.assistant.events import (AssistantEvent, EventError, EventLog,
                                    KINDS, MAX_TEXT, NullSink, SOURCES)


class TestWhatMayBePublished(unittest.TestCase):

    def setUp(self):
        self.log = EventLog()

    def test_a_row_needs_a_kind_and_a_source_from_the_two_lists(self):
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                self.assertTrue(self.log.publish(kind, "voice", text="x"))
        for source in sorted(SOURCES):
            with self.subTest(source=source):
                self.assertTrue(self.log.publish("assistant", source, text="x"))

    def test_an_invented_kind_or_source_is_refused(self):
        with self.assertRaises(EventError):
            self.log.publish("debug", "voice", text="x")
        with self.assertRaises(EventError):
            self.log.publish("assistant", "openai", text="x")

    def test_an_invented_field_is_refused_rather_than_ignored(self):
        """The ignored case is how a payload ends up somewhere nobody expected.

        `publish(..., result=...)` that silently dropped `result` would read as
        working, and the next person would add `text=json.dumps(result)`.
        """
        with self.assertRaises(EventError):
            self.log.publish("tool", "agent", result={"lines": ["…"]})

    def test_text_is_bounded(self):
        event = self.log.publish("assistant", "voice", text="x" * (MAX_TEXT * 2))
        self.assertEqual(MAX_TEXT, len(event.text))

    def test_metadata_is_scalars_only(self):
        event = self.log.publish("done", "agent", text="finished",
                                 meta={"steps": 4, "seconds": 12.5,
                                       "clean": True, "note": "ok"})
        self.assertEqual({"steps": 4, "seconds": 12.5, "clean": True,
                          "note": "ok"}, event.meta)

    def test_a_nested_object_is_refused(self):
        """One level of `{"detail": {...}}` and the rule is gone."""
        with self.assertRaises(EventError):
            self.log.publish("tool", "agent", meta={"detail": {"lines": 200}})
        with self.assertRaises(EventError):
            self.log.publish("tool", "agent", meta={"detail": ["a", "b"]})

    def test_the_names_a_raw_payload_arrives_under_are_refused(self):
        """Refused rather than truncated. A truncated tool result on a screen
        is still a tool result on a screen."""
        for name in ("result", "output", "reasoning", "messages", "arguments",
                     "api_key", "RESULT", "Thinking"):
            with self.subTest(name=name):
                with self.assertRaises(EventError):
                    self.log.publish("tool", "agent", meta={name: "…"})

    def test_ok_is_a_boolean_or_absent(self):
        self.assertIsNone(self.log.publish("tool", "agent").ok)
        self.assertIs(True, self.log.publish("tool", "agent", ok=True).ok)
        with self.assertRaises(EventError):
            self.log.publish("tool", "agent", ok="yes")

    def test_the_null_sink_validates_and_keeps_nothing(self):
        """What a service with no page open is handed. It must still refuse a
        row that would have leaked, or the rule is only true when somebody is
        looking."""
        sink = NullSink()
        self.assertIsNone(sink.publish("assistant", "voice", text="hello"))
        with self.assertRaises(EventError):
            sink.publish("tool", "agent", meta={"result": "…"})


class TestTheCursor(unittest.TestCase):

    def setUp(self):
        self.log = EventLog(depth=5)

    def test_ids_rise_and_are_never_reused(self):
        ids = [self.log.publish("user", "voice", text=str(n)).id
               for n in range(12)]
        self.assertEqual(sorted(set(ids)), ids)
        self.assertEqual(1, ids[0])

    def test_a_reader_is_handed_only_what_it_has_not_seen(self):
        for n in range(3):
            self.log.publish("user", "voice", text=str(n))
        rows, cursor = self.log.collect(0)
        self.assertEqual(["0", "1", "2"], [r["text"] for r in rows])
        again, cursor2 = self.log.collect(cursor)
        self.assertEqual([], again)
        self.assertEqual(cursor, cursor2)

    def test_two_readers_each_see_everything(self):
        """The page and the phone hold separate cursors. Collecting must not
        consume, or whichever polled first would be the only one told."""
        for n in range(3):
            self.log.publish("user", "voice", text=str(n))
        first, _ = self.log.collect(0)
        second, _ = self.log.collect(0)
        self.assertEqual([r["text"] for r in first], [r["text"] for r in second])

    def test_what_was_evicted_unread_is_counted(self):
        for n in range(9):
            self.log.publish("user", "voice", text=str(n))
        self.assertEqual(4, self.log.dropped())
        rows, _ = self.log.collect(0)
        self.assertEqual(5, len(rows))
        self.assertEqual("4", rows[0]["text"])

    def test_clearing_does_not_rewind_the_counter(self):
        """A page holding an old cursor must ask for something that never
        arrives, rather than be handed the next session's rows as the tail of
        its own."""
        first = self.log.publish("user", "voice", text="a").id
        self.log.clear()
        self.assertGreater(self.log.publish("user", "voice", text="b").id, first)

    def test_an_empty_collect_waits_and_gives_up(self):
        started = time.monotonic()
        rows, cursor = self.log.collect(0, timeout=0.1)
        self.assertEqual(([], 0), (rows, cursor))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_waiting_reader_is_woken_by_a_publish(self):
        got: list = []

        def read():
            got.append(self.log.collect(0, timeout=5.0))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(0.05)
        self.log.publish("assistant", "voice", text="here")
        reader.join(timeout=5.0)
        self.assertFalse(reader.is_alive())
        self.assertEqual(1, len(got[0][0]))



class TestAPageThatOutLivedTheProcess(unittest.TestCase):
    """The kiosk does not reload, and the ids start at 1 again when the
    service does.

    So a panel that had reached 16 asks a restarted assistant for rows after
    16, is handed back its own 16, and goes deaf -- permanently, because
    nothing in the reply could ever bring the number down. That is every
    `systemctl --user restart aipi5.service`, which is the deploy step, on a
    screen with no keyboard to reload with.

    Found on the device. Every test in this file used one log for the whole
    test, which is the one arrangement where it cannot happen.
    """

    def test_a_cursor_from_before_the_restart_is_sent_everything(self):
        after = EventLog()
        after.publish("user", "voice", text="a")
        after.publish("assistant", "voice", text="b")
        rows, cursor = after.collect(16)
        self.assertEqual(["a", "b"], [r["text"] for r in rows])
        self.assertEqual(2, cursor)

    def test_it_does_not_wait_out_the_long_poll_first(self):
        """A page holding a stale cursor would otherwise sit blank for the
        whole poll interval on every pass, which on a kiosk is forever."""
        after = EventLog()
        after.publish("user", "voice", text="a")
        started = time.monotonic()
        rows, _ = after.collect(16, timeout=5.0)
        self.assertEqual(1, len(rows))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_an_empty_restarted_log_still_reports_a_cursor_of_zero(self):
        """Nothing to send yet, and the page must come down off 16 anyway --
        the next row is id 1 and it would otherwise never be asked for."""
        self.assertEqual(([], 0), EventLog().collect(16, timeout=0.1))

    def test_a_cursor_orphaned_by_clear_is_left_alone(self):
        """The other side of the same coin, and the reason this is not simply
        `since > cursor()`. `clear()` promises that an old cursor asks for
        something that never arrives rather than being handed the next
        session's rows as the tail of its own -- so the counter it is measured
        against is the highest id ever issued, not the newest row still held.
        """
        log = EventLog()
        for n in range(5):
            log.publish("user", "voice", text=str(n))
        log.clear()
        self.assertEqual(([], 3), log.collect(3, timeout=0.1))
        log.publish("assistant", "voice", text="new")
        rows, _ = log.collect(3)
        self.assertEqual(["new"], [r["text"] for r in rows])

    def test_a_cursor_at_exactly_the_high_water_mark_is_not_a_restart(self):
        log = EventLog()
        for n in range(3):
            log.publish("user", "voice", text=str(n))
        self.assertEqual(([], 3), log.collect(3, timeout=0.1))


class TestTheWireShape(unittest.TestCase):
    """What a page actually receives. Absent rather than empty, so a row is
    small: this is polled continuously by a browser on a Pi."""

    def test_only_the_fields_that_carry_something_are_sent(self):
        log = EventLog(clock=lambda: 1000.0)
        row = log.publish("assistant", "voice", text="hello").as_dict()
        self.assertEqual({"id": 1, "at": 1000.0, "kind": "assistant",
                          "source": "voice", "text": "hello"}, row)

    def test_a_tool_row_carries_its_name_and_whether_it_worked(self):
        log = EventLog(clock=lambda: 1000.0)
        row = log.publish("tool", "agent", tool="read_journal", ok=False,
                          text="the journal was unreadable", run="r-1").as_dict()
        self.assertEqual("read_journal", row["tool"])
        self.assertIs(False, row["ok"])
        self.assertEqual("r-1", row["run"])

    def test_a_capture_row_carries_a_token_and_not_a_path(self):
        """`/api/camera/capture?t=` takes a token. A path in this field would
        be a filesystem path published to every reader on loopback."""
        log = EventLog()
        row = log.publish("capture", "voice", capture="1756700000.5").as_dict()
        self.assertEqual("1756700000.5", row["capture"])
        self.assertNotIn("path", row)

    def test_an_event_cannot_be_edited_after_it_is_handed_out(self):
        log = EventLog()
        event = log.publish("assistant", "voice", text="hello")
        with self.assertRaises(Exception):
            event.text = "something else"       # frozen dataclass
        self.assertIsInstance(event, AssistantEvent)


if __name__ == "__main__":
    unittest.main()
