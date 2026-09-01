"""One utterance, captured on request and handed back as words.

The agent console on the touchscreen needs text, and this panel has no
keyboard. `squeekboard` is installed and the compositor is labwc, but a page
that autofocuses an input raised nothing — measured on the device — and even if
it had, there is no pinyin IME on it, so half the household could not type into
it. The device already has an excellent bilingual recogniser three inches from
the person's face. So: press the microphone, say the thing, edit the words if
they came out wrong, send.

**This is not a turn.** The assistant's own path — wake word, or the Talk
page's Listen button — captures, transcribes, routes, and *answers*. Asking it
to dictate would mean asking a question and having it reply to it. What
happens here stops at the transcript: no router, no model, no speech.

## Why a rendezvous rather than a queue

`UiState` is a queue because a button press is fire-and-forget — the page posts
`{"action": "weather"}` and finds out what happened by watching the screen.
Dictation has an answer that only the caller wants, so the HTTP thread has to
wait for it.

It waits *without touching the microphone*, which is the rule this file exists
to keep. There is exactly one thing reading the capture device and it is the
loop in `main.py`; a second reader is not a race, it is an `ALSA` device that
allows one open and an assistant that stops hearing. So the HTTP thread leaves
a request here, the loop notices it on the next frame, does the work on its own
thread, and fills the answer in.

## What happens when nobody picks it up

The loop can be somewhere else: in a call, in a turn, or blocked on a
microphone that has gone away. So a request carries a deadline and expires on
its own, and the loop refuses one that has already expired rather than starting
a capture nobody is waiting for any more. Both sides can therefore give up
without either having to tell the other.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How long the page waits for words before giving up. Generous, because most
#: of it is the person: the wake-word path allows several seconds of silence
#: before somebody starts speaking and then however long they take, and a
#: dictation box is if anything the place people think about what to say.
DEFAULT_TIMEOUT_S = 25.0


@dataclass
class Heard:
    """What came back. Falsey unless there are words in it."""

    ok: bool = False
    text: str = ""
    #: The language the recogniser reported, so the page can say which voice
    #: it heard. Not used to route anything — nothing is routed.
    language: str = ""
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict:
        return ({"ok": True, "text": self.text, "language": self.language}
                if self.ok else {"ok": False, "error": self.error})


@dataclass
class _Request:
    """One caller waiting for words."""

    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    heard: Heard = field(default_factory=Heard)

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.deadline and not self.done.is_set()

    def answer(self, heard: Heard) -> None:
        self.heard = heard
        self.done.set()


class Dictation:
    """The one slot a request sits in between the page and the voice loop."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._request: _Request | None = None
        #: Counted rather than logged per use: the journal here is volatile and
        #: this is the sort of thing somebody asks about a week later.
        self.captures = 0

    # ── the page's side ─────────────────────────────────────────────

    def ask(self, timeout_s: float | None = None) -> Heard:
        """Wait for one utterance. Never raises. Called on an HTTP thread.

        Refuses rather than queues when one is already in flight. Two people
        cannot dictate at once into one microphone, and a second request that
        waited would be answered with words the first person said.
        """
        wait = self.timeout_s if timeout_s is None else timeout_s
        request = _Request(deadline=time.monotonic() + wait)
        with self._lock:
            if self._request is not None and not self._request.expired:
                return Heard(error="Already listening for something else.")
            self._request = request

        if not request.done.wait(wait):
            with self._lock:
                if self._request is request:
                    self._request = None
            # The honest sentence: nothing was heard *and* nothing may have
            # been listening. The loop is in a call, in a turn, or holding a
            # microphone that has gone away, and the page cannot tell which.
            log.info("dictation: nothing came back within %.0fs", wait)
            return Heard(error="I couldn't listen just then. Try again.")
        return request.heard

    # ── the voice loop's side ───────────────────────────────────────

    def waiting(self) -> bool:
        """Is somebody waiting for words? Cheap enough for the frame loop.

        Called once per audio frame, which is why it takes no lock in the
        common case — the attribute read is atomic and a missed request is
        picked up on the next frame a fiftieth of a second later.
        """
        request = self._request
        return request is not None and not request.done.is_set()

    def take(self) -> _Request | None:
        """Claim the waiting request, or None. Called from the voice loop."""
        with self._lock:
            request = self._request
            if request is None:
                return None
            if request.expired:
                # Nobody is listening for this any more, so capturing would
                # take the microphone for a couple of seconds to fill in an
                # answer that goes nowhere.
                log.info("dictation: the request expired before it was taken")
                self._request = None
                return None
            self._request = None
            self.captures += 1
            return request

    def describe(self) -> dict:
        return {"waiting": self.waiting(), "captures": self.captures}
