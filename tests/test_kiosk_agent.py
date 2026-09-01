"""The agent console on the touchscreen, and the microphone that feeds it.

Assertions on file text, following `tests/test_ui_assets.py` and
`tests/test_agent_page.py`. Blunt, and right for the same reason it is right
there: what these guard is invisible until somebody is standing in front of the
panel with no way to type.

The console was the phone's alone by decision — a chat surface on a screen with
no keyboard is a chat surface nobody can use. What changed the decision is
dictation, so the two are tested together: the page, the three routes behind
it, and the rule that none of them goes near the microphone.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "aipi5" / "ui" / "web" / "index.html"
SERVER = ROOT / "aipi5" / "ui" / "server.py"
MAIN = ROOT / "aipi5" / "main.py"


class TestThePageIsWholeAndAddressable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding="utf-8")

    def test_every_piece_the_script_addresses_is_in_the_markup(self):
        """`el()` on a missing id returns null, and the failure is a blank
        screen with a TypeError nobody on a kiosk can see."""
        for ident in ("page-agent", "agent-log", "agent-state", "agent-text",
                      "agent-send", "agent-mic", "agent-compose", "agent-stop",
                      "agent-hint", "agent-dot", "agent-approval",
                      "agent-approval-what", "agent-approval-detail",
                      "agent-approval-warning", "agent-approve", "agent-deny"):
            with self.subTest(id=ident):
                self.assertIn(f'id="{ident}"', self.page)

    def test_it_is_a_view_in_the_one_document(self):
        """Not a window. On a kiosk with no title bars a second window is a
        place nobody can get back from — the rule the whole page is built on.
        """
        self.assertRegex(self.page, r'<div id="page-agent" class="page">')
        block = self.page[self.page.index('<div id="page-agent"'):]
        block = block[:block.index("</div>\n\n<!--")]
        self.assertIn("data-back", block)

    def test_it_is_reachable_from_the_home_screen(self):
        self.assertIn('<button data-page="agent">', self.page)

    def test_it_is_started_and_stopped_with_the_page(self):
        # Every page that holds something open is torn down in `show()`, and a
        # long poll that outlived its page would be a request per screen the
        # person visited afterwards.
        self.assertIn('if (page === "agent") startAgent();', self.page)
        self.assertIn('if (previous === "agent") stopAgent();', self.page)

    def test_the_transcript_is_never_drawn_with_innerhtml(self):
        """Half of what is drawn here is a log line off this device, and a log
        line is somewhere a string somebody else wrote can end up."""
        console = self.page[self.page.index("function drawAgentEvent"):]
        console = console[:console.index("async function agentSay")]
        self.assertNotIn("innerHTML", console)
        self.assertIn("textContent", console)

    def test_the_compose_box_can_be_selected_in(self):
        """`user-select: none` on `html, body` is right for the rest of a page
        nobody selects text on, and it makes a textarea unusable."""
        block = self.page[self.page.index("#agent-text {"):]
        self.assertIn("user-select: text", block[:block.index("}")])

    def test_a_hidden_button_is_actually_hidden(self):
        """`hidden` loses to the page's own `button { display: flex }`.

        The attribute's `display: none` is a user-agent rule and an author
        rule beats it, so a button with `hidden` set is simply visible — seen
        on the panel, where Stop sat in the header of an idle console. Both
        hidden buttons here, and any added later, depend on this one line.
        """
        self.assertIn("button[hidden] { display: none; }", self.page)
        self.assertIn('id="agent-stop" hidden', self.page)

    def test_the_dictation_hint_is_a_place_errors_can_land(self):
        # There is nowhere else for one to go: the row is not a transcript
        # event, and an alert on a kiosk is a dialog nobody can dismiss.
        block = self.page[self.page.index('el("agent-mic").addEventListener'):]
        self.assertIn('el("agent-hint").textContent = heard.error', block)


class TestTheWireMatches(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding="utf-8")
        cls.server = SERVER.read_text(encoding="utf-8")

    def test_every_agent_route_the_page_calls_exists(self):
        called = set(re.findall(r'"(/api/agent/[a-z]+)', self.page))
        self.assertTrue(called, "the page calls no agent routes at all")
        for path in sorted(called):
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', self.server)

    def test_the_console_is_refused_when_the_agent_is_not_installed(self):
        self.assertIn("_agent_missing", self.server)
        self.assertIn("the agent is not installed on this device", self.server)
        for handler in ("_agent_poll", "_agent_say"):
            with self.subTest(handler=handler):
                body = re.search(rf"def {handler}\(self.*?\n\n    def ",
                                 self.server, re.S).group(0)
                self.assertIn("self._agent_missing()", body)

    def test_the_page_may_send_three_messages_and_no_others(self):
        """The list is what the *page* may originate, not what the runtime
        understands. `agent.gesture` has its own route with its own bounds,
        and `agent.open` is the assistant's message about a page somebody
        asked for out loud — reachable from a text box it would be a URL bar
        with a language model in it.
        """
        from aipi5.ui.server import AGENT_MESSAGES

        self.assertEqual(AGENT_MESSAGES,
                         {"agent.ask", "agent.stop", "agent.answer"})
        sent = set(re.findall(r'type: "(agent\.[a-z]+)"', self.page))
        self.assertTrue(sent)
        self.assertTrue(sent <= AGENT_MESSAGES, sent - AGENT_MESSAGES)

    def test_the_badge_is_published_with_the_rest_of_the_state(self):
        # Off the snapshot the browser flag already reads, so a closed console
        # costs the agent nothing.
        for key in ("agent_busy", "agent_pending"):
            with self.subTest(key=key):
                self.assertIn(f'payload["{key}"]', self.server)
                self.assertIn(f"state.{key}", self.page)


class TestNobodyElseTouchesTheMicrophone(unittest.TestCase):
    """One reader of the capture device, and it is the voice loop.

    ALSA allows one open. A second reader is not a race that shows up as
    stutter; it is an assistant that stops hearing. So the HTTP handler leaves
    a request in `Dictation` and waits, and the loop does the work.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = SERVER.read_text(encoding="utf-8")
        cls.main = MAIN.read_text(encoding="utf-8")

    def test_the_handler_only_asks(self):
        body = re.search(r"def _dictate_post\(self.*?\n\n    def ",
                         self.server, re.S).group(0)
        self.assertIn("dictation.ask()", body)
        for forbidden in ("Microphone", "endpointer", "stt", "frames"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_capture_happens_on_the_voice_loop(self):
        body = re.search(r"def dictate\(assistant, mic, frames\).*?\n\ndef ",
                         self.main, re.S).group(0)
        self.assertIn("assistant.endpointer.collect(frames)", body)
        self.assertIn("assistant.stt.listen(audio)", body)
        self.assertIn("request.answer(", body)

    def test_dictation_answers_and_does_not_reply(self):
        """A turn's first half and none of its second.

        Routing what was dictated would mean the assistant answering a
        question that was addressed to the agent, and speaking it would mean
        it reading somebody's own words back to them.
        """
        body = re.search(r"def dictate\(assistant, mic, frames\).*?\n\ndef ",
                         self.main, re.S).group(0)
        # The *code*, not the docstring — which says "the router is not asked"
        # and would satisfy a check over the whole function. This project's
        # oldest testing trap, and it caught this test on its first run.
        code = re.sub(r'"""[\s\S]*?"""', "", body)
        code = re.sub(r"^\s*#.*$", "", code, flags=re.M)
        for forbidden in ("router", "match_sequence", "assistant.answer",
                          "speaker.say", "say(assistant"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, code)

    def test_the_caller_is_always_answered(self):
        """Every path fills the answer in, including the one that raised.

        The alternative is an HTTP thread asleep until its deadline for a
        failure that has already happened and is already in the journal.
        """
        body = re.search(r"def dictate\(assistant, mic, frames\).*?\n\ndef ",
                         self.main, re.S).group(0)
        finally_block = body[body.index("finally:"):]
        self.assertIn("request.answer(heard)", finally_block)

    def test_it_is_not_judged_against_the_turn_budget(self):
        """The elapsed time here is somebody talking.

        A turn is judged against 2500 ms, and this is not bounded by anything
        the code does. Measured on the device before it was taken out: a
        capture with nobody speaking logged `turn 4201ms to audio [OVER by
        1701ms]`, and a real sentence would have been worse. A journal full of
        verdicts that mean nothing is a journal people stop reading, which is
        the failure `Turn.judged_ms` exists to prevent.
        """
        body = re.search(r"def dictate\(assistant, mic, frames\)[\s\S]*?\n\ndef ",
                         self.main).group(0)
        code = re.sub(r'"""[\s\S]*?"""', "", body)
        self.assertNotIn("begin_turn", code)
        self.assertNotIn("end_turn", code)
        # It still has to leave the machine idle, or the screen says
        # "Listening…" until the next wake word.
        self.assertIn("assistant.machine.to(State.IDLE)", code)

    def test_the_wake_detector_and_the_buffer_are_reset_afterwards(self):
        """The detector has been starved for the length of the utterance and
        the buffer holds all of it. Without both, the first thing the
        assistant does next is act on a fragment of what was dictated."""
        body = re.search(r"def dictate\(assistant, mic, frames\).*?\n\ndef ",
                         self.main, re.S).group(0)
        self.assertIn("assistant.detector_wake.reset()", body)
        self.assertIn("mic.drain()", body)

    def test_it_runs_before_the_wake_detector_is_fed(self):
        """The capture eats the frames the detector would have read, and a
        detector part-way through a phrase then acts on the half it kept —
        the same reason the call branch skips `detect` rather than discarding
        its answer."""
        loop = self.main[self.main.index("while not stopping:"):]
        dictation_at = loop.index("if assistant.dictation.waiting():")
        detect_at = loop.index("woke = assistant.detector_wake.detect(frame)")
        self.assertLess(dictation_at, detect_at)


if __name__ == "__main__":
    unittest.main()
