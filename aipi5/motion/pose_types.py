"""What a pose is, once it has stopped being a tensor.

This is the whole vocabulary between the accelerator and everything that wants
to know where somebody's hands are. Deliberately small, deliberately free of
numpy, and deliberately not shaped like the model that produced it: a game
consuming these must not have to know that the pose came off a YOLOv8 head at
640x640 with letterbox padding, because the next model will not.

**Coordinates are normalised to the camera image, 0.0 to 1.0.** Not to the
model input, which is a padded square that does not correspond to anything a
person can see, and not to the screen, which is 1280x800 today and is the
game's business rather than the pose service's. One conversion happens in
`aipi5/motion/geometry.py`, on the way out of the decoder, and after that
nothing downstream multiplies by a resolution it had to look up.

**x is already mirrored** by the time it reaches here — see `geometry.mirror`.
That is a decision about what the numbers *mean* rather than a display
convenience: a consumer that received unmirrored coordinates and mirrored them
itself would be a consumer that can get it wrong, and there is exactly one
right answer for a person standing in front of a camera pointed at them.

**The full skeleton is always carried, never only the wrists.** Section 51:
Fruit Ninja needs two keypoints and the next four games need shoulders, hips,
knees and ankles, and a service that exposed only what its first consumer used
would have to be rewritten for its second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# COCO's seventeen, in the order every COCO-trained pose model emits them. The
# index is what comes out of the tensor; the name is what the rest of the
# project uses, because `keypoints["left_wrist"]` survives a model swap and
# `keypoints[9]` silently does not.
KEYPOINT_NAMES: tuple[str, ...] = (
    "nose",                                    # 0
    "left_eye", "right_eye",                   # 1, 2
    "left_ear", "right_ear",                   # 3, 4
    "left_shoulder", "right_shoulder",         # 5, 6
    "left_elbow", "right_elbow",               # 7, 8
    "left_wrist", "right_wrist",               # 9, 10
    "left_hip", "right_hip",                   # 11, 12
    "left_knee", "right_knee",                 # 13, 14
    "left_ankle", "right_ankle",               # 15, 16
)

#: Reverse lookup, built once. Used by the decoder and by tests.
KEYPOINT_INDEX: dict[str, int] = {name: i for i, name in enumerate(KEYPOINT_NAMES)}

#: The two that Fruit Ninja is entirely about. Named here rather than in the
#: game because the calibration screen and the debug overlay want them too, and
#: three copies of the string "left_wrist" is three places to typo it.
WRISTS: tuple[str, str] = ("left_wrist", "right_wrist")

#: The joints a stylised body needs, and no more. Section 5 of the Fruit Ninja
#: upgrade: the player's shadow behind the fruit is built from these rather
#: than from a segmentation mask, because the accelerator is already running
#: one model and a second one would cost the pose rate the game is built on.
#:
#: The upper body is the minimum useful figure, while knees and ankles are sent
#: opportunistically. They are often outside the frame at the distance this
#: game is played from, so the renderer never requires them; when they *are*
#: believable they let the ninja wear the loose, tapered trousers in the visual
#: reference instead of ending abruptly at the hips.
#:
#: Fifteen points at two rounded numbers each is still only a few hundred bytes
#: a frame. Eyes stay debug-only because the hood asset does not need them.
SILHOUETTE: tuple[str, ...] = (
    "nose",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
    "left_ear", "right_ear",
)

#: What "the player is standing where the camera can see them" means, checked
#: by `PersonPose.upper_body_visible`. Shoulders rather than hips because
#: Fruit Ninja is played from about the waist up and requiring hips would push
#: everybody a metre further back for no gain — section 21.
UPPER_BODY: tuple[str, ...] = ("left_shoulder", "right_shoulder")

# How the skeleton joins up, for the debug overlay only. Never used by
# gameplay. Pairs of indices into KEYPOINT_NAMES.
SKELETON: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 3), (0, 2), (2, 4),            # head
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),   # arms across the shoulders
    (5, 11), (6, 12), (11, 12),                # torso
    (11, 13), (12, 14), (13, 15), (14, 16),    # legs
)


@dataclass(frozen=True)
class KeyPoint:
    """One joint: where it is, and how much the model believes it.

    `x` and `y` are normalised to the camera image and may fall outside
    0.0-1.0. That is not a bug to clamp away here — YOLOv8-pose will happily
    place a wrist just off the edge of the frame when most of the arm is
    visible, and that estimate is usually right. Whoever draws it decides what
    to do about it; `Consumers` that need a screen coordinate clamp there.
    """

    x: float
    y: float
    confidence: float

    def believable(self, threshold: float) -> bool:
        return self.confidence >= threshold

    def as_dict(self) -> dict:
        # Rounded on the way out. These go over an HTTP poll several times a
        # second and the sixteenth decimal place of a wrist position is bytes
        # spent on noise — the underlying estimate is not accurate to the pixel,
        # let alone to a millionth of a frame width.
        return {"x": round(self.x, 4), "y": round(self.y, 4),
                "confidence": round(self.confidence, 3)}


@dataclass(frozen=True)
class PersonPose:
    """One person the model found, with as much of their skeleton as it saw.

    `keypoints` is keyed by name and is always complete — all seventeen, every
    frame, including the ones the model is not confident about. Absence would
    mean two different things (not detected, or detected badly) and a consumer
    would have to handle both anyway; a low confidence says which.
    """

    confidence: float
    keypoints: dict[str, KeyPoint]
    #: Normalised (x, y, width, height) in camera space. What person selection
    #: uses — see `pose_filter.choose_player` — and never used for drawing.
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    #: Stable across frames while this person keeps being matched to the same
    #: track. 0 means untracked.
    track_id: int = 0

    def point(self, name: str) -> KeyPoint | None:
        return self.keypoints.get(name)

    def visible(self, name: str, threshold: float) -> bool:
        point = self.keypoints.get(name)
        return point is not None and point.confidence >= threshold

    def upper_body_visible(self, threshold: float) -> bool:
        """Both shoulders. The calibration screen's "stand back a bit" test."""
        return all(self.visible(name, threshold) for name in UPPER_BODY)

    def silhouette(self, threshold: float) -> dict:
        """The joints a body outline is drawn from, filtered.

        Joints below `threshold` are **omitted rather than sent with a low
        confidence**, which is the opposite of what `as_dict` does and is
        deliberate. `as_dict` serves a debug overlay whose whole job is to show
        what the model believes, including where it is unsure. This serves a
        shadow, and a shadow drawn through a guessed elbow does not look
        uncertain — it looks like the player has a broken arm. Whoever draws it
        can only leave out what is not here.
        """
        return {name: (round(point.x, 3), round(point.y, 3))
                for name in SILHOUETTE
                if (point := self.keypoints.get(name)) is not None
                and point.confidence >= threshold}

    def wrists_visible(self, threshold: float) -> bool:
        return all(self.visible(name, threshold) for name in WRISTS)

    @property
    def area(self) -> float:
        """Bounding-box area, which is how "closest to the camera" is decided."""
        return max(0.0, self.box[2]) * max(0.0, self.box[3])

    @property
    def centre_x(self) -> float:
        return self.box[0] + self.box[2] / 2

    def as_dict(self) -> dict:
        return {
            "confidence": round(self.confidence, 3),
            "track_id": self.track_id,
            "box": [round(v, 4) for v in self.box],
            "keypoints": {name: point.as_dict()
                          for name, point in self.keypoints.items()},
        }


