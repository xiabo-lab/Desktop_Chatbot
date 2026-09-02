"""Asking before something that rings, sends, or cannot be taken back.

The agent has had an approval desk since it could edit files. This is the same
idea one level down, for the everyday tools: ringing a phone, deleting a
birthday, opening a page nobody named. Those are not maintenance and they must
not cost a 24-step run, but they are also not "set the volume to thirty" — a
phone that rings in somebody's pocket because a half-heard sentence sounded
like a request is the kind of thing a house stops trusting a device over.

**The model never decides that somebody said yes.** That is the whole design.
A tool that needs consent does not act; it parks a closure here and answers the
model with a question to ask out loud. What comes back is decided by a phrase
matcher over the raw transcript (`is_affirmative`, AIA's, in `main.py`) or by a
finger on a button — never by the model's reading of the next sentence. A model
that reports "they said yes" is reporting, not deciding, and this desk does not
read its reports.

**Everything that is not a clear yes is a no.** Silence, a timeout, "hang on",
a token that has expired, a token that has already been answered. For an action
that cannot be taken back, "I could not tell" has to mean no — which is AIA's
rule for spoken confirmations and is inherited here rather than reinvented.

**One question at a time.** A second `ask` while one is waiting is refused
rather than queued or stacked. There is one room, one voice and one card, and
two questions in flight is a yes landing on whichever of them the person was
not answering.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: How long a question stays answerable. Long enough for somebody to think
#: about it, short enough that a "yes" shouted at the device ten minutes later
#: about something else cannot land on it.
TTL_S = 60.0

MAX_QUESTION = 300
MAX_DETAIL = 500


@dataclass
class Pending:
    """One unanswered question, and the closure that runs if the answer is yes.

    `action` takes no arguments and is built by the handler out of values it
    has already validated. Nothing the model wrote is captured in it as data to
    be re-parsed later — by the time this exists the arguments are a device
    name from an enum, or an id that was looked up and found.
    """

    token: str
    what: str
    question: str
    detail: str
    created: float
    ttl: float
    #: What the transcript says once this has actually happened -- "removed a
    #: birthday", not "asked about removing a birthday". Fixed text from the
    #: handler, carrying none of the arguments, for the same reason `SAID` in
    #: `aipi5/llm/tools.py` is fixed text.
    said: str = ""
    action: Callable[[], dict] = field(repr=False, default=lambda: {"ok": True})

    def expired(self, now: float) -> bool:
        return now - self.created > self.ttl

    def as_dict(self) -> dict:
        return {"token": self.token, "what": self.what,
                "question": self.question, "detail": self.detail,
                "expires_in": round(self.ttl - (time.time() - self.created))}


class ConsentDesk:
    """The one pending question, and the two ways it can be answered."""

    def __init__(self, clock=time.time, ttl_s: float = TTL_S, on_change=None,
                 on_done=None):
        self.clock = clock
        self.ttl_s = ttl_s
        #: Called with the `Pending` when one appears and with None when one
        #: goes, so a page can draw and clear a card without polling for it.
        #: Never called with the lock held.
        self.on_change = on_change
        #: `on_done(said, allowed, ok)` — called once a question has been
        #: resolved, with what actually happened.
        #:
        #: Separate from `on_change` because the two answer different
        #: questions. `on_change(None)` fires for a yes, a no and a timeout
        #: alike; the transcript needs to distinguish them, and a row saying
        #: "removed a birthday" after somebody said no is worse than no row.
        self.on_done = on_done
        self._lock = threading.Lock()
        self._pending: Pending | None = None

    # ── asking ──────────────────────────────────────────────────────

    def ask(self, what: str, question: str, action: Callable[[], dict],
            detail: str = "", said: str = "") -> Pending | None:
        """Park an action behind a question. None if one is already waiting."""
        now = self.clock()
        with self._lock:
            if self._pending is not None and not self._pending.expired(now):
                log.info("refusing to ask about %r: %r is still waiting",
                         what, self._pending.what)
                return None
            pending = Pending(
                token="c-" + uuid.uuid4().hex[:12],
                what=str(what)[:80],
                question=str(question)[:MAX_QUESTION],
                detail=str(detail)[:MAX_DETAIL],
                created=now, ttl=self.ttl_s, action=action,
                said=str(said)[:80])
            self._pending = pending
        self._announce(pending)
        return pending

    # ── answering ───────────────────────────────────────────────────

    def waiting(self) -> Pending | None:
        """The question still open, or None. Expiry is noticed here.

        Read by the voice loop on every pass, which is what makes a question
        nobody answered disappear on its own rather than at the moment somebody
        happens to say something else.
        """
        now = self.clock()
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if pending.expired(now):
                self._pending = None
            else:
                return pending
        log.info("nobody answered about %r in %.0fs, so nothing was done",
                 pending.what, pending.ttl)
        self._announce(None)
        return None

    def answer(self, token: str, allow) -> dict:
        """Resolve by token. Runs the action only on an unambiguous yes.

        The token is taken off the desk **before** the action runs, so a second
        tap during the round trip finds nothing waiting rather than doing it
        twice — which is the difference between one phone ringing and two.
        """
        token = str(token or "")
        with self._lock:
            pending = self._pending
            if pending is None or pending.token != token:
                return {"ok": False, "error": "that is no longer waiting"}
            if pending.expired(self.clock()):
                self._pending = None
                pending = None
            else:
                self._pending = None
        if pending is None:
            self._announce(None)
            return {"ok": False, "error": "that is no longer waiting"}

        self._announce(None)
        # `is True`, not truthy. The voice loop's matcher answers True, False
        # or None, and None means "I could not tell" — which for something that
        # rings a phone has to be a no rather than a coin toss.
        if allow is not True:
            log.info("%r was declined", pending.what)
            self._finished(pending, allowed=False, ok=True)
            return {"ok": True, "allowed": False, "what": pending.what}
        log.info("%r was approved", pending.what)
        try:
            outcome = pending.action()
        except Exception as exc:                     # noqa: BLE001
            log.exception("%r failed after it was approved", pending.what)
            self._finished(pending, allowed=True, ok=False)
            return {"ok": False, "allowed": True, "what": pending.what,
                    "error": f"it was approved but did not work ({exc})"}
        self._finished(pending, allowed=True, ok=True)
        answer = {"ok": True, "allowed": True, "what": pending.what}
        if isinstance(outcome, dict):
            answer["result"] = outcome
        return answer

    def cancel(self, why: str = "") -> None:
        """Withdraw the question without answering it. Also a no."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            log.info("withdrew the question about %r%s", pending.what,
                     f" ({why})" if why else "")
            self._announce(None)

    # ── for the page ────────────────────────────────────────────────

    def snapshot(self) -> dict | None:
        pending = self.waiting()
        return pending.as_dict() if pending is not None else None

    def _finished(self, pending: Pending, allowed: bool, ok: bool) -> None:
        """Say what actually happened, once it has. Never raises."""
        if self.on_done is None:
            return
        try:
            self.on_done(pending.said, allowed, ok)
        except Exception:                            # noqa: BLE001
            log.exception("the consent listener failed")

    def _announce(self, pending: Pending | None) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(pending)
        except Exception:                            # noqa: BLE001
            log.exception("the consent listener failed")
