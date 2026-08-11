"""The assistant's voice outranks everything else in the room.

AIA already knows how to get the music out of the way: `aia.audio.ducking.Ducker`
pauses every MPRIS player that is currently playing and resumes exactly those
afterwards. It pauses rather than mutes, deliberately — a muted song keeps
playing and loses the seconds it was silent for, while a paused one resumes on
the bar it stopped at. Nothing here re-implements any of that.

What this adds is **re-entrancy**, and it exists because the obvious fix to
"the assistant talks over the music" introduces a subtler bug than the one it
fixes. The voice path already ducks around a whole turn: pause, listen,
think, speak, restore. Making the button and page paths duck around their own
speech means two ducks can nest — a button pressed mid-turn, a page that speaks
while the voice loop is still holding the floor. `Ducker.duck()` begins with
`self._paused = []` and re-scans the bus, so the inner call finds nothing
playing (the outer one already paused it), remembers nothing, and the outer
call's memory of what to resume is gone with it. The music never comes back,
and it never comes back *silently* — no error, no log line, just a device that
stopped playing music at some point and nobody can say when.

So: one lock, one depth counter, and the rule that only the outermost holder
touches the bus. Everything that speaks acquires this; the innermost speaker
does not need to know whether it is innermost.

Used as a context manager, because the property that matters is that the music
comes back even when the thing in the middle raised:

    with assistant.audio.priority():
        speaker.say(reply, language)
"""

from __future__ import annotations

import contextlib
import logging
import threading

log = logging.getLogger(__name__)


class AudioPriority:
    """Duck/restore around anything that makes the assistant's own noise.

    Wraps one `Ducker`. Not a subclass: the wrapped object is AIA's and the
    relationship here is "uses", not "is a" — this adds a policy about *when*
    ducking happens, and none about how.
    """

    def __init__(self, ducker):
        self._ducker = ducker
        # Guards the depth and serialises the playerctl calls. Held across the
        # duck and the restore, both of which shell out — but never across the
        # speech itself, which is the long part. See `priority()`.
        self._lock = threading.Lock()
        self._depth = 0
        self._ducked = False

    @contextlib.contextmanager
    def priority(self):
        """Hold the floor. The music comes back when the last holder lets go.

        Re-entrant across threads as well as within one, which is the case that
        actually happens here: the voice loop holds this for a whole turn while
        the presence watcher and the HTTP handler run in their own threads.
        """
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def acquire(self) -> None:
        """Take the floor, ducking if this is the outermost holder."""
        with self._lock:
            self._depth += 1
            if self._depth > 1:
                return
            # `duck()` answers whether it paused anything. Remembered rather
            # than recomputed, so `release()` does not ask the bus a second
            # time about players it already knows it did not touch.
            self._ducked = self._ducker.duck()

    def release(self) -> None:
        """Give it up, restoring if this was the outermost holder."""
        with self._lock:
            if self._depth == 0:
                # Nothing is broken by this and something is wrong upstream of
                # it, so it is worth a line rather than an exception on a path
                # whose whole job is to be the thing that cannot fail.
                log.warning("audio priority released more times than acquired")
                return
            self._depth -= 1
            if self._depth or not self._ducked:
                return
            self._ducker.restore()
            self._ducked = False

    def forget(self) -> None:
        """Do not resume when the floor is given up.

        For the commands that stopped the music on purpose. Everything the
        `Ducker` remembers is dropped, so the outermost release becomes a no-op
        — and the depth is left alone, because the callers still nested and
        still have to unwind.
        """
        with self._lock:
            self._ducker.forget()
            self._ducked = False

    @property
    def held(self) -> bool:
        """Whether anything currently holds the floor. For the settings page."""
        with self._lock:
            return self._depth > 0

    @property
    def ducked(self) -> bool:
        """Whether a player was actually paused, as opposed to none playing.

        The voice loop drains the microphone on this rather than on `held`,
        because what the drain is for is the buffered moment of *music* between
        the wake word firing and the pause taking effect. Draining when nothing
        was playing throws away the beginning of the command for no reason.
        """
        with self._lock:
            return self._ducked

    def describe(self) -> dict:
        with self._lock:
            return {"held_by": self._depth, "ducked": self._ducked}
