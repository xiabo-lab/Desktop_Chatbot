"""Twenty-one hand landmarks, from a crop the accelerator pointed at.

Hand control moved onto the AI HAT+ and got smooth, and lost something doing
it: pose gives wrists and no fingers, so it could not tell an open palm from an
arm swinging past. That was reported straight away -- "it only sees my arm
move" -- and it is the right complaint. A gesture nobody made should not act.

**So the two halves are split between the two processors.** The accelerator
answers *where* a hand is, which it does thirty times a second for nothing.
This answers *what shape it is in*, on a 224-pixel square around that point:

    Hailo   person -> wrist            30 fps, free, already running
    CPU     crop  -> 21 landmarks      16 ms, measured

Sixteen milliseconds, against the 57-70 ms the browser paid for the same model
on a whole frame. The difference is entirely that it no longer has to *find*
the hand -- MediaPipe's palm detector is skipped, because the pose model has
already done that job better.

**Run without MediaPipe's framework, on purpose.** `mediapipe` 1.0.1 installs
on this device and is killed by the kernel the moment it builds its XNNPACK
delegate -- reproducibly, with memory to spare, on both the gesture graph and
the plain landmarker. The `.task` bundle is a zip of ordinary TFLite models, so
this reads `hand_landmarks_detector.tflite` straight out of it and runs it on
LiteRT. Fewer moving parts than the framework, and the one part that would not
run is gone.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

#: The model's input, and what the crop is resized to.
SIDE = 224

#: How much of the frame's width the crop covers. A hand at arm's length is
#: about a fifth of the picture; a third leaves room for the wrist estimate to
#: be a little off and for the fingers to be further from the camera than the
#: wrist is.
CROP = 0.34

#: Below this the model is guessing at a hand that is not there. It reports
#: presence itself, which is worth more than a confidence read off landmark
#: spread.
PRESENT = 0.5

#: Fingers whose tip is no further from the wrist than their middle knuckle are
#: curled. Three of four rather than all four: one finger commonly reads
#: straight through a frame where the hand is turning.
CURLED = 0.10
CURLED_ENOUGH = 3

#: The five landmarks that bound the palm. Their centre is steadier than the
#: wrist -- which is the point of using it to steer -- because it averages five
#: estimates instead of trusting one.
PALM = (0, 5, 9, 13, 17)
TIPS_AND_KNUCKLES = ((8, 6), (12, 10), (16, 14), (20, 18))


class Shape:
    """What one hand was doing, in the frame's own normalised coordinates."""

    def __init__(self, x: float, y: float, closed: bool, score: float,
                 spread: float):
        #: The palm's centre, 0..1 across the *camera* image.
        self.x = x
        self.y = y
        #: True for a fist. See `_closed`.
        self.closed = closed
        #: What the model thinks of its own answer.
        self.score = score
        #: The hand's size as a fraction of the frame, for logging.
        self.spread = spread

    @property
    def open(self) -> bool:
        return not self.closed

    def __repr__(self) -> str:                       # pragma: no cover
        return "Shape(%.3f, %.3f, %s, %.2f)" % (
            self.x, self.y, "fist" if self.closed else "palm", self.score)


