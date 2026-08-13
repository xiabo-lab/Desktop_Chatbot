"""Which screensaver the time of day calls for.

One rule, and the whole of section 24 lives in it:

    07:00 – 21:00     the photo slideshow
    21:01 – 06:59     the clock and the weather

**The night window crosses midnight and that is the only hard part.** The
obvious implementation — `start <= t <= end` — is false for every minute of the
night, because 23:00 is not between 21:01 and 06:59 on a number line. Written
the other way round it is easy: the *day* window never crosses midnight, so the
comparison is a plain one and night is simply everything else. That inversion
is deliberate and is why this module talks about the day window rather than the
night one, even though the night one is the interesting case.

Kept as minutes since midnight rather than as `time` objects. A minute is the
resolution the specification is written in, integers compare without surprises,
and the boundary tests read as the numbers a person would check.

**The Pi's local time, always.** `datetime.now()` with no timezone argument
asks the C library, which asks `/etc/localtime`, which is what `timedatectl`
set — so daylight saving arrives on its own and there is no offset written down
anywhere to go stale. Nothing here makes a network call; a device with no link
still knows whether it is night.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

MINUTES_A_DAY = 24 * 60

#: The specification's schedule, as the fallback for a configuration that does
#: not name one. `07:00` and `21:01` rather than `07:00` and `21:00` because
#: 21:00 itself is a daytime minute — section 24 says so explicitly.
DEFAULT_DAY_START = "07:00"
DEFAULT_NIGHT_START = "21:01"


def _minutes(value: object) -> int | None:
    """`"21:01"` as 1261, or None when it is not a time at all."""
    parts = str(value if value is not None else "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def parse_hhmm(value: object, fallback: str) -> int:
    """`"21:01"` as 1261 minutes past midnight.

    Forgiving on the way in and loud about it: a screensaver that refuses to
    start because somebody typed `7:00` instead of `07:00` would be a worse
    failure than one that quietly understands both. Anything genuinely
    unreadable falls back to the specification's value and says so, because the
    alternative is a device that silently shows the wrong screen at night and
    nothing in the journal explaining why.
    """
    minute = _minutes(value)
    if minute is not None:
        return minute
    if str(value if value is not None else "").strip():
        log.warning("screensaver: %r is not a HH:MM time; using %s",
                    value, fallback)
    return _minutes(fallback) or 0


def format_hhmm(minutes: int) -> str:
    """1261 as `"21:01"`, for the settings page and the log."""
    minutes %= MINUTES_A_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Window:
    """A daily span, in minutes past midnight, that may cross midnight.

    `start` is inclusive and `end` is exclusive, which is what makes the
    boundaries fall where section 24 puts them without a single `+ 1` anywhere:
    a day window of [07:00, 21:01) contains 21:00 and does not contain 21:01.
    """

    start: int
    end: int

    def contains(self, minute: int) -> bool:
        minute %= MINUTES_A_DAY
        if self.start == self.end:
            # A zero-length window would mean "never", and a configuration that
            # says day and night begin at the same moment much more likely
            # means somebody got it wrong. Always, and the caller logs it.
            return True
        if self.start < self.end:
            return self.start <= minute < self.end
        # Crosses midnight: two spans, either side of 00:00.
        return minute >= self.start or minute < self.end

    def __str__(self) -> str:
        return f"{format_hhmm(self.start)}–{format_hhmm(self.end)}"


class ScheduleManager:
    """Day or night, from a clock this object is handed rather than reads.

    The clock is injected for the reason section 30 gives: the boundaries have
    to be tested at 06:59 and 00:00, and moving the Pi's system clock to do it
    would disturb TLS, Tailscale and every certificate on the device. So tests
    pass a `datetime`, and production passes nothing and gets `datetime.now()`.
    """

    def __init__(self, day_start: int, night_start: int):
        self.day_start = day_start % MINUTES_A_DAY
        self.night_start = night_start % MINUTES_A_DAY
        if self.day_start == self.night_start:
            log.warning("screensaver: day and night both start at %s, so the "
                        "night screen will never be shown",
                        format_hhmm(self.day_start))

    @classmethod
    def from_config(cls, cfg) -> "ScheduleManager":
        return cls(parse_hhmm(cfg.day_start, DEFAULT_DAY_START),
                   parse_hhmm(cfg.night_start, DEFAULT_NIGHT_START))

    @property
    def day(self) -> Window:
        """The daytime span. Night is its complement, never stored separately.

        One window and its inverse rather than two windows, so the two cannot
        drift into overlapping or into leaving a minute that belongs to
        neither — which on this device would be a screen showing nothing at
        all for sixty seconds a day.
        """
        return Window(self.day_start, self.night_start)

    def is_day(self, now: datetime | None = None) -> bool:
        now = datetime.now() if now is None else now
        return self.day.contains(now.hour * 60 + now.minute)

    def describe(self) -> dict:
        """For the settings page and `/api/system`."""
        return {
            "day_start": format_hhmm(self.day_start),
            "night_start": format_hhmm(self.night_start),
            "day_window": str(self.day),
            "night_window": str(Window(self.night_start, self.day_start)),
        }