@dataclass(frozen=True)
class PoseFrame:
    """Everything the model saw in one camera frame.

    `timestamp` is `time.monotonic()` at *capture*, not at decode, and the
    difference is the whole point of carrying it: it is what lets the game
    measure how stale the hand position it is drawing actually is, and what
    `pose_filter` differentiates against to get a slash velocity that does not
    change when the accelerator has a slow frame.
    """

    timestamp: float
    persons: tuple[PersonPose, ...] = ()
    #: How long the accelerator took on this frame, in seconds. For the debug
    #: panel and the performance report; never used by gameplay.
    inference_s: float = 0.0
    #: Camera frame size this was decoded from, for the debug overlay's benefit.
    source_size: tuple[int, int] = (0, 0)

    @property
    def empty(self) -> bool:
        return not self.persons

    def as_dict(self) -> dict:
        return {
            "timestamp": round(self.timestamp, 3),
            "inference_ms": round(self.inference_s * 1000, 1),
            "persons": [person.as_dict() for person in self.persons],
        }


@dataclass
class PoseStats:
    """Counters the debug panel and the final report read.

    Mutable and deliberately not locked. Every field is written by exactly one
    thread (the pose loop) and read by others for display, and on CPython an
    int or float assignment is atomic — a settings page that reads a frame
    count one frame stale is not a problem worth a lock on the inference path.
    """

    frames: int = 0
    dropped: int = 0
    #: Exponentially smoothed, so the number on screen does not flicker.
    inference_fps: float = 0.0
    camera_fps: float = 0.0
    last_inference_s: float = 0.0
    #: Capture to decoded pose, which is the half of hand-to-screen latency
    #: this process is responsible for.
    pipeline_s: float = 0.0
    errors: int = 0
    detail: str = ""
    history: list = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "dropped": self.dropped,
            "inference_fps": round(self.inference_fps, 1),
            "camera_fps": round(self.camera_fps, 1),
            "inference_ms": round(self.last_inference_s * 1000, 1),
            "pipeline_ms": round(self.pipeline_s * 1000, 1),
            "errors": self.errors,
            "detail": self.detail,
        }
