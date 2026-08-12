"""What the screen currently shows, and what the screen asks for back.

Four threads touch this — the voice loop, the presence detector, the HTTP
handler and the browser's poll — so it exists to be the one place they meet,
with a lock around it and no other shared mutable state anywhere in the
project.

Deliberately a plain dictionary behind a lock rather than a set of typed
fields. What the page renders changes far more often than what the assistant
does, and a schema here would mean editing three files to put one more number
on the screen. The lock is held only for the swap, never across any I/O.

**Actions go the other way, and are a fixed list.** The screen has buttons on
it — section 23 — so the UI is not read-only the way AIA's is, and that is a
deliberate departure worth naming. What makes it safe is that a button posts a
name from an enum into a queue, and the voice loop decides what that means;
there is no action that maps to anything destructive. Reboot and closing the
player remain spoken commands confirmed out loud, and shutdown — which cannot
be confirmed out loud, see `ShutdownCountdown` — is still only ever *started*
by the voice. The screen's part in it is to draw it and to cancel it.

**Each button then has its own ten-second cooldown, enforced here.** A finger
on a capacitive touchscreen produces repeats — a tap that bounces, an
impatient second press while a page is still loading, a child holding a button
down — and each of those used to be another camera capture, another news
fetch, another launch. The cooldown is per action, so pressing Camera never
disables Weather, and it is on the server rather than only in the page because
the page can be reloaded and the queue cannot tell an accidental repeat from a
deliberate one. The remaining seconds are published so the buttons can show
what they are doing rather than appearing broken.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

# Everything the screen may ask for. A tuple, checked by membership, so adding
# a button is a visible change here as well as in the page.
#
# Note what is absent and why: nothing that powers the machine off, restarts
# it, or closes Kodama-Lite. Reboot and closing the player are `confirm=True`
# in AIA's plugin declarations and are answered out loud before they run — a
# button cannot hold that conversation, so a button does not get to start it.
# Shutdown is answered by a touch instead (`ShutdownCountdown`), and that touch
# is not an action either: it can only ever stop something already counting,
# and it goes to `/api/shutdown` rather than through this queue.
ACTIONS = (
    "talk",         # open the conversation page and start a turn
    "call",         # open the remote video call page
    "camera",       # take a picture and describe it
    "weather",      # today's weather, on its page and briefly out loud
    "news",         # today's local news, on its page and briefly out loud
    "kodama",       # open the music player, or raise the window it already has
    "wake",         # start a turn, as though the wake word had fired
)

# How long a button ignores itself after being pressed. Ten seconds is the
# specification's number and it is generous on purpose: every one of these
# actions takes seconds of real work — a capture and a vision request, a feed
# fetch, a Tauri app cold-starting — and the whole failure being prevented is
# somebody pressing again because nothing has visibly happened yet.
COOLDOWN_S = 10.0

# `wake` is exempt. It is not one of the six page buttons: it is what a touch
# on the screensaver sends, and what the Talk page sends to start listening
# again. Rate-limiting the way a person gets the assistant's attention would
# mean a device that ignores somebody who tried to talk to it twice.
UNTHROTTLED = ("wake",)

# A person can press a button faster than a turn can finish, and a queue that
# absorbed all of it would replay a minute of impatient tapping.
#
# This used to be 2 — "the one being handled, and the one they meant" — and
# that was right when the buttons were four ways of starting the same kind of
# turn. It is wrong now that each one opens a page: pressing Weather, then
# News, then Talk is navigation, not impatience, and at depth 2 the third
# press was silently dropped while its page opened anyway. Measured on the
# device, which is the only reason this changed.
#
# One slot per action instead. The cooldown above is what now does the job
# depth 2 was doing — a repeat of the *same* action inside ten seconds never
# reaches the queue at all — so nothing here can be a burst of one button, and
# the queue's remaining job is only to hold one of each distinct request.
QUEUE_DEPTH = len(ACTIONS)


class UiState:
    """The snapshot the page polls, and the queue the buttons post into."""

    def __init__(self, cooldown_s: float = COOLDOWN_S):
        self._lock = threading.Lock()
        self.cooldown_s = cooldown_s
        #: monotonic time each action was last accepted at.
        self._accepted: dict[str, float] = {}
        self._state: dict = {
            "assistant": "idle",
            "presence": "unknown",
            "screensaver": False,
            "listening_text": "",
            "weather": None,
            "now_playing": None,
            "camera_description": None,
            # Bumped every time a new description lands, so the camera page can
            # tell "the assistant answered again" from "the same answer is
            # still on screen". Comparing the text would treat two identical
            # descriptions of an unchanged room as one event.
            "camera_description_id": 0,
            "kodama_running": False,
            "degraded": [],
            "updated": time.time(),
        }
        self._actions: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)

    # ── the writers' side ────────────────────────────────────────────

    def update(self, **fields) -> None:
        """Merge fields into the snapshot. Cheap enough to call every turn."""
        with self._lock:
            self._state.update(fields)
            self._state["updated"] = time.time()

    def describe_camera(self, text: str | None) -> None:
        """Publish a new camera description, with a new id.

        Separate from `update` because the id must move with the text and
        nothing else may set it — a caller that updated one without the other
        would give the camera page an answer it never displays, or make it
        re-run its fade on an answer it is already showing.
        """
        with self._lock:
            self._state["camera_description"] = text
            if text:
                self._state["camera_description_id"] += 1
            self._state["updated"] = time.time()

    def snapshot(self) -> dict:
        """A copy, for serving. Copied inside the lock and read outside it."""
        with self._lock:
            payload = dict(self._state)
            payload["cooldowns"] = self._remaining()
            return payload

    # ── the screen's side ────────────────────────────────────────────

    def _left(self, action: str) -> float:
        """Exact seconds left on one action. Caller holds the lock.

        Separate from `_remaining` because the rounding there is for a screen
        and this is a decision. Reading the rounded value let a press through
        in the last 0.05 s of the cooldown — `round(0.04, 1)` is `0.0`, which
        is indistinguishable from ready. Small, and exactly the kind of gap a
        double tap lands in.
        """
        at = self._accepted.get(action)
        if at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (time.monotonic() - at))

    def _remaining(self) -> dict:
        """Seconds left on each cooling action, for the screen. Lock held.

        Only the actions actually cooling appear, so the common case — nothing
        pressed recently — adds an empty object to the poll rather than five
        zeroes twice a second.
        """
        remaining = {}
        for action in self._accepted:
            left = self._left(action)
            if left > 0:
                remaining[action] = round(left, 1)
        return remaining

    def cooling(self, action: str) -> float:
        """Seconds until `action` may be pressed again. 0 when it is ready."""
        with self._lock:
            return self._left(action)

    def request(self, action: str) -> bool:
        """A button was pressed. False if it is not real, cooling, or we are busy.

        Three refusals, in the order they are cheapest to decide. Refusing
        rather than blocking: the HTTP handler must answer immediately, and a
        person who taps four times while the assistant is speaking meant it
        once.
        """
        if action not in ACTIONS:
            log.warning("the screen asked for %r, which is not an action", action)
            return False

        with self._lock:
            if action not in UNTHROTTLED:
                left = self._left(action)
                if left > 0:
                    # Debug, not info. This fires on every extra tap of an
                    # impatient press and the whole point is that it is
                    # expected — at info it would be the loudest thing in the
                    # journal on a device somebody is actually using.
                    log.debug("%s is cooling for another %.1fs", action, left)
                    return False
            try:
                self._actions.put_nowait(action)
            except queue.Full:
                log.debug("dropping %r; the assistant is still busy", action)
                return False
            # Marked only once accepted, so a press refused by a full queue
            # does not start a cooldown for work that never happened.
            self._accepted[action] = time.monotonic()

        log.info("screen requested %s", action)
        return True

    def take_action(self) -> str | None:
        """The next pending button press, or None. Never blocks."""
        try:
            return self._actions.get_nowait()
        except queue.Empty:
            return None
