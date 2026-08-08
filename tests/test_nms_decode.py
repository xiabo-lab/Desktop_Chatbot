"""Reading a HAILO NMS-BY-CLASS buffer.

The accelerator runs YOLO's post-process on-chip, so what comes back is not
boxes but a flat float32 buffer whose per-class blocks are each only as long as
their own count. Walking that wrongly does not raise — it reads a score out of
the middle of somebody else's bounding box, or off the end of the buffer
entirely, and reports a confident detection built from whatever was in memory.
Which is to say it fails as a screensaver that flickers, on a device, weeks
later.

The layout was confirmed rather than assumed: `hailortcli parse-hef` reports 80
classes and 100 boxes per class, and 80 x (1 + 100 x 5) = 40,080 floats, which
is exactly the buffer size the model declares. These tests build that shape by
hand so the walk can be checked without an accelerator.
"""

from __future__ import annotations

import unittest

from aipi5.vision.person_detection import PERSON_CLASS, decode_nms_by_class


def buffer(detections: dict[int, list[float]], classes: int = 80) -> list[float]:
    """A buffer in the accelerator's layout: {class: [score, ...]}.

    Boxes are filled with recognisable coordinates rather than zeros, so a
    walk that lands one float out reads a coordinate and the test notices.
    """
    flat: list[float] = []
    for index in range(classes):
        scores = detections.get(index, [])
        flat.append(float(len(scores)))
        for score in scores:
            flat.extend([0.1, 0.2, 0.3, 0.4, score])
    return flat


class TestDecode(unittest.TestCase):

    def test_no_detections_at_all(self):
        present, best = decode_nms_by_class(buffer({}), PERSON_CLASS, 0.45)
        self.assertFalse(present)
        self.assertEqual(best, 0.0)

    def test_a_person_above_the_threshold(self):
        present, best = decode_nms_by_class(buffer({0: [0.91]}), PERSON_CLASS, 0.45)
        self.assertTrue(present)
        self.assertAlmostEqual(best, 0.91, places=5)

    def test_a_person_below_the_threshold(self):
        present, best = decode_nms_by_class(buffer({0: [0.30]}), PERSON_CLASS, 0.45)
        self.assertFalse(present, "0.30 is under the 0.45 floor")
        self.assertAlmostEqual(best, 0.30, places=5)

    def test_the_best_of_several_people(self):
        present, best = decode_nms_by_class(
            buffer({0: [0.51, 0.88, 0.62]}), PERSON_CLASS, 0.45)
        self.assertTrue(present)
        self.assertAlmostEqual(best, 0.88, places=5)

    def test_other_classes_are_not_people(self):
        # A room full of chairs (56) and a dog (16) is an empty room. This is
        # the test that catches an off-by-one in the class walk, because a
        # decoder that starts at the wrong offset finds these.
        present, best = decode_nms_by_class(
            buffer({16: [0.99], 56: [0.97, 0.95]}), PERSON_CLASS, 0.45)
        self.assertFalse(present)
        self.assertEqual(best, 0.0)

    def test_a_person_behind_a_crowded_earlier_class(self):
        # Class 0 comes first, so nothing can precede it — but the walk must
        # still be able to reach a later class correctly, which is the same
        # arithmetic. Asked of class 3 with two crowded classes in front.
        flat = buffer({0: [0.99] * 40, 1: [0.80] * 25, 3: [0.77]})
        present, best = decode_nms_by_class(flat, class_index=3, threshold=0.45)
        self.assertTrue(present)
        self.assertAlmostEqual(best, 0.77, places=5)

    def test_variable_counts_do_not_shift_the_walk(self):
        # Each block is only as long as its own count, so a decoder assuming a
        # fixed stride drifts further out of step with every class.
        flat = buffer({0: [], 1: [0.9] * 7, 2: [0.5], 5: [0.83]})
        present, best = decode_nms_by_class(flat, class_index=5, threshold=0.45)
        self.assertTrue(present)
        self.assertAlmostEqual(best, 0.83, places=5)

    def test_a_truncated_buffer_is_no_person(self):
        # A short read, or a model whose output is not this shape at all.
        # Must not report a detection assembled from off the end.
        flat = [3.0, 0.1, 0.2]          # claims three boxes, carries half of one
        present, best = decode_nms_by_class(flat, PERSON_CLASS, 0.45)
        self.assertFalse(present)
        self.assertEqual(best, 0.0)

    def test_a_negative_count_is_rejected(self):
        present, best = decode_nms_by_class([-5.0, 0.9, 0.9, 0.9, 0.9, 0.9],
                                            PERSON_CLASS, 0.45)
        self.assertFalse(present)

    def test_an_empty_buffer_is_no_person(self):
        self.assertEqual(decode_nms_by_class([], PERSON_CLASS, 0.45), (False, 0.0))

    def test_the_real_buffer_size_is_walked_without_running_off(self):
        # 40,080 floats is what this HEF declares. An all-zero buffer of that
        # size is the everyday case — an empty room — and it must walk all 80
        # classes and stop cleanly.
        flat = [0.0] * 40080
        self.assertEqual(decode_nms_by_class(flat, PERSON_CLASS, 0.45),
                         (False, 0.0))

    def test_a_full_buffer_at_maximum_occupancy(self):
        # Every class at its 100-box maximum: 80 x (1 + 500) = 40,080.
        flat = buffer({index: [0.5] * 100 for index in range(80)})
        self.assertEqual(len(flat), 40080, "the layout arithmetic itself")
        present, best = decode_nms_by_class(flat, PERSON_CLASS, 0.45)
        self.assertTrue(present)


if __name__ == "__main__":
    unittest.main()
