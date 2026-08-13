"""What the screen shows when nobody is using it.

Two screensavers now rather than one — a photo slideshow through the day and a
clock over the weather at night — and one object that decides which. The split
is section 26's: `ScheduleManager` answers *which*, the existing
`ScreensaverPolicy` in `aipi5/core/presence.py` answers *when*, and
`ScreensaverManager` is the only thing that puts the two together.

Nothing here draws anything. The decision is published on the state poll the
page already makes and the page renders it; that keeps the whole of the
day/night rule testable on a machine with no display, which is where the
awkward cases — 06:59, 21:00, 21:01, midnight — actually get checked.
"""

from aipi5.screensaver.manager import Mode, ScreensaverManager
from aipi5.screensaver.schedule import ScheduleManager, parse_hhmm

__all__ = ["Mode", "ScheduleManager", "ScreensaverManager", "parse_hhmm"]
