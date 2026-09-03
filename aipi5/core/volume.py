"""How loud the device is, and the one place that decides it.

The assistant had no volume control at all. Everything it says goes out at
whatever the sink happens to be set to, which is fine until somebody wants it
quieter and finds that neither the screen nor the agent can do anything about
it -- reported exactly that way: "agent cannot lower the volume".

**A number in the configuration is the right shape for this**, and not only
because it was asked for. The agent's write path is `set_config`, which edits
one key in place, validates the file still loads, restarts the service and
rolls back if it does not -- all of it already built, already approved, already
audited. A volume that lived anywhere else would need a new privileged
operation to change it, which is a new thing that can go wrong for a number
that fits on one line.

**PipeWire, through `wpctl`.** Measured on the device: `wpctl set-volume
@DEFAULT_AUDIO_SINK@ 0.55` moves it and `wpctl get-volume` reads it back, on
the HDMI sink everything already plays through. `amixer` also exists here and
is deliberately not used -- it addresses a card and a control, so it would need
to know which, and `@DEFAULT_AUDIO_SINK@` is the thing that follows the device
if the output ever changes.

Applied at startup and after any change, which needs no watcher: `set_config`
restarts the assistant, so a new number arrives the same way every other
setting does.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path
from aipi5.core import yaml_edit

from aia.plugins.base import CommandSpec, Plugin, Result
from aia.plugins.kodama import parse_level

log = logging.getLogger(__name__)

#: The sink to set. Not a card and not an index -- this follows whatever the
#: device is actually playing through, which is the point.
SINK = "@DEFAULT_AUDIO_SINK@"

#: Long enough for a control that answers instantly, short enough that a
#: wireplumber which has gone away cannot hold up the assistant's start.
TIMEOUT_S = 5.0


def apply(percent: int) -> bool:
    """Set the output volume. True if it took, False and a log line if not.

    Never raises. A device that will not set its volume is a device that is
    slightly too loud, and that must not be a device that will not start.
    """
    percent = max(0, min(100, int(percent)))
    try:
        done = subprocess.run(
            ["wpctl", "set-volume", SINK, f"{percent / 100:.2f}"],
            capture_output=True, text=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        log.warning("volume: wpctl is not installed, so %d%% was not applied",
                    percent)
        return False
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("volume: could not set %d%% (%s)", percent, exc)
        return False
    if done.returncode != 0:
        log.warning("volume: wpctl refused %d%% (%s)", percent,
                    (done.stderr or done.stdout).strip()[:120])
        return False
    # Volume and mute are independent PipeWire properties. A slider moved
    # above zero must recover a sink muted by another application, otherwise
    # it can visibly say 55% while the room remains silent.
    if percent > 0:
        try:
            unmuted = subprocess.run(
                ["wpctl", "set-mute", SINK, "0"], capture_output=True,
                text=True, timeout=TIMEOUT_S)
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            log.warning("volume: set to %d%% but could not unmute it (%s)",
                        percent, exc)
            return False
        if unmuted.returncode != 0:
            log.warning("volume: set to %d%% but could not unmute it (%s)",
                        percent, (unmuted.stderr or unmuted.stdout).strip()[:120])
            return False
    log.info("volume set to %d%%", percent)
    return True


def read() -> int | None:
    """What the sink is at now, 0-100, or None if it cannot be asked.

    For the settings page and for anybody wondering whether the number in the
    file is the number in force.
    """
    try:
        done = subprocess.run(["wpctl", "get-volume", SINK],
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if done.returncode != 0:
        return None
    # "Volume: 0.55", and "Volume: 0.55 [MUTED]" when it is muted.
    for word in done.stdout.split():
        try:
            return max(0, min(100, int(round(float(word) * 100))))
        except ValueError:
            continue
    return None


class VolumeControl:
    """The shared output level used by every application on the device.

    Game sounds and calls come from Chromium, Agent Talk and assistant speech
    come from their speech processes, and Kodama-Lite has its own player. They
    are separate streams, but they all meet at PipeWire's default sink. Setting
    that sink is therefore the one real master control; no per-application
    coordination or process discovery is needed.

    The selected value is also written back to the existing ``audio.volume``
    key. WirePlumber normally remembers the sink level, but AIPI5 deliberately
    applies its configured value at startup, so leaving the file unchanged
    would make a touchscreen adjustment disappear at the next restart.
    """

    def __init__(self, configured: int, source: Path | None = None):
        self.source = Path(source) if source is not None else None
        self._configured = max(0, min(100, int(configured)))
        self._lock = threading.Lock()
        self._error = ""

    def apply_configured(self) -> bool:
        """Put the configured level in force at startup."""
        with self._lock:
            ok = apply(self._configured)
            self._error = "" if ok else "The system audio output is unavailable."
            return ok

    def set(self, percent: int) -> bool:
        """Apply and persist a 0-100 master level, rolling back on failure."""
        percent = max(0, min(100, int(percent)))
        with self._lock:
            previous = read()
            if previous is None:
                previous = self._configured
            if not apply(percent):
                self._error = "The system audio output did not accept the new volume."
                return False
            try:
                if self.source is not None:
                    _persist(self.source, percent)
            except OSError as exc:
                # A change that works until the next reboot is a misleading
                # success. Put the audible state back in step with the file.
                apply(previous)
                self._error = f"The volume could not be saved: {exc}"
                log.warning("volume: could not persist %d%% to %s (%s)",
                            percent, self.source, exc)
                return False
            self._configured = percent
            self._error = ""
            return True

    def describe(self) -> dict:
        """A settings-page snapshot, including a useful degraded state."""
        with self._lock:
            actual = read()
            return {
                "level": self._configured if actual is None else actual,
                "configured": self._configured,
                "available": actual is not None,
                "persistent": self.source is not None,
                "error": self._error,
                "controls": ["Game", "Call", "Kodama-Lite", "Browser",
                             "Agent Talk"],
            }


def _persist(path: Path, percent: int) -> None:
    """Set `audio.volume` in the deployed configuration.

    The line-based edit this used to do by hand now lives in
    `aipi5/core/yaml_edit.py`, because the screensaver schedule became a second
    thing the screen writes and the quoting rule in there is the kind of
    detail that gets fixed once and reintroduced by the copy.

    Still not a YAML round trip, for the reason it never was: dumping the file
    back would discard every comment in it, and the comments are most of the
    file.
    """
    yaml_edit.write_scalar(path, "audio", "volume", percent)


class SystemVolume(Plugin):
    """"Turn the volume down", said out loud, meaning the room.

    The spoken half of the settings page's **All applications** slider, and it
    did not exist. AIA's only volume command belongs to the Kodama-Lite
    plugin -- `volume {level}`, `音量调到{level}` -- and it sets the *music
    player's* own level. So the fast router, which matches before the model is
    ever asked, sent every spoken volume request to the music player: it worked
    while music was playing, changed nothing else in the house, and answered
    "the music player is not running" when it was not. Reported exactly that
    way: the volume only works for Kodama-Lite.

    Typing the same sentence went somewhere else entirely. The compose box does
    not run the router, so it reached the model and the model has
    `set_master_volume`, which moves the PipeWire sink. Two ways of asking, two
    different volumes, and nothing on screen to say which you had reached.

    This is the one that matches the slider. `AIPI5Player` below drops Kodama's
    command so there is no second answer to the same question -- see the note
    there for why that is a removal rather than a tie-break.
    """

    name = "volume"
    description = "The output level every application shares"

    def __init__(self, control: "VolumeControl"):
        self.control = control

    def available(self) -> bool:
        """Always. The same reasoning `KodamaLauncher` gives for its own.

        `main.py` refuses a whole chain when any plugin in it reports itself
        unavailable, and says so as "<description> is not currently running" --
        which for a volume control is both untrue and unhelpful. A device whose
        audio has gone is a device that should say *that*, from the handler,
        where the real reason is known.
        """
        return True

    def commands(self) -> list[CommandSpec]:
        return [
            CommandSpec(
                name="set_volume",
                description="Set the level every application shares, 0-100",
                handler=self.set_level,
                params={"level": "0-100"},
                # Spoken, unlike Kodama's was. The confirmation arrives *at*
                # the new level, which is the only feedback that tells somebody
                # across the room that the thing they asked for happened -- and
                # at zero, silence says it just as well.
                speaks=True,
                # Kodama's phrases, exactly, plus the two spellings people
                # actually used on the device that it never had. Taken over
                # rather than competed with: the router ranks by score and
                # these would have tied, leaving which volume you changed to
                # the order a list was built in.
                phrases={
                    "en": ("volume {level}", "set volume to {level}",
                           "set the volume to {level}",
                           "turn volume to {level}",
                           "turn the volume to {level}",
                           "change the volume to {level}"),
                    "zh": ("音量{level}", "音量调到{level}", "把音量调到{level}",
                           "声音调到{level}", "把声音调到{level}"),
                },
            ),
        ]

    def set_level(self, level: str) -> Result:
        """Set the shared level, and read back what the device actually took.

        `parse_level` is AIA's and handles "30", "thirty percent", "百分之三十"
        and "三十" -- which is what the router hands over, because it captures
        the words as they were said.
        """
        wanted = parse_level(level)
        if wanted is None:
            # Asked rather than guessed. A volume nobody said is not a volume.
            return Result.failed("What volume?", "音量调到多少？")

        if not self.control.set(wanted):
            state = self.control.describe()
            detail = state.get("error") or "the audio output did not accept it"
            log.warning("volume: a spoken request for %d%% failed (%s)",
                        wanted, detail)
            return Result.failed(
                "I could not change the volume just now.",
                "我现在没能调整音量。")

        # What the device has, not what was asked for. `VolumeControl.set`
        # rolls back when it cannot persist, so repeating the request would be
        # the assistant announcing a change that had already been undone.
        actual = self.control.describe()["level"]
        return Result.done(f"Volume {actual} percent.", f"音量百分之{actual}。")
