"""One skeleton, read two ways: the coach that is drawn and the pose that is scored.

Every yoga pose in this game is **fourteen bone directions and nothing else**.
That single table is run forwards through `forward_kinematics` to place the
coach's joints, and the joints are then run through `measure` to produce the
numbers the player is scored against. The player's own landmarks go through the
*same* `measure`. So the coach cannot be drawn in a pose the scorer is not
asking for — there is one number to edit per joint per pose, and both halves of
the game read it.

**Angles are absolute screen directions, in degrees, in image coordinates.**
0 is to the right, 90 is *down* (y grows downward in a camera image, and
fighting that convention here would mean fighting it in `yoga.js` too), 180 is
to the left and -90 is up. A bone's angle is the direction from its parent joint
to its child: `left_thigh` is the direction from the left hip to the left knee,
so a person standing up has both thighs at 90.

**The coach faces away from the player**, which is the same third-person view
the Boxing player is drawn in, and it is not a stylistic choice. The pose stream
arrives already mirrored (see `motion/geometry.mirror`), so a player's
anatomical left hand sits at the *smaller* x. A coach seen from behind has their
left hand at the smaller x as well. The two agree without a single conversion,
which means "raise your left arm" is the same sentence for both of them and no
part of this game ever has to decide which way round a side is.

**Scale is shoulder width.** Not height, not the bounding box: a player folded
forward has half the height and the same shoulders, and the bounding box also
changes when they raise their arms. This is the same reference `boxing/motion.py`
measures in, for the same reason — one set of numbers has to work for a tall
player at two metres and a short one at one and a half.

What this file deliberately cannot represent is a pose seen from the side or
lying on the floor. Both collapse the shoulder line, which is the scale, and
both put limbs behind other limbs where a 2D model has nothing to say about
them. Every pose in `poses.py` is therefore a frontal-plane pose — which is also
how a teacher demonstrates one to a room, so the constraint and the requirement
that the pose be *legible* turn out to be the same constraint.
"""

from __future__ import annotations

import math

#: Straight up, down, left and right, so the pose tables read as poses rather
#: than as arithmetic.
UP = -90.0
DOWN = 90.0
LEFT = 180.0
RIGHT = 0.0

#: Bone lengths, in units of the spine (hip centre to shoulder centre).
#:
#: Roughly anthropometric rather than heroically anime, and that matters more
#: than it looks: the ratio measurements in `measure` — how far apart the feet
#: are, how low the hips have sunk — take the *coach's* proportions as the
#: target a real person is compared against. A fashionably long-legged coach
#: would quietly ask every player to stand in a wider stance than the pose
#: actually wants. The legs are stylised by about 5%, which is a tenth of the
#: stance tolerance and is the whole budget that was available for it.
#:
#: Mirrored in `yoga.js` as `RIG`, because the browser does the same forward
#: kinematics to draw the tween between two poses. `tests/test_yoga.py` checks
#: the two copies agree.
RIG: dict[str, float] = {
    "spine": 1.00,
    "neck": 0.40,
    "upper_arm": 0.64,
    "forearm": 0.56,
    "thigh": 0.88,
    "shin": 0.86,
    "shoulder_half": 0.42,
    "hip_half": 0.30,
}

#: The fourteen. `shoulder_line` and `hip_line` are not really bones and are
#: usually left out of a pose table — they default to square across the spine,
#: which is true of a body that is upright or leaning sideways. They exist for
#: the one case where that is wrong: a forward fold drops the shoulders below
#: the hips without swapping which side each of them is on, and a rig that
#: derived the shoulder line from the spine alone would draw the coach's head
#: on backwards.
BONE_NAMES: tuple[str, ...] = (
    "spine", "neck", "shoulder_line", "hip_line",
    "left_upper_arm", "left_forearm",
    "right_upper_arm", "right_forearm",
    "left_thigh", "left_shin",
    "right_thigh", "right_shin",
)

#: A body standing still with its arms at its sides. Every pose in `poses.py`
#: is written as the handful of bones that differ from this, so a pose table
#: says what the pose *is* rather than restating what a body is.
STANDING: dict[str, float] = {
    "spine": UP,
    "neck": UP,
    "left_upper_arm": 96.0,
    "left_forearm": 96.0,
    "right_upper_arm": 84.0,
    "right_forearm": 84.0,
    "left_thigh": 90.0,
    "left_shin": 90.0,
    "right_thigh": 90.0,
    "right_shin": 90.0,
}

