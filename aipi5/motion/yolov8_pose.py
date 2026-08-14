"""Turning nine raw tensors into seventeen joints.

`yolov8s_pose_h10.hef` does **not** post-process on the accelerator. The person
detector next door (`aipi5/vision/person_detection.py`) uses a HEF that does —
it hands back a finished NMS buffer and the decode there is thirty lines of
walking counts — and it is worth naming the difference, because it is the
reason this file exists at all. `hailortcli parse-hef` on the pose model:

    Input  input_layer1 UINT8,  NHWC(640x640x3)
    Output conv43 UINT8,  NHWC(80x80x64)   conv44 UINT8, NHWC(80x80x1)   conv45 UINT16, NHWC(80x80x51)
    Output conv57 UINT8,  NHWC(40x40x64)   conv58 UINT8, NHWC(40x40x1)   conv59 UINT16, NHWC(40x40x51)
    Output conv70 UINT8,  NHWC(20x20x64)   conv71 UINT8, NHWC(20x20x1)   conv72 UINT16, NHWC(20x20x51)

Three scales, three tensors each: 64 channels of box distribution, 1 of person
score, 51 of keypoints (17 x 3). Everything after the accelerator — the
distribution-to-distance decode, the anchor arithmetic, the keypoint scaling,
NMS — is here.

**The tensors are identified by shape, never by name.** `conv43` is a name a
recompile is free to change and the ordering the runtime returns them in is not
promised; 64 channels at 80x80 is what the tensor *is*. Section 42's "incorrect
HEF architecture" failure is then a clear exception at load time rather than a
model that runs and produces joints in a shuffled order.

The decode itself was written against Hailo's own reference post-process
(`/usr/include/hailo/tappas/pose_estimation/yolov8pose_postprocess.cpp`, LGPL)
to get the constants right, and reimplemented in numpy rather than copied — no
LGPL code is present or linked here. Two things in it are easy to get subtly
wrong and are called out where they happen: the anchor centres carry a half-cell
offset, and **the class score is already a probability**, because the sigmoid is
folded into the compiled graph.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: 17 keypoints x (x, y, visibility).
KEYPOINTS = 17
KEYPOINT_CHANNELS = KEYPOINTS * 3

#: Channels in the box head: four sides, each a distribution over 16 bins.
#: "Distribution Focal Loss" — the model predicts how far each edge is as a
#: probability over discrete distances rather than one regressed number.
REG_BINS = 16
BOX_CHANNELS = 4 * REG_BINS

#: One class. This is a person-only pose model, and the score tensor is one
#: channel wide because of it.
CLASS_CHANNELS = 1

#: What the model was compiled for. Checked rather than assumed: a 640x640 HEF
#: swapped for a 512x512 one would otherwise decode every joint at the wrong
#: scale and still look plausible.
INPUT_SIZE = 640


class PoseDecodeError(ValueError):
    """The tensors are not the shape a YOLOv8-pose head produces."""


def _sigmoid(values):
    import numpy as np

    # Clipped before the exponential rather than after, because np.exp(800)
    # warns and produces inf, and the result of the division is then nan
    # rather than the 0.0 it mathematically is.
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30.0, 30.0)))


def _softmax_last(values):
    import numpy as np

    shifted = values - np.max(values, axis=-1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=-1, keepdims=True)


def group_by_scale(tensors: dict, input_size: int = INPUT_SIZE) -> list[dict]:
    """Sort raw outputs into one (box, score, keypoints, stride) set per scale.

    Keyed on channel count and spatial size, for the reason in the module
    docstring: names are not a contract and ordering is not either.

    Returns the scales fine-to-coarse (stride 8, 16, 32), which is the order
    the reference walks them in. Nothing downstream depends on the order —
    every proposal carries its own stride — but a stable order makes two runs
    comparable when something is being debugged.
    """
    import numpy as np

    by_scale: dict[int, dict] = {}
    for name, tensor in tensors.items():
        array = np.asarray(tensor)
        # NHWC with the batch axis still on it, which is what InferModel hands
        # back for a batch size of one.
        if array.ndim == 4:
            array = array[0]
        if array.ndim != 3:
            raise PoseDecodeError(
                f"{name} has shape {np.shape(tensor)}; a YOLOv8-pose head "
                "emits 3-dimensional NHWC tensors")

        height, width, channels = array.shape
        if height != width:
            raise PoseDecodeError(f"{name} is {height}x{width}; expected square")
        if input_size % height:
            raise PoseDecodeError(
                f"{name} is {height}x{width}, which is not a whole-number "
                f"stride of the {input_size}px input")

        stride = input_size // height
        slot = by_scale.setdefault(stride, {"stride": stride})
        if channels == BOX_CHANNELS:
            slot["box"] = array
        elif channels == CLASS_CHANNELS:
            slot["score"] = array
        elif channels == KEYPOINT_CHANNELS:
            slot["keypoints"] = array
        else:
            raise PoseDecodeError(
                f"{name} has {channels} channels; expected {BOX_CHANNELS} "
                f"(box), {CLASS_CHANNELS} (score) or {KEYPOINT_CHANNELS} "
                "(keypoints)")

    if not by_scale:
        raise PoseDecodeError("the model returned no output tensors")

    scales = []
    for stride in sorted(by_scale):
        slot = by_scale[stride]
        missing = [part for part in ("box", "score", "keypoints")
                   if part not in slot]
        if missing:
            raise PoseDecodeError(
                f"stride {stride} is missing its {', '.join(missing)} tensor")
        scales.append(slot)
    return scales


def _anchor_centres(size: int, stride: int):
    """Pixel centres of every cell at one scale, flattened row-major.

    The half-cell offset is the part worth staring at. A cell's *centre* is
    `(index + 0.5) * stride`, not `index * stride`, and getting it wrong shifts
    every joint by half a cell — four pixels at stride 8, sixteen at stride 32.
    That is small enough to look like jitter and large enough to make a hand
    miss a fruit it was drawn on top of.

    Row-major to match NHWC's flatten: cell (row, col) is at index
    `row * size + col`, which is what `reshape(-1, ...)` produces.
    """
    import numpy as np

    coordinates = (np.arange(size, dtype=np.float32) + 0.5) * stride
    centre_y, centre_x = np.meshgrid(coordinates, coordinates, indexing="ij")
    return centre_x.reshape(-1), centre_y.reshape(-1)


def decode(tensors: dict, score_threshold: float = 0.5,
           iou_threshold: float = 0.7, input_size: int = INPUT_SIZE,
           max_detections: int = 8) -> list[dict]:
    """Raw tensors to a list of people, in model pixel coordinates.

    Each entry is `{"score", "box": (x1, y1, x2, y2), "keypoints": (17, 3)}`
    where the keypoint columns are x, y and visibility. Coordinates are model
    pixels — `geometry.Letterbox.to_camera` is what makes them mean something
    in the room, and it is deliberately not done here so that this function can
    be tested against hand-built tensors with no camera in the picture.

    Thresholded *before* the box decode, which is where nearly all the time
    goes: 8400 proposals of which typically one clears 0.5, and decoding a
    distribution over 16 bins for four sides of 8399 boxes nobody wants is the
    difference between this costing 2 ms and costing 30.
    """
    import numpy as np

    scales = group_by_scale(tensors, input_size)

    boxes: list = []
    scores: list = []
    keypoints: list = []

    for scale in scales:
        stride = scale["stride"]
        score_map = scale["score"].astype(np.float32).reshape(-1)

        # **Not sigmoided.** The compiled graph already ends in one, so this is
        # a probability in 0-1 as it stands. Applying another sigmoid here is
        # the single easiest mistake to make in this file and it does not look
        # like a mistake: sigmoid(0.9) is 0.71 and sigmoid(0.1) is 0.52, so
        # every detection survives, ranks the same way, and the confidence
        # threshold quietly stops meaning anything.
        candidates = np.nonzero(score_map >= score_threshold)[0]
        if candidates.size == 0:
            continue
        # Cheap guard against a frame of noise producing thousands of
        # proposals and turning one slow frame into a stall.
        if candidates.size > max_detections * 64:
            best = np.argsort(score_map[candidates])[-max_detections * 64:]
            candidates = candidates[best]

        size = input_size // stride
        centre_x, centre_y = _anchor_centres(size, stride)
        centre_x = centre_x[candidates]
        centre_y = centre_y[candidates]

        # Box: (n, 4, 16) distributions -> expected distance in cells -> pixels.
        raw = scale["box"].astype(np.float32).reshape(-1, 4, REG_BINS)[candidates]
        distribution = _softmax_last(raw)
        bins = np.arange(REG_BINS, dtype=np.float32)
        distance = np.sum(distribution * bins, axis=-1) * stride   # (n, 4) ltrb

        boxes.append(np.stack([
            centre_x - distance[:, 0],
            centre_y - distance[:, 1],
            centre_x + distance[:, 2],
            centre_y + distance[:, 3],
        ], axis=1))
        scores.append(score_map[candidates])

        # Keypoints: (n, 17, 3). x and y are offsets from the cell centre in
        # half-strides; the visibility column *does* need a sigmoid, unlike the
        # class score above, because it is a separate head that was not folded.
        raw_kpts = (scale["keypoints"].astype(np.float32)
                    .reshape(-1, KEYPOINTS, 3)[candidates])
        decoded = np.empty_like(raw_kpts)
        decoded[:, :, 0] = stride * (raw_kpts[:, :, 0] * 2 - 0.5) + centre_x[:, None]
        decoded[:, :, 1] = stride * (raw_kpts[:, :, 1] * 2 - 0.5) + centre_y[:, None]
        decoded[:, :, 2] = _sigmoid(raw_kpts[:, :, 2])
        keypoints.append(decoded)

    if not boxes:
        return []

    all_boxes = np.concatenate(boxes)
    all_scores = np.concatenate(scores)
    all_keypoints = np.concatenate(keypoints)

    kept = nms(all_boxes, all_scores, iou_threshold, max_detections)
    return [{"score": float(all_scores[i]),
             "box": tuple(float(v) for v in all_boxes[i]),
             "keypoints": all_keypoints[i]} for i in kept]


def nms(boxes, scores, iou_threshold: float, limit: int) -> list[int]:
    """Greedy non-maximum suppression. Indices of the survivors, best first.

    Plain and vectorised per step rather than clever. There is one person in
    front of this device nearly always and a handful at most, so the loop runs
    once or twice — an implementation tuned for hundreds of boxes would be
    slower here for the overhead alone.
    """
    import numpy as np

    order = list(np.argsort(scores)[::-1])
    areas = ((boxes[:, 2] - boxes[:, 0]).clip(0)
             * (boxes[:, 3] - boxes[:, 1]).clip(0))
    kept: list[int] = []

    while order and len(kept) < limit:
        best = int(order.pop(0))
        kept.append(best)
        if not order:
            break

        rest = np.asarray(order, dtype=int)
        overlap_w = (np.minimum(boxes[best, 2], boxes[rest, 2])
                     - np.maximum(boxes[best, 0], boxes[rest, 0])).clip(0)
        overlap_h = (np.minimum(boxes[best, 3], boxes[rest, 3])
                     - np.maximum(boxes[best, 1], boxes[rest, 1])).clip(0)
        overlap = overlap_w * overlap_h
        union = areas[best] + areas[rest] - overlap
        # A degenerate box has zero union; treat it as no overlap rather than
        # dividing by zero and suppressing everything against a nan.
        iou = np.where(union > 0, overlap / np.maximum(union, 1e-9), 0.0)
        order = [int(index) for index, score in zip(rest, iou)
                 if score < iou_threshold]

    return kept
