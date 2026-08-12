"""Powering the device off, and the three seconds in which it can be stopped.

Every other destructive command in this project is answered out loud: the
assistant asks, holds the floor, and listens for "确定". Shutdown cannot be,
and the reason is not taste. `poweroff` takes the audio stack down with it, so
the one command this device cannot narrate is the last one it runs — and a
question whose answer arrives after the microphone has gone is not a question.

So it is answered by presence instead. The screen shows 3, 2, 1 and a touch
anywhere cancels, which is the one answer somebody standing in front of a kiosk
can always give. The newer AIA reached the same conclusion and this matches its
policy; the older copy on the Pi still declares `confirm=True`, which is why
nothing here reads that flag.

Two rules hold it together, and both are about failing towards a device that
stays on:

**It fails closed.** If nothing says the countdown is on a screen, the shutdown
does not happen. Otherwise a dead display, a crashed Chromium and a person
choosing not to cancel are indistinguishable from here, and only one of those
should end with the device off.

**The clock starts when the screen says it is drawing it**, not when the
countdown is created. The page learns by polling, so starting the clock here
would spend the first half-second of somebody's three on a round trip they
cannot see, and the first number they were promised would be 2.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class ShutdownCountdown:
    """The visible, touch-cancellable delay. One at a time, by construction."""

    #: How long the screen has to say it is showing the countdown. The page
    #: polls twice a second, so this is four chances to answer before the
    #: shutdown is refused.
    ACK_TIMEOUT_S = 2.0

    def __init__(self, seconds: float = 3):
        #: Whole seconds in a deployment — the screen draws them one per
        #: second. A float is allowed so a test need not take three of them.
        self.seconds = seconds
        self._lock = threading.Lock()
        #: Rises with every countdown, so an answer about an old one — a page
        #: reloaded mid-count, a request that arrived late — cannot be mistaken
        #: for an answer about this one.
        self._token = 0
        self._live = False
        self._shown: threading.Event | None = None
        self._cancelled: threading.Event | None = None

    def payload(self) -> dict | None:
        """What the screen needs to draw it, or None when nothing is counting."""
        with self._lock:
            if not self._live:
                return None
            return {"token": self._token, "seconds": self.seconds}

    def run(self, publish) -> bool:
        """Show the countdown and block. True only if it ran to the end.

        `publish` is called when the countdown appears and again when it is
        over, because the screen learns about both the same way it learns about
        everything else here.
        """
        with self._lock:
            self._token += 1
            self._live = True
            shown = self._shown = threading.Event()
            cancelled = self._cancelled = threading.Event()
        publish()
        try:
            if not shown.wait(self.ACK_TIMEOUT_S):
                log.error("nothing is showing the shutdown countdown; "
                          "refusing to power off")
                return False
            if cancelled.wait(self.seconds):
                log.info("shutdown cancelled by a touch")
                return False
            log.info("shutdown countdown finished; powering off")
            return True
        finally:
            with self._lock:
                self._live = False
                self._shown = self._cancelled = None
            publish()

    def showing(self, token: int) -> bool:
        """The screen reports that the countdown is in front of somebody."""
        with self._lock:
            if not self._live or token != self._token:
                return False
            event = self._shown
        if event is None:
            return False
        event.set()
        return True

    def cancel(self, token: int = 0) -> bool:
        """Somebody touched the screen.

        Deliberately permissive about the token, unlike `showing`. Cancelling
        is the safe direction: a stale token is a reason to keep the device on,
        never a reason to carry on towards powering it off.
        """
        with self._lock:
            event = self._cancelled if self._live else None
        if event is None:
            return False
        event.set()
        return True


def countdown_and_run(assistant, intent, language):
    """Count down, and run the command only if nobody stopped it.

    Returns `(reply, intent)`. **A cancelled shutdown returns `None` for the
    intent**, and that is not cosmetic: the voice loop reads it to decide
    whether the music comes back. Reporting a command that did not run would
    leave a paused player behind for something the person stopped.

    Reached by the command's *name*, never by `confirm` — see the module
    docstring for why the two AIA copies disagree about that flag and why it
    must not matter here.
    """
    if assistant.countdown.run(assistant.publish):
        return intent.command.handler(**intent.arguments).say(language), intent
    return ("Cancelled." if language == "en" else "已取消。"), None
