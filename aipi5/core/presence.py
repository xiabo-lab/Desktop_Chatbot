"""Is somebody in the room, and should the screen have gone away.

Two decisions, kept in one place and kept free of any I/O, so the whole of the
behaviour the specification describes in sections 20, 21, 25 and 26 can be
tested on a machine with no camera — which is where it has to be tested,
because reproducing "person walks out of frame for nine seconds and comes back"
by hand on the device is slow and not repeatable.

**Presence is debounced, and asymmetrically.** A detector that is right 95% of
the time still drops a frame every twenty, and a UI wired straight to it would
flick to the screensaver and back while somebody sat still. So arriving needs
`frames_to_appear` consecutive positives and leaving needs
`frames_to_disappear` consecutive negatives, and the two are different numbers
on purpose: walking up to the device should feel immediate, while walking out
of shot for a moment should not take the screen away. Consecutive rather than a
rolling average because that is the property that is actually wanted — "the
detector has agreed with itself N times running" — and because it is the one a
person tuning it on the Pi can reason about from the log.

**The screensaver is a second, slower decision on top of it.** Presence going
away does not raise the screensaver; presence having been away for
`timeout_seconds` does. Presence coming back takes it down at once, with no
timer and nothing to touch — section 26 is explicit that the user must not have
to touch the screen to get out of it.

The clock is passed in. Everything here is about durations, and a class that
reads `time.monotonic()` itself can only be tested by sleeping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class Presence(Enum):
    PERSON_PRESENT = "person_present"
    PERSON_NOT_PRESENT = "person_not_present"
    # Before enough frames have been seen to have an opinion. Distinct from
    # "not present" so that the first seconds after boot do not count towards
    # the screensaver timeout — a device that started with nobody watching
    # should show its UI for a moment, not come up already asleep.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PresenceEvent:
    """What changed, for the log and for anything that wants to react."""

    previous: Presence
    current: Presence
    at: float

    @property
    def arrived(self) -> bool:
        return self.current is Presence.PERSON_PRESENT

    @property
    def left(self) -> bool:
        return self.current is Presence.PERSON_NOT_PRESENT


class PresenceTracker:
    """Consecutive-frame debounce over a noisy person detector.

    `observe(seen, now)` is called once per detector frame and returns a
    `PresenceEvent` on the frames where the answer actually changed, else None.
    Returning the change rather than the state means a caller can react to
    arrival without having to remember what it saw last time.
    """

    def __init__(self, frames_to_appear: int = 2, frames_to_disappear: int = 8):
        if frames_to_appear < 1 or frames_to_disappear < 1:
            raise ValueError("a state change needs at least one frame of evidence")
        self.frames_to_appear = frames_to_appear
        self.frames_to_disappear = frames_to_disappear
        self._state = Presence.UNKNOWN
        # How many frames in a row have agreed with each other, and on what.
        self._streak = 0
        self._streak_of: bool | None = None

    @property
    def state(self) -> Presence:
        return self._state

    @property
    def streak(self) -> int:
        """Consecutive frames agreeing with each other. For the settings page."""
        return self._streak

    def observe(self, seen: bool, now: float | None = None) -> PresenceEvent | None:
        now = time.monotonic() if now is None else now

        if seen is self._streak_of:
            self._streak += 1
        else:
            self._streak_of = seen
            self._streak = 1

        needed = self.frames_to_appear if seen else self.frames_to_disappear
        if self._streak < needed:
            return None

        target = Presence.PERSON_PRESENT if seen else Presence.PERSON_NOT_PRESENT
        if target is self._state:
            # Already there. The streak keeps counting — it is what the
            # settings page shows as confidence — but nothing changed, and a
            # caller must not be told that it did.
            return None

        previous, self._state = self._state, target
        log.info("presence %s -> %s after %d frames",
                 previous.value, target.value, self._streak)
        return PresenceEvent(previous, target, now)

    def reset(self) -> None:
        """Forget everything, as after the detector was restarted.

        Back to UNKNOWN rather than to absent, for the same reason UNKNOWN
        exists: a detector that has just come back has not observed an empty
        room, it has observed nothing.
        """
        self._state = Presence.UNKNOWN
        self._streak = 0
        self._streak_of = None


class ScreensaverPolicy:
    """When the screensaver is up, given presence and a clock.

    Deliberately not a thread and not a timer. It is asked `should_show(now)`
    by whatever is already looping — the UI state publisher — so there is no
    second schedule to reason about, and the answer is a pure function of the
    last presence change and the current time.
    """

    def __init__(self, timeout_seconds: float = 60.0, enabled: bool = True):
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self._empty_since: float | None = None
        self._showing = False

    @property
    def showing(self) -> bool:
        return self._showing

    @property
    def empty_since(self) -> float | None:
        return self._empty_since

    def presence_changed(self, event: PresenceEvent) -> None:
        """Start or cancel the countdown.

        Coming back takes the screensaver down here, immediately, rather than
        waiting for the next `should_show` — the person is standing in front of
        the device and the screen has to be theirs by the time they have
        finished walking up to it.
        """
        if event.arrived:
            self._empty_since = None
            if self._showing:
                log.info("person returned; leaving the screensaver")
            self._showing = False
        elif event.left:
            self._empty_since = event.at

    def should_show(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if not self.enabled:
            # Turning it off must also take down one that is already up,
            # rather than leaving the last screensaver of the session on the
            # display until something else happens to change the state.
            self._showing = False
            return False
        if self._empty_since is None:
            return self._showing
        if not self._showing and now - self._empty_since >= self.timeout_seconds:
            log.info("no person for %.0fs; showing the screensaver",
                     now - self._empty_since)
            self._showing = True
        return self._showing

    def suppress(self, now: float | None = None,
                 person_present: bool = False) -> None:
        """Take the screensaver down, because something happened.

        For the case the timeout cannot see: somebody speaks to the assistant
        from outside the camera's view, or taps the screen. A voice in the room
        is proof of a person in it whatever the detector believes, and
        answering a question onto a screensaver would be answering it into a
        clock face.

        **The countdown restarts rather than stopping**, and that distinction
        is the whole of this method's history. It used to clear `_empty_since`
        outright, which reads as "wait for presence to tell us the room is
        empty again" — except presence had *already* said so, and the tracker
        only reports changes. So a single spoken command in an empty room took
        the screensaver away permanently: verified on the device, still showing
        the full UI to nobody 75 seconds later, with the room empty the whole
        time and no further event coming.

        `person_present` is what decides between the two. Somebody standing in
        front of the camera should not have a countdown running at all — their
        arrival already cleared it — while activity with nobody in view means
        the room is still empty and the clock should come back on schedule.
        """
        now = time.monotonic() if now is None else now
        if self._showing:
            log.info("activity while the screensaver was up; taking it down")
        self._showing = False
        # Restart from this moment when the room is empty; stop entirely when
        # somebody is actually there.
        self._empty_since = None if person_present else now
