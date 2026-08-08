"""Camera Module 3, opened once and shared by both readers.

The camera allows exactly one owner, the same way the microphone does. Two
things in this assistant want frames from it — the person detector, twice a
second forever, and the vision question, once when somebody asks — and if each
opened its own `Picamera2` the second would fail with a device-busy error at
the moment a person spoke to it.

So there is one camera, configured with **two streams**: `main` at the
configured still size for the frame that gets described, and `lores` at
detector resolution for the frame that gets classified locally. libcamera
produces both from the same sensor read, so the detector costs no extra capture
and the still needs no reconfiguration to take. This is the whole reason the
class exists rather than a pair of functions.

**A missing or broken camera is a degraded assistant, never a dead one.**
Section 37 requires that camera failure does not kill the assistant, so every
method here answers with `None` and a log line. `available()` is what the tools
ask before offering to look at anything.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# What the detector is fed. Small on purpose: person detection at 640x480 is
# as accurate as at 1280x720 for a room-sized scene and a quarter of the
# pixels to move across the bus twice a second.
LORES_SIZE = (640, 480)


@dataclass(frozen=True)
class Capture:
    """One still, on disk and ready to send."""

    path: Path
    taken_at: float
    width: int
    height: int

    def as_data_url(self) -> str | None:
        """The image as a `data:` URL, which is how the vision API takes it.

        Read at send time rather than held in memory from capture, because the
        gap between the two is a network request and holding a megabyte of JPEG
        across it buys nothing.
        """
        try:
            encoded = base64.b64encode(self.path.read_bytes()).decode("ascii")
        except OSError as exc:
            log.warning("could not read the capture at %s: %s", self.path, exc)
            return None
        return f"data:image/jpeg;base64,{encoded}"


class Camera:
    """Picamera2, or a clear explanation of why not."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._camera = None
        self._lock = threading.Lock()
        self._error: str | None = None
        self._started = False

        if not cfg.enabled:
            self._error = "disabled in the configuration"
            log.info("camera disabled")
            return

        try:
            cfg.scratch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # /dev/shm not being writable is worth knowing about here rather
            # than on the first capture, but it is not fatal on its own — the
            # detector never writes a file.
            log.warning("cannot create the capture directory %s: %s", cfg.scratch, exc)

    def open(self) -> bool:
        """Start the camera. True if there is one running afterwards.

        Called once at startup so a missing camera is a line in the boot log
        beside the other subsystem checks, rather than something discovered
        when a person asks a question and gets an apology.
        """
        if self._error and not self.cfg.enabled:
            return False
        with self._lock:
            if self._started:
                return True
            try:
                from picamera2 import Picamera2
            except ImportError as exc:
                self._error = f"picamera2 is not installed ({exc})"
                log.warning("camera unavailable: %s", self._error)
                return False

            try:
                camera = Picamera2()
                configuration = camera.create_video_configuration(
                    main={"size": (self.cfg.capture_width, self.cfg.capture_height),
                          "format": "RGB888"},
                    lores={"size": LORES_SIZE, "format": "RGB888"},
                    # The detector reads whichever frame is current rather than
                    # waiting for one, so a deep queue would hand it a frame
                    # from a second ago and call it "now".
                    buffer_count=4,
                )
                camera.configure(configuration)
                camera.options["quality"] = self.cfg.jpeg_quality
                camera.start()
            except Exception as exc:
                # Picamera2 raises a wide variety of things — RuntimeError from
                # libcamera, IndexError when no camera is enumerated at all —
                # and none of them should reach the voice loop.
                self._error = str(exc)
                log.warning("camera would not start: %s", exc)
                self._close_quietly(locals().get("camera"))
                return False

            self._camera = camera
            self._started = True
            self._error = None
            # Auto exposure and auto white balance need a moment of real frames
            # before they settle, and the first capture after a cold start is
            # otherwise dark or green. Paid once, at boot, where nobody is
            # waiting on it.
            time.sleep(self.cfg.warmup_ms / 1000)
            log.info("camera ready (%dx%d still, %dx%d for detection)",
                     self.cfg.capture_width, self.cfg.capture_height, *LORES_SIZE)
            return True

    def available(self) -> bool:
        return self._started

    @property
    def error(self) -> str | None:
        return self._error

    def capture_still(self) -> Capture | None:
        """A fresh frame, written to tmpfs as JPEG.

        Always fresh. Section 19 is explicit that an old image must not be
        reused unless it was asked for, and the reason is that "what do you
        see" asked twice, five minutes apart, in a room where something has
        changed, has two different right answers.
        """
        with self._lock:
            if not self._started or self._camera is None:
                log.info("no camera to capture from (%s)", self._error or "not started")
                return None

            path = self.cfg.scratch / f"{time.strftime('%Y%m%d-%H%M%S')}.jpg"
            try:
                self._camera.capture_file(str(path), format="jpeg")
            except Exception as exc:
                log.warning("capture failed: %s", exc)
                return None

        self._prune()
        log.info("captured %s", path)
        return Capture(path=path, taken_at=time.time(),
                       width=self.cfg.capture_width, height=self.cfg.capture_height)

    def frame(self):
        """The current low-resolution frame, as a numpy array, or None.

        For the detector. Deliberately does not block waiting for a new frame:
        the detector runs on its own schedule and the honest answer to "what
        does the camera see now" is the most recent frame, not the next one.
        """
        with self._lock:
            if not self._started or self._camera is None:
                return None
            try:
                return self._camera.capture_array("lores")
            except Exception as exc:
                log.debug("could not read a detection frame: %s", exc)
                return None

    def _prune(self) -> None:
        """Keep the last few captures and delete the rest.

        These land in /dev/shm, which is RAM. A megabyte per question is
        nothing; a megabyte per question for six months is the whole machine.
        A handful is kept rather than none because when a description is wrong,
        the picture it was wrong about is the only way to find out why.
        """
        try:
            existing = sorted(self.cfg.scratch.glob("*.jpg"))
        except OSError:
            return
        for stale in existing[:-10]:
            try:
                stale.unlink()
            except OSError:
                log.debug("could not prune %s", stale)

    @staticmethod
    def _close_quietly(camera) -> None:
        if camera is None:
            return
        try:
            camera.close()
        except Exception:
            log.debug("closing a half-started camera failed", exc_info=True)

    def describe(self) -> dict:
        """For the settings page."""
        return {
            "enabled": self.cfg.enabled,
            "running": self._started,
            "still": f"{self.cfg.capture_width}x{self.cfg.capture_height}",
            "detection": f"{LORES_SIZE[0]}x{LORES_SIZE[1]}",
            "error": self._error,
        }

    def close(self) -> None:
        with self._lock:
            camera, self._camera = self._camera, None
            self._started = False
        if camera is None:
            return
        try:
            camera.stop()
        except Exception:
            log.debug("stopping the camera failed", exc_info=True)
        self._close_quietly(camera)
