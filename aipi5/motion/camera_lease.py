"""Borrowing the Brio for a game, and giving it back.

The assistant's `Camera` (`aipi5/vision/camera.py`) is tuned for a reader that
arrives twice a second and wants the freshest possible frame: one V4L2 buffer,
and every read drains the queue empty and then blocks for the next frame the
sensor produces. That is exactly right for presence detection and exactly wrong
here, and the measurements say so plainly — on this device, in this room:

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
two, so there is nothing to buy above it.

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

#: Capture size for gameplay. 640x360 is not a compromise here — it is the
#: *exact* size a 16:9 frame is scaled to when it is letterboxed into the pose
#: model's 640x640 square, so capturing at it removes the downscale entirely
#: rather than merely making it cheaper. Section 8: nothing is gained by
#: pushing 1280x720 through a pipeline that will throw three quarters of it
#: away before the accelerator sees a pixel.
GAME_WIDTH, GAME_HEIGHT = 640, 360

#: Two, for the reason in the module docstring. Not one, which halves the
#: frame rate; not four, which is three frames of latency for no frame rate.
GAME_BUFFERS = 2

GAME_FPS = 30

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
                 borrower: str = "an AI Motion game"):
        self._camera = camera
        self._borrower = borrower
        self._width, self._height, self._fps = width, height, fps
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
        #: Smoothed capture rate, for the debug panel.
        self.fps = 0.0
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
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            capture.set(cv2.CAP_PROP_FPS, self._fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, GAME_BUFFERS)

            # Proof, not `isOpened()`. The Brio's metadata node opens perfectly
            # and never yields an image — the same trap `vision/camera.py`
            # documents, and it costs a game that starts and shows no player.
            ok, frame = capture.read()
            if not ok or frame is None or not getattr(frame, "size", 0):
                capture.release()
                last = f"{node} opened but produced no frame"
                continue

            self._device = str(node)
            height, width = frame.shape[:2]
            if (width, height) != (self._width, self._height):
                log.info("AI Motion: asked the camera for %dx%d and got %dx%d",
                         self._width, self._height, width, height)
                self._width, self._height = width, height
            log.info("AI Motion: capturing %dx%d at %d fps on %s (%d buffers)",
                     width, height, self._fps, node, GAME_BUFFERS)
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
            ok, frame = self._capture.read()
            now = time.monotonic()
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
                # BGR to RGB once, here, because every consumer wants RGB and
                # doing it downstream would cost a copy of every frame.
                self._frame = frame[:, :, ::-1]
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
        """The newest frame and when it was captured, or `(None, 0.0)`.

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

            # Back to BGR, because `_run` flipped it to RGB for the model and
            # `imencode` writes whatever order it is given as if it were BGR.
            # Without this the preview is blue people in an orange room, which
            # looks like a broken camera rather than a swapped channel.
            #
            # `cvtColor` rather than `frame[:, :, ::-1]`: the slice is a view
            # with a negative stride, and OpenCV wants a contiguous buffer.
            image = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            height, width = image.shape[:2]
            if width > max_width:
                scale = max_width / width
                image = cv2.resize(image, (max_width, int(height * scale)),
                                   interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".jpg", image,
                                      [cv2.IMWRITE_JPEG_QUALITY, quality])
        except Exception as exc:
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
            "fps": round(self.fps, 1),
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
