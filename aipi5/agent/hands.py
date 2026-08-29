"""The camera, at speed, while the agent has a page on the screen.

Hand control shipped reading `/api/camera/stream`, and the person who tried it
said it was not responsive. It was not, and the reason was measured rather than
guessed -- the preview path was timed inside the assistant:

    wake=0.0  lock=278.1  read=139.9  resize=1.6  encode=1.5  total=421.1 ms

Two findings, and neither was the one assumed. The **encode is free** (1.5 ms),
which is why asking for a smaller width changed nothing. The **read is 138 ms**,
which is two grabs at the 69 ms the Brio falls to in a dim room once its
auto-exposure has halved the sensor rate. And the largest term is not work at
all: **278 ms waiting for the camera lock**, because the presence detector and
the preview each perform their own drain-read and one waits out the other.

2.4 frames a second. A sweep of the hand lasts about a second, so the gesture
reader was deciding from two samples, and mostly declining to.

`Camera` is not at fault -- it is tuned, deliberately and correctly, for a
reader that arrives twice a second and wants the freshest possible frame: one
V4L2 buffer, and every read drains the queue empty and blocks for the next
frame the sensor produces. That is exactly right for presence and exactly wrong
for a hand.

**So this borrows the device instead, exactly as a game does.** The numbers in
`aipi5/motion/camera_lease.py` were taken on this camera and settle it:

    Camera.frame() as the detector uses it      89 ms      11 FPS
    one buffer, continuous reads                67 ms      15 FPS
    two buffers, continuous reads               33 ms      30 FPS

A continuous capture thread on two buffers gives 30 fps *and* fresher frames
than the drain does, because a queue that is always being emptied never holds a
stale one.

Presence detection stops while the lease is held. That is correct rather than
tolerated, and it is the same reasoning the games use: somebody driving a page
with their hand is somebody standing in front of the device, and the last thing
that should happen is the screensaver deciding the room is empty. The
screensaver is held off explicitly here for the same reason -- a person reading
a web page holds still, which is precisely what the idle timer watches for.

**Nothing here decides to take the camera.** It is handed the one fact it acts
on -- whether the agent has a browser open -- by `Housekeeping`, once a second.
A game outranks it: if the Brio is already lent, hand control does without and
says so, rather than taking a camera out of a game somebody is playing.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

#: Who this appears as in `Camera.lend`, in the settings page and in the error
#: a game gets if it tries to start while a hand is driving a page.
BORROWER = "hand control"

#: What the screensaver is told, so `release` cannot take off a hold that
#: belongs to a call or a game.
HOLD = "hand control"

#: The width served to the recogniser. MediaPipe's hand model does not want
#: more, and the encode is the one part of this that was never slow.
PREVIEW_WIDTH = 480

#: How long the feed stays up after the browser closes. Zero would drop the
#: camera between one page and the next -- the agent closing a tab and opening
#: another is a normal thing to ask for, and re-acquiring costs a second or two
#: of the person waving at nothing.
LINGER_S = 8.0


class HandFeed:
    """Holds the camera while the agent's browser is up, and gives it back."""

    def __init__(self, camera, *, screen=None, clock=None):
        self._camera = camera
        self._screen = screen
        self._clock = clock or __import__("time").monotonic
        self._lock = threading.Lock()
        self._lease = None
        self._held_screen = False
        #: When the browser was last seen open, so `LINGER_S` can be measured.
        self._wanted_at = 0.0
        #: Why the feed is not running, for the settings page and the logs.
        self.error = ""
        #: Bumped every time the camera is taken. The page watches it and
        #: reconnects its video stream when it changes, because a stream that
        #: was open across a release is dead and an `<img>` will not say so.
        #: An exact signal, where guessing from the pixels was not: a still
        #: room and a frozen picture look identical to any cheap comparison.
        self.generation = 0

    # ── what Housekeeping calls ─────────────────────────────────────

    def sync(self, browser_open: bool) -> None:
        """One fact in, once a second. Idempotent, and never raises."""
        now = self._clock()
        with self._lock:
            if browser_open:
                self._wanted_at = now
                if self._lease is None:
                    self._start()
                return
            if self._lease is None:
                return
            if now - self._wanted_at < LINGER_S:
                return
            self._stop()

    def close(self) -> None:
        """Give everything back. For shutdown, and safe to call twice."""
        with self._lock:
            self._stop()

    # ── what the HTTP route calls ───────────────────────────────────

    def preview_jpeg(self, width: int = PREVIEW_WIDTH):
        """The newest frame, or None when the feed is not running.

        Cheap by construction: the capture thread has already decoded it, so
        this is a resize and an encode, which together measured under 2 ms.
        """
        lease = self._lease
        if lease is None:
            return None
        try:
            return lease.preview_jpeg(width)
        except Exception as exc:                        # noqa: BLE001
            log.debug("hand feed frame failed: %s", exc)
            return None

    @property
    def active(self) -> bool:
        return self._lease is not None

    def describe(self) -> dict:
        lease = self._lease
        info = {"active": lease is not None, "error": self.error,
                "generation": self.generation}
        if lease is not None:
            try:
                info.update(lease.describe() or {})
            except Exception:                           # noqa: BLE001
                pass
        return info

    # ── the borrowing itself ────────────────────────────────────────

    def _start(self) -> None:
        """Caller holds the lock. Sets `error` rather than raising."""
        from aipi5.motion.camera_lease import CameraLease, CameraLeaseError

        if self._camera is None:
            self.error = "this device has no camera"
            return
        if getattr(self._camera, "lent", False):
            # A game, or a video call. Both outrank a hand: one has somebody
            # playing it and the other has somebody on the other end.
            self.error = "the camera is being used by another feature"
            return

        lease = CameraLease(self._camera, borrower=BORROWER)
        try:
            lease.acquire()
        except CameraLeaseError as exc:
            self.error = str(exc)
            log.info("hand control could not take the camera: %s", exc)
            return
        except Exception as exc:                        # noqa: BLE001
            self.error = f"the camera would not start ({exc})"
            log.warning("hand control could not take the camera: %s", exc)
            return

        self._lease = lease
        self.error = ""
        self.generation += 1
        # The screensaver, for the reason in the module docstring: somebody
        # reading a web page is somebody holding still, and holding still is
        # what the idle timer is watching for.
        if self._screen is not None and not self._held_screen:
            try:
                self._screen.hold(HOLD)
                self._held_screen = True
            except Exception as exc:                    # noqa: BLE001
                log.debug("could not hold the screensaver: %s", exc)
        log.info("hand control has the camera")

    def _stop(self) -> None:
        """Caller holds the lock. Gives the camera back whatever went wrong."""
        lease, self._lease = self._lease, None
        if lease is not None:
            try:
                lease.release()
            except Exception as exc:                    # noqa: BLE001
                log.warning("hand control could not release the camera: %s", exc)
            else:
                log.info("hand control gave the camera back")
        if self._held_screen and self._screen is not None:
            try:
                self._screen.release(HOLD)
            except Exception as exc:                    # noqa: BLE001
                log.debug("could not release the screensaver: %s", exc)
        self._held_screen = False
