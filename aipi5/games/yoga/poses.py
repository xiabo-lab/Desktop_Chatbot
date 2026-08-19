"""The pose library: thirty-three frontal-plane poses, as bone tables.

Each entry is the handful of bone directions that differ from a body standing
still, plus what to call the pose and what to say about it. Everything else —
where the coach's knee goes on screen, what angle the player's knee is supposed
to be at, how wide their feet should be — is derived from that one table by
`rig.pose_metrics`. There is no second list of target numbers to keep in step
with the first, because there was never a first.

**Every pose is presented in the frontal plane** — square to the camera, limbs
moving left and right rather than towards the lens. That is a hard requirement
rather than a preference, and `rig.py` explains the half of it that is about
measurement: shoulder width is the scale everything is divided by, and a body
turned side-on has no shoulder width. The other half is that a pose a camera
cannot see is also a pose a player cannot copy, so the poses that survive the
constraint are exactly the ones a teacher would turn to face the room for.

What is therefore *not* here: anything on the floor (the Brio is on a desk and
sees a standing person; a person lying down is out of frame), anything turned
side-on, and deep twists, whose whole content is a rotation the camera is
looking straight down. Sun Salutation's floor half is the notable casualty. The
standing half of it — fold, half-lift, upward salute — is in the warm-up.

Nothing here is therapeutic, prenatal or injury-specific, and it is not
supposed to be: this is a game that asks somebody to copy a shape, and a
sequence built for a condition is a different thing with a different duty of
care.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from aipi5.games.yoga.rig import bones, pose_metrics

#: Shorthand for `relax`, so a pose that is entirely about the arms can say so
#: without listing eight metrics it does not care about.
LEGS: tuple[str, ...] = (
    "left_hip", "right_hip", "left_knee", "right_knee",
    "stance", "stack", "left_foot_height", "right_foot_height")
ARMS: tuple[str, ...] = (
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "reach", "left_hand_height", "right_hand_height")


@dataclass(frozen=True)
class Pose:
    """One shape, and the two sentences that get somebody into it.

    `emphasis` and `relax` move a metric's weight up or down for this pose
    only. They are the difference between "your knee is at the wrong angle"
    being the point of Warrior II and being noise in Mountain Pose, and they
    are also how a pose says which of its numbers the camera can actually see:
    Chair is scored on how low the hips have sunk and explicitly *not* on the
    knee bend, because from the front there is barely any knee bend to see.
    """

    id: str
    name: str
    sanskrit: str
    instruction: str
    cue: str
    #: 1 easy, 2 moderate, 3 hard. Only used to build lessons and to check that
    #: the ones we built go in the right direction — see `lesson.py`.
    level: int
    family: str
    table: dict[str, float]
    scales: dict[str, float] = field(default_factory=dict)
    #: "" for a symmetric pose; otherwise the side that is doing the work, so
    #: a lesson can be checked for doing both of them.
    side: str = ""
    emphasis: tuple[str, ...] = ()
    relax: tuple[str, ...] = ()
    #: Multiplies every tolerance for this pose. Above 1 for shapes a frontal
    #: camera reports poorly — a forward fold's spine is pointing at the lens
    #: and its estimated direction is correspondingly vague.
    tolerance_scale: float = 1.0
    #: Feedback wording this pose wants instead of the generic phrasing, keyed
    #: by metric: (what to say when the player has too little, too much).
    cues: dict[str, tuple[str, str]] = field(default_factory=dict)

    @cached_property
    def targets(self) -> dict[str, float]:
        """What the coach's body measures. The thing the player is compared to."""
        return pose_metrics(self.table, self.scales)

    @property
    def mirror_id(self) -> str:
        """The same pose on the other side, or this one if it is symmetric."""
        if self.side == "left":
            return self.id[:-5] + "_right"
        if self.side == "right":
            return self.id[:-6] + "_left"
        return self.id


# ── warm-up ──────────────────────────────────────────────────────────

