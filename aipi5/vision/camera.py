"""The Logitech Brio 101, opened once and shared by both readers.

The camera allows exactly one owner, the same way the microphone does. Two
things in this assistant want frames from it — the person detector, twice a
second forever, and the vision question, once when somebody asks — and if each
opened its own handle on `/dev/video0` the second would fail with a device-busy
error at the moment a person spoke to it. So there is one `Camera`, one V4L2
handle inside it, and a lock. That is the whole reason the class exists rather
than a pair of functions.

**This is a USB camera now, not the CSI one.** The Camera Module 3 was replaced
with a Brio 101, and the difference is not only which library opens it:

* *No two streams.* picamera2 could be configured with a `main` still stream
  and a `lores` detection stream that libcamera produced from one sensor read,
  so the detector cost no extra capture. UVC gives one stream at one size.
  There is one frame here and both readers get it — the detector resizes it to
  its model's input anyway (`person_detection._resize`), so the second stream
  was never buying it anything except pixels it immediately threw away.
* *Frames queue up.* picamera2 handed back the current frame. V4L2 hands back
  the *oldest* buffer the driver has filled, and at 30 fps against a detector
  that looks twice a second that is a frame from up to a tenth of a second ago
  — or several seconds ago, if the queue is deep and nobody drains it. See
  `_read` for what is done about it, because "the camera saw a person" arriving
  four seconds late is a screensaver that lifts after somebody has left.
* *It is MJPEG off the wire and JPEG onto the disk*, with a decode in between
  that the CSI path did not have. Cheap on a Pi 5 at two frames a second, and
  the reason OpenCV is now a dependency where picamera2 was.

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

# Where V4L2 devices appear, and where their human-readable names do. Both are
# Linux-only and neither is touched off the Pi — `_candidates` degrades to the
# configured device, which is how these modules stay importable on the
# development machine the tests run on.
DEV = Path("/dev")
SYSFS = Path("/sys/class/video4linux")

# How many buffers to grab before decoding one, when the driver will not say
# how deep its queue is. Four is what OpenCV's V4L2 backend allocates by
# default; `_read` prefers the number the device actually reports.
DEFAULT_DRAIN = 5

# Bounds on that number. The floor is 2 because one grab can only ever return
# the buffer that was already sitting there — see `_read`. The ceiling is a
# guard against a driver reporting something absurd and turning every capture
# into a multi-second wait.
MIN_DRAIN, MAX_DRAIN = 2, 8

# How long the whole device search may take before the assistant gives up and
# starts without a camera. See `open()`: this is a bound on how long a missing
# camera can keep the microphone from coming up, not a target. The first
# candidate is always tried in full, however long it takes, because a bound
# that can refuse the likeliest camera is worse than no bound.
SEARCH_BUDGET_S = 12.0

# What the camera page's live preview is sent. Smaller and cheaper than a
# still on purpose — it is drawn at roughly half a 1280 px panel and replaced
# several times a second, so pixels beyond this are encode time spent on a
# frame nobody will look at for more than a fraction of a second.
PREVIEW_WIDTH = 640

# How long to keep trying to take the camera back after a video call, and how
# often. Both measured against the thing that actually happens: the browser is
# still holding the node when the call ends, and lets go a moment later.
#
# The window is generous because the cost of giving up too early is the whole
# session — no person detection, no screensaver, no camera page — while the
# cost of trying for another minute is one `open()` every two seconds against a
# device that is nearly always there. Bounded rather than endless so an
# unplugged camera produces one error and not a warning every two seconds until
# somebody notices.
RECLAIM_WINDOW_S = 60.0
RECLAIM_RETRY_S = 2.0
PREVIEW_QUALITY = 70


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


def _attribute(node: Path, name: str) -> str:
    """One sysfs attribute of a video node, or "" if it is not readable."""
    try:
        return (SYSFS / node.name / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sysfs_name(node: Path) -> str:
    """What the kernel calls this video node, e.g. "Brio 101"."""
    return _attribute(node, "name")


def _driver(node: Path) -> str:
    """Which kernel driver owns it — "uvcvideo" for any USB webcam."""
    try:
        return (SYSFS / node.name / "device" / "driver").resolve().name
    except OSError:
        return ""


def _rank(node: Path, hint: str) -> tuple:
    """Sort key: the likeliest camera first.

    Three fields, in the order they are trusted. The driver comes first because
    it is the only one of the three that is a fact about what the node *is*;
    the name is a product string, and the index is a convention.
    """
    return (
        # `uvcvideo` is every USB webcam and nothing else on this Pi, where the
        # other eighteen nodes belong to the ISP and the HEVC decoder.
        0 if _driver(node) == "uvcvideo" else 1,
        # Then the configured product name, which is what tells two webcams
        # apart once somebody plugs in a second one.
        0 if hint and hint in _sysfs_name(node).lower() else 1,
        # Then the node's own index within its device: UVC gives 0 to the
        # capture node and 1 to the metadata node, and the metadata node opens
        # perfectly while never producing an image.
        _to_int(_attribute(node, "index"), 99),
        _to_int(node.name[len("video"):], 999),
    )


def _to_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _candidates(cfg) -> list[str]:
    """Which device nodes to try, best first.

    A configured `device` is taken literally and is the only thing tried — a
    deployment that names a node and then silently gets a different camera is
    the failure that is discovered from a description of the wrong room.

    Otherwise the nodes are ranked, because `/dev/video0` is not an identity.
    This Pi has twenty video nodes: two are the Brio's, and the other eighteen
    are the ISP and the HEVC decoder, which are always present and are never
    the camera. The Brio's own two are a capture node and a metadata node, and
    the metadata node opens cleanly and never yields an image. Which is which
    moves with what else was plugged in at boot, so this ranks on the driver,
    the product name and the UVC node index rather than trusting the number.

    The ISP and decoder nodes are then dropped rather than merely ranked last,
    and that is a measurement rather than a preference: refusing a node that is
    not a camera costs about two seconds, this runs before the microphone is
    up, and eighteen of them came to 81 s of an assistant that could not hear.
    Nothing is lost by dropping them, because a memory-to-memory ISP node
    cannot produce a camera frame however long it is asked.

    A node is worth trying if its driver is `uvcvideo` — every USB webcam and
    nothing else here — or if its name matches the hint, which is what keeps a
    camera on some other driver reachable by naming it in the configuration.
    Neither test involves opening anything.
    """
    wanted = str(cfg.device or "").strip()
    if wanted and wanted.lower() != "auto":
        return [wanted]

    try:
        nodes = list(DEV.glob("video*"))
    except OSError as exc:
        log.warning("cannot enumerate video devices: %s", exc)
        return []

    hint = str(cfg.name_hint or "").strip().lower()
    worth_trying = [node for node in nodes
                    if _driver(node) == "uvcvideo"
                    or (hint and hint in _sysfs_name(node).lower())]
    if not worth_trying:
        # The whole list, because this is the message somebody debugs from and
        # the answer is usually either a USB cable or a `name_hint` that no
        # longer matches something in it.
        log.warning("no USB camera among %d video nodes: %s", len(nodes),
                    ", ".join(f"{n.name} ({_sysfs_name(n) or 'unnamed'})"
                              for n in sorted(nodes)) or "none")
        return []

    worth_trying.sort(key=lambda node: _rank(node, hint))
    if hint and not any(hint in _sysfs_name(n).lower() for n in worth_trying):
        log.info("no camera is named %r; trying %d USB video node(s)",
                 cfg.name_hint, len(worth_trying))
    return [str(n) for n in worth_trying]


class Camera:
    """One UVC camera, or a clear explanation of why not."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._capture = None
        self._lock = threading.Lock()
        self._error: str | None = None
        self._started = False
        self._device = ""
        self._name = ""
        self._size = (cfg.capture_width, cfg.capture_height)
        self._drain = DEFAULT_DRAIN
        #: Who currently has the device instead of us, or "". While this is
        #: set, `open()` refuses — see `lend`.
        self._lent = ""
        #: Who we are trying to take it back from, or "". See `reclaim`.
        self._reclaiming = ""
        self._reclaim_until = 0.0
        self._last_attempt = 0.0

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
        if not self.cfg.enabled:
            return False
        with self._lock:
            if self._started:
                return True
            # Lent out. The person detector polls twice a second and would
            # otherwise race Chromium for the node all through a call — winning
            # occasionally, which is worse than losing, because it takes the
            # picture away from the person on the other end.
            if self._lent:
                return False
            try:
                import cv2
            except ImportError as exc:
                self._error = f"OpenCV is not installed ({exc})"
                log.warning("camera unavailable: %s", self._error)
                return False

            candidates = _candidates(self.cfg)
            if not candidates:
                self._error = "no USB camera found — check the cable"
                log.warning("camera unavailable: %s", self._error)
                return False

            # This runs on the startup path, before the microphone is up, so an
            # unbounded search is an assistant that cannot hear for as long as
            # the search takes. Refusing one node costs seconds; before
            # `_candidates` learned to drop the ISP, refusing this Pi's twenty
            # of them measured at 81 s with the camera merely busy. A camera
            # that is missing is a degraded assistant, and it must not also be
            # a minute of deafness. This is the backstop behind that filter,
            # for the deployment with several cameras attached.
            deadline = time.monotonic() + SEARCH_BUDGET_S
            tried = 0
            for device in candidates:
                if tried and time.monotonic() > deadline:
                    log.warning("giving up the camera search after %.0f s and "
                                "%d of %d nodes", SEARCH_BUDGET_S, tried,
                                len(candidates))
                    break
                tried += 1
                capture = self._try_open(cv2, device)
                if capture is not None:
                    self._capture = capture
                    self._started = True
                    self._error = None
                    self._device = device
                    self._name = _sysfs_name(Path(device)) or "USB camera"
                    log.info("camera ready: %s on %s at %dx%d, %d grabs a frame",
                             self._name, device, *self._size, self._drain)
                    return True

            # Named, not listed. This goes across the top of a 1280 px screen
            # beside the weather, and the first candidate is the one somebody
            # needs anyway — the ranking already decided it was the likeliest
            # camera on the device.
            self._error = (f"no camera on {candidates[0]}"
                           + (f" or {tried - 1} other node(s)" if tried > 1 else ""))
            log.warning("camera would not start: %s", self._error)
            return False

    def _try_open(self, cv2, device: str):
        """Open one node, configure it, and prove it produces frames.

        Proof matters and a successful `isOpened()` is not it. The Brio's
        metadata node opens perfectly and never yields an image, and so does a
        capture node already held by something else on some driver versions —
        both would leave the assistant reporting a working camera and
        describing nothing. A decoded frame is the only answer that cannot be
        wrong.
        """
        try:
            index = int(device)
        except ValueError:
            index = None

        try:
            # CAP_V4L2 explicitly rather than CAP_ANY. Every property set below
            # — the fourcc, the buffer count, the frame size — is honoured by
            # the V4L2 backend and silently ignored by the GStreamer one, which
            # CAP_ANY may pick instead depending on how this OpenCV was built.
            # Asking by name means a wrong pixel format is an error at open
            # time rather than a camera that works and is inexplicably 640x480.
            capture = cv2.VideoCapture(index if index is not None else device,
                                       cv2.CAP_V4L2)
            if not capture.isOpened():
                capture.release()
                return None

            # MJPEG before the size, and this order is not a style choice: the
            # driver's list of sizes depends on the pixel format, so asking for
            # 1280x720 while still in YUYV gets it clamped to whatever the
            # uncompressed mode can do over USB 2.0 bandwidth.
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.capture_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.capture_height)
            capture.set(cv2.CAP_PROP_FPS, self.cfg.fps)
            # One buffer, so there is as little as possible standing between a
            # read and the sensor. It is a request the backend and the driver
            # are both free to round back up, which is why the answer is read
            # back rather than assumed — see `_read` for what it is used for.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._drain = self._drain_for(capture.get(cv2.CAP_PROP_BUFFERSIZE))

            frame = self._warm_up(capture)
            if frame is None:
                capture.release()
                return None
        except Exception as exc:
            # OpenCV raises cv2.error, which is a subclass of Exception and
            # nothing more specific worth catching, and a missing device can
            # also surface as a plain RuntimeError from the backend.
            log.debug("%s did not open: %s", device, exc)
            return None

        height, width = frame.shape[:2]
        self._size = (width, height)
        if (width, height) != (self.cfg.capture_width, self.cfg.capture_height):
            # Not fatal. The Brio offers 1280x720; a camera that does not gets
            # described at whatever it does offer, and `Capture` carries the
            # real numbers rather than the configured ones.
            log.warning("asked %s for %dx%d and got %dx%d",
                        device, self.cfg.capture_width, self.cfg.capture_height,
                        width, height)
        return capture

    @staticmethod
    def _drain_for(buffersize) -> int:
        """How many grabs empty the queue and then fetch a live frame.

        The device's own answer plus one. A driver that honours a single buffer
        needs two grabs — one to throw the waiting frame away and one that
        blocks until the sensor produces a new one — where the four-deep
        default needs five. Getting this from the device rather than assuming
        the default is worth about 140 ms a capture on this camera, which is
        most of what a person waits for after "what do you see".
        """
        try:
            depth = int(buffersize)
        except (TypeError, ValueError):
            depth = 0
        if depth < 1:
            # 0 or a negative is "this backend does not implement the property",
            # not "there is no queue".
            return DEFAULT_DRAIN
        return max(MIN_DRAIN, min(MAX_DRAIN, depth + 1))

    def _warm_up(self, capture):
        """Read frames until auto-exposure has settled. The last one, or None.

        Auto exposure and auto white balance need a moment of real frames
        before they converge, and the first capture after a cold start is
        otherwise dark. On the CSI camera this was a sleep; on a UVC one it has
        to be actual reads, because the sensor is not streaming at all until
        somebody dequeues a buffer — a sleep here would warm up nothing and the
        first frame would be as dark as it ever was.

        Doubles as the proof that this node is a camera, which is why it
        returns the frame rather than a bool.
        """
        deadline = time.monotonic() + self.cfg.warmup_ms / 1000
        frame = None
        while True:
            ok, latest = capture.read()
            if ok and latest is not None and getattr(latest, "size", 0):
                frame = latest
            elif frame is None:
                # The very first read failing is a node that is not a camera.
                # A later one failing is a dropped frame, which is normal on
                # USB and not a reason to give up on a device that has already
                # produced an image.
                return None
            if time.monotonic() >= deadline:
                return frame

    def available(self) -> bool:
        return self._started

    @property
    def error(self) -> str | None:
        return self._error

    def _read(self):
        """A frame captured just now, decoded. Caller holds the lock.

        The drain is the whole of this function's reason to exist, and what it
        is draining is not what it first looks like. V4L2 is a queue and
        `read()` returns the oldest buffer the driver filled — but the useful
        part is *when* that buffer was filled. Both readers here arrive every
        500 ms at the soonest, so the driver fills its queue within a few frame
        periods of the last read and then drops frames until somebody comes
        back. Every buffer waiting in the queue was therefore captured just
        after the *previous* read, not just before this one: on a 500 ms
        cadence the plain read is around 450 ms stale, and no amount of
        draining to the end of the queue fixes it.

        What fixes it is draining the queue *empty* and then taking the next
        frame the sensor produces, which is the one grab in here that blocks.
        `_drain` is sized for exactly that: the queue depth the device reported,
        plus one. Grabbing without retrieving costs a dequeue and a requeue and
        no JPEG decode, so the stale ones are nearly free and the cost of the
        whole call is one frame period.

        Measured on the Brio: 5 ms of decode on top of a wait that is 35 ms in
        a lit room and about 70 ms in a dim one, where the camera's own
        auto-exposure has halved the frame rate.
        """
        capture = self._capture
        for _ in range(self._drain):
            if not capture.grab():
                # A camera unplugged mid-run fails here rather than raising.
                # Say so once, at the level that gets read, because every
                # symptom downstream of this is silence.
                log.warning("the camera stopped delivering frames")
                return None
        ok, frame = capture.retrieve()
        if not ok or frame is None or not getattr(frame, "size", 0):
            log.warning("the camera returned an empty frame")
            return None
        return frame

    def capture_still(self) -> Capture | None:
        """A fresh frame, written to tmpfs as JPEG.

        Always fresh. Section 19 is explicit that an old image must not be
        reused unless it was asked for, and the reason is that "what do you
        see" asked twice, five minutes apart, in a room where something has
        changed, has two different right answers.
        """
        with self._lock:
            if not self._started or self._capture is None:
                log.info("no camera to capture from (%s)", self._error or "not started")
                return None

            try:
                import cv2

                frame = self._read()
                if frame is None:
                    return None

                path = self.cfg.scratch / f"{time.strftime('%Y%m%d-%H%M%S')}.jpg"
                # Re-encoded rather than passed through, even though what came
                # off the camera was already JPEG: OpenCV decodes on retrieve
                # and does not hand back the compressed buffer. One extra
                # generation of JPEG loss at quality 80 is invisible to a model
                # being asked what room this is.
                written = cv2.imwrite(str(path), frame,
                                      [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
                if not written:
                    log.warning("could not write the capture to %s", path)
                    return None
            except Exception as exc:
                log.warning("capture failed: %s", exc)
                return None

            height, width = frame.shape[:2]

        self._prune()
        log.info("captured %s (%dx%d)", path, width, height)
        return Capture(path=path, taken_at=time.time(), width=width, height=height)

    def frame(self):
        """The current frame as an RGB numpy array, or None.

        For the detector. RGB rather than the BGR OpenCV hands back, because
        that is what both detection models were trained on — picamera2's
        "RGB888" was BGR in memory, so the CSI path had this quietly backwards
        and nobody noticed.

        Nobody noticed because it very nearly does not matter: measured on this
        device, the same photo of a crowd scores 0.943 through the reversal and
        0.947 without it. The reversal is here because feeding a model the
        channel order it was trained on is free and being right for the right
        reason survives the next model swap, not because it rescued anything.

        Returned full size. The detector resizes to its model's input as its
        first step, so a downscale here would be a resample thrown away.
        """
        with self._lock:
            if not self._started or self._capture is None:
                return None
            try:
                frame = self._read()
            except Exception as exc:
                log.debug("could not read a detection frame: %s", exc)
                return None
        if frame is None:
            return None
        return frame[:, :, ::-1]

    def preview_jpeg(self, max_width: int = PREVIEW_WIDTH) -> bytes | None:
        """One frame as JPEG bytes, for the camera page's live preview.

        Separate from `capture_still` because nothing about it is a capture:
        no file, no prune, no record that it happened. It is the picture on the
        screen, thrown away as soon as it has been sent.

        Downscaled and encoded at a lower quality than a still, because it is
        being drawn at about half the panel's width and re-encoded several
        times a second. At 640 px this is ~25 KB a frame over loopback against
        ~58 KB for the still — the difference is the encode time, which is paid
        on the HTTP thread while the detector is waiting for the same lock.

        Note what this shares with everything else here: the one camera handle
        and the one lock. A preview is another reader, and the reason the
        stream is rate-limited in `aipi5/ui/server.py` rather than here is that
        the limit is a property of who is watching, not of the camera.
        """
        with self._lock:
            if not self._started or self._capture is None:
                return None
            try:
                import cv2

                frame = self._read()
                if frame is None:
                    return None
                height, width = frame.shape[:2]
                if width > max_width:
                    scale = max_width / width
                    frame = cv2.resize(frame, (max_width, int(height * scale)),
                                       interpolation=cv2.INTER_AREA)
                ok, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])
            except Exception as exc:
                log.debug("preview frame failed: %s", exc)
                return None
        return buffer.tobytes() if ok else None

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

    # ── lending the camera to something else ─────────────────────────
    #
    # A video call needs this exact device, and it is Chromium that needs it —
    # the media on both ends of a call is the browser's. A UVC node allows one
    # reader, so for as long as a call is up this process must not be it.
    #
    # `yield`-free and explicit rather than a context manager, because the two
    # ends are driven from different places: the release is requested by the
    # call page over the loopback API, and the reacquire happens when the call
    # state machine says the call is over — which may be because the *phone*
    # hung up, on a thread that knows nothing about whoever released it.

    def lend(self, to: str = "a call") -> bool:
        """Close the handle and refuse to reopen until `reclaim`. True if lent.

        Idempotent: lending an already-lent camera is what happens when a call
        reconnects, and it must not count as a second borrower or the reclaim
        would need to be paired.
        """
        with self._lock:
            if self._lent:
                return True
            self._lent = to
        # Closed outside the lock, because `close` takes it. The window between
        # these two lines is harmless: `_lent` is already set, so nothing can
        # reopen into it.
        was_running = self._started
        self.close()
        log.info("camera lent to %s%s", to,
                 "" if was_running else " (it was not running)")
        return True

    def reclaim(self) -> bool:
        """Take the camera back. True when it is running again.

        **The first attempt usually fails, and that is expected.** It is made
        the instant the call ends, and the browser has not finished letting go
        of the device — measured on the Pi: `camera would not start: no camera
        on /dev/video0`, one second after a hang-up, with `fuser` showing the
        node free moments later. The old version of this method tried once,
        warned, and stopped, so the assistant lost its camera for the rest of
        the session: no person detection, no screensaver, and nothing to
        connect it to a call that had ended normally.

        So a failure arms a retry instead. `retry_reclaim` does the work, from
        the voice loop's idle path, and gives up after `RECLAIM_WINDOW_S` so an
        unplugged camera does not become a warning every two seconds forever.
        """
        with self._lock:
            if not self._lent:
                return self._started
            borrower, self._lent = self._lent, ""
            self._reclaiming = borrower
            self._reclaim_until = time.monotonic() + RECLAIM_WINDOW_S
            self._last_attempt = 0.0
        return self.retry_reclaim(immediately=True)

    def retry_reclaim(self, immediately: bool = False) -> bool:
        """One bounded attempt to reopen a camera a call has finished with.

        Called from the voice loop's idle path, so it must be cheap when there
        is nothing to do — which is almost always. Returns True once the camera
        is running again.
        """
        with self._lock:
            if not self._reclaiming:
                return self._started
            borrower = self._reclaiming
            now = time.monotonic()
            if not immediately and now - self._last_attempt < RECLAIM_RETRY_S:
                return False
            expired = now > self._reclaim_until
            self._last_attempt = now

        if self.open():
            with self._lock:
                self._reclaiming = ""
            log.info("camera reclaimed from %s", borrower)
            return True

        if expired:
            with self._lock:
                self._reclaiming = ""
            # Once, at the end, rather than on every attempt. This is the line
            # that explains a dark camera page and a screensaver that never
            # lifts, so it says what to do about it.
            log.error("gave up reclaiming the camera from %s after %.0fs: %s — "
                      "the assistant has no camera until it is restarted",
                      borrower, RECLAIM_WINDOW_S, self._error)
        return False

    @property
    def lent(self) -> bool:
        return bool(self._lent)

    @property
    def reclaiming(self) -> bool:
        return bool(self._reclaiming)

    def describe(self) -> dict:
        """For the settings page."""
        return {
            "enabled": self.cfg.enabled,
            "running": self._started,
            "device": self._device or str(self.cfg.device),
            "name": self._name,
            "still": f"{self._size[0]}x{self._size[1]}",
            "lent_to": self._lent or None,
            "error": self._error,
        }

    def close(self) -> None:
        with self._lock:
            capture, self._capture = self._capture, None
            self._started = False
        if capture is None:
            return
        try:
            capture.release()
        except Exception:
            log.debug("releasing the camera failed", exc_info=True)
