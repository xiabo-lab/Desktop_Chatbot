"""The pose model, on the AI HAT+ 2.

The sibling of `aipi5/vision/person_detection.py::HailoDetector`, and it
follows that module's hard-won conclusion rather than rediscovering it: on a
**Hailo-10H the classic vstream API is not implemented**. `ConfigureParams.
create_from_hef`, `network_group.activate()` and `InferVStreams` are what every
Raspberry Pi example on the internet uses and every one of them ends at
`libhailort failed with error: 7 (HAILO_NOT_IMPLEMENTED)` on this part, with a
correctly seated device that `hailortcli fw-control identify` answers for.
`VDevice.create_infer_model()` is the API that works and is also the one
HailoRT 4.18+ recommends generally.

**Two users, one accelerator, and only one `VDevice` in the world.** The device
comes from `aipi5/core/accelerator.py` rather than being created here, because
a Hailo-10H permits exactly one virtual device — a second one fails with
`HAILO_OUT_OF_PHYSICAL_DEVICES` whether it is in another process or the same
one, and whether or not round-robin scheduling is set. Round-robin timeshares
*models configured on one device*; it does not make two devices possible. That
is measured, not inferred: see the table in `accelerator.py`.

Section 32's choice is therefore Option B, applied to the model rather than the
device: the pose model is configured when AI Motion opens and released when it
closes, while the device underneath it stays open for as long as anything wants
it. A 13 MB three-context HEF held resident for a feature nobody is using is
13 MB of the HAT's memory and a permanent share of the scheduler; configuring
it costs about a second, once per session.

Output format is set to FLOAT32 so the runtime dequantizes. Doing it here would
mean reading `qp_scale`/`qp_zp` per tensor and getting the UINT16 keypoint
output's scaling right by hand, for arithmetic the runtime already does.
"""

from __future__ import annotations

import logging
import threading
import time

from aipi5.core import accelerator
from aipi5.motion import geometry, yolov8_pose
from aipi5.motion.pose_types import (KEYPOINT_NAMES, KeyPoint, PersonPose,
                                     PoseFrame)

log = logging.getLogger(__name__)

#: What each HailoRT output format is, as a numpy type name. Keyed on the tail
#: of `str(FormatType.UINT8)` rather than on the enum members themselves,
#: because this module has to stay importable on a machine with no HailoRT —
#: which is where most of the tests run.
#:
#: A format that is not in here is not guessed at. It falls back to asking the
#: runtime for FLOAT32, which is the slow path this file exists to avoid and is
#: still the right answer: a wrong guess about the width of an integer does not
#: fail, it decodes a person made of noise.
_NUMPY_TYPE_NAMES = {"UINT8": "uint8", "UINT16": "uint16", "FLOAT32": "float32"}


def _numpy_type(format_type, np):
    """`FormatType.UINT8` to `np.uint8`, or None for one nobody here has seen."""
    name = _NUMPY_TYPE_NAMES.get(str(format_type).rsplit(".", 1)[-1].upper())
    return getattr(np, name) if name else None

# How long one inference may take before it counts as a wedged accelerator.
# The measured figure is around 25 ms; two seconds is a bound, not a target,
# and it is shorter than the person detector's ten because this one is on an
# interactive path where a two-second stall is already a lost game.
INFER_TIMEOUT_MS = 2_000


class PoseUnavailable(RuntimeError):
    """The accelerator or the model would not come up. Carries the reason.

    Raised rather than returned because every caller of `HailoPose` either has
    a working pose model or has an error to put on the screen (section 41),
    and there is no third thing to do with a half-started one.
    """


