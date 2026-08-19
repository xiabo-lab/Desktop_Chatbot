"""The YOLOv8-pose decode, including the integers it now reads directly.

`aipi5/motion/yolov8_pose.py` had no tests, which was survivable while it was
a straight port of a reference post-process and stopped being survivable the
moment it started dequantising the accelerator's output itself. The arithmetic
that turns nine tensors into seventeen joints is exactly the kind that fails
*plausibly*: a wrong zero point moves every joint a little, which looks like
tracking that is merely loose.

So the important test here is the comparison one — decode the same person from
floats and from the quantised integers those floats came from, and require the
two to agree. That is the property the optimisation actually claims, stated
directly rather than approached through timings.

Skipped without numpy, which the development machine does not have; the Pi's
virtualenv does, and this suite runs there too.
"""

from __future__ import annotations

import unittest

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    np = None

from aipi5.motion import yolov8_pose

INPUT = 640
STRIDE = 32
SIZE = INPUT // STRIDE           # 20x20 cells


def _tensors(row: int = 8, col: int = 12, score: float = 0.9,
             distance_bin: int = 4, visibility: float = 3.0):
    """One scale's worth of float tensors with a single person in one cell.

    Deliberately built the way the model would emit them rather than the way
    the decoder wants them: box sides are logits over sixteen distance bins,
    the class score is already a probability (the sigmoid is folded into the
    compiled graph) and the keypoint visibility is not.
    """
    box = np.zeros((SIZE, SIZE, yolov8_pose.BOX_CHANNELS), dtype=np.float32)
    scores = np.zeros((SIZE, SIZE, 1), dtype=np.float32)
    keypoints = np.zeros((SIZE, SIZE, yolov8_pose.KEYPOINT_CHANNELS),
                         dtype=np.float32)

    scores[row, col, 0] = score
    # A peaked distribution on one bin for all four sides. 12 is high enough
    # that softmax puts essentially all the mass there, so the expected
    # distance is the bin index and the test can assert an exact pixel.
    for side in range(4):
        box[row, col, side * yolov8_pose.REG_BINS + distance_bin] = 12.0
    for joint in range(yolov8_pose.KEYPOINTS):
        keypoints[row, col, joint * 3 + 0] = 0.25 + joint * 0.01
        keypoints[row, col, joint * 3 + 1] = 0.40 + joint * 0.01
        keypoints[row, col, joint * 3 + 2] = visibility

    return {"box_head": box, "score_head": scores, "kpt_head": keypoints}


def _quantise(tensors: dict) -> tuple[dict, dict]:
    """The same tensors as the accelerator would hand them back.

    UINT8 for the box and score heads and UINT16 for the keypoints, which is
    what `hailortcli parse-hef` reports for this model. Returns the integer
    tensors and the `{name: (scale, zero point)}` map that undoes it.
    """
    plan = {
        "box_head": (np.uint8, 0.075, 62.0),
        "score_head": (np.uint8, 1 / 255, 0.0),
        "kpt_head": (np.uint16, 0.00042, 18260.0),
    }
    quantised, quant = {}, {}
    for name, values in tensors.items():
        dtype, scale, zero = plan[name]
        raw = np.rint(values / scale + zero)
        limit = np.iinfo(dtype).max
        quantised[name] = np.clip(raw, 0, limit).astype(dtype)
        quant[name] = (scale, zero)
    return quantised, quant


