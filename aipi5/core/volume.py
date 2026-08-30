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
            return int(round(float(word) * 100))
        except ValueError:
            continue
    return None
