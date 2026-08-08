"""Is there a person in front of the camera, decided on this device.

Continuous, local, and never uploaded. That constraint — section 20 — is what
picks the implementation: at two frames a second, sending frames away would be
about 172,000 requests a day of a room somebody lives in, which is unacceptable
on privacy grounds before it is unacceptable on cost or latency grounds.

Three backends behind one interface:

* **hailo** — the AI HAT+ 2. A YOLOv8n HEF compiled for the accelerator, run
  through HailoRT. This is what the accelerator is for; inference happens off
  the CPU entirely, which matters because the four cores are already spoken for
  by the wake recogniser, SenseVoice and Piper.
* **cpu** — onnxruntime and an SSD-MobileNet, for a Pi with no HAT. It works
  and it costs a core; the interval is what keeps that survivable.
* **disabled** — no detection. Presence stays UNKNOWN and the screensaver never
  engages, which is the correct behaviour for a device that cannot see.

A backend that fails to load is not fatal and does not fall back silently to a
different one. It logs what went wrong and reports itself unavailable, so
"person detection is not running" is a thing the settings page says rather than
something inferred from a screensaver that never appears.

The thread here only *detects*. What the answer means — how many frames of
agreement it takes to change a UI, when the screensaver comes up — is
`aipi5/core/presence.py`, which has no camera in it and is tested without one.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

# COCO's class index for "person". Both model families here are COCO-trained,
# which is why one constant covers both.
PERSON_CLASS = 0

# What the CPU model wants. SSD-MobileNet v1 is fixed at 300x300 and will
# silently produce nonsense at any other size.
SSD_INPUT = (300, 300)


class Detector(ABC):
    """Something that can answer "is there a person in this frame"."""

    name = "detector"

    @abstractmethod
    def detect(self, frame) -> tuple[bool, float]:
        """(person present, best confidence). Must never raise."""

    def available(self) -> bool:
        return True

    def close(self) -> None:
        pass


class NullDetector(Detector):
    """Detection turned off, or a backend that would not load."""

    name = "disabled"

    def __init__(self, reason: str = "disabled"):
        self.reason = reason

    def detect(self, frame) -> tuple[bool, float]:
        return False, 0.0

    def available(self) -> bool:
        return False


# How long one inference may take before it is treated as a lost frame. The
# measured figure on this device is 28 ms; ten seconds is a bound on a wedged
# accelerator, not a target.
INFER_TIMEOUT_MS = 10_000


def decode_nms_by_class(buffer, class_index: int = PERSON_CLASS,
                        threshold: float = 0.45,
                        classes: int = 80) -> tuple[bool, float]:
    """Read the best score for one class out of a HAILO NMS-BY-CLASS buffer.

    The HEFs in `/usr/share/hailo-models` run their YOLO post-process *on the
    accelerator*, so what comes back is not raw boxes but a flat float32 buffer
    laid out per class:

        [count_0, y1 x1 y2 x2 score, ... (count_0 times),
         count_1, y1 x1 y2 x2 score, ... (count_1 times),
         ... for every class]

    The sizes check out exactly, which is how this layout was confirmed rather
    than assumed: `hailortcli parse-hef` reports 80 classes and 100 boxes per
    class, and 80 x (1 + 100 x 5) = 40,080 floats, which is the buffer size the
    model declares.

    Only one class is ever wanted here — person is 0 — but the counts have to
    be walked in order to find it, because each class's block is only as long
    as its own count. Classes after the one wanted are not read at all.

    Returns `(present, best_score)`. A malformed buffer is "no person" rather
    than an exception: this is on a thread that must outlive anything that goes
    wrong in it.

    Deliberately plain Python with no numpy. It reads at most 80 counts and one
    class's worth of scores — a few hundred floats out of forty thousand —
    so there is nothing here for numpy to speed up, and staying out of it means
    this function can be tested on a machine that has no accelerator *and* no
    numpy, which is where the walk arithmetic actually gets checked.
    """
    size = len(buffer)
    at = 0
    best = 0.0
    for current in range(classes):
        if at >= size:
            break
        count = int(buffer[at])
        at += 1
        # A count that cannot fit in what is left means the buffer is not the
        # shape this function believes. Stop rather than read past the end and
        # report a score built from whatever happened to be in memory.
        if count < 0 or at + count * 5 > size:
            log.debug("NMS buffer ended unexpectedly at class %d", current)
            break
        if current == class_index:
            for box in range(count):
                best = max(best, float(buffer[at + box * 5 + 4]))
            break
        at += count * 5

    return best >= threshold, best


class HailoDetector(Detector):
    """YOLOv8 on the AI HAT+ 2, through HailoRT's InferModel API.

    **The API matters, and the old one does not work here.** This device is a
    Hailo-10H. The classic path — `ConfigureParams.create_from_hef`,
    `network_group.activate()`, `InferVStreams` — is what most Raspberry Pi
    examples use, and on the 10H every one of them ends at
    `libhailort failed with error: 7 (HAILO_NOT_IMPLEMENTED)`. The device is
    seated, the runtime is installed and `hailortcli fw-control identify`
    answers correctly; the vstream API is simply not implemented for this part.

    `VDevice.create_infer_model()` is, and it is also the API HailoRT 4.18+
    recommends generally — so this is not a 10H special case, it is the current
    way. Measured here at **28 ms** an inference.

    The device, the model and the configured model are all built once and held.
    Configuring per frame would put the multi-context load of a 5-context HEF
    on every one of two frames a second.
    """

    name = "hailo"

    def __init__(self, model_path, confidence: float):
        self.confidence = confidence
        self._ok = False
        self._vdevice = None
        self._configured_ctx = None
        self._configured = None
        self._input_hw = None
        self._output_size = 0

        if not model_path.exists():
            log.warning("no Hailo model at %s — run scripts/get_person_model.sh "
                        "on the Pi", model_path)
            return

        try:
            from hailo_platform import HailoSchedulingAlgorithm, VDevice
        except ImportError as exc:
            log.warning("HailoRT's Python bindings are not installed (%s). "
                        "Install hailo-all on the Pi, or set "
                        "person_detection.backend to 'cpu'.", exc)
            return

        try:
            import numpy as np

            params = VDevice.create_params()
            # Round-robin because this process may not be the only thing on the
            # accelerator — this Pi also runs an LLM stack against it — and the
            # scheduler is what stops two users of one device from deadlocking.
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN

            self._vdevice = VDevice(params)
            model = self._vdevice.create_infer_model(str(model_path))
            model.set_batch_size(1)

            shape = tuple(model.input().shape)          # (height, width, 3)
            self._input_hw = (shape[1], shape[0])       # (width, height)
            self._output_size = int(np.prod(model.output().shape))

            # Entered by hand rather than with `with`, because it has to stay
            # configured for the life of the detector and `close()` is what
            # ends it.
            self._configured_ctx = model.configure()
            self._configured = self._configured_ctx.__enter__()
            self._model = model
            self._ok = True
            log.info("Hailo person detection ready (%s, input %dx%d, "
                     "output %d floats)", model_path.name,
                     self._input_hw[0], self._input_hw[1], self._output_size)
        except Exception as exc:
            log.warning("could not bring up the Hailo accelerator: %s", exc)
            self.close()

    def available(self) -> bool:
        return self._ok

    def detect(self, frame) -> tuple[bool, float]:
        if not self._ok:
            return False, 0.0
        try:
            import numpy as np

            # Contiguous because the buffer is handed to native code, and a
            # view produced by fancy indexing is not.
            image = np.ascontiguousarray(_resize(frame, self._input_hw),
                                         dtype=np.uint8)
            output = np.zeros((self._output_size,), dtype=np.float32)

            bindings = self._configured.create_bindings()
            bindings.input().set_buffer(image)
            bindings.output().set_buffer(output)
            self._configured.run([bindings], INFER_TIMEOUT_MS)

            return decode_nms_by_class(output, PERSON_CLASS, self.confidence)
        except Exception as exc:
            # Never raises into the loop: an accelerator that has stopped
            # answering must degrade to "no opinion", not end presence
            # detection for the session.
            log.warning("Hailo inference failed: %s", exc)
            return False, 0.0

    def close(self) -> None:
        self._ok = False
        ctx, self._configured_ctx = self._configured_ctx, None
        self._configured = None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                log.debug("releasing the configured model failed", exc_info=True)
        vdevice, self._vdevice = self._vdevice, None
        if vdevice is None:
            return
        try:
            vdevice.release()
        except Exception:
            log.debug("releasing the Hailo device failed", exc_info=True)


class CpuDetector(Detector):
    """SSD-MobileNet through onnxruntime, for a Pi with no accelerator.

    Costs roughly a third of a core at two frames a second on a Pi 5, which is
    survivable and is exactly the cost the AI HAT+ exists to remove. The
    interval in the configuration is the knob if it turns out not to be.
    """

    name = "cpu"

    def __init__(self, model_path, confidence: float):
        self.confidence = confidence
        self._session = None

        if not model_path.exists():
            log.warning("no CPU detection model at %s", model_path)
            return
        try:
            import onnxruntime
        except ImportError as exc:
            log.warning("onnxruntime is not installed (%s); no CPU detection", exc)
            return
        try:
            self._session = onnxruntime.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"])
            self._input = self._session.get_inputs()[0].name
            log.info("CPU person detection ready (%s)", model_path.name)
        except Exception as exc:
            log.warning("could not load %s: %s", model_path, exc)
            self._session = None

    def available(self) -> bool:
        return self._session is not None

    def detect(self, frame) -> tuple[bool, float]:
        if self._session is None:
            return False, 0.0
        try:
            import numpy as np

            resized = _resize(frame, SSD_INPUT)
            batch = np.expand_dims(resized, axis=0).astype("uint8")
            outputs = self._session.run(None, {self._input: batch})
            return _best_person(outputs, self.confidence)
        except Exception as exc:
            log.warning("CPU inference failed: %s", exc)
            return False, 0.0


def _resize(frame, size: tuple[int, int]):
    """Frame to (width, height), without pulling in OpenCV.

    Nearest-neighbour by index arithmetic. It is crude, and for the question
    being asked — is there a person-shaped thing in this room — it is
    indistinguishable from a proper resample, while OpenCV is 40 MB of wheel
    and another native dependency on a device that is already carefully
    provisioned.
    """
    import numpy as np

    height, width = frame.shape[:2]
    target_w, target_h = size
    rows = (np.arange(target_h) * height // target_h).clip(0, height - 1)
    cols = (np.arange(target_w) * width // target_w).clip(0, width - 1)
    return frame[rows[:, None], cols]


def _best_person(outputs, threshold: float) -> tuple[bool, float]:
    """Read the highest-confidence person out of whatever the model returned.

    Both model families are handled by one function because both ultimately
    hand back arrays of `(class, score)` in some arrangement, and the arrangement
    varies more between *versions* of a model than between the two families —
    a YOLOv8 HEF post-processed on-chip returns per-class detection lists,
    while a raw SSD returns parallel arrays. Rather than hard-code either
    layout, this finds the person entries in whatever shape arrived and takes
    the best score. Anything genuinely unreadable is "no person", logged once.
    """
    import numpy as np

    values = list(outputs.values()) if isinstance(outputs, dict) else list(outputs)
    if not values:
        return False, 0.0

    best = 0.0

    # Shape A — Hailo's NMS output: a list, indexed by class, of (N, 5) boxes
    # where column 4 is the score. The person list is index 0.
    first = values[0]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, np.ndarray)):
        person = first[PERSON_CLASS] if len(first) > PERSON_CLASS else []
        person = np.asarray(person)
        if person.size:
            best = float(np.max(person[..., -1]))
        return best >= threshold, best

    # Shape B — SSD's parallel arrays: boxes, classes, scores, count.
    if len(values) >= 3:
        classes = np.asarray(values[1]).reshape(-1)
        scores = np.asarray(values[2]).reshape(-1)
        if classes.size and classes.size == scores.size:
            # SSD-MobileNet's COCO labels are 1-based, so "person" is 1 there
            # and 0 in the YOLO families. Accepting both costs nothing and
            # saves a model swap from silently detecting bicycles.
            mask = (classes == PERSON_CLASS) | (classes == PERSON_CLASS + 1)
            if mask.any():
                best = float(np.max(scores[mask]))
            return best >= threshold, best

    # Shape C — a single (N, 6) array of [x, y, x, y, score, class].
    array = np.asarray(first)
    if array.ndim == 2 and array.shape[1] >= 6:
        mask = array[:, 5] == PERSON_CLASS
        if mask.any():
            best = float(np.max(array[mask, 4]))
        return best >= threshold, best

    log.debug("unrecognised detector output shape; treating as no person")
    return False, 0.0


def build(cfg) -> Detector:
    """The configured detector, or a Null one that says why not."""
    if not cfg.enabled:
        return NullDetector("disabled in the configuration")

    backend = cfg.backend
    if backend == "hailo":
        detector = HailoDetector(cfg.model, cfg.confidence)
    elif backend == "cpu":
        detector = CpuDetector(cfg.cpu_model, cfg.confidence)
    elif backend in ("disabled", "none", ""):
        return NullDetector("disabled in the configuration")
    else:
        log.error("unknown person_detection.backend %r — expected 'hailo', "
                  "'cpu' or 'disabled'", backend)
        return NullDetector(f"unknown backend {backend!r}")

    # Deliberately no automatic fallback from hailo to cpu. Falling back would
    # turn "the accelerator is not working" into "the assistant is mysteriously
    # using a core it did not used to", which is the kind of degradation that
    # is discovered months later from a thermal graph.
    if not detector.available():
        return NullDetector(f"{backend} backend could not start — see the log")
    return detector


class PresenceWatcher:
    """Runs the detector on a schedule and reports changes.

    A daemon thread rather than work folded into the voice loop, because the
    voice loop blocks on `next(frames)` waiting for a microphone frame and
    would only look at the camera between utterances — which in a quiet room is
    never, and a quiet room is precisely when presence matters.

    `on_change` is called from this thread with a `PresenceEvent`. It must not
    block: it publishes to the UI state, which is a dictionary swap.
    """

    def __init__(self, camera, detector: Detector, tracker, interval_ms: int,
                 on_change=None):
        self.camera = camera
        self.detector = detector
        self.tracker = tracker
        self.interval = interval_ms / 1000
        self.on_change = on_change
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_confidence = 0.0
        self._frames = 0

    def start(self) -> None:
        if not self.detector.available():
            log.info("person detection is not running; the screensaver will "
                     "not engage")
            return
        self._thread = threading.Thread(target=self._run, name="aipi5-presence",
                                        daemon=True)
        self._thread.start()
        log.info("person detection started on the %s backend, every %.0f ms",
                 self.detector.name, self.interval * 1000)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                frame = self.camera.frame()
                if frame is not None:
                    seen, confidence = self.detector.detect(frame)
                    self._last_confidence = confidence
                    self._frames += 1
                    event = self.tracker.observe(seen)
                    if event is not None and self.on_change is not None:
                        self.on_change(event)
            except Exception:
                # This thread must outlive anything that goes wrong in it.
                # A detector that throws every frame is a log full of traces
                # and a screensaver that never comes up — bad, and much better
                # than a thread that died silently in the first minute and left
                # presence frozen at whatever it was.
                log.exception("presence detection frame failed")

            # Sleep the remainder rather than a fixed interval, so a slow
            # inference does not turn a 500 ms cadence into 500 ms plus however
            # long the accelerator took.
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.interval - elapsed))

    def describe(self) -> dict:
        return {
            "backend": self.detector.name,
            "running": self._thread is not None and self._thread.is_alive(),
            "state": self.tracker.state.value,
            "streak": self.tracker.streak,
            "confidence": round(self._last_confidence, 3),
            "frames": self._frames,
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.detector.close()
