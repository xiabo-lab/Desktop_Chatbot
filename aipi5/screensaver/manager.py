"""The one object that decides what the idle screen shows.

Section 26 asks for a single `ScreensaverManager` and section 27 asks for the
behaviour to be an explicit state. Both are here, and the reason they are worth
insisting on is the failure they prevent: two screensavers that each decide for
themselves whether it is their turn will, at 21:01, both believe it is — and
what reaches the display is whichever one's timer happened to fire last.

    ACTIVE_UI ──1 min──> camera released
              ─10 min──> DAY_PHOTOS / NIGHT_WEATHER
              ─30 min──> monitor output off

    DAY_PHOTOS   ──activity──> ACTIVE_UI
    NIGHT_WEATHER──activity──> ACTIVE_UI
    DAY_PHOTOS   ──21:01─────> NIGHT_WEATHER
    NIGHT_WEATHER──07:00─────> DAY_PHOTOS

**The mode is chosen every time it is asked for, not on a boundary event.**
That is section 23's reboot requirement and it comes for free from asking the
clock rather than remembering an answer: a Pi that boots at 23:00 has never
seen 21:01 go past, and a manager that waited for the transition would show
photographs to a dark room until morning.

**It composes rather than replaces.** `ScreensaverPolicy` in
`aipi5/core/presence.py` already owns the idle decision, is already driven by
the presence detector, and is already tested; section 3 says not to grow a
second idle timer beside it, so this holds one and asks it. Everything about
*when* the screen goes away is still over there.

Nothing here draws, sleeps, or starts a thread. It is asked for its state by
whatever is already looping — `Assistant.publish` — for the same reason
`ScreensaverPolicy` is: one schedule in the process, not three.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum

from aipi5.screensaver.schedule import ScheduleManager

log = logging.getLogger(__name__)


class Mode(Enum):
    """What the screen is showing. Section 27's states, by their names.

    `ACTIVE_UI` is the absence of a screensaver rather than a screen of its
    own — the normal assistant UI is underneath the whole time, which is what
    makes waking instant and is why a touch never has to load anything.
    """

    ACTIVE_UI = "active_ui"
    DAY_PHOTOS = "day_photos"
    NIGHT_WEATHER = "night_weather"

    @property
    def is_screensaver(self) -> bool:
        return self is not Mode.ACTIVE_UI


#: What a mode is called on the wire and in the journal. Section 25's example
#: log line uses these spellings, so they are what the page and `/api/system`
#: carry too — one vocabulary, so grepping the journal for what the screen was
#: doing finds it.
WIRE = {
    Mode.ACTIVE_UI: "active",
    Mode.DAY_PHOTOS: "day-photos",
    Mode.NIGHT_WEATHER: "night-weather",
}


class ScreensaverManager:
    """Idle policy plus schedule, and one answer.

    `policy` is the existing `ScreensaverPolicy`; `schedule` is a
    `ScheduleManager`. `photos_ready` is a callable rather than a flag because
    it changes while the device runs — an unauthorised Google account, an empty
    cache, a sync that has not finished — and a daytime screensaver that fell
    back to the clock at boot must start showing photographs the moment there
    are some, without a restart.
    """

    def __init__(self, policy, schedule: ScheduleManager, *,
                 photos_ready=lambda: False, day_mode: str = "photos",
                 night_mode: str = "weather", timezone: str = ""):
        self.policy = policy
        self.schedule = schedule
        self._photos_ready = photos_ready
        #: Only ever reported, never used to compute anything — the clock is
        #: the C library's and the C library reads `/etc/localtime`. Carried
        #: here because "the night screen came on an hour early" is a timezone
        #: question every time, and section 25 asks for it to be visible.
        self.timezone = timezone
        #: `photos` or `clock`. A deployment that wants the clock all day —
        #: no Google account, or simply a preference — sets this and never
        #: touches the rest of the feature.
        self.day_mode = day_mode
        self.night_mode = night_mode
        self._mode = Mode.ACTIVE_UI
        #: Set while something outranks the screensaver entirely: a call, a
        #: shutdown countdown. Section 19's priority list, as one boolean —
        #: the ordering it describes has exactly two outcomes at this level,
        #: "the screensaver may show" and "it may not".
        self._held_by = ""

    # ── the decision ─────────────────────────────────────────────────

    def mode(self, now: datetime | None = None,
             monotonic: float | None = None) -> Mode:
        """What should be on the screen, right now.

        Two clocks, on purpose. `now` is wall-clock local time and answers
        *which* screensaver; `monotonic` is elapsed time and answers *whether*
        there should be one. Using wall-clock for the timeout would make an NTP
        step at boot — which this device does have, it has no RTC — either fire
        the screensaver instantly or postpone it by however far the clock
        jumped.
        """
        if self._held_by:
            return self._enter(Mode.ACTIVE_UI)
        if not self.policy.should_show(monotonic):
            return self._enter(Mode.ACTIVE_UI)
        return self._enter(self._scheduled(now))

    def _scheduled(self, now: datetime | None) -> Mode:
        """The screensaver the clock calls for, ignoring whether one is due."""
        if self.schedule.is_day(now):
            if self.day_mode == "photos" and self._photos_ready():
                return Mode.DAY_PHOTOS
            # No photographs to show. The clock is the fallback rather than an
            # error screen — section 18 — and it is the *night* screen drawn in
            # the day, which is the right choice because it is the only other
            # thing this device knows how to put on an idle display.
            return Mode.NIGHT_WEATHER
        if self.night_mode == "photos" and self._photos_ready():
            return Mode.DAY_PHOTOS
        return Mode.NIGHT_WEATHER

    def _enter(self, mode: Mode) -> Mode:
        if mode is not self._mode:
            log.info("[screensaver] %s -> %s", WIRE[self._mode], WIRE[mode])
            self._mode = mode
        return mode

    # ── what outranks it ─────────────────────────────────────────────

    def hold(self, why: str) -> None:
        """Something more important than an idle screen is happening.

        A call, ringing or connected, is the case section 19 names and the one
        this exists for. Idempotent: it is called from the call reconciler,
        which runs on every poll of a live call.
        """
        if self._held_by == why:
            return
        if not self._held_by:
            log.info("[screensaver] held off by %s", why)
        self._held_by = why

    def release(self, why: str = "") -> None:
        """The interruption is over; the idle countdown applies again.

        `why` guards against the release that belongs to something else — a
        finished call must not take the screensaver off hold for a shutdown
        countdown still on screen.
        """
        if not self._held_by:
            return
        if why and why != self._held_by:
            return
        log.info("[screensaver] no longer held off by %s", self._held_by)
        self._held_by = ""

    @property
    def held_by(self) -> str:
        return self._held_by

    # ── publishing ───────────────────────────────────────────────────

    def snapshot(self, now: datetime | None = None,
                 monotonic: float | None = None,
                 display_attention: bool = False) -> dict:
        """What goes on the state poll the page makes twice a second.

        `showing` stays a plain boolean and keeps its old name because that is
        what the page has always read; `mode` is the new half. Sending both
        rather than replacing one with the other means a page and a server that
        are briefly out of step during a deploy still agree about the only
        thing that matters, which is whether the screen has gone away.
        """
        mode = self.mode(now, monotonic)
        held = bool(self._held_by)
        return {
            "showing": mode.is_screensaver,
            "mode": WIRE[mode],
            "held_by": self._held_by,
            # The hardware stages share the policy's one idle origin but are
            # suppressed by anything that holds off the idle screen (a game,
            # call, photo picker, or shutdown countdown).
            "camera_idle": (not held
                            and self.policy.should_release_camera(monotonic)),
            # `display_attention` is deliberately narrower than a hold. An
            # authenticated incoming call must light the panel while it is
            # ringing, but must not reopen the camera before the call is
            # answered. A hold would suppress both hardware stages.
            "display_off": (not held and not display_attention
                            and self.policy.should_power_off_display(monotonic)),
        }

    #: What each half of the day may show. Two, not three: `clock` and
    #: `weather` are the same screen — `_scheduled` only ever branches on
    #: `photos` — so offering both would be offering a choice the device does
    #: not make. The spellings differ because the configuration has always
    #: used `day_mode: photos|clock` and `night_mode: weather|clock|photos`,
    #: and changing a deployed file's vocabulary to tidy an inconsistency is a
    #: worse trade than carrying it.
    DAY_MODES = ("photos", "clock")
    NIGHT_MODES = ("photos", "weather")

    def set_schedule(self, day_start: int | None = None,
                     night_start: int | None = None,
                     day_mode: str | None = None,
                     night_mode: str | None = None) -> dict:
        """Move the boundaries or change what each half shows. Returns
        `describe()`.

        **No restart and no watcher.** `mode()` re-reads the schedule every
        time it is called and `Assistant.publish` calls it twice a second, so
        a change here is on the screen within half a second of the button.

        Times arrive already parsed, in minutes; modes are validated against
        the two tuples above. Whoever calls this is responsible for writing the
        value to the configuration afterwards — see
        `aipi5/core/screen_settings.py`, which does both and rolls this back if
        the write fails.
        """
        if day_mode is not None:
            if day_mode not in self.DAY_MODES:
                raise ValueError(f"the day screen cannot be {day_mode!r}")
            self.day_mode = day_mode
        if night_mode is not None:
            if night_mode not in self.NIGHT_MODES:
                raise ValueError(f"the night screen cannot be {night_mode!r}")
            self.night_mode = night_mode
        self.schedule.set(day_start, night_start)
        log.info("[screensaver] schedule now %s day=%s, %s night=%s",
                 self.schedule.day, self.day_mode,
                 self.schedule.describe()["night_window"], self.night_mode)
        return self.describe()

    def describe(self) -> dict:
        """`/api/system`, for the settings page."""
        payload = {
            "enabled": self.policy.enabled,
            "camera_timeout_s": self.policy.camera_timeout_seconds,
            "timeout_s": self.policy.timeout_seconds,
            "display_off_s": self.policy.display_off_seconds,
            # The second countdown: how long the screen stays up after a touch
            # woke it and the camera has not found anybody yet. Reported because
            # "it went dark while I was reading it" is a question about this
            # number and nothing else. See `ScreensaverPolicy`.
            "wake_grace_s": self.policy.wake_grace_seconds,
            "showing": self._mode.is_screensaver,
            "mode": WIRE[self._mode],
            "day_mode": self.day_mode,
            "night_mode": self.night_mode,
            "held_by": self._held_by,
            "photos_ready": bool(self._photos_ready()),
            "timezone": self.timezone,
            "local_time": datetime.now().strftime("%H:%M"),
        }
        payload.update(self.schedule.describe())
        return payload

    def log_startup(self, timezone: str = "", now: datetime | None = None) -> None:
        """Section 25's three lines, at boot.

        Written out in full rather than folded into one line because this is
        what somebody greps for at 23:00 when the screen is showing the wrong
        thing, and the three facts they need are the timezone the Pi believes
        it is in, the time it believes it is, and the conclusion it drew. Any
        two of those without the third leaves the question open.
        """
        now = datetime.now() if now is None else now
        scheduled = self._scheduled(now)
        log.info("[screensaver] timezone=%s", timezone or self.timezone or "unset")
        log.info("[screensaver] local_time=%s", now.strftime("%H:%M"))
        log.info("[screensaver] mode=%s", WIRE[scheduled])
