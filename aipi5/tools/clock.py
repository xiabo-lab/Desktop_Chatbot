"""What time it is, in the language it was asked in.

The smallest tool here and the one most likely to be dismissed as unnecessary.
It is not: a language model has no clock, and one asked what time it is will
either refuse or invent an answer, and the invented one is delivered with the
same confidence as everything else it says. So the time is a tool, fetched from
the device, and the model is only ever handed the answer.

The timezone comes from the configuration rather than from the system, because
the two can genuinely differ — a Pi that has never had its timezone set runs on
UTC and would tell a room in San Jose that it is four in the morning.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# Spoken, not printed. "14:35" is correct and nobody says it; a synthesiser
# reading it aloud produces "fourteen thirty-five", which is not how anyone in
# this house tells the time.
_CN_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class Clock:
    """The device's idea of local time, for one configured zone."""

    def __init__(self, timezone: str = "America/Los_Angeles"):
        self.timezone_name = timezone
        try:
            self.zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A typo in the YAML, or a Pi with no tzdata. Falling back to the
            # system zone is better than raising: every other tool still works
            # and the time is at worst wrong by an offset, which is visible on
            # the screensaver rather than silent.
            log.warning("unknown timezone %r; using the system zone instead", timezone)
            self.zone = None

    def now(self) -> datetime:
        return datetime.now(self.zone) if self.zone else datetime.now()

    def as_dict(self) -> dict:
        """What the model and the UI both receive."""
        moment = self.now()
        return {
            "iso": moment.isoformat(timespec="seconds"),
            "time": moment.strftime("%-I:%M %p").lower() if _supports_dash()
            else moment.strftime("%I:%M %p").lstrip("0").lower(),
            "date": moment.strftime("%A, %B %d, %Y"),
            "timezone": self.timezone_name,
            "hour": moment.hour,
            "minute": moment.minute,
        }

    def speak(self, language: str = "en") -> str:
        """The time as a sentence, in the language of the question."""
        moment = self.now()
        if language == "zh":
            hour = moment.hour % 12 or 12
            part = "上午" if moment.hour < 12 else ("下午" if moment.hour < 18 else "晚上")
            weekday = _CN_WEEKDAYS[moment.weekday()]
            return (f"现在是{part}{hour}点{moment.minute}分，"
                    f"{moment.month}月{moment.day}日，{weekday}。")
        hour = moment.hour % 12 or 12
        meridiem = "AM" if moment.hour < 12 else "PM"
        return (f"It's {hour}:{moment.minute:02d} {meridiem} on "
                f"{moment.strftime('%A, %B')} {moment.day}.")


def _supports_dash() -> bool:
    """Does this platform's strftime accept the `%-I` no-padding flag?

    glibc does, which covers the Pi, and Windows does not — where it raises
    rather than being ignored, so the development machine cannot run the tests
    without this. Asked once and cheaply rather than wrapped in a try at every
    call site.
    """
    try:
        datetime(2000, 1, 2, 3, 4).strftime("%-I")
        return True
    except ValueError:
        return False
