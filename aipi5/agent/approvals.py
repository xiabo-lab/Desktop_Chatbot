"""Asking a person, and waiting.

This is `aipi5/core/shutdown.py`'s `ShutdownCountdown` at a different timescale
and **with the polarity inverted**, which is the whole of the design:

    ShutdownCountdown                 ApprovalDesk
    -----------------                 ------------
    proceeds unless somebody stops    does nothing unless somebody allows
    2 s to acknowledge                minutes, because a phone is in a pocket
    timeout -> proceed                timeout -> refuse
    `showing` is strict about the     `approve` is strict about the token
      token; `cancel` is permissive     `deny` is permissive
    blocks the voice-loop thread      blocks the run thread only

The token discipline is copied exactly, because the failure it prevents is the
same one: an answer about a prompt that has already gone must never be read as
an answer about the current one. A phone that reloads mid-approval, a push
notification tapped twice, a `deny` arriving after a timeout — all of those have
to be inert.

The one that flips is which direction is permissive. There, being loose about a
*cancel* was safe, because the safe outcome was the device staying on. Here the
dangerous direction is `approve`, so that is the strict one and `deny` is the
one that may arrive late and still be honoured.

**A restart does not carry approvals over.** They live in memory and die with
the process, which is correct: a restart is a reason to ask again, never a
reason to assume the answer was yes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How long a change waits for an answer. Long, because the person it is asking
#: is not necessarily holding the phone — a backgrounded iOS web app does not
#: poll, so without a push notification this window is the whole of the answer.
DEFAULT_TIMEOUT_S = 300.0


@dataclass
class Ask:
    token: str
    run: str
    op: str
    what: str
    detail: str = ""
    warning: str = ""
    asked_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def as_event(self) -> dict:
        return {"type": "agent.approval", "run": self.run, "token": self.token,
                "op": self.op, "what": self.what, "detail": self.detail,
                "warning": self.warning, "expires_at": self.expires_at}


class ApprovalDesk:
    """One question at a time, and only ever from the run's own thread."""

    def __init__(self, emit, timeout_s: float = DEFAULT_TIMEOUT_S,
                 clock=time.monotonic):
        self.emit = emit
        self.timeout_s = timeout_s
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: Ask | None = None
        self._answered: threading.Event | None = None
        self._allowed = False
        self._counter = 0

    # ── what the run asks ───────────────────────────────────────────

    def ask(self, run: str, op: str, what: str, detail: str = "",
            warning: str = "", timeout_s: float | None = None) -> tuple[bool, str]:
        """Put the question and block this thread until it is answered.

        Returns (allowed, why). **A timeout is a refusal**, and the sentence
        says so rather than pretending something went wrong: a change nobody is
        watching should not happen, and the person should be told it did not.
        """
        wait = self.timeout_s if timeout_s is None else timeout_s
        with self._lock:
            if self._pending is not None:
                return False, "something else is already waiting to be approved"
            self._counter += 1
            token = f"{run}.{self._counter}.{uuid.uuid4().hex[:6]}"
            ask = Ask(token=token, run=run, op=op, what=what, detail=detail,
                      warning=warning, expires_at=time.time() + wait)
            self._pending = ask
            self._answered = threading.Event()
            self._allowed = False
            answered = self._answered

        self.emit(ask.as_event())
        try:
            if not answered.wait(wait):
                self.emit({"type": "agent.approval.gone", "run": run,
                           "token": token, "outcome": "timeout"})
                return False, (f"nobody answered within "
                               f"{wait / 60:.0f} minutes, so I did not do it")
            with self._lock:
                allowed = self._allowed
        finally:
            with self._lock:
                if self._pending is not None and self._pending.token == token:
                    self._pending = None
                    self._answered = None

        if allowed:
            return True, "you approved it"
        return False, "you said no"

    # ── what the phone sends ────────────────────────────────────────

    def answer(self, token: str, allow: bool) -> dict:
        with self._lock:
            pending = self._pending
            if pending is None:
                return {"ok": False, "error": "there is nothing waiting"}

            if allow and token != pending.token:
                # Strict, and this is the direction that matters. An approval
                # for a prompt that has gone must never approve the one that
                # replaced it.
                log.warning("refusing an approval for a stale token")
                return {"ok": False,
                        "error": "that was for a different request. Look at "
                                 "the one on screen now."}
            if not allow and token and token != pending.token:
                # Permissive, deliberately. A "no" about anything is a reason
                # not to proceed, whichever prompt it was aimed at.
                log.info("honouring a deny with a stale token")

            self._allowed = bool(allow)
            event = self._answered
        if event is not None:
            event.set()
        self.emit({"type": "agent.approval.gone", "run": pending.run,
                   "token": pending.token,
                   "outcome": "approved" if allow else "denied"})
        return {"ok": True, "outcome": "approved" if allow else "denied"}

    # ── what the screen and the pusher read ─────────────────────────

    def pending(self) -> Ask | None:
        with self._lock:
            return self._pending

    def snapshot(self) -> dict | None:
        ask = self.pending()
        if ask is None:
            return None
        return {"token": ask.token, "op": ask.op, "what": ask.what,
                "detail": ask.detail, "warning": ask.warning,
                "expires_at": ask.expires_at}

    def cancel(self, run: str = "") -> None:
        """Stop waiting, because the run is over. Leaves no card on the phone."""
        with self._lock:
            pending = self._pending
            if pending is None or (run and pending.run != run):
                return
            self._allowed = False
            event = self._answered
        if event is not None:
            event.set()
        self.emit({"type": "agent.approval.gone", "run": pending.run,
                   "token": pending.token, "outcome": "cancelled"})
