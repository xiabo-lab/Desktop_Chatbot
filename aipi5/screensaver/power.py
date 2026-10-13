"""What the staged idle policy switches off.

The screensaver used to be a decision about pixels. Idle now has two hardware
edges around it: release the USB camera after one minute, then replace the
idle screen with a black sleep layer after thirty. Both come back on activity.

**Why.** Person detection is the only thing on this device that reads the camera
continuously. Every 500 ms it is a USB transfer off the Brio, a JPEG decode and
an inference on the accelerator — and none of that is worth paying for a room
with nobody in it and a screen nobody is looking at. Measured cost of one cycle
on this hardware: 59–70 ms of camera read plus 40–49 ms of inference.

    one minute idle      -> the detector parks, the Brio is released
    ten minutes idle     -> the clock or Google Photos appears
    thirty minutes idle  -> the kiosk becomes completely black
    touch or wake word   -> the Brio reopens, the detector looks again
                           and the normal UI returns

The last stage deliberately leaves the Wayland output powered. On the fitted
HDMI panel, ``wlopm --off`` also removes power from the panel's internal USB
hub: ``ILITEK-TOUCH`` disappears from ``/proc/bus/input/devices`` and only
enumerates again after the output is turned on. A device which no longer
exists cannot report the first tap intended to wake it. The black layer costs
the LCD backlight, but it is the only touch-to-wake implementation this
hardware can support without a separate always-powered touch controller.

**What it costs is the no-touch promise.** Section 26 had
presence take the screensaver down by itself, which a closed camera cannot do.
The wake word still can, because the microphone is never released — so a person
who would rather speak than touch is no worse off, and `enabled=False` puts the
old behaviour back in one line of configuration.

**Order matters in both directions, and for the same reason.** The detector is
parked *before* the camera is released, because every read it makes wakes a
sleeping camera — `Camera.frame` is deliberately built that way, so that nothing
else on the device needs to know this state exists — and a frame already in
flight would reopen the node a moment after it closed. `PresenceWatcher.pause`
waits that frame out before returning. Coming back, the camera is opened first,
so the detector's first look finds a device rather than reopening one itself.

Kept out of `aipi5/main.py` so it can be tested at all: `main` imports the whole
assistant, and this is a four-state handoff between two pieces of hardware —
exactly the sort of thing that has to be checkable without either.
"""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger(__name__)


class IdleHardware:
    """Releases the camera at the first idle stage, and takes it back after.

    `watcher` is a *callable* returning the `PresenceWatcher`, or None. Late
    binding on purpose: the watcher is built in `Assistant.start()`, after the
    camera has been opened and long after this object exists, and a deployment
    with no accelerator never gets one at all.
    """

    def __init__(self, camera, watcher=lambda: None, *, enabled: bool = True,
                 why: str = "the room is idle and nobody is there"):
        self.camera = camera
        self._watcher = watcher
        self.enabled = enabled
        self.why = why
        #: What the camera stage was doing last time we were told. An edge
        #: rather than a state because both sides cost something — closing a
        #: UVC node and reopening it are each hundreds of milliseconds — and
        #: the caller is `Assistant.publish`, which runs several times a second.
        self._asleep = False

    @property
    def asleep(self) -> bool:
        """Whether the camera is currently released on our account."""
        return self._asleep

    def camera_changed(self, should_release: bool) -> None:
        """Called with the camera idle-stage answer, as often as you like.

        Safe from any thread, including the detector's own: `pause` knows not to
        wait for a cycle it is itself inside.
        """
        if not self.enabled:
            return
        if should_release == self._asleep:
            return
        self._asleep = should_release
        watcher = self._watcher()

        if should_release:
            # Parked first. See the module docstring: the alternative is a frame
            # in flight reopening the camera immediately after it was released.
            if watcher is not None:
                watcher.pause(self.why)
            self.camera.sleep(self.why)
        else:
            self.camera.wake()
            if watcher is not None:
                watcher.resume()

    def screen_changed(self, screensaver_showing: bool) -> None:
        """Compatibility alias for callers from before idle became staged."""
        self.camera_changed(screensaver_showing)

    def describe(self) -> dict:
        """For `/api/system`, beside the camera and the detector it moves."""
        return {"enabled": self.enabled, "camera_released": self._asleep}


class DisplayPower:
    """Own the final, black idle stage while keeping touch input alive.

    The name is retained because it is part of `/api/system` and the config's
    deployed vocabulary. ``off`` now means *visually asleep*: Chromium draws
    black over the whole panel and consumes the first wake touch. The physical
    Wayland output stays on because this panel disconnects its USB touchscreen
    when output power is removed.

    ``wlopm --on`` remains here as recovery. A device upgraded while its old
    version left the output off must light up once, and every wake edge ensures
    an external command has not powered it down behind us. No path in this
    class sends ``--off``.

    ``runner`` is injectable because a development machine and the unit suite
    have no Wayland compositor. It follows ``subprocess.run``'s signature.
    """

    def __init__(self, *, enabled: bool = True, command: str = "wlopm",
                 runner=subprocess.run):
        self.enabled = enabled
        self.command = command
        self._runner = runner
        self._off: bool | None = None
        self._wanted_off: bool | None = None
        self._error = ""
        self._lock = threading.Lock()

    @property
    def off(self) -> bool:
        return self._off is True

    def screen_changed(self, should_turn_off: bool) -> None:
        """Publish a black-sleep edge and keep the physical output awake."""
        if not self.enabled:
            return
        with self._lock:
            if should_turn_off == self._wanted_off:
                return
            first = self._wanted_off is None
            self._wanted_off = should_turn_off
            self._off = should_turn_off

            # Entering black sleep needs no hardware command; the page already
            # owns that layer. On the first reconcile we still force the
            # output on, even if the machine booted after thirty idle minutes,
            # to recover a panel left powered down by the previous release.
            if should_turn_off and not first:
                self._error = ""
                log.info("display entered touch-wake black sleep")
                return

            try:
                result = self._runner(
                    [self.command, "--on", "*"],
                    capture_output=True, text=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                self._error = str(exc)
                log.warning("could not ensure the display is on: %s", exc)
                return

            if result.returncode:
                detail = (result.stderr or result.stdout or
                          f"{self.command} exited {result.returncode}").strip()
                self._error = detail
                log.warning("could not ensure the display is on: %s", detail)
                return

            self._error = ""
            log.info("display %s; physical output kept on for touch wake",
                     "entered black sleep" if should_turn_off else "awake")

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "off": self.off,
            "requested_off": self._wanted_off is True,
            "mode": "touch-wake-black",
            "command": self.command,
            "error": self._error or None,
        }
