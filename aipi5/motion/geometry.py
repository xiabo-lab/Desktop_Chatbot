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


#: The camera shape every *shape* measurement in this project is expressed in.
#:
#: Camera space is normalised to the frame, so x and y are on different scales
#: whenever the frame is not square — at 16:9 one unit of x is 1.78 times the
#: physical distance of one unit of y. Everything that measures a **position**
#: is fine with that, because the screen is stretched the same way on purpose
#: (see `to_screen`). Everything that measures a **shape** is not: an angle, or
#: a vertical distance divided by a shoulder width, silently carries the
#: frame's aspect ratio in it.
#:
#: That was invisible while there was only ever one camera mode, and stopped
#: being invisible the moment a 4:3 mode was worth considering. A bone at a
#: true 45 degrees reads as 60.6 degrees in a 16:9 frame and 53.1 degrees in a
#: 4:3 one — **7.5 degrees of systematic error** on every diagonal limb, which
#: is most of a yoga tolerance and would mark Triangle Pose wrong for a player
#: doing it correctly.
#:
#: So the shape consumers work in this reference aspect rather than in the
#: camera's, and every tuned constant in `boxing/config.py`, `yoga/poses.py`
#: and `motion/gestures.py` keeps the meaning it was measured with, whatever
#: the camera is set to. 16:9 because that is what all of them were measured
#: on; the number matters only in that it does not change.
SHAPE_ASPECT = 16 / 9


def shape_scale(aspect: float) -> float:
    """Factor on x that takes camera space to `SHAPE_ASPECT` space.

    Derived rather than fitted. A horizontal distance `d` occupies `d / W` of a
    frame whose field is `W` wide, so re-expressing it in a reference frame of
    field `W_ref` is a multiply by `W / W_ref` — and with the vertical field
    shared, that ratio is just the ratio of the two aspects. A 4:3 crop of a
    16:9 field gives 0.75, which is exactly the fraction of the horizontal view
    it kept.

    y is left alone, so a purely vertical measurement is untouched and a purely
    horizontal one is scaled uniformly — only the mixed ones move, which are
    precisely the ones that were wrong.
    """
    if aspect <= 0:
        return 1.0
    return aspect / SHAPE_ASPECT


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


#: The grey Ultralytics trains YOLO with, so a model sees padding it recognises
#: as padding rather than as a large black object at the edge of every frame.
LETTERBOX_FILL = 114


def letterbox_image(frame, target_w: int, target_h: int,
                    fill: int = LETTERBOX_FILL, into=None,
                    swap_rb: bool = False):
    """Resize a camera frame into the model's square, padding to fit.

    Returns `(padded, Letterbox)`. numpy and cv2 are imported here rather than
    at module scope so that everything above — the arithmetic that actually
    gets things wrong — stays importable and testable on a machine with
    neither, which is where the tests run.

    `into` is a caller-owned `target_h x target_w x 3` uint8 array to write
    into, already filled with `fill`. Passing one is what lets the hot path
    skip both the allocation and the 1.2 MB memset of a grey border that is
    identical on every frame of a session; passing None allocates a fresh one,
    which is what the tests and any one-shot caller want.

    `swap_rb` converts BGR to RGB **after** the downscale rather than before.
    That ordering is the whole point of the flag: the camera hands back BGR,
    the model wants RGB, and doing it on the 640x360 result is a quarter of the
    pixels of doing it on the 1280x720 source. The alternative that reads more
    naturally — reversing the last axis at the camera and resizing the result —
    is the expensive one, because a reversed axis is a view with a negative
    stride and OpenCV cannot work on one: it silently copies the whole frame
    first, so the "free" slice costs a full-resolution copy *and* leaves the
    resize reading backwards through memory.
    """
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    box = Letterbox(width, height, target_w, target_h)
    new_w, new_h = box.scaled
    pad_x, pad_y = box.padding

    padded = into
    if padded is None:
        padded = np.full((target_h, target_w, 3), fill, dtype=np.uint8)

    window = padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w]
    # INTER_LINEAR rather than INTER_AREA, which is the better downscale.
    # Measured on the Pi: AREA costs about 3 ms more per frame at 1280x720 to
    # 640x360 and changes no keypoint by a pixel, and 3 ms is 10% of the frame
    # budget for a game whose whole problem is latency.
    #
    # `dst=window` writes straight into the padded square, which needs the
    # window to be one unbroken run of memory. It is whenever the fitted image
    # is as wide as the square — true for every camera frame wider than it is
    # tall, which is all of them — and the check is here rather than an
    # assumption because a portrait frame would silently get a resize that
    # OpenCV reallocated and nobody ever read.
    if window.flags["C_CONTIGUOUS"]:
        cv2.resize(frame, (new_w, new_h), dst=window,
                   interpolation=cv2.INTER_LINEAR)
    else:
        window[:] = cv2.resize(frame, (new_w, new_h),
                               interpolation=cv2.INTER_LINEAR)
    if swap_rb:
        cv2.cvtColor(window, cv2.COLOR_BGR2RGB, dst=window)
    return padded, box
