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

`IdleHardware` is the third piece and the newest: what the idle screen switches
*off*. The camera is released while the screensaver is up, because a detector
polling an empty room twice a second is the largest thing this device does for
nobody. See `power.py` — including what it costs, which is that waking is now a
touch or a wake word rather than walking up.
"""

from aipi5.screensaver.manager import Mode, ScreensaverManager
from aipi5.screensaver.power import IdleHardware
from aipi5.screensaver.schedule import ScheduleManager, parse_hhmm

__all__ = ["IdleHardware", "Mode", "ScheduleManager", "ScreensaverManager",
           "parse_hhmm"]