class HailoPose:
    """One configured pose model, held for as long as AI Motion is open.

    Not thread-safe by accident — `infer` takes a lock. One pose loop calls it,
    but the settings page asks `describe()` from an HTTP thread and a stop can
    arrive from another, and a configured model being torn down underneath a
    running inference is a segfault rather than an exception.
    """

    def __init__(self, model_path, *, score_threshold: float = 0.5,
                 iou_threshold: float = 0.7):
        self.model_path = model_path
        self.score_threshold = score_threshold
        self.iou_threshold = iou_threshold
        self._lock = threading.Lock()
        self._held = False
        self._configured_ctx = None
        self._configured = None
        self._model = None
        self._input_hw = (yolov8_pose.INPUT_SIZE, yolov8_pose.INPUT_SIZE)
        self._outputs: dict[str, tuple] = {}
        #: Allocated once at load and reused for every frame. See `_allocate`.
        self._input_buffer = None
        self._output_buffers: dict = {}
        self._bindings = None
        #: The numpy type each output really comes back as, and the
        #: `(scale, zero point)` that turns it into a number. See the loop in
        #: `__init__` that fills them.
        self._dtypes: dict = {}
        self._quant: dict = {}
        self._ok = False

        if not model_path.exists():
            raise PoseUnavailable(
                f"no pose model at {model_path}. The Raspberry Pi AI HAT+ "
                "packages ship one at /usr/share/hailo-models/"
                "yolov8s_pose_h10.hef — check motion.pose_model in the "
                "configuration.")

        try:
            from hailo_platform import FormatType
        except ImportError as exc:
            raise PoseUnavailable(
                f"HailoRT's Python bindings are not installed ({exc}). "
                "AI Motion games require the AI HAT+ 2.") from exc

        try:
            vdevice = accelerator.acquire("AI Motion pose")
            self._held = True
        except accelerator.AcceleratorUnavailable as exc:
            raise PoseUnavailable(str(exc)) from exc

        try:
            import numpy as np

            model = vdevice.create_infer_model(str(model_path))
            model.set_batch_size(1)

            shape = tuple(model.input().shape)              # (height, width, 3)
            self._input_hw = (shape[1], shape[0])           # (width, height)
            if shape[0] != yolov8_pose.INPUT_SIZE:
                # Not fatal — the decoder takes the input size as a parameter
                # and the anchor arithmetic follows it — but it means the HEF
                # is not the one this was measured against, and the person
                # reading the log should know which one it got.
                log.warning("the pose model wants %dx%d rather than the "
                            "expected %d square", shape[1], shape[0],
                            yolov8_pose.INPUT_SIZE)

            # **The accelerator's own integers, converted lazily.** This used
            # to ask the runtime for FLOAT32 and let it dequantise all nine
            # tensors on the way out. That is 3.9 MB per frame and was measured
            # on this device at 8.5 ms — thirty per cent of the whole inference
            # — to produce 8,400 numbers the decoder looks at and 3.8 MB it
            # discards untouched. `yolov8_pose.decode` now converts the few
            # hundred rows that clear the score threshold, and takes the
            # quantisation parameters gathered here to do it.
            for output in model.outputs:
                self._outputs[output.name] = tuple(output.shape)
                dtype = _numpy_type(output.format.type, np)
                infos = list(output.quant_infos or ())
                if dtype is None or len(infos) != 1:
                    # Per-channel quantisation, or a type this does not know.
                    # Neither is what this HEF does, and both are recoverable
                    # by handing the work back to the runtime — which is
                    # slower and correct, and much better than a fast wrong
                    # answer from a recompiled model.
                    log.warning("pose output %s is %s with %d quantisation "
                                "records; letting the runtime dequantise it",
                                output.name, output.format.type, len(infos))
                    output.set_format_type(FormatType.FLOAT32)
                    self._dtypes[output.name] = np.float32
                    self._quant.pop(output.name, None)
                    continue
                self._dtypes[output.name] = dtype
                self._quant[output.name] = (float(infos[0].qp_scale),
                                            float(infos[0].qp_zp))

            self._configured_ctx = model.configure()
            self._configured = self._configured_ctx.__enter__()
            self._model = model
            self._np = np
            self._allocate(np)
            self._ok = True

            log.info("AI Motion: pose model loaded (%s, input %dx%d, "
                     "%d output tensors)", model_path.name,
                     self._input_hw[0], self._input_hw[1], len(self._outputs))
        except PoseUnavailable:
            raise
        except Exception as exc:
            self.close()
            raise PoseUnavailable(
                f"could not bring up the pose model on the AI HAT+ 2: {exc}"
            ) from exc

    def _allocate(self, np) -> None:
        """Reserve every buffer one inference needs, once, at load.

        **This used to happen inside `infer`, on every frame.** The output
        tensors of this HEF come back as FLOAT32 and total about 3.9 MB —
        80x80x64 and 80x80x51 are most of it — so a fresh `np.zeros` per output
        per frame meant allocating and *zeroing* 3.9 MB thirty times a second
        for values the accelerator is about to overwrite completely. The zero
        fill was pure waste: not one byte of it is ever read.

        The bindings object is reused for the same reason and is safe to reuse
        for exactly one reason: `infer` runs one job at a time under a lock and
        does not return until `run()` has. Bindings may not be shared between
        inferences that are in flight together, which is what the asynchronous
        API does — this is the synchronous one, and there is only ever one.

        The input buffer is a 640x640x3 uint8 square that `letterbox_image`
        writes the resized frame into. Its padding is filled once here, because
        the grey border is the same grey on every frame of a session and
        repainting it thirty times a second is 1.2 MB of memset for a picture
        that never changes.
        """
        self._input_buffer = np.full(
            (self._input_hw[1], self._input_hw[0], 3),
            geometry.LETTERBOX_FILL, dtype=np.uint8)
        bindings = self._configured.create_bindings()
        self._output_buffers = {
            name: np.empty(shape, dtype=self._dtypes.get(name, np.float32))
            for name, shape in self._outputs.items()
        }
        for name, buffer in self._output_buffers.items():
            bindings.output(name).set_buffer(buffer)
        self._bindings = bindings

    # ── inference ────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._ok

    @property
    def input_size(self) -> tuple[int, int]:
        return self._input_hw

    def infer(self, frame, captured_at: float) -> PoseFrame:
        """One camera frame in, one `PoseFrame` out. Never raises.

        `frame` is **BGR**, full camera resolution, exactly as the driver
        produced it. The letterbox, the resize and the channel swap all happen
        here rather than in the camera, because they are properties of *this
        model's* input and the camera is shared with the person detector, which
        wants a different size — and because doing the swap after the downscale
        is a quarter of the work of doing it before (`letterbox_image`).

        Section 8: the full camera frame is never sent anywhere. It is resized
        and padded once, on this thread, into a buffer reserved at load, and
        what crosses into native code is the 640x640 square the model consumes.
        """
        if not self._ok:
            return PoseFrame(timestamp=captured_at)

        np = self._np
        height, width = frame.shape[:2]
        try:
            began = time.monotonic()
            image, box = geometry.letterbox_image(frame, *self._input_hw,
                                                  into=self._input_buffer,
                                                  swap_rb=True)
            preprocess = time.monotonic() - began

            started = time.monotonic()
            with self._lock:
                if not self._ok:
                    return PoseFrame(timestamp=captured_at)
                bindings = self._bindings
                bindings.input().set_buffer(image)
                buffers = self._output_buffers
                self._configured.run([bindings], INFER_TIMEOUT_MS)
            elapsed = time.monotonic() - started

            decoded = time.monotonic()
            people = yolov8_pose.decode(
                buffers, score_threshold=self.score_threshold,
                iou_threshold=self.iou_threshold,
                input_size=self._input_hw[0], quant=self._quant)
            decode_s = time.monotonic() - decoded
        except Exception as exc:
            # Never raises into the pose loop. An accelerator that has stopped
            # answering must degrade to "nobody is there", which the game shows
            # as a lost player, rather than ending the session with a trace.
            log.warning("pose inference failed: %s", exc)
            return PoseFrame(timestamp=captured_at, source_size=(width, height))

        return PoseFrame(
            timestamp=captured_at,
            persons=tuple(self._to_person(person, box) for person in people),
            inference_s=elapsed,
            preprocess_s=preprocess,
            decode_s=decode_s,
            source_size=(width, height),
        )

    @staticmethod
    def _to_person(raw: dict, box: geometry.Letterbox) -> PersonPose:
        """Model pixels to a mirrored, camera-normalised `PersonPose`.

        Both conversions happen here and only here — see `geometry` for why the
        letterbox correction cannot be skipped and why the mirror is not a
        display preference.
        """
        keypoints = {}
        for index, name in enumerate(KEYPOINT_NAMES):
            x, y, confidence = raw["keypoints"][index]
            camera_x, camera_y = box.to_camera(float(x), float(y))
            keypoints[name] = KeyPoint(x=geometry.mirror(camera_x),
                                       y=camera_y,
                                       confidence=float(confidence))

        x1, y1, x2, y2 = raw["box"]
        left, top = box.to_camera(x1, y1)
        right, bottom = box.to_camera(x2, y2)
        # Mirroring swaps which edge is the left one, so the box is rebuilt
        # from the mirrored corners rather than having its origin flipped —
        # a box with a negative width is a person-selection bug that only
        # shows up when two people are in shot.
        mirrored_left, mirrored_right = geometry.mirror(right), geometry.mirror(left)

        return PersonPose(
            confidence=raw["score"],
            keypoints=keypoints,
            box=(mirrored_left, top,
                 mirrored_right - mirrored_left, bottom - top),
            # The camera's own shape, carried so that whoever measures an angle
            # can undo it — see `PersonPose.shaped`. From the letterbox rather
            # than from configuration, because it has to be the shape of the
            # frame that actually arrived: the driver is free to answer a
            # request for one size with another, and does.
            aspect=(box.source_w / box.source_h if box.source_h else
                    geometry.SHAPE_ASPECT),
        )

    # ── teardown ─────────────────────────────────────────────────────

    def describe(self) -> dict:
        """For the game's AI-accelerator panel — section 44."""
        return {
            "model": self.model_path.name,
            "loaded": self._ok,
            "input": f"{self._input_hw[0]}x{self._input_hw[1]}",
            "outputs": len(self._outputs),
            #: How many outputs come back as the accelerator's own integers.
            #: Fewer than `outputs` means a tensor fell back to the runtime's
            #: FLOAT32 conversion, which is a real 8.5 ms and worth being able
            #: to see from the panel rather than only from the log.
            "quantised": len(self._quant),
        }

    def close(self) -> None:
        """Release the model and the device. Idempotent.

        Section 30: no Hailo resource may outlive the game that asked for it.
        The order matters — the configured model has to be exited before the
        device it was configured on is released, or the runtime logs a
        use-after-free at exit that looks like a crash in something else.
        """
        with self._lock:
            self._ok = False
            ctx, self._configured_ctx = self._configured_ctx, None
            self._configured = None
            self._model = None
            # Dropped before the configured model goes, because the bindings
            # hold buffers the runtime was given pointers to.
            self._bindings = None
            self._output_buffers = {}
            self._input_buffer = None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                log.debug("releasing the configured pose model failed",
                          exc_info=True)
        if self._held:
            self._held = False
            accelerator.release("AI Motion pose")
            log.info("AI Motion: pose model released")


#: What the accelerator says it is — section 44's verification panel asks this.
#: Re-exported rather than reimplemented: the device is the shared one, so the
#: question belongs to whoever owns it, and a second copy of the answer here
#: would be a second thing to keep true.
identify = accelerator.identify