#: The joints `forward_kinematics` produces and `measure` consumes. The same
#: names the pose model uses, so the player's landmarks drop straight in.
JOINT_NAMES: tuple[str, ...] = (
    "head", "shoulder_mid", "hip_mid",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
)

#: Which landmark each rig joint is read from when the body is a real person.
#: `nose` stands in for the head because it is the only head point the pose
#: model reports reliably at this distance, and only its *direction* from the
#: shoulders is ever used.
LANDMARK_FOR: dict[str, str] = {
    "head": "nose",
    "left_shoulder": "left_shoulder", "right_shoulder": "right_shoulder",
    "left_elbow": "left_elbow", "right_elbow": "right_elbow",
    "left_wrist": "left_wrist", "right_wrist": "right_wrist",
    "left_hip": "left_hip", "right_hip": "right_hip",
    "left_knee": "left_knee", "right_knee": "right_knee",
    "left_ankle": "left_ankle", "right_ankle": "right_ankle",
}


def wrap(degrees: float) -> float:
    """Fold an angle into (-180, 180]."""
    value = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if value == -180.0 else value


def angular_distance(first: float, second: float) -> float:
    """How far apart two directions are, the short way round. Always >= 0."""
    return abs(wrap(first - second))


def _step(origin: tuple[float, float], degrees: float,
          length: float) -> tuple[float, float]:
    radians = math.radians(degrees)
    return (origin[0] + length * math.cos(radians),
            origin[1] + length * math.sin(radians))


def bones(**overrides: float) -> dict[str, float]:
    """A pose table: `STANDING`, with the bones this pose actually changes."""
    table = dict(STANDING)
    for name, value in overrides.items():
        if name not in BONE_NAMES:
            raise KeyError(f"{name!r} is not a bone; see BONE_NAMES")
        table[name] = float(value)
    return table


def forward_kinematics(table: dict[str, float],
                       scales: dict[str, float] | None = None
                       ) -> dict[str, tuple[float, float]]:
    """Place every joint, with the hip centre at the origin.

    `scales` shortens individual bones and exists for one honest reason: a
    frontal camera sees a limb pointing towards it as a *short* limb, not as a
    limb at a different angle. A forward fold's spine is not bent — it is
    rotated out of the image plane, and the only faithful thing a 2D rig can do
    with that is draw it short. Because the scoring targets are measured off
    these same joints, shortening the coach's spine also tells the scorer to
    expect a short one, which is exactly what the camera will report.
    """
    scales = scales or {}

    def length(name: str) -> float:
        return RIG[name] * float(scales.get(name, 1.0))

    spine = table["spine"]
    shoulder_line = table.get("shoulder_line", spine - 90.0)
    hip_line = table.get("hip_line", spine - 90.0)

    hip_mid = (0.0, 0.0)
    shoulder_mid = _step(hip_mid, spine, length("spine"))
    joints: dict[str, tuple[float, float]] = {
        "hip_mid": hip_mid,
        "shoulder_mid": shoulder_mid,
        "head": _step(shoulder_mid, table["neck"], length("neck")),
        # `shoulder_line` points towards the body's own left, which on a coach
        # seen from behind is towards the left of the screen.
        "left_shoulder": _step(shoulder_mid, shoulder_line, length("shoulder_half")),
        "right_shoulder": _step(shoulder_mid, shoulder_line + 180.0,
                                length("shoulder_half")),
        "left_hip": _step(hip_mid, hip_line, length("hip_half")),
        "right_hip": _step(hip_mid, hip_line + 180.0, length("hip_half")),
    }
    for side in ("left", "right"):
        joints[f"{side}_elbow"] = _step(
            joints[f"{side}_shoulder"], table[f"{side}_upper_arm"],
            length("upper_arm"))
        joints[f"{side}_wrist"] = _step(
            joints[f"{side}_elbow"], table[f"{side}_forearm"], length("forearm"))
        joints[f"{side}_knee"] = _step(
            joints[f"{side}_hip"], table[f"{side}_thigh"], length("thigh"))
        joints[f"{side}_ankle"] = _step(
            joints[f"{side}_knee"], table[f"{side}_shin"], length("shin"))
    return joints


# ── what a pose is measured by ───────────────────────────────────────
#
# Twelve angles and seven ratios. The angles are *signed* differences between
# two bone directions, and the sign is load-bearing: an unsigned elbow angle
# cannot tell an arm reaching out to the side from the same arm folded across
# the chest, and both are 90 degrees from the spine.
#
# Every one of these is invariant to where the player is standing and how big
# they are. The angles are invariant by construction; the ratios are divided by
# shoulder width, which is the one length on a body that a frontal pose never
# changes.

