"""The screensaver schedule, changed from the screen and kept across restarts.

`VolumeControl`'s twin, and deliberately the same shape: apply to the running
object first, persist second, and put the live value back if the write fails.
A device whose screen and whose file disagree is worse than one that refused.

**This reverses a decision, so here is the argument.** The settings page used
to show the schedule read-only, and said why: the times "live in one place in
the YAML so they can be changed without touching the screensaver logic, and a
second way to set them is a second thing that can disagree." That objection is
about a second *store*, and it is right about one. It is not an argument
against a second *editor* of the one store, which this device already has —
`VolumeControl` writes `audio.volume`, and the agent writes settings through
`apply_config`.

Four things keep it honest:

1. **There is still exactly one store.** The deployed `config/aipi5.yaml`. No
   localStorage, no JSON sidecar, nothing with an opinion of its own.
2. **A deploy does not overwrite it.** `scripts/deploy.sh` lists that file as
   protected, so a boundary set from the touchscreen survives.
3. **The write goes through `aipi5/core/yaml_edit.py`**, which keeps the
   comments and the quoting — `21:01` unquoted would be read back as the
   integer 1261.
4. **The page renders the running value, never the file.** Somebody who edits
   the YAML over ssh while the service is up sees the running value, which is
   the true one, and their edit takes effect at the next start. Exactly what
   the volume does today.
"""

from __future__ import annotations

import logging

from aipi5.core import yaml_edit
from aipi5.screensaver.schedule import format_hhmm, parse_hhmm

log = logging.getLogger(__name__)

#: The section these keys live under in the deployed file.
SECTION = "screensaver"


class ScheduleError(ValueError):
    """A value the screen sent that the device will not accept.

    Distinct from `yaml_edit.SettingMissing`, which is a broken installation
    rather than a bad request: this one becomes a sentence in front of
    somebody's finger, so it says what is wrong with what they pressed.
    """


class ScreensaverSettings:
    """Reads the running manager, writes the deployed file."""

    def __init__(self, manager, source=None):
        #: The live `ScreensaverManager`. Changed first, so the screen is
        #: right even in the moment before the file is.
        self.manager = manager
        #: The deployed `config/aipi5.yaml`, or None where there is no file to
        #: write — a test, or a device running on defaults. The change still
        #: applies; it simply does not survive a restart, and `describe` says
        #: so rather than implying it was saved.
        self.source = source

    def describe(self) -> dict:
        payload = dict(self.manager.describe())
        payload["persistent"] = self.source is not None
        payload["day_modes"] = list(self.manager.DAY_MODES)
        payload["night_modes"] = list(self.manager.NIGHT_MODES)
        return payload

    def set(self, day_start=None, night_start=None,
            day_mode=None, night_mode=None) -> dict:
        """Validate, apply, persist. Raises `ScheduleError` on a bad value.

        Every argument is optional and only what was sent is touched, so the
        page can send one field per tap without restating the other three and
        racing itself.
        """
        before = self.manager.describe()
        wanted = {
            "day_start": _minutes(day_start, "the day"),
            "night_start": _minutes(night_start, "the night"),
        }
        day, night = _at(wanted, "day_start", before), _at(wanted, "night_start", before)
        if day == night:
            # `Window.contains` reads this as "always day", and the night
            # screen then never appears at all. `ScheduleManager` only warns;
            # a person pressing a button deserves to be told.
            raise ScheduleError("the day and the night cannot start at the "
                                "same time, or the night screen never shows")

        try:
            self.manager.set_schedule(day_mode=day_mode, night_mode=night_mode,
                                      **wanted)
        except ValueError as exc:
            raise ScheduleError(str(exc)) from exc

        if self.source is None:
            return self.describe()

        changed = {"day_start": _text(wanted["day_start"]),
                   "night_start": _text(wanted["night_start"]),
                   "day_mode": day_mode, "night_mode": night_mode}
        try:
            for key, value in changed.items():
                if value is not None:
                    yaml_edit.write_scalar(self.source, SECTION, key, value)
        except OSError as exc:
            # A change that lasts until the next restart is a misleading
            # success. Put the running schedule back so the screen and the
            # file agree again, and say what happened.
            self._restore(before)
            log.warning("screensaver: could not save the schedule (%s)", exc)
            raise ScheduleError(f"the schedule could not be saved: {exc}") from exc

        return self.describe()

    def _restore(self, before: dict) -> None:
        """Undo a change the file would not accept. Never raises."""
        try:
            self.manager.set_schedule(
                day_start=parse_hhmm(before.get("day_start"), "07:00"),
                night_start=parse_hhmm(before.get("night_start"), "21:01"),
                day_mode=before.get("day_mode"),
                night_mode=before.get("night_mode"))
        except (ValueError, TypeError):
            log.exception("screensaver: could not put the schedule back")


def _minutes(value, which: str):
    """An "HH:MM" from the page as minutes past midnight, or None.

    **Validated rather than forgiven.** `parse_hhmm` substitutes the
    specification's time for anything it cannot read and logs a warning, which
    is right for a config file read at boot and wrong for a button somebody
    just pressed: typing 25:00 must be refused, not silently turned into 07:00.
    """
    if value is None:
        return None
    text = str(value).strip()
    hours, _, rest = text.partition(":")
    try:
        hour, minute = int(hours), int(rest)
    except ValueError:
        raise ScheduleError(f"{which} needs a time like 07:00, not "
                            f"{text!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"there is no such time as {text!r}")
    return hour * 60 + minute


def _at(wanted: dict, key: str, before: dict) -> int:
    """The value after this change, whether or not this change sets it."""
    if wanted.get(key) is not None:
        return wanted[key]
    return parse_hhmm(before.get(key), "07:00" if key == "day_start" else "21:01")


def _text(minutes) -> str | None:
    return None if minutes is None else format_hhmm(minutes)