@unittest.skipIf(np is None, "numpy is not installed on this machine")
class TestTheDecodeItself(unittest.TestCase):

    def test_one_person_is_found_where_the_cell_says(self):
        people = yolov8_pose.decode(_tensors(), score_threshold=0.5,
                                    input_size=INPUT)
        self.assertEqual(len(people), 1)
        self.assertAlmostEqual(people[0]["score"], 0.9, places=5)

    def test_the_box_carries_the_half_cell_offset(self):
        """`(index + 0.5) * stride`, not `index * stride`. Section 17's trap."""
        person = yolov8_pose.decode(_tensors(row=8, col=12, distance_bin=4),
                                    input_size=INPUT)[0]
        centre_x = (12 + 0.5) * STRIDE
        centre_y = (8 + 0.5) * STRIDE
        reach = 4 * STRIDE
        x1, y1, x2, y2 = person["box"]
        self.assertAlmostEqual(x1, centre_x - reach, delta=0.5)
        self.assertAlmostEqual(y1, centre_y - reach, delta=0.5)
        self.assertAlmostEqual(x2, centre_x + reach, delta=0.5)
        self.assertAlmostEqual(y2, centre_y + reach, delta=0.5)

    def test_keypoints_are_offsets_from_the_cell_centre(self):
        person = yolov8_pose.decode(_tensors(row=8, col=12), input_size=INPUT)[0]
        centre_x = (12 + 0.5) * STRIDE
        centre_y = (8 + 0.5) * STRIDE
        x, y, visible = person["keypoints"][0]
        self.assertAlmostEqual(x, STRIDE * (0.25 * 2 - 0.5) + centre_x, delta=0.5)
        self.assertAlmostEqual(y, STRIDE * (0.40 * 2 - 0.5) + centre_y, delta=0.5)
        # The visibility column *is* sigmoided; the class score is not.
        self.assertAlmostEqual(visible, 1 / (1 + np.exp(-3.0)), places=4)

    def test_nothing_below_the_threshold_survives(self):
        self.assertEqual(
            yolov8_pose.decode(_tensors(score=0.3), score_threshold=0.5,
                               input_size=INPUT),
            [])


@unittest.skipIf(np is None, "numpy is not installed on this machine")
class TestReadingTheAcceleratorsIntegers(unittest.TestCase):
    """The claim the lazy dequantise makes, stated as an equality.

    Not "it is faster" — that is measured on the device by
    `scripts/bench_motion.py` — but "it decodes the same person", which is the
    part that can go wrong silently and the part a timing run would never
    notice.
    """

    def test_the_integers_decode_to_the_same_person_as_the_floats(self):
        floats = _tensors()
        integers, quant = _quantise(floats)

        from_floats = yolov8_pose.decode(floats, input_size=INPUT)[0]
        from_integers = yolov8_pose.decode(integers, input_size=INPUT,
                                           quant=quant)[0]

        self.assertAlmostEqual(from_floats["score"], from_integers["score"],
                               delta=1 / 255)
        for a, b in zip(from_floats["box"], from_integers["box"]):
            # Within a model pixel, which is a fifth of a screen pixel once the
            # 640-square is mapped back onto the panel.
            self.assertAlmostEqual(a, b, delta=1.0)
        for a, b in zip(from_floats["keypoints"].reshape(-1),
                        from_integers["keypoints"].reshape(-1)):
            self.assertAlmostEqual(a, b, delta=1.0)

    def test_a_wrong_zero_point_is_not_quietly_survivable(self):
        """Guards the test above from passing for the wrong reason.

        If the comparison held whatever the zero point was, it would not be
        testing the dequantise at all — it would be testing that both paths
        find *a* person somewhere.
        """
        floats = _tensors()
        integers, quant = _quantise(floats)
        wrong = dict(quant)
        wrong["kpt_head"] = (quant["kpt_head"][0], quant["kpt_head"][1] + 400)

        good = yolov8_pose.decode(integers, input_size=INPUT, quant=quant)[0]
        bad = yolov8_pose.decode(integers, input_size=INPUT, quant=wrong)[0]
        moved = abs(good["keypoints"][0][0] - bad["keypoints"][0][0])
        self.assertGreater(moved, 5.0)

    def test_no_quant_means_the_tensors_are_already_floats(self):
        floats = _tensors()
        self.assertEqual(
            yolov8_pose.decode(floats, input_size=INPUT)[0]["box"],
            yolov8_pose.decode(floats, input_size=INPUT, quant={})[0]["box"])

    def test_grouping_keeps_each_tensors_own_scale_and_zero_point(self):
        integers, quant = _quantise(_tensors())
        grouped = yolov8_pose.group_by_scale(integers, INPUT, quant)[0]
        self.assertEqual(grouped["quant"]["box"], quant["box_head"])
        self.assertEqual(grouped["quant"]["score"], quant["score_head"])
        self.assertEqual(grouped["quant"]["keypoints"], quant["kpt_head"])

    def test_an_unlisted_tensor_falls_back_to_the_identity(self):
        """A HEF whose names moved must decode as floats, not as nonsense."""
        grouped = yolov8_pose.group_by_scale(_tensors(), INPUT,
                                             {"something else": (0.5, 3.0)})[0]
        for part in ("box", "score", "keypoints"):
            self.assertEqual(grouped["quant"][part], yolov8_pose.IDENTITY_QUANT)


if __name__ == "__main__":
    unittest.main()
