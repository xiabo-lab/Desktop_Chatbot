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
there is no action that maps to anything destructive, and shutdown, reboot and
closing the player remain spoken commands that are confirmed out loud.
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
# it, or closes Kodama-Lite. Those are `confirm=True` commands in AIA's plugin
# declarations and they are answered out loud before they run — a button
# cannot hold that conversation, so a button does not get to start it.
ACTIONS = (
    "camera",       # take a picture and describe it
    "weather",      # say the weather
    "news",         # say the local news
    "kodama",       # open the music player
    "wake",         # start a turn, as though the wake word had fired
)

# A person can press a button faster than a turn can finish, and a queue that
# absorbed all of it would replay a minute of impatient tapping. Two deep: the
# one being handled, and the one they meant.
QUEUE_DEPTH = 2


class UiState:
    """The snapshot the page polls, and the queue the buttons post into."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = {
            "assistant": "idle",
            "presence": "unknown",
            "screensaver": False,
            "listening_text": "",
            "weather": None,
            "now_playing": None,
            "camera_description": None,
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

    def snapshot(self) -> dict:
        """A copy, for serving. Copied inside the lock and read outside it."""
        with self._lock:
            return dict(self._state)

    # ── the screen's side ────────────────────────────────────────────

    def request(self, action: str) -> bool:
        """A button was pressed. False if it is not a real action or we are busy.

        Refusing when the queue is full rather than blocking: the HTTP handler
        must answer immediately, and a person who taps four times while the
        assistant is speaking meant it once.
        """
        if action not in ACTIONS:
            log.warning("the screen asked for %r, which is not an action", action)
            return False
        try:
            self._actions.put_nowait(action)
        except queue.Full:
            log.debug("dropping %r; the assistant is still busy", action)
            return False
        log.info("screen requested %s", action)
        return True

    def take_action(self) -> str | None:
        """The next pending button press, or None. Never blocks."""
        try:
            return self._actions.get_nowait()
        except queue.Empty:
            return None