_POSES: tuple[Pose, ...] = (
    Pose(
        id="mountain", name="Mountain Pose", sanskrit="Tadasana", level=1,
        family="warmup",
        instruction="Stand tall with your feet under your hips.",
        cue="Arms relaxed by your sides, shoulders down.",
        table=bones(),
        emphasis=("torso", "stance"),
        relax=("head", "left_elbow", "right_elbow"),
    ),
    Pose(
        id="mountain_breath", name="Mountain Breath", sanskrit="Samasthiti",
        level=1, family="cooldown",
        instruction="Bring your palms together at your chest.",
        cue="Close your eyes and slow your breathing.",
        table=bones(left_upper_arm=100, left_forearm=-30,
                    right_upper_arm=80, right_forearm=-150),
        emphasis=("reach", "left_hand_height", "right_hand_height"),
        relax=("head",),
    ),
    Pose(
        id="upward_salute", name="Upward Salute", sanskrit="Urdhva Hastasana",
        level=1, family="warmup",
        instruction="Sweep both arms straight up over your head.",
        cue="Reach through your fingertips and lift your ribs.",
        table=bones(left_upper_arm=-96, left_forearm=-93,
                    right_upper_arm=-84, right_forearm=-87),
        emphasis=("left_hand_height", "right_hand_height",
                  "left_shoulder", "right_shoulder"),
    ),
    Pose(
        id="cactus_arms", name="Cactus Arms", sanskrit="Nirlamba Bhujangasana",
        level=1, family="warmup",
        instruction="Open your arms wide and bend both elbows upward.",
        cue="Elbows at shoulder height, palms facing forward.",
        table=bones(left_upper_arm=175, left_forearm=-88,
                    right_upper_arm=5, right_forearm=-92),
        emphasis=("left_elbow", "right_elbow", "left_shoulder", "right_shoulder"),
        cues={"left_elbow": ("Bend your left elbow up to shoulder height",
                             "Open your left elbow wider"),
              "right_elbow": ("Bend your right elbow up to shoulder height",
                              "Open your right elbow wider")},
    ),
    Pose(
        id="neck_tilt_left", name="Neck Release", sanskrit="Greeva Sanchalana",
        level=1, family="warmup", side="left",
        instruction="Tilt your head gently towards your left shoulder.",
        cue="Let the opposite shoulder stay heavy and low.",
        table=bones(neck=-122),
        # The whole pose is one joint, so the generous default band would make
        # it indistinguishable from standing still — see the confusability
        # check in `tests/test_yoga.py`.
        tolerance_scale=0.50,
        emphasis=("head",),
        relax=LEGS + ARMS,
        cues={"head": ("Tilt your head further to the left",
                       "Ease your head back towards centre")},
    ),
    Pose(
        id="neck_tilt_right", name="Neck Release", sanskrit="Greeva Sanchalana",
        level=1, family="warmup", side="right",
        instruction="Tilt your head gently towards your right shoulder.",
        cue="Let the opposite shoulder stay heavy and low.",
        table=bones(neck=-58),
        tolerance_scale=0.50,
        emphasis=("head",),
        relax=LEGS + ARMS,
        cues={"head": ("Tilt your head further to the right",
                       "Ease your head back towards centre")},
    ),
    Pose(
        id="shoulder_opener", name="Eagle Arms", sanskrit="Garudasana Bahu",
        level=1, family="warmup",
        instruction="Cross your arms in front and stack the elbows.",
        cue="Lift the elbows to shoulder height and draw them away from you.",
        # Arms *in front* rather than behind, and that is the camera's doing:
        # arms swept back are pointing away from the lens, so from the front
        # they are indistinguishable from arms hanging down. Eagle arms open
        # the same shoulders and are unmistakable from any angle.
        table=bones(left_upper_arm=55, left_forearm=-70,
                    right_upper_arm=125, right_forearm=-110),
        emphasis=("left_elbow", "right_elbow", "reach",
                  "left_hand_height", "right_hand_height"),
        relax=LEGS + ("torso", "head"),
    ),
    Pose(
        id="side_bend_left", name="Standing Side Bend",
        sanskrit="Parsva Tadasana", level=1, family="warmup", side="left",
        instruction="Reach your right arm over and lean to your left.",
        cue="Keep both feet planted and your hips level.",
        table=bones(spine=-112, neck=-112,
                    right_upper_arm=-112, right_forearm=-125,
                    left_upper_arm=100, left_forearm=100),
        emphasis=("torso", "right_hand_height"),
        cues={"torso": ("Lean a little further to your left",
                        "Come back up — you have leaned too far")},
    ),
    Pose(
        id="side_bend_right", name="Standing Side Bend",
        sanskrit="Parsva Tadasana", level=1, family="warmup", side="right",
        instruction="Reach your left arm over and lean to your right.",
        cue="Keep both feet planted and your hips level.",
        table=bones(spine=-68, neck=-68,
                    left_upper_arm=-68, left_forearm=-55,
                    right_upper_arm=80, right_forearm=80),
        emphasis=("torso", "left_hand_height"),
        cues={"torso": ("Lean a little further to your right",
                        "Come back up — you have leaned too far")},
    ),
    Pose(
        id="crescent_moon_left", name="Crescent Moon",
        sanskrit="Ardha Chandrasana", level=2, family="standing", side="left",
        instruction="Reach both arms overhead and curve towards your left.",
        cue="Lengthen upward first, then bend to the side.",
        table=bones(spine=-115, neck=-115,
                    left_upper_arm=-124, left_forearm=-130,
                    right_upper_arm=-118, right_forearm=-126),
        emphasis=("torso", "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="crescent_moon_right", name="Crescent Moon",
        sanskrit="Ardha Chandrasana", level=2, family="standing", side="right",
        instruction="Reach both arms overhead and curve towards your right.",
        cue="Lengthen upward first, then bend to the side.",
        table=bones(spine=-65, neck=-65,
                    right_upper_arm=-56, right_forearm=-50,
                    left_upper_arm=-62, left_forearm=-54),
        emphasis=("torso", "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="forward_fold", name="Standing Forward Fold",
        sanskrit="Uttanasana", level=1, family="warmup",
        instruction="Hinge at your hips and fold down over your legs.",
        cue="Let your head and arms hang heavy.",
        # `shoulder_line` and `hip_line` both, and forgetting the second one is
        # not a subtle bug: the default derives them from the spine, so a spine
        # that points downwards puts the coach's left hip on their right and
        # silently halves every stance measurement taken from it.
        table=bones(spine=92, neck=92, shoulder_line=180, hip_line=180,
                    left_upper_arm=96, left_forearm=96,
                    right_upper_arm=84, right_forearm=84),
        scales={"spine": 0.42, "neck": 0.70},
        # A folded spine points at the lens, so its estimated direction is the
        # least reliable number on the whole body. Loose, and scored mostly on
        # where the hands have ended up.
        tolerance_scale=1.35,
        emphasis=("torso", "stance"),
        relax=("head", "left_elbow", "right_elbow"),
        cues={"torso": ("Fold further forward from your hips",
                        "Come up a little — you are folding past your legs")},
    ),

    # ── standing ─────────────────────────────────────────────────────

    Pose(
        id="chair", name="Chair Pose", sanskrit="Utkatasana", level=2,
        family="standing",
        instruction="Bend your knees and sit your hips back and down.",
        cue="Both arms reach up beside your ears.",
        table=bones(spine=-90, neck=-90,
                    left_thigh=96, left_shin=84,
                    right_thigh=84, right_shin=96,
                    left_upper_arm=-98, left_forearm=-95,
                    right_upper_arm=-82, right_forearm=-85),
        scales={"thigh": 0.56, "spine": 0.90},
        # The knees bend towards the camera, so from the front there is almost
        # no knee angle to measure. How far the hips have dropped is the whole
        # signal, and it is a good one.
        emphasis=("stack", "left_hand_height", "right_hand_height"),
        relax=("left_knee", "right_knee"),
        cues={"stack": ("Sit lower — bend your knees more",
                        "Rise up a little, you have gone too low")},
    ),
    Pose(
        id="star", name="Star Pose", sanskrit="Utthita Tadasana", level=1,
        family="standing",
        instruction="Step your feet wide and open your arms up and out.",
        cue="Make a bright, even star with straight limbs.",
        table=bones(left_thigh=110, left_shin=104,
                    right_thigh=70, right_shin=76,
                    left_upper_arm=-140, left_forearm=-140,
                    right_upper_arm=-40, right_forearm=-40),
        emphasis=("stance", "reach", "left_shoulder", "right_shoulder"),
    ),
    Pose(
        id="goddess", name="Goddess Pose", sanskrit="Utkata Konasana", level=2,
        family="standing",
        instruction="Feet wide and turned out, bend both knees over your toes.",
        cue="Elbows bent at shoulder height, chest lifted.",
        table=bones(left_thigh=135, left_shin=90,
                    right_thigh=45, right_shin=90,
                    left_upper_arm=175, left_forearm=-88,
                    right_upper_arm=5, right_forearm=-92),
        emphasis=("left_knee", "right_knee", "stance", "stack"),
    ),
    Pose(
        id="wide_leg_fold", name="Wide-Legged Forward Fold",
        sanskrit="Prasarita Padottanasana", level=2, family="standing",
        instruction="Feet wide and straight, fold forward between them.",
        cue="Let your hands travel down towards the floor.",
        table=bones(spine=90, neck=90, shoulder_line=180, hip_line=180,
                    left_thigh=115, left_shin=115,
                    right_thigh=65, right_shin=65,
                    left_upper_arm=96, left_forearm=96,
                    right_upper_arm=84, right_forearm=84),
        scales={"spine": 0.45, "neck": 0.70},
        tolerance_scale=1.30,
        emphasis=("torso", "stance"),
        relax=("head", "left_elbow", "right_elbow"),
    ),
    Pose(
        id="warrior_one_left", name="Warrior I", sanskrit="Virabhadrasana I",
        level=2, family="standing", side="left",
        instruction="Left knee bent over the ankle, right leg long behind.",
        cue="Both arms sweep straight up over your head.",
        table=bones(left_thigh=145, left_shin=92,
                    right_thigh=52, right_shin=52,
                    left_upper_arm=-98, left_forearm=-95,
                    right_upper_arm=-82, right_forearm=-85),
        emphasis=("left_knee", "right_knee", "stance",
                  "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="warrior_one_right", name="Warrior I", sanskrit="Virabhadrasana I",
        level=2, family="standing", side="right",
        instruction="Right knee bent over the ankle, left leg long behind.",
        cue="Both arms sweep straight up over your head.",
        table=bones(right_thigh=35, right_shin=88,
                    left_thigh=128, left_shin=128,
                    left_upper_arm=-98, left_forearm=-95,
                    right_upper_arm=-82, right_forearm=-85),
        emphasis=("left_knee", "right_knee", "stance",
                  "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="warrior_two_left", name="Warrior II", sanskrit="Virabhadrasana II",
        level=2, family="standing", side="left",
        instruction="Bend your left knee and open both arms wide.",
        cue="Look over your left hand; sink your hips low.",
        table=bones(neck=-100,
                    left_thigh=150, left_shin=92,
                    right_thigh=55, right_shin=55,
                    left_upper_arm=180, left_forearm=180,
                    right_upper_arm=0, right_forearm=0),
        emphasis=("left_knee", "right_knee", "stance", "reach",
                  "left_shoulder", "right_shoulder"),
        cues={"left_knee": ("Bend your left knee more — take it over the ankle",
                            "Ease your left knee back over your ankle")},
    ),
    Pose(
        id="warrior_two_right", name="Warrior II", sanskrit="Virabhadrasana II",
        level=2, family="standing", side="right",
        instruction="Bend your right knee and open both arms wide.",
        cue="Look over your right hand; sink your hips low.",
        table=bones(neck=-80,
                    right_thigh=30, right_shin=88,
                    left_thigh=125, left_shin=125,
                    left_upper_arm=180, left_forearm=180,
                    right_upper_arm=0, right_forearm=0),
        emphasis=("left_knee", "right_knee", "stance", "reach",
                  "left_shoulder", "right_shoulder"),
        cues={"right_knee": ("Bend your right knee more — take it over the ankle",
                             "Ease your right knee back over your ankle")},
    ),
    Pose(
        id="triangle_left", name="Triangle Pose", sanskrit="Trikonasana",
        level=2, family="standing", side="left",
        instruction="Straight legs wide, tip your torso over the left leg.",
        cue="Left hand down the shin, right arm straight up.",
        table=bones(spine=-140, neck=-120,
                    left_thigh=115, left_shin=115,
                    right_thigh=65, right_shin=65,
                    left_upper_arm=118, left_forearm=112,
                    right_upper_arm=-62, right_forearm=-68),
        emphasis=("torso", "stance", "right_hand_height", "left_hand_height"),
        cues={"torso": ("Tip further over your left leg",
                        "You have gone past the pose — lift your chest")},
    ),
    Pose(
        id="triangle_right", name="Triangle Pose", sanskrit="Trikonasana",
        level=2, family="standing", side="right",
        instruction="Straight legs wide, tip your torso over the right leg.",
        cue="Right hand down the shin, left arm straight up.",
        table=bones(spine=-40, neck=-60,
                    left_thigh=115, left_shin=115,
                    right_thigh=65, right_shin=65,
                    right_upper_arm=62, right_forearm=68,
                    left_upper_arm=-118, left_forearm=-112),
        emphasis=("torso", "stance", "right_hand_height", "left_hand_height"),
        cues={"torso": ("Tip further over your right leg",
                        "You have gone past the pose — lift your chest")},
    ),
    Pose(
        id="side_angle_left", name="Extended Side Angle",
        sanskrit="Utthita Parsvakonasana", level=2, family="standing",
        side="left",
        instruction="Bend your left knee and rest your left forearm on it.",
        cue="Sweep the right arm over your ear in one long line.",
        table=bones(spine=-132, neck=-116,
                    left_thigh=150, left_shin=92,
                    right_thigh=55, right_shin=55,
                    left_upper_arm=132, left_forearm=150,
                    right_upper_arm=-126, right_forearm=-132),
        emphasis=("torso", "left_knee", "stance", "right_hand_height"),
    ),
    Pose(
        id="side_angle_right", name="Extended Side Angle",
        sanskrit="Utthita Parsvakonasana", level=2, family="standing",
        side="right",
        instruction="Bend your right knee and rest your right forearm on it.",
        cue="Sweep the left arm over your ear in one long line.",
        table=bones(spine=-48, neck=-64,
                    right_thigh=30, right_shin=88,
                    left_thigh=125, left_shin=125,
                    right_upper_arm=48, right_forearm=30,
                    left_upper_arm=-54, left_forearm=-48),
        emphasis=("torso", "right_knee", "stance", "left_hand_height"),
    ),

    # ── balance ──────────────────────────────────────────────────────

    Pose(
        id="tree_heart_left", name="Tree Pose", sanskrit="Vrksasana", level=2,
        family="balance", side="left",
        instruction="Stand on your left leg and lift your right foot to it.",
        cue="Palms together at your chest. Stay tall and balanced.",
        table=bones(right_thigh=55, right_shin=172,
                    left_upper_arm=100, left_forearm=-30,
                    right_upper_arm=80, right_forearm=-150),
        emphasis=("right_knee", "right_foot_height", "torso"),
        cues={"right_foot_height": ("Lift your right foot higher up the leg",
                                    "Bring your right foot down a little"),
              "right_knee": ("Open your right knee out to the side",
                             "Draw your right knee back towards the front")},
    ),
    Pose(
        id="tree_heart_right", name="Tree Pose", sanskrit="Vrksasana", level=2,
        family="balance", side="right",
        instruction="Stand on your right leg and lift your left foot to it.",
        cue="Palms together at your chest. Stay tall and balanced.",
        table=bones(left_thigh=125, left_shin=8,
                    left_upper_arm=100, left_forearm=-30,
                    right_upper_arm=80, right_forearm=-150),
        emphasis=("left_knee", "left_foot_height", "torso"),
        cues={"left_foot_height": ("Lift your left foot higher up the leg",
                                   "Bring your left foot down a little"),
              "left_knee": ("Open your left knee out to the side",
                            "Draw your left knee back towards the front")},
    ),
    Pose(
        id="tree_overhead_left", name="Tree Pose, Arms High",
        sanskrit="Vrksasana", level=3, family="balance", side="left",
        instruction="Balance on your left leg, right foot to the inner thigh.",
        cue="Grow both arms straight up like branches.",
        table=bones(right_thigh=55, right_shin=172,
                    left_upper_arm=-98, left_forearm=-94,
                    right_upper_arm=-82, right_forearm=-86),
        emphasis=("right_knee", "right_foot_height", "torso",
                  "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="tree_overhead_right", name="Tree Pose, Arms High",
        sanskrit="Vrksasana", level=3, family="balance", side="right",
        instruction="Balance on your right leg, left foot to the inner thigh.",
        cue="Grow both arms straight up like branches.",
        table=bones(left_thigh=125, left_shin=8,
                    left_upper_arm=-98, left_forearm=-94,
                    right_upper_arm=-82, right_forearm=-86),
        emphasis=("left_knee", "left_foot_height", "torso",
                  "left_hand_height", "right_hand_height"),
    ),
    Pose(
        id="half_moon_left", name="Half Moon", sanskrit="Ardha Chandrasana",
        level=3, family="balance", side="left",
        instruction="Balance on your left leg and lift the right leg level.",
        cue="Left hand towards the floor, right arm straight up.",
        table=bones(spine=-148, neck=-128,
                    left_thigh=100, left_shin=96,
                    right_thigh=-2, right_shin=-2,
                    left_upper_arm=108, left_forearm=100,
                    right_upper_arm=-58, right_forearm=-58),
        emphasis=("right_hip", "right_foot_height", "torso",
                  "right_hand_height"),
        cues={"right_foot_height": ("Lift your right leg up to hip height",
                                    "Lower your right leg to hip height")},
    ),
    Pose(
        id="half_moon_right", name="Half Moon", sanskrit="Ardha Chandrasana",
        level=3, family="balance", side="right",
        instruction="Balance on your right leg and lift the left leg level.",
        cue="Right hand towards the floor, left arm straight up.",
        table=bones(spine=-32, neck=-52,
                    right_thigh=80, right_shin=84,
                    left_thigh=182, left_shin=182,
                    right_upper_arm=72, right_forearm=80,
                    left_upper_arm=-122, left_forearm=-122),
        emphasis=("left_hip", "left_foot_height", "torso",
                  "left_hand_height"),
        cues={"left_foot_height": ("Lift your left leg up to hip height",
                                   "Lower your left leg to hip height")},
    ),
    Pose(
        id="hand_to_toe_left", name="Extended Hand to Toe",
        sanskrit="Utthita Hasta Padangusthasana", level=3, family="balance",
        side="left",
        instruction="Stand on your left leg and take the right leg out wide.",
        cue="Right hand towards the foot, left arm out for balance.",
        table=bones(left_thigh=92, left_shin=90,
                    right_thigh=-4, right_shin=-4,
                    right_upper_arm=6, right_forearm=-6,
                    left_upper_arm=176, left_forearm=176),
        emphasis=("right_hip", "right_knee", "right_foot_height", "torso"),
    ),
    Pose(
        id="hand_to_toe_right", name="Extended Hand to Toe",
        sanskrit="Utthita Hasta Padangusthasana", level=3, family="balance",
        side="right",
        instruction="Stand on your right leg and take the left leg out wide.",
        cue="Left hand towards the foot, right arm out for balance.",
        table=bones(right_thigh=88, right_shin=90,
                    left_thigh=184, left_shin=184,
                    left_upper_arm=174, left_forearm=186,
                    right_upper_arm=4, right_forearm=4),
        emphasis=("left_hip", "left_knee", "left_foot_height", "torso"),
    ),
)

#: Every pose, by id. The lessons in `lesson.py` refer to poses by id and are
#: checked against this at import, so a typo in a sequence is an ImportError on
#: the Pi rather than a blank coach fifteen minutes into somebody's practice.
POSES: dict[str, Pose] = {pose.id: pose for pose in _POSES}


def get(pose_id: str) -> Pose:
    try:
        return POSES[pose_id]
    except KeyError:
        raise KeyError(f"there is no yoga pose called {pose_id!r}") from None
