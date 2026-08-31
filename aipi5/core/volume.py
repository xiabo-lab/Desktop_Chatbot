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
import os
import subprocess
import threading
from pathlib import Path

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
    """Atomically replace the existing ``audio.volume`` scalar.

    This intentionally does not round-trip YAML: doing so would discard the
    deployment file's comments and formatting. The key must already exist,
    matching the same conservative rule used by the agent's configuration
    editor.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    in_audio = False
    changed = False
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if content and not content[0].isspace():
            in_audio = stripped == "audio:"
            continue
        if not in_audio or not stripped.startswith("volume:"):
            continue
        before_comment, marker, comment = content.partition("#")
        indent = before_comment[:len(before_comment) - len(before_comment.lstrip())]
        ending = line[len(content):]
        rebuilt = f"{indent}volume: {percent}"
        if marker:
            rebuilt += f"  # {comment.strip()}"
        lines[index] = rebuilt + ending
        changed = True
        break
    if not changed:
        raise OSError(f"there is no audio.volume setting in {path}")

    updated = "".join(lines)
    if updated == text:
        return
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.volume-{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