#: metric -> the joints it cannot be computed without.
METRIC_JOINTS: dict[str, tuple[str, ...]] = {
    "left_elbow": ("left_shoulder", "left_elbow", "left_wrist"),
    "right_elbow": ("right_shoulder", "right_elbow", "right_wrist"),
    "left_shoulder": ("left_shoulder", "left_elbow", "shoulder_mid", "hip_mid"),
    "right_shoulder": ("right_shoulder", "right_elbow", "shoulder_mid", "hip_mid"),
    "left_hip": ("left_hip", "left_knee", "shoulder_mid", "hip_mid"),
    "right_hip": ("right_hip", "right_knee", "shoulder_mid", "hip_mid"),
    "left_knee": ("left_hip", "left_knee", "left_ankle"),
    "right_knee": ("right_hip", "right_knee", "right_ankle"),
    "torso": ("shoulder_mid", "hip_mid"),
    "head": ("head", "shoulder_mid", "hip_mid"),
    "stance": ("left_ankle", "right_ankle"),
    "stack": ("hip_mid", "left_ankle", "right_ankle"),
    "reach": ("left_wrist", "right_wrist"),
    "left_hand_height": ("left_wrist", "shoulder_mid"),
    "right_hand_height": ("right_wrist", "shoulder_mid"),
    "left_foot_height": ("left_ankle", "hip_mid"),
    "right_foot_height": ("right_ankle", "hip_mid"),
}

#: The ones measured in degrees. The rest are lengths over shoulder width, and
#: the two are compared differently — an angle wraps and a ratio does not.
ANGLE_METRICS: frozenset[str] = frozenset({
    "left_elbow", "right_elbow", "left_shoulder", "right_shoulder",
    "left_hip", "right_hip", "left_knee", "right_knee", "torso", "head",
})

#: Left becomes right under a mirror, and every signed angle changes sign with
#: it: mirroring x sends a direction θ to 180 - θ, so a difference of two
#: directions simply negates. This is what lets `scoring.py` ask the one
#: question a yoga game has to be able to answer — "is this person doing the
#: pose beautifully on the wrong side?" — for the price of a second comparison
#: rather than a second pose table.
MIRROR_PAIRS: tuple[tuple[str, str], ...] = (
    ("left_elbow", "right_elbow"),
    ("left_shoulder", "right_shoulder"),
    ("left_hip", "right_hip"),
    ("left_knee", "right_knee"),
    ("left_hand_height", "right_hand_height"),
    ("left_foot_height", "right_foot_height"),
)

#: The shoulder span below which nothing is measured. A player who has turned
#: side-on, or walked most of the way out of frame, produces a tiny span and
#: then every ratio divided by it explodes. Matches the floor
#: `boxing/motion.py` and `motion/gestures.py` already use.
MIN_SPAN = 0.055


def _direction(start: tuple[float, float], end: tuple[float, float]) -> float:
    return math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))


