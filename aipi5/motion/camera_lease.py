"""Borrowing the Logitech BRIO 4K for a game, and giving it back.

The assistant's `Camera` (`aipi5/vision/camera.py`) is tuned for a reader that
arrives twice a second and wants the freshest possible frame: one V4L2 buffer,
and every read drains the queue empty and then blocks for the next frame the
sensor produces. That is exactly right for presence detection and exactly wrong
here. The original Brio 101 measurements established the buffer design:

    Camera.frame() as the detector uses it      89 ms      11 FPS
    one buffer, continuous reads                67 ms      15 FPS
    two buffers, continuous reads               33 ms      30 FPS

The middle row is the surprising one and it is the whole reason this module
exists. **A single V4L2 buffer halves the frame rate of a continuous reader.**
With one buffer the driver has nowhere to put the next frame while userspace
is holding the only one, so it sits idle until the buffer is requeued and then
waits for the *following* frame start — one wasted frame period, every frame,
forever. Two buffers is enough to fix it completely and is also the shallowest
queue that does, which matters because every extra buffer is another frame of
hand-to-screen latency (section 49). Three and four measured identically to
two, so there is nothing to buy above it. The replacement BRIO 4K adds a
1280x720 MJPEG mode at 90 fps. Capture deliberately runs ahead of the Hailo
pose loop and replaces unconsumed frames, cutting sensor-to-inference age
without ever building a backlog.

**Ownership is `Camera.lend()`/`.reclaim()`, not a second mechanism.** Section
31 wants one explicit owner at a time, and this project already has that,
already handles the borrower who does not let go immediately, and has had it
proven by a year of video calls. A game borrows the device the same way a call
does. The consequence is that person detection stops while a game is running,
which is correct rather than merely tolerable: section 39 requires the
screensaver to stay away during play, so an idle presence detector is exactly
what should happen.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: Verified on the installed BRIO 4K, which offers exactly two modes above 30
#: fps: this one and 1280x720 at 90. MJPEG is essential — neither fits on USB
#: uncompressed, and the camera advertises both as compressed modes only.
GAME_WIDTH, GAME_HEIGHT = 640, 480

#: Two, for the reason in the module docstring. Not one, which halves the
#: frame rate; not four, which is three frames of latency for no frame rate.
GAME_BUFFERS = 2

GAME_FPS = 120

#: The pixel format asked of the driver. MJPG is the only one this camera
#: offers above 30 fps at any useful size, and the JPEG decode it costs is paid
#: on the capture thread rather than on the pose thread — see `_run`. YUYV and
#: NV12 are accepted here so the alternatives can be measured against it
#: (`scripts/bench_motion.py`) rather than argued about.
GAME_FOURCC = "MJPG"

#: How long to wait for the borrowed device to start producing frames before
#: giving up and telling the player. Generous because a UVC camera coming out
#: of the assistant's hands sometimes needs a moment, and bounded because
#: section 41 requires an error on screen rather than a game that never starts.
OPEN_TIMEOUT_S = 6.0

#: How long `frame()` will wait for the capture thread to produce the first
#: frame before answering None.
FIRST_FRAME_S = 3.0


class CameraLeaseError(RuntimeError):
    """The Brio could not be borrowed. The message goes on screen."""


class CameraLease:
    """The Brio, borrowed from the assistant and read at capture rate.

    One capture thread does nothing but read, so the driver's queue never grows
    — which is section 49's requirement, and the reason it is a thread rather
    than a read inside the pose loop. If inference falls behind, the newest
    frame simply replaces the previous one in `_frame` and the old one is
    dropped, un-inferred. A queue here would mean a player whose hands are
    where they were half a second ago, which is worse than a player whose hands
    are merely updated less often.
    """

    def __init__(self, camera, *, width: int = GAME_WIDTH,
                 height: int = GAME_HEIGHT, fps: int = GAME_FPS,
                 fourcc: str = GAME_FOURCC,
                 borrower: str = "an AI Motion game"):
        self._camera = camera
        self._borrower = borrower
        self._width, self._height, self._fps = width, height, fps
        self._fourcc = fourcc
        self._requested_size = (width, height)
        self._capture = None
        self._device = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: The newest frame and when it was captured. Replaced, never queued.
        self._frame = None
        self._frame_at = 0.0
        self._new_frame = threading.Event()
        self._held = False
        self._lost = ""
        self._negotiated_fps = 0.0
        self._format = ""
        #: Smoothed capture rate, for the debug panel.
        self.fps = 0.0
        #: Smoothed time inside one `read()`. Mostly the wait for the next
        #: frame, plus the MJPEG decode when the format is compressed — which
        #: is the only place that decode is visible, and the number that says
        #: whether a compressed mode is paying for itself.
        self.read_s = 0.0
        self.frames = 0
        self.dropped = 0

    # ── acquiring ────────────────────────────────────────────────────

    def acquire(self) -> None:
        """Take the camera. Raises `CameraLeaseError` with a reason if not.

        Section 41's "the Logitech Brio is being used by another feature" is
        this raising, and the message is deliberately specific about *which*
        feature — a person who is on a video call and pressed Play needs to be
        told that, not "camera unavailable".
        """
        if self._held:
            return

        if self._camera is None:
            raise CameraLeaseError("this device has no camera configured")
        if getattr(self._camera, "lent", False):
            raise CameraLeaseError(
                "the Logitech Brio is being used by another feature")

        try:
            import cv2
        except ImportError as exc:
            raise CameraLeaseError(f"OpenCV is not installed ({exc})") from exc

        # Ask the assistant to let go first, and remember the node it was using
        # — that is the one the by-name search already decided was the Brio, so
        # there is no reason to run the search a second time and some risk in
        # doing so if a second webcam has appeared.
        device = (self._camera.describe() or {}).get("device") or ""
        self._camera.lend(self._borrower)
        self._held = True
        log.info("AI Motion: camera acquired: %s",
                 (self._camera.describe() or {}).get("name") or "USB camera")

        try:
            self._capture = self._open(cv2, device)
        except Exception:
            # Anything at all here gives the camera straight back. A game that
            # failed to start must not also cost the assistant its camera for
            # the rest of the session.
            self.release()
            raise

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aipi5-motion-capture",
                                        daemon=True)
        self._thread.start()

    def _open(self, cv2, device: str):
        """Open the node and configure it for gameplay. Never returns None."""
        from aipi5.vision.camera import (_negotiated_fourcc, _negotiated_fps,
                                         _set_dynamic_framerate)

        deadline = time.monotonic() + OPEN_TIMEOUT_S
        last = "no camera node to try"

        candidates = [device] if device else []
        if not candidates:
            # The assistant never had it open, so find it the same way it
            # would. Imported here rather than at module scope so this module
            # stays importable on a machine with no /dev.
            from aipi5.vision.camera import _candidates
            candidates = _candidates(self._camera.cfg)

        for node in candidates:
            if time.monotonic() > deadline:
                break
            try:
                index = int(node)
            except ValueError:
                index = None
            # The BRIO otherwise lengthens exposure in room light and silently
            # turns a negotiated 90 fps stream into roughly 38 fps. Fixed
            # cadence lets auto exposure adjust gain instead. The assistant
            # restores dynamic exposure when it reclaims the camera.
            _set_dynamic_framerate(node, False)
            capture = cv2.VideoCapture(index if index is not None else node,
                                       cv2.CAP_V4L2)
            if not capture.isOpened():
                capture.release()
                last = f"{node} would not open"
                continue

            # MJPEG before the size, as in `vision/camera.py`: the driver's
            # list of sizes depends on the pixel format, so a size asked for
            # while still in YUYV gets clamped to what the uncompressed mode
            # can carry over USB.
            capture.set(cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter_fourcc(*self._fourcc))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            capture.set(cv2.CAP_PROP_FPS, self._fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, GAME_BUFFERS)
            negotiated_fps = _negotiated_fps(capture, cv2)
            negotiated_format = _negotiated_fourcc(capture, cv2)

            # Proof, not `isOpened()`. The Brio's metadata node opens perfectly
            # and never yields an image — the same trap `vision/camera.py`
            # documents, and it costs a game that starts and shows no player.
            ok, frame = capture.read()
            if not ok or frame is None or not getattr(frame, "size", 0):
                capture.release()
                last = f"{node} opened but produced no frame"
                continue

            self._device = str(node)
            self._negotiated_fps = negotiated_fps
            self._format = negotiated_format
            height, width = frame.shape[:2]
            if (width, height) != (self._width, self._height):
                log.info("AI Motion: asked the camera for %dx%d and got %dx%d",
                         self._width, self._height, width, height)
                self._width, self._height = width, height
            if negotiated_fps and negotiated_fps + 0.5 < self._fps:
                log.warning("AI Motion: requested %d fps from %s; the driver "
                            "negotiated %.1f fps instead", self._fps, node,
                            negotiated_fps)
            cadence = negotiated_fps or float(self._fps)
            log.info("AI Motion: capturing %dx%d %s at %.1f fps on %s "
                     "(%d buffers)", width, height,
                     negotiated_format or "unknown format", cadence, node,
                     GAME_BUFFERS)
            return capture

        raise CameraLeaseError(f"the camera would not start — {last}")

    # ── the capture thread ───────────────────────────────────────────

    def _run(self) -> None:
        """Read as fast as the camera delivers. Newest frame wins.

        Deliberately does no work beyond the read and the colour swap. Anything
        else here — a resize, a copy, a callback — is time the driver spends
        with a full queue and nowhere to put the next frame, which is the
        failure this whole module is about.
        """
        smoothing = 0.15
        previous = 0.0
        while not self._stop.is_set():
            began = time.monotonic()
            ok, frame = self._capture.read()
            now = time.monotonic()
            read = now - began
            self.read_s = (read if not self.read_s
                           else self.read_s + smoothing * (read - self.read_s))
            if not ok or frame is None or not getattr(frame, "size", 0):
                # Unplugged mid-game. Section 43: the game pauses and says so,
                # rather than the process ending.
                self._lost = "the camera stopped delivering frames"
                log.warning("AI Motion: lost the camera — %s", self._lost)
                return

            with self._lock:
                # Still set means nobody took the last one, so it is about to
                # be overwritten un-inferred. Counted rather than queued —
                # section 49 — because a climbing number here is the signal
                # that inference is behind capture, and a queue would turn that
                # signal into half a second of lag instead.
                if self._new_frame.is_set():
                    self.dropped += 1
                # **Stored exactly as the driver gave it: BGR, contiguous.**
                # This used to hand out `frame[:, :, ::-1]`, which reads as a
                # free view and is not — a reversed axis has a negative stride,
                # so both consumers had to copy the whole frame before they
                # could touch it, one of them at full resolution on the pose
                # thread. The model's channel swap now happens after the
                # downscale (`geometry.letterbox_image(swap_rb=True)`) and the
                # preview needs no swap at all, because BGR is what `imencode`
                # already writes.
                self._frame = frame
                self._frame_at = now
                self.frames += 1
            self._new_frame.set()

            if previous:
                interval = now - previous
                if interval > 0:
                    instant = 1.0 / interval
                    self.fps = (instant if not self.fps
                                else self.fps + smoothing * (instant - self.fps))
            previous = now

    # ── reading ──────────────────────────────────────────────────────

    def frame(self, wait: bool = True):
        """The newest **BGR** frame and when it was captured, or `(None, 0.0)`.

        Blocks until a frame the caller has not already seen arrives, so the
        pose loop runs at capture rate without polling — and never longer than
        one camera timeout, so a camera that has stopped does not wedge the
        loop that is supposed to notice.
        """
        if wait and not self._new_frame.wait(FIRST_FRAME_S):
            return None, 0.0
        with self._lock:
            frame, at = self._frame, self._frame_at
            self._new_frame.clear()
        return frame, at

    def preview_jpeg(self, max_width: int = 480, quality: int = 65) -> bytes | None:
        """The newest frame as JPEG, for the start screen's preview.

        This exists because the assistant's own `/api/camera/stream` cannot
        serve it: that reads `Camera`, and `Camera` is *lent* for the whole
        time a game is open, so it answers 503. The first version of the start
        screen pointed an `<img>` at it and got a broken image on the one
        screen whose entire job is to show somebody that the camera can see
        them — section 20.

        Encoded from the frame the pose loop is already being handed rather
        than by reading the camera again, so a page watching the preview costs
        one JPEG encode and takes nothing away from inference.
        """
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        try:
            import cv2

            # No colour conversion: the lease keeps the driver's BGR, and BGR
            # is what `imencode` writes. This was a full-frame `cvtColor` back
            # when `_run` handed out RGB, purely to undo it.
            image = frame
            height, width = image.shape[:2]
            if width > max_width:
                scale = max_width / width
                image = cv2.resize(image, (max_width, int(height * scale)),
                                   interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".jpg", image,
                                      [cv2.IMWRITE_JPEG_QUALITY, quality])
        except Exception as exc:  # noqa: BLE001
            log.debug("game preview frame failed: %s", exc)
            return None
        return buffer.tobytes() if ok else None

    @property
    def size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self._lost)

    @property
    def lost(self) -> str:
        """Why the camera went away mid-game, or "" if it has not."""
        return self._lost

    def describe(self) -> dict:
        name = ""
        if self._camera is not None:
            name = (self._camera.describe() or {}).get("name") or ""
        return {
            "name": name or "USB camera",
            "device": self._device,
            "size": f"{self._width}x{self._height}",
            "format": self._format or None,
            "requested_format": self._fourcc,
            "requested": (f"{self._requested_size[0]}x"
                          f"{self._requested_size[1]}@{self._fps}"),
            "negotiated_fps": (round(self._negotiated_fps, 1)
                               if self._negotiated_fps else None),
            "fps": round(self.fps, 1),
            "read_ms": round(self.read_s * 1000, 1),
            "frames": self.frames,
            "dropped": self.dropped,
            "running": self.running,
            "error": self._lost,
        }

    # ── giving it back ───────────────────────────────────────────────

    def release(self) -> None:
        """Stop capturing and hand the Brio back. Idempotent, never raises.

        Section 30 in one method, and the order is what makes it work: the
        thread stops before the handle closes, because a `read()` in flight
        against a released `VideoCapture` is a segfault rather than an
        exception. The assistant is asked for the camera back last, and through
        `reclaim()` rather than `open()`, because `reclaim` is the one that
        knows the first attempt usually fails and arms a retry.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                # Nothing useful to do about it, and saying so beats a silent
                # leak — this would mean a `read()` blocked in the driver.
                log.warning("AI Motion: the capture thread did not stop")

        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                log.debug("releasing the game camera failed", exc_info=True)

        with self._lock:
            self._frame = None
        self._new_frame.clear()

        if self._held:
            self._held = False
            back = False
            try:
                back = self._camera.reclaim()
            except Exception:
                log.warning("AI Motion: handing the camera back failed",
                            exc_info=True)
            log.info("AI Motion: camera released (%s)",
                     "the assistant has it back" if back
                     else "the assistant is still reacquiring it")
