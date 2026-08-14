"""Coordinate mapping, letterboxing and mirroring — section 46's first two.

These run on the development machine with no camera, no accelerator and no
numpy, which is the point: this is the arithmetic that silently gets things
wrong, and it is wrong in a way that looks like "the tracking is a bit loose"
rather than like a failure.
"""

from __future__ import annotations

import unittest

from aipi5.motion import geometry
from aipi5.motion.geometry import Letterbox


class TestLetterbox(unittest.TestCase):
    """1280x720 into 640x640 is the shape this device actually uses."""

    def setUp(self) -> None:
        self.box = Letterbox(1280, 720, 640, 640)

    def test_scale_uses_the_tighter_axis(self):
        # 640/1280 = 0.5 is tighter than 640/720 = 0.888.
        self.assertAlmostEqual(self.box.scale, 0.5)

    def test_content_is_letterboxed_not_stretched(self):
        self.assertEqual(self.box.scaled, (640, 360))
        # 140 blank rows top and bottom; nothing at the sides.
        self.assertEqual(self.box.padding, (0, 140))

    def test_centre_of_the_image_is_the_centre_of_the_square(self):
        x, y = self.box.to_camera(320, 320)
        self.assertAlmostEqual(x, 0.5)
        self.assertAlmostEqual(y, 0.5)

    def test_top_of_the_image_is_not_the_top_of_the_square(self):
        """The failure section 17 is about.

        A wrist at the top edge of the camera image sits at y=140 in model
        space, not y=0. Code that normalised by 640 would call it 0.219 and
        put the hand a fifth of the way down the screen.
        """
        _, y_at_image_top = self.box.to_camera(320, 140)
        self.assertAlmostEqual(y_at_image_top, 0.0)

        naive = 140 / 640
        self.assertGreater(abs(naive - y_at_image_top), 0.2)

    def test_bottom_of_the_image(self):
        _, y = self.box.to_camera(320, 500)
        self.assertAlmostEqual(y, 1.0)

    def test_horizontal_is_unpadded_at_this_aspect(self):
        left, _ = self.box.to_camera(0, 320)
        right, _ = self.box.to_camera(640, 320)
        self.assertAlmostEqual(left, 0.0)
        self.assertAlmostEqual(right, 1.0)

    def test_error_grows_towards_the_edges(self):
        """Why the mistake is hard to spot: it is zero in the middle."""
        for model_y, expected in ((320, 0.5), (230, 0.25), (410, 0.75)):
            _, y = self.box.to_camera(320, model_y)
            self.assertAlmostEqual(y, expected, places=6)

    def test_a_640x360_capture_needs_the_same_correction(self):
        """The game captures at 640x360, which is scale 1.0 and still padded."""
        box = Letterbox(640, 360, 640, 640)
        self.assertAlmostEqual(box.scale, 1.0)
        self.assertEqual(box.padding, (0, 140))
        _, y = box.to_camera(320, 140)
        self.assertAlmostEqual(y, 0.0)

    def test_a_square_source_needs_no_padding(self):
        box = Letterbox(720, 720, 640, 640)
        self.assertEqual(box.padding, (0, 0))
        x, y = box.to_camera(0, 0)
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0)

    def test_a_tall_source_pads_at_the_sides(self):
        box = Letterbox(480, 640, 640, 640)
        self.assertEqual(box.scaled, (480, 640))
        self.assertEqual(box.padding, (80, 0))
        x, _ = box.to_camera(80, 320)
        self.assertAlmostEqual(x, 0.0)

    def test_out_of_frame_keypoints_are_not_clamped(self):
        """A wrist just off the edge is usually a real estimate."""
        _, y = self.box.to_camera(320, 100)
        self.assertLess(y, 0.0)

    def test_a_degenerate_source_does_not_divide_by_zero(self):
        box = Letterbox(0, 0, 640, 640)
        self.assertEqual(box.to_camera(10, 10), (0.0, 0.0))


class TestMirror(unittest.TestCase):
    """Section 17 and section 46: right hand right must move the sword right."""

    def test_mirror_is_its_own_inverse(self):
        for value in (0.0, 0.25, 0.5, 0.75, 1.0):
            self.assertAlmostEqual(geometry.mirror(geometry.mirror(value)), value)

    def test_the_centre_does_not_move(self):
        self.assertAlmostEqual(geometry.mirror(0.5), 0.5)

    def test_moving_a_hand_right_moves_the_sword_right(self):
        """The whole requirement, stated as the physical situation.

        The camera faces the player, so when the player moves a hand to *their*
        right it travels towards the *left* of the unmirrored camera image —
        decreasing x. After mirroring, screen x must increase.
        """
        image_x_before = 0.60          # hand somewhere right of centre in frame
        image_x_after = 0.40           # player moved it to their right

        screen_before = geometry.mirror(image_x_before)
        screen_after = geometry.mirror(image_x_after)

        self.assertGreater(screen_after, screen_before)

    def test_moving_a_hand_left_moves_the_sword_left(self):
        self.assertLess(geometry.mirror(0.70), geometry.mirror(0.30))

    def test_vertical_is_never_flipped(self):
        """Up is up. Mirroring y would be a completely different bug."""
        box = Letterbox(1280, 720, 640, 640)
        _, high = box.to_camera(320, 200)
        _, low = box.to_camera(320, 440)
        self.assertLess(high, low)


class TestToScreen(unittest.TestCase):

    def test_maps_the_unit_square_onto_the_panel(self):
        self.assertEqual(geometry.to_screen(0.0, 0.0, 1280, 800), (0.0, 0.0))
        self.assertEqual(geometry.to_screen(1.0, 1.0, 1280, 800), (1280.0, 800.0))
        self.assertEqual(geometry.to_screen(0.5, 0.5, 1280, 800), (640.0, 400.0))

    def test_clamps_by_default(self):
        x, y = geometry.to_screen(1.4, -0.2, 1280, 800)
        self.assertEqual((x, y), (1280.0, 0.0))

    def test_can_be_asked_not_to_clamp(self):
        """Collision needs the true segment, not one flattened to the edge.

        A hand that swings off the side of the screen and back would otherwise
        produce a segment that runs along the edge, slicing whatever is there.
        """
        x, y = geometry.to_screen(1.4, -0.2, 1280, 800, clamp=False)
        self.assertAlmostEqual(x, 1792.0)
        self.assertAlmostEqual(y, -160.0)

    def test_full_pipeline_from_model_pixels_to_screen(self):
        """Model space -> camera space -> mirror -> screen, end to end."""
        box = Letterbox(1280, 720, 640, 640)

        # A wrist at the left of the model image, vertically centred.
        camera_x, camera_y = box.to_camera(64, 320)
        self.assertAlmostEqual(camera_x, 0.1)
        self.assertAlmostEqual(camera_y, 0.5)

        screen_x, screen_y = geometry.to_screen(geometry.mirror(camera_x),
                                                camera_y, 1280, 800)
        # Mirrored to the right-hand side of the panel.
        self.assertAlmostEqual(screen_x, 1152.0)
        self.assertAlmostEqual(screen_y, 400.0)


if __name__ == "__main__":
    unittest.main()
