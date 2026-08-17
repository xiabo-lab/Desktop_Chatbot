"""What the idle screen switches off.

The screensaver used to be a decision about pixels. This is the other half of
it: while the screen is away, the camera goes with it.

**Why.** Person detection is the only thing on this device that reads the camera
continuously. Every 500 ms it is a USB transfer off the Brio, a JPEG decode and
an inference on the accelerator — and none of that is worth paying for a room
with nobody in it and a screen nobody is looking at. Measured cost of one cycle
on this hardware: 59–70 ms of camera read plus 40–49 ms of inference.

    screensaver up       -> the detector parks, the Brio is released
    touch or wake word   -> the Brio reopens, the detector looks again
    nobody found in the grace period -> back to the screensaver, camera off

**What it costs is the promise that no touch is necessary.** Section 26 had
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

log = logging.getLogger(__name__)


class IdleHardware:
    """Releases the camera while the screensaver is up, and takes it back after.

    `watcher` is a *callable* returning the `PresenceWatcher`, or None. Late
    binding on purpose: the watcher is built in `Assistant.start()`, after the
    camera has been opened and long after this object exists, and a deployment
    with no accelerator never gets one at all.
    """

    def __init__(self, camera, watcher=lambda: None, *, enabled: bool = True,
                 why: str = "the screen is asleep and nobody is there"):
        self.camera = camera
        self._watcher = watcher
        self.enabled = enabled
        self.why = why
        #: What the screen was doing last time we were told. An edge rather than
        #: a state because both sides cost something — closing a UVC node and
        #: reopening it are each hundreds of milliseconds — and the caller is
        #: `Assistant.publish`, which runs several times a second.
        self._asleep = False

    @property
    def asleep(self) -> bool:
        """Whether the camera is currently released on our account."""
        return self._asleep

    def screen_changed(self, screensaver_showing: bool) -> None:
        """Called with the screensaver's answer, as often as you like.

        Safe from any thread, including the detector's own: `pause` knows not to
        wait for a cycle it is itself inside.
        """
        if not self.enabled:
            return
        if screensaver_showing == self._asleep:
            return
        self._asleep = screensaver_showing
        watcher = self._watcher()

        if screensaver_showing:
            # Parked first. See the module docstring: the alternative is a frame
            # in flight reopening the camera immediately after it was released.
            if watcher is not None:
                watcher.pause(self.why)
            self.camera.sleep(self.why)
        else:
            self.camera.wake()
            if watcher is not None:
                watcher.resume()

    def describe(self) -> dict:
        """For `/api/system`, beside the camera and the detector it moves."""
        return {"enabled": self.enabled, "camera_released": self._asleep}