def measure(joints: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Turn joint positions into the numbers a pose is judged by.

    Fed by `forward_kinematics` for the coach and by `player_joints` for the
    person in front of the camera, and it must stay that way: the moment the
    two sides measure the same thing two ways, a pose becomes unscorable in a
    way no test that only looks at one of them can see.

    Joints that are missing simply produce no metrics — see `METRIC_JOINTS`.
    The caller decides what an unmeasurable knee means, because the answer is
    different for "the player is standing too close" and "the player has their
    leg behind them".
    """
    present = {name: point for name, point in joints.items() if point is not None}
    if "left_shoulder" in present and "right_shoulder" in present:
        present.setdefault("shoulder_mid", (
            (present["left_shoulder"][0] + present["right_shoulder"][0]) / 2,
            (present["left_shoulder"][1] + present["right_shoulder"][1]) / 2))
    if "left_hip" in present and "right_hip" in present:
        present.setdefault("hip_mid", (
            (present["left_hip"][0] + present["right_hip"][0]) / 2,
            (present["left_hip"][1] + present["right_hip"][1]) / 2))

    span = 0.0
    if "left_shoulder" in present and "right_shoulder" in present:
        span = math.dist(present["left_shoulder"], present["right_shoulder"])
    if span < MIN_SPAN:
        return {}

    def has(metric: str) -> bool:
        return all(name in present for name in METRIC_JOINTS[metric])

    out: dict[str, float] = {}
    spine_down = 0.0
    if "shoulder_mid" in present and "hip_mid" in present:
        spine = _direction(present["hip_mid"], present["shoulder_mid"])
        spine_down = wrap(spine + 180.0)
        out["torso"] = wrap(spine - UP)
        if has("head"):
            out["head"] = wrap(
                _direction(present["shoulder_mid"], present["head"]) - spine)

    for side in ("left", "right"):
        if has(f"{side}_elbow"):
            upper = _direction(present[f"{side}_shoulder"], present[f"{side}_elbow"])
            fore = _direction(present[f"{side}_elbow"], present[f"{side}_wrist"])
            # Zero is a straight arm; the magnitude is how far it is folded and
            # the sign is which way.
            out[f"{side}_elbow"] = wrap(fore - upper)
        if has(f"{side}_shoulder"):
            upper = _direction(present[f"{side}_shoulder"], present[f"{side}_elbow"])
            # Measured against the spine rather than against vertical, so a
            # player leaning into a triangle is not told their arm is wrong.
            out[f"{side}_shoulder"] = wrap(upper - spine_down)
        if has(f"{side}_hip"):
            thigh = _direction(present[f"{side}_hip"], present[f"{side}_knee"])
            out[f"{side}_hip"] = wrap(thigh - spine_down)
        if has(f"{side}_knee"):
            thigh = _direction(present[f"{side}_hip"], present[f"{side}_knee"])
            shin = _direction(present[f"{side}_knee"], present[f"{side}_ankle"])
            out[f"{side}_knee"] = wrap(shin - thigh)
        if has(f"{side}_hand_height"):
            out[f"{side}_hand_height"] = (
                present["shoulder_mid"][1] - present[f"{side}_wrist"][1]) / span
        if has(f"{side}_foot_height"):
            out[f"{side}_foot_height"] = (
                present["hip_mid"][1] - present[f"{side}_ankle"][1]) / span

    if has("stance"):
        out["stance"] = abs(
            present["left_ankle"][0] - present["right_ankle"][0]) / span
    if has("reach"):
        out["reach"] = abs(
            present["left_wrist"][0] - present["right_wrist"][0]) / span
    if has("stack"):
        # How far the hips sit above the feet. This is the *only* thing a
        # frontal camera can see about a squat: bending the knees in the
        # sagittal plane moves almost nothing sideways, so Chair pose and
        # standing up straight have nearly the same knee angle on screen and
        # very different numbers here.
        ankle_y = (present["left_ankle"][1] + present["right_ankle"][1]) / 2
        out["stack"] = (ankle_y - present["hip_mid"][1]) / span
    return out


def mirrored(metrics: dict[str, float]) -> dict[str, float]:
    """The same body, reflected. Left/right swap and every angle changes sign."""
    swap = {}
    for left, right in MIRROR_PAIRS:
        swap[left] = right
        swap[right] = left
    out: dict[str, float] = {}
    for name, value in metrics.items():
        target = swap.get(name, name)
        out[target] = -value if name in ANGLE_METRICS else value
    return out


def player_joints(person, confidence: float) -> dict[str, tuple[float, float]]:
    """Rig joints from one `PersonPose`, dropping what the model is unsure of.

    Omitted rather than passed through with a low confidence, which is the
    choice `PersonPose.silhouette` makes and for the same reason: a knee angle
    computed through a guessed ankle is not an uncertain knee angle, it is a
    wrong one, and it would be scored as confidently as a real one.
    """
    if person is None:
        return {}
    # `measure` reads these as a *shape* — twelve signed angles and seven
    # ratios — so they are taken in the reference camera shape rather than in
    # the camera's own, where a diagonal limb carries the frame's aspect ratio
    # in it. A 4:3 camera moves a true 45-degree bone by 7.5 degrees, which is
    # most of a pose tolerance. See `PersonPose.shaped`.
    person = person.shaped()
    joints: dict[str, tuple[float, float]] = {}
    for joint, landmark in LANDMARK_FOR.items():
        point = person.point(landmark)
        if point is not None and point.confidence >= confidence:
            joints[joint] = (point.x, point.y)
    return joints


def pose_metrics(table: dict[str, float],
                 scales: dict[str, float] | None = None) -> dict[str, float]:
    """The scoring target for one pose table. Coach in, numbers out."""
    return measure(forward_kinematics(table, scales))


#: What a body standing still measures. Used by `scoring.weights` to work out
#: which joints a pose is actually *about*: a metric a pose leaves exactly
#: where standing leaves it cannot tell that pose apart from doing nothing, and
#: should not be carrying a full share of the score for it.
STANDING_METRICS: dict[str, float] = pose_metrics(STANDING)
