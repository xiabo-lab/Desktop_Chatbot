"""Getting from a padded 640x640 square back to the room, and then to the screen.

Three coordinate spaces, and section 17 is a warning about what happens when
two of them are confused:

    model space     640x640, the letterboxed square the accelerator sees.
                    Origin top-left. A wrist here is in *padded* pixels.
    camera space    0.0-1.0 of the actual camera image, 1280x720. Mirrored.
                    This is what a `PoseFrame` carries.
    screen space    1280x800 pixels of touchscreen. The game's, and nobody
                    else's.

The trap is the middle one. A 1280x720 frame fitted into a 640x640 square is
scaled by 0.5 and then padded with 140 blank rows top and bottom — so a wrist
at the vertical centre of the *image* is at y=320 in model space, and a wrist
at the top edge of the image is at y=140, not y=0. Multiplying a model
coordinate by the screen height, or even normalising it by 640, puts every
hand in the wrong place by up to a fifth of the screen and puts the error in
the direction that is hardest to notice: it is zero at the centre and grows
towards the edges, so it looks like the tracking is merely "a bit loose".

`unletterbox` is the only place that arithmetic happens. Everything else in
this package works in camera space, where 0.5 means the middle of the room.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Letterbox:
    """How one camera frame was fitted into the model's square input.

    Built once per (camera size, model size) pair and reused for every frame,
    because it is a property of the two resolutions and nothing else. Cheap
    enough to build per frame; kept because doing so makes it obvious that a
    camera resolution change must produce a new one.
    """

    #: The camera image, in pixels.
    source_w: int
    source_h: int
    #: The model input, in pixels. Square in every pose model so far, but not
    #: assumed to be.
    target_w: int
    target_h: int

    @property
    def scale(self) -> float:
        """One number for both axes — that is what makes it a letterbox.

        Fitting by the tighter axis preserves the aspect ratio, which pose
        models care about a great deal: a person squashed horizontally into a
        square has shoulders where the model expects a child's, and the
        keypoint confidences fall off in a way that looks like bad lighting.
        """
        if self.source_w <= 0 or self.source_h <= 0:
            return 1.0
        return min(self.target_w / self.source_w, self.target_h / self.source_h)

    @property
    def scaled(self) -> tuple[int, int]:
        """Size of the image content inside the padded square."""
        return (int(round(self.source_w * self.scale)),
                int(round(self.source_h * self.scale)))

    @property
    def padding(self) -> tuple[int, int]:
        """(left, top) blank pixels. Centred, which is what everything assumes.

        Centred rather than top-left purely because it is what the rest of the
        world does, and a model fine-tuned on centred letterboxing does
        measurably better on it. The important thing is that whatever this
        returns is also what `letterbox_image` actually pastes at.
        """
        width, height = self.scaled
        return ((self.target_w - width) // 2, (self.target_h - height) // 2)

    def to_camera(self, x: float, y: float) -> tuple[float, float]:
        """Model pixels to normalised camera coordinates. The important one.

        Not clamped. A keypoint the model placed slightly outside the frame is
        usually a real estimate of a hand that is genuinely just off-camera,
        and clamping it here would pin it to the edge and make it look like a
        hand resting against the side of the screen. Whoever draws it decides.
        """
        width, height = self.scaled
        pad_x, pad_y = self.padding
        if width <= 0 or height <= 0:
            return 0.0, 0.0
        return (x - pad_x) / width, (y - pad_y) / height


def mirror(x: float) -> float:
    """Flip a normalised x so the room behaves like a mirror.

    Section 17, and it is worth being explicit about why this is not optional.
    The camera faces the player, so the player's right hand appears on the
    *left* of the unmirrored image. Drawing that directly means a person moves
    their hand right and the sword goes left, which nobody can play through —
    it is not a preference, it is the difference between a game and a
    frustration.

    Applied once, in the decoder, so that `PoseFrame` coordinates are already
    what a person watching the screen would expect. Doing it in the game
    instead would mean every future game has to remember to.
    """
    return 1.0 - x


def to_screen(x: float, y: float, width: int, height: int,
              clamp: bool = True) -> tuple[float, float]:
    """Normalised camera coordinates to screen pixels.

    The camera is 16:9 and the screen is 16:10, so this deliberately *stretches*
    rather than letterboxing a second time. Two reasons, and they agree: a
    letterboxed game area would leave 80 px dead bands the player's hands
    cannot reach on a screen that is already only 800 px tall, and the
    distortion is 11% on one axis of a hand position that the player is
    steering by watching it — nobody can perceive an anisotropy they are
    closing the loop on visually.

    Clamped by default, because this is the last step before something is
    drawn and a sword tip 40 px off the side of the panel is just invisible.
    """
    sx, sy = x * width, y * height
    if clamp:
        sx = min(max(sx, 0.0), float(width))
        sy = min(max(sy, 0.0), float(height))
    return sx, sy


def letterbox_image(frame, target_w: int, target_h: int, fill: int = 114):
    """Resize a camera frame into the model's square, padding to fit.

    `fill` is 114 because that is the grey Ultralytics trains YOLO with, and a
    model sees padding it recognises as padding rather than as a large black
    object at the edge of every frame.

    Returns `(padded, Letterbox)`. numpy and cv2 are imported here rather than
    at module scope so that everything above — the arithmetic that actually
    gets things wrong — stays importable and testable on a machine with
    neither, which is where the tests run.
    """
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    box = Letterbox(width, height, target_w, target_h)
    new_w, new_h = box.scaled
    pad_x, pad_y = box.padding

    # INTER_LINEAR rather than INTER_AREA, which is the better downscale.
    # Measured on the Pi: AREA costs about 3 ms more per frame at 1280x720 to
    # 640x360 and changes no keypoint by a pixel, and 3 ms is 10% of the frame
    # budget for a game whose whole problem is latency.
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    padded = np.full((target_h, target_w, 3), fill, dtype=np.uint8)
    padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return padded, box
