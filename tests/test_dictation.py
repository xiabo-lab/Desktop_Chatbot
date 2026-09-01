"""Dictation: one utterance, captured on request and handed back as words.

The agent console lives on a panel with no keyboard, so this is how text gets
into it. What is checked here is the rendezvous rather than the recognising —
the recogniser is AIA's and is measured on the device. What can go wrong here
is a thread waiting forever, two callers sharing one microphone, or a capture
started for somebody who has already given up.

`Dictation` touches no audio device and imports nothing that does, which is
what lets all of this run on a laptop.
"""

from __future__ import annotations

import threading
import time
import unittest

from aipi5.ui.dictation import Dictation, Heard


class TestHeard(unittest.TestCase):

    def test_it_is_falsey_until_there_are_words(self):
        self.assertFalse(Heard())
        self.assertFalse(Heard(error="nope"))
        self.assertTrue(Heard(ok=True, text="hello"))

    def test_the_wire_shape_carries_one_or_the_other(self):
        good = Heard(ok=True, text="打开音乐", language="zh").as_dict()
        self.assertEqual(good, {"ok": True, "text": "打开音乐", "language": "zh"})
        bad = Heard(error="I didn't catch that.").as_dict()
        self.assertEqual(bad, {"ok": False, "error": "I didn't catch that."})
        # Never both. A page that reads `text` on a failure would put an error
        # sentence into the box as though somebody had said it.
        self.assertNotIn("text", bad)


class TestTheRendezvous(unittest.TestCase):
    """The page's thread and the voice loop's, meeting over one slot."""

    def loop(self, dictation: Dictation, heard: Heard, delay: float = 0.0):
        """Stand in for the voice loop: notice, take, answer."""
        def run():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if dictation.waiting():
                    time.sleep(delay)
                    request = dictation.take()
                    if request is not None:
                        request.answer(heard)
                    return
                time.sleep(0.005)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_words_reach_the_caller(self):
        dictation = Dictation()
        self.loop(dictation, Heard(ok=True, text="check the free space",
                                   language="en"))
        heard = dictation.ask(timeout_s=5)
        self.assertTrue(heard)
        self.assertEqual(heard.text, "check the free space")
        self.assertEqual(dictation.captures, 1)

    def test_nothing_heard_is_an_answer_too(self):
        dictation = Dictation()
        self.loop(dictation, Heard(error="I didn't catch that."))
        heard = dictation.ask(timeout_s=5)
        self.assertFalse(heard)
        self.assertIn("catch", heard.error)

    def test_a_loop_that_never_looks_does_not_hang_the_page(self):
        """The case that matters most. The loop can be in a call, in a turn,
        or blocked on a microphone that has been unplugged — and an HTTP
        thread waiting on any of those is a thread held for the life of the
        process."""
        dictation = Dictation()
        started = time.monotonic()
        with self.assertLogs("aipi5.ui.dictation", level="INFO"):
            heard = dictation.ask(timeout_s=0.2)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(heard)
        self.assertIn("couldn't listen", heard.error)

    def test_the_slot_is_cleared_when_a_caller_gives_up(self):
        # Otherwise the next press is refused as "already listening" for a
        # request nobody is waiting on, forever.
        dictation = Dictation()
        with self.assertLogs("aipi5.ui.dictation", level="INFO"):
            dictation.ask(timeout_s=0.1)
        self.assertFalse(dictation.waiting())
        self.loop(dictation, Heard(ok=True, text="again"))
        self.assertTrue(dictation.ask(timeout_s=5))

    def test_two_at_once_is_refused_rather_than_queued(self):
        """Two people cannot dictate into one microphone, and a second
        request that waited would be answered with the first person's words."""
        dictation = Dictation()
        first = {}

        def slow_caller():
            first["heard"] = dictation.ask(timeout_s=5)

        thread = threading.Thread(target=slow_caller, daemon=True)
        thread.start()
        while not dictation.waiting():
            time.sleep(0.005)

        second = dictation.ask(timeout_s=5)
        self.assertFalse(second)
        self.assertIn("Already listening", second.error)

        # And the first is still live, and still gets its answer.
        request = dictation.take()
        self.assertIsNotNone(request)
        request.answer(Heard(ok=True, text="mine"))
        thread.join(5)
        self.assertEqual(first["heard"].text, "mine")

    def test_a_request_nobody_is_waiting_for_is_not_captured(self):
        """The loop must not take the microphone for a couple of seconds to
        fill in an answer that goes nowhere."""
        dictation = Dictation()
        with self.assertLogs("aipi5.ui.dictation", level="INFO"):
            dictation.ask(timeout_s=0.05)
        # `ask` clears its own slot on the way out, so put an expired one back
        # by hand — this is the race where the loop looks a moment later.
        from aipi5.ui.dictation import _Request
        dictation._request = _Request(deadline=time.monotonic() - 1)
        with self.assertLogs("aipi5.ui.dictation", level="INFO") as caught:
            self.assertIsNone(dictation.take())
        self.assertTrue(any("expired" in line for line in caught.output))
        self.assertEqual(dictation.captures, 0)

    def test_taking_it_twice_gets_nothing_the_second_time(self):
        dictation = Dictation()
        threading.Thread(target=lambda: dictation.ask(timeout_s=2),
                         daemon=True).start()
        while not dictation.waiting():
            time.sleep(0.005)
        self.assertIsNotNone(dictation.take())
        self.assertIsNone(dictation.take())

    def test_nothing_is_waiting_on_a_fresh_one(self):
        dictation = Dictation()
        self.assertFalse(dictation.waiting())
        self.assertIsNone(dictation.take())
        self.assertEqual(dictation.describe(),
                         {"waiting": False, "captures": 0})


class TestItStaysOffTheAudioPath(unittest.TestCase):

    def test_it_imports_nothing_that_opens_a_device(self):
        """The rule this module exists to keep: exactly one thing reads the
        capture device, and it is the loop in `main.py`. A second reader is
        not a race — ALSA allows one open, and the assistant stops hearing.
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "aipi5" / "ui" / "dictation.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"logging", "threading", "time", "dataclasses"})


if __name__ == "__main__":
    unittest.main()