class Fingers:
    """The landmark model, loaded once and asked about crops."""

    def __init__(self, bundle: Path | str, *, threads: int = 2):
        self._path = Path(bundle)
        self._model = None
        self._input = None
        self._outputs = None
        self._threads = threads
        self.error = ""

    # ── loading ─────────────────────────────────────────────────────

    def start(self) -> bool:
        """Load the model. False and an `error` rather than an exception.

        Hand control has to keep working without fingers -- sweeps come off the
        wrist and do not need this -- so a missing model costs the palm gate
        and the fist, not the feature.
        """
        if self._model is not None:
            return True
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError as exc:
            self.error = f"the landmark runtime is not installed ({exc})"
            return False
        try:
            blob = self._extract()
            self._model = Interpreter(model_content=blob,
                                      num_threads=self._threads)
            self._model.allocate_tensors()
            self._input = self._model.get_input_details()[0]
            self._outputs = self._model.get_output_details()
        except Exception as exc:                      # noqa: BLE001
            self.error = f"the landmark model would not load ({exc})"
            self._model = None
            return False
        self.error = ""
        log.info("hand landmarks ready (%s)", self._path.name)
        return True

    def _extract(self) -> bytes:
        """The landmark model, out of the `.task` bundle or on its own."""
        if self._path.suffix == ".tflite":
            return self._path.read_bytes()
        with zipfile.ZipFile(self._path) as bundle:
            for name in bundle.namelist():
                if "landmark" in name and name.endswith(".tflite"):
                    return bundle.read(name)
        raise RuntimeError("no landmark model inside %s" % self._path.name)

    @property
    def ready(self) -> bool:
        return self._model is not None

    # ── one look ────────────────────────────────────────────────────

    def read(self, frame, x: float, y: float) -> Shape | None:
        """Landmarks near (x, y), which is normalised **camera** space.

        Not mirrored: the caller un-mirrors before asking, because pose
        keypoints arrive mirrored and the picture does not.
        """
        if self._model is None:
            return None
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None

        height, width = frame.shape[:2]
        half = int(CROP * width / 2)
        cx, cy = int(x * width), int(y * height)
        left, top = cx - half, cy - half
        # Taken with the edges padded rather than clipped, so a hand at the
        # side of the picture is still centred in the square the model sees.
        # A clipped crop shifts the hand off-centre and the landmarks follow.
        patch = np.zeros((half * 2, half * 2, 3), dtype=frame.dtype)
        fx0, fy0 = max(0, left), max(0, top)
        fx1, fy1 = min(width, left + half * 2), min(height, top + half * 2)
        if fx1 <= fx0 or fy1 <= fy0:
            return None
        patch[fy0 - top:fy1 - top, fx0 - left:fx1 - left] = frame[fy0:fy1, fx0:fx1]

        try:
            square = cv2.resize(patch, (SIDE, SIDE), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(square, cv2.COLOR_BGR2RGB)
            tensor = (rgb.astype(np.float32) / 255.0)[None, ...]
            self._model.set_tensor(self._input["index"], tensor)
            self._model.invoke()
            points, score = self._read_out(np)
        except Exception as exc:                      # noqa: BLE001
            log.debug("landmark read failed: %s", exc)
            return None
        if points is None or score < PRESENT:
            return None

        # Landmarks are in the crop's own pixels; put them back in the frame.
        span = half * 2
        px = (points[:, 0] / SIDE) * span + left
        py = (points[:, 1] / SIDE) * span + top
        middle = (float(px[list(PALM)].mean() / width),
                  float(py[list(PALM)].mean() / height))
        reach = float(np.hypot(px[9] - px[0], py[9] - py[0]) / width)
        return Shape(middle[0], middle[1],
                     self._closed(px, py), float(score), reach)

    def _read_out(self, np):
        """The 63 landmark floats and the presence score, whatever the order.

        Two outputs are 63 wide -- screen landmarks and world landmarks -- and
        two are single scores. Which index each lands on is not promised by the
        bundle, so they are picked by shape rather than by position.
        """
        wide, scores = [], []
        for spec in self._outputs:
            value = self._model.get_tensor(spec["index"])
            flat = np.array(value).reshape(-1)
            (wide if flat.size >= 63 else scores).append(flat)
        if not wide:
            return None, 0.0
        points = wide[0][:63].reshape(21, 3)
        score = float(scores[0][0]) if scores else 1.0
        # The bundle reports presence as a logit on some builds and a
        # probability on others. Squash anything outside 0..1.
        if score > 1.0 or score < 0.0:
            score = 1.0 / (1.0 + pow(2.718281828, -score))
        return points, score

    @staticmethod
    def _closed(px, py) -> bool:
        """A fist: fingertips no further from the wrist than their knuckles.

        Measured against the hand's own size so it holds at any distance from
        the camera, and the thumb is left out -- it stays out sideways on a
        real fist and would only ever argue with the other four.
        """
        import math

        span = math.hypot(px[9] - px[0], py[9] - py[0]) or 1e-6
        curled = 0
        for tip, knuckle in TIPS_AND_KNUCKLES:
            far = math.hypot(px[tip] - px[0], py[tip] - py[0])
            mid = math.hypot(px[knuckle] - px[0], py[knuckle] - py[0])
            if (far - mid) / span < CURLED:
                curled += 1
        return curled >= CURLED_ENOUGH
