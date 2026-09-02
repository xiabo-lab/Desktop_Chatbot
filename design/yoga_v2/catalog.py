"""The v2 pose catalog: every shape the 21 courses can ask for.

Two tiers, and the split is the whole architecture of v2.

**Scored tier.** Frontal-plane standing poses, measured the way the shipping
game already measures them — `rig.measure` normalising by shoulder width along
the image axes. Every one of the 32 poses already in `aipi5/games/yoga/poses.py`
is here by reference (`existing`), so the artwork and the tuned tolerances come
across untouched. The handful of new ones need bone tables written before they
can ship, and are marked `needs_table`.

**Guided tier.** Floor, seated, kneeling, quadruped, prone and supine. The
coach demonstrates the complete movement and the voice guides it; the pose is
not scored. **This is the settled policy, not a staging post.** A pose is
scored only where the existing, proven body-tracking system already handles it
reliably — which means frontal-plane standing work — and everything else is
guided. The floor-tracking investigation in `floor-tracking-spike.md` is closed
and its conclusion recorded there; the camera system is not being redesigned
and the tracking project is not being extended to chase these poses.

What "guided" means precisely, and it is not a degraded experience:

    score_enabled       false
    accuracy UI         hidden for the duration of the pose
    coach               demonstrates the whole movement as normal
    voice guidance      normal
    breathing guidance  normal
    hold timer          runs normally
    lesson progress     advances normally
    transitions         animated normally

A pose is emphatically **not** dropped from the curriculum for being unscoreable
— Savasana, Child's Pose and Cat-Cow are the spine of a real class, and a yoga
lesson that ends standing up because the camera cannot see the floor is a worse
lesson, not a safer one.

Holds come from §5's bands rather than from one number per class, and breath
counts are derived from the hold at a comfortable cadence rather than the other
way round — a player asked to fit four breaths into twelve seconds is being
asked to breathe fast, which is the opposite of the point.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── posture families ─────────────────────────────────────────────────────────
#
# These are the nodes of the transition graph in `courses.py`. §16 asks for
# transitions inside a movement family rather than everything routing through
# Mountain, and a family is exactly "a set of poses you can move between
# without changing what is on the floor".

STANDING = "standing-neutral"      # feet together or hip width, upright
WIDE = "standing-wide"             # feet wide apart, warrior and triangle
LUNGE = "standing-lunge"           # one foot forward, back foot lifted
FOLD = "standing-fold"             # standing but inverted over the legs
KNEELING = "kneeling"              # shins down, hips over or behind knees
QUADRUPED = "quadruped"            # hands and knees
PRONE = "prone"                    # front of the body on the floor
SEATED = "seated"                  # sitting on the floor
SUPINE = "supine"                  # on the back
INVERTED = "inverted"              # downward dog and friends

FLOOR_FAMILIES = frozenset({KNEELING, QUADRUPED, PRONE, SEATED, SUPINE, INVERTED})

#: Hold bands, straight from §5. `(minimum, default, maximum)` in seconds.
#:
#: A pose's default is what a course gets if it does not say otherwise; the
#: bounds are what `yoga_curriculum_report.py` refuses to let a course exceed,
#: so a class cannot quietly turn a balance pose into a forty-second endurance
#: test because the arithmetic needed the time.
BANDS: dict[str, tuple[float, float, float]] = {
    "dynamic": (3.0, 8.0, 18.0),        # moving through, one to three breaths
    "balance": (10.0, 18.0, 30.0),      # harder to hold, so held shorter
    "hatha": (20.0, 28.0, 45.0),        # the standing shapes, three to seven breaths
    "stretch": (20.0, 30.0, 45.0),      # gentle, floor, no strength demand
    "relax": (30.0, 60.0, 120.0),       # child's pose, savasana
}

#: Seconds per comfortable breath. §6 wants breath counts to be real rather
#: than decorative, and this is the number that makes them real: a hold's
#: breath count is derived from its seconds, never the reverse.
BREATH_SECONDS = 5.5


def breaths_for(hold_s: float) -> int:
    return max(1, round(hold_s / BREATH_SECONDS))


@dataclass(frozen=True)
class PoseSpec:
    """One shape, and everything the curriculum needs to know about it."""

    id: str
    name: str
    sanskrit: str
    family: str
    level: int                              # 1 easy, 2 moderate, 3 hard
    band: str                               # key into BANDS
    categories: tuple[str, ...] = ()
    side: str = ""                          # "", "left", "right"
    bilateral: bool = False                 # has a mirror that must be paired
    #: The id in `aipi5/games/yoga/poses.py` this reuses, if any. Where this is
    #: set the artwork, the bone table and the tuned tolerances already exist.
    existing: str = ""
    #: True when a scored pose still needs a bone table authored.
    needs_table: bool = False
    #: How this pose is tracked. "proven" -- already shipping and measured.
    #: "new" -- standing, and there is a metric among the seventeen that tells
    #: it apart from its neighbours. "unreliable" -- the camera cannot
    #: distinguish it, so it is guided even though it is standing.
    tracking: str = "proven"
    tracking_note: str = ""
    #: Scored off deliberately, whatever the family and the tracking say.
    #: The reason is always the same one: the pose the *coach* has to show to
    #: teach it well is not the pose the frontal bone table can describe, and
    #: teaching wins. Chair is the case -- a squat is a bend in the sagittal
    #: plane, invisible to a frontal table and to the camera both, so the
    #: choice was between a coach standing with straight legs that scores and
    #: a coach sitting back into a real Chair that does not.
    guided_only: bool = False
    instruction: str = ""
    cue: str = ""

    @property
    def scored(self) -> bool:
        """Scored needs two things: a standing pose *and* a way to see it.

        Standing is necessary and not sufficient. `rig.measure` produces
        exactly seventeen numbers, and a pose whose entire content lies along
        the camera axis -- a rotation, a small backbend, a lifted heel -- moves
        none of them. Scoring such a pose would reward whatever it *is*
        confusable with, which is worse than not scoring it at all.
        """
        if self.guided_only:
            return False
        return self.family not in FLOOR_FAMILIES and self.tracking != "unreliable"

    @property
    def normalisation(self) -> str:
        """Which `measure` a scored version of this pose would need."""
        return "image" if self.scored else "body"

    @property
    def default_hold(self) -> float:
        return BANDS[self.band][1]

    @property
    def hold_bounds(self) -> tuple[float, float]:
        low, _, high = BANDS[self.band]
        return low, high

    @property
    def mirror_id(self) -> str:
        if self.side == "left":
            return self.id[:-5] + "_right"
        if self.side == "right":
            return self.id[:-6] + "_left"
        return self.id


def _pair(base: str, name: str, sanskrit: str, family: str, level: int,
          band: str, categories: tuple[str, ...], existing: str = "",
          needs_table: bool = False, tracking: str = "proven",
          tracking_note: str = "", instruction: str = "", cue: str = "",
          ) -> list[PoseSpec]:
    """A sided pose, both ways round.

    Left and right are two sides of one exercise, not two poses — §11 — but
    they still need separate ids because a class has to be able to say which
    one it is asking for. `existing` names the *left* id in the shipping
    library; the right one is derived, exactly as the shipping code derives it.
    """
    out = []
    for side in ("left", "right"):
        out.append(PoseSpec(
            id=f"{base}_{side}", name=name, sanskrit=sanskrit, family=family,
            level=level, band=band, categories=categories, side=side,
            bilateral=True,
            existing=(f"{existing}_{side}" if existing else ""),
            needs_table=needs_table, tracking=tracking,
            tracking_note=tracking_note,
            instruction=instruction.replace("{side}", side),
            cue=cue.replace("{side}", side)))
    return out


_SPECS: list[PoseSpec] = []


def _add(*specs) -> None:
    for spec in specs:
        if isinstance(spec, list):
            _SPECS.extend(spec)
        else:
            _SPECS.append(spec)


# ── scored tier: arrival and warm-up ─────────────────────────────────────────

_add(
    PoseSpec("mountain", "Mountain Pose", "Tadasana", STANDING, 1, "hatha",
             ("standing", "centering"), existing="mountain",
             instruction="Stand tall, feet under your hips, arms by your sides.",
             cue="Weight even through both feet; shoulders soft."),
    PoseSpec("mountain_breath", "Mountain Breath", "Tadasana", STANDING, 1,
             "relax", ("standing", "breathing"), existing="mountain_breath",
             instruction="Stand still and let your breath settle.",
             cue="Nothing to achieve here; just breathe."),
    PoseSpec("upward_salute", "Upward Salute", "Urdhva Hastasana", STANDING, 1,
             "dynamic", ("standing", "mobility"), existing="upward_salute",
             instruction="Sweep both arms overhead and reach up.",
             cue="Lift through the ribs, keep the shoulders down."),
    PoseSpec("cactus_arms", "Cactus Arms", "Nirvanasana", STANDING, 1,
             "dynamic", ("standing", "shoulders"), existing="cactus_arms",
             instruction="Bend both elbows to shoulder height and open the chest.",
             cue="Draw the shoulder blades together."),
    PoseSpec("shoulder_opener", "Shoulder Opener", "Baddha Hastasana", STANDING,
             1, "stretch", ("standing", "shoulders"), existing="shoulder_opener",
             instruction="Clasp your hands low behind you and lift the chest.",
             cue="Roll the shoulders back, not up."),
    PoseSpec("gentle_back_reach", "Gentle Back Reach", "Anuvittasana", STANDING,
             1, "dynamic", ("standing", "spine"), tracking="unreliable",
             tracking_note="A small backbend is a sagittal movement seen "
                           "end-on. The spine shortens by a few percent and no "
                           "other metric moves, so it is indistinguishable "
                           "from standing with the hands behind the back.",
             instruction="Hands to your lower back and lift the chest gently.",
             cue="A small lift, not a deep bend."),
    PoseSpec("weight_shift", "Weight Shift", "Tadasana", STANDING, 1, "dynamic",
             ("standing", "balance"), tracking="unreliable",
             tracking_note="Shifting weight is a translation of the hips of a "
                           "few centimetres with no joint angle change. "
                           "Nothing in the seventeen metrics reports it.",
             instruction="Shift your weight slowly from one foot to the other.",
             cue="Feel the whole sole of each foot."),
)
_add(_pair("neck_tilt", "Neck Release", "Griva Sanchalana", STANDING, 1,
           "stretch", ("standing", "neck"), existing="neck_tilt",
           instruction="Tip your {side} ear toward your {side} shoulder.",
           cue="Let the other shoulder stay heavy."))
_add(_pair("side_bend", "Standing Side Bend", "Ardha Chandrasana", STANDING, 1,
           "stretch", ("standing", "spine"), existing="side_bend",
           instruction="Reach up and lean over to the {side}.",
           cue="Lengthen both sides of the waist."))
_add(_pair("crescent_moon", "Crescent Moon", "Chandrasana", STANDING, 2,
           "hatha", ("standing", "spine"), existing="crescent_moon",
           instruction="Arms overhead, curve gently to the {side}.",
           cue="Keep both feet planted."))
_add(_pair("standing_twist", "Standing Twist", "Katichakrasana", STANDING, 1,
           "dynamic", ("standing", "spine"), tracking="unreliable",
           tracking_note="A rotation about the vertical axis, which is the axis the camera looks straight down. poses.py excludes deep twists for exactly this reason.",
           instruction="Turn your chest to the {side}, arms soft.",
           cue="Turn from the ribs, keep the hips facing forward."))

# ── scored tier: folds and standing strength ─────────────────────────────────

_add(
    PoseSpec("forward_fold", "Standing Forward Fold", "Uttanasana", FOLD, 1,
             "stretch", ("standing", "hamstrings"), existing="forward_fold",
             instruction="Hinge at the hips and fold down over your legs.",
             cue="Soft knees are fine; let the head hang."),
    PoseSpec("half_lift", "Half Lift", "Ardha Uttanasana", FOLD, 1, "dynamic",
             ("standing", "spine"), needs_table=True, tracking="new",
             tracking_note="Distinguishable from Forward Fold by hand height: "
                           "hands at the shins put the wrists well above the "
                           "floor while the shoulders stay low. Needs the "
                           "forward_fold treatment -- a spine `scales` entry "
                           "and a widened tolerance.",
             instruction="Hands to your shins and lengthen your back flat.",
             cue="Long spine, chest forward, gaze down."),
    PoseSpec("wide_leg_fold", "Wide-Leg Forward Fold", "Prasarita Padottanasana",
             FOLD, 2, "stretch", ("standing", "hamstrings", "hips"),
             existing="wide_leg_fold",
             instruction="Feet wide, fold forward between them.",
             cue="Weight slightly forward into the toes."),
    PoseSpec("chair", "Chair Pose", "Utkatasana", STANDING, 2, "balance",
             ("standing", "strength"), existing="chair", guided_only=True,
             tracking_note="Guided by choice, not by limitation. Sitting back "
                           "into a chair is a sagittal bend: seen from the "
                           "front, deep Chair and standing upright are nearly "
                           "the same seventeen numbers, so the shipped table "
                           "drew straight legs. The coach now shows it from a "
                           "three-quarter view with the knees actually bent.",
             instruction="Bend your knees and sit your hips back, as if "
                         "sitting into a chair behind you.",
             cue="Keep the chest lifted and the weight in your heels; "
                 "reach the arms up alongside your ears."),
    PoseSpec("star", "Star Pose", "Utthita Tadasana", WIDE, 1, "hatha",
             ("standing", "opening"), existing="star",
             instruction="Feet wide, arms wide, stand tall.",
             cue="Reach out through the fingers."),
    PoseSpec("goddess", "Goddess Pose", "Utkata Konasana", WIDE, 2, "hatha",
             ("standing", "strength", "hips"), existing="goddess",
             instruction="Feet wide and turned out, bend both knees.",
             cue="Knees track over the toes."),
)
_add(_pair("warrior_one", "Warrior I", "Virabhadrasana I", LUNGE, 2, "hatha",
           ("standing", "strength", "hips"), existing="warrior_one",
           instruction="{side} foot forward, bend that knee, arms overhead.",
           cue="Hips facing forward; back heel down."))
_add(_pair("warrior_two", "Warrior II", "Virabhadrasana II", WIDE, 2, "hatha",
           ("standing", "strength", "hips"), existing="warrior_two",
           instruction="Bend your {side} knee and open both arms wide.",
           cue="Look over your {side} hand; sink the hips."))
_add(_pair("triangle", "Triangle Pose", "Trikonasana", WIDE, 2, "hatha",
           ("standing", "hamstrings", "spine"), existing="triangle",
           instruction="Straighten the {side} leg and reach down over it.",
           cue="Open the chest to the ceiling."))
_add(_pair("side_angle", "Extended Side Angle", "Utthita Parsvakonasana", WIDE,
           2, "hatha", ("standing", "strength", "hips"), existing="side_angle",
           instruction="{side} knee bent, {side} forearm to the thigh, top arm over.",
           cue="One long line from the back foot to the top hand."))
_add(_pair("reverse_warrior", "Reverse Warrior", "Viparita Virabhadrasana",
           WIDE, 2, "hatha", ("standing", "spine"), needs_table=True, tracking="new",
           tracking_note="Clearly separable from Warrior II: the torso lean moves `torso`, and one arm up with the other down moves both hand heights and `reach` a long way.",
           instruction="From Warrior II, turn the {side} palm up and lean back.",
           cue="Keep the front knee bent as you reach."))
_add(_pair("high_lunge", "High Lunge", "Ashta Chandrasana", LUNGE, 2, "hatha",
           ("standing", "strength", "hips"), tracking="unreliable",
           tracking_note="Differs from Warrior I only by the lifted back heel. The pose model reports ankles, not heels, and the ankle barely moves -- so no metric separates the two. Score Warrior I; guide this.",
           instruction="{side} foot forward, back heel lifted, arms overhead.",
           cue="Back leg strong and straight."))
_add(_pair("pyramid", "Pyramid Pose", "Parsvottanasana", LUNGE, 3, "stretch",
           ("standing", "hamstrings"), tracking="unreliable",
           tracking_note="Feet are staggered front-to-back, which projects to almost no x separation, so `stance` reads like a normal fold. Frontally it is Forward Fold with the feet apart in the invisible axis.",
           instruction="{side} foot forward and straight, fold over the front leg.",
           cue="Square the hips before you fold."))

# ── scored tier: balance ─────────────────────────────────────────────────────

_add(_pair("tree_heart", "Tree Pose", "Vrksasana", STANDING, 2, "balance",
           ("standing", "balance"), existing="tree_heart",
           instruction="Stand on your {side} leg, other foot to the inner thigh.",
           cue="Press foot and leg together; fix your gaze."))
_add(_pair("tree_overhead", "Tree Pose, arms high", "Vrksasana", STANDING, 3,
           "balance", ("standing", "balance"), existing="tree_overhead",
           instruction="From Tree, reach both arms overhead.",
           cue="Keep the standing hip level."))
_add(_pair("half_moon", "Half Moon", "Ardha Chandrasana", WIDE, 3, "balance",
           ("standing", "balance"), existing="half_moon",
           instruction="Balance on the {side} leg, lift the other leg parallel.",
           cue="Stack the top hip over the bottom one."))
_add(_pair("hand_to_toe", "Hand to Big Toe", "Utthita Hasta Padangusthasana",
           STANDING, 3, "balance", ("standing", "balance", "hamstrings"),
           existing="hand_to_toe",
           instruction="Stand on the {side} leg and lift the other knee, then extend.",
           cue="Stand tall rather than leaning back."))
_add(_pair("standing_knee", "Standing Knee Hold", "Utthita Padangusthasana",
           STANDING, 2, "balance", ("standing", "balance"), needs_table=True, tracking="new",
           tracking_note="One thigh horizontal with the knee folded moves hip, knee and foot height on that side simultaneously. Among the clearest shapes in the library.",
           instruction="Lift the {side} knee and hold it in with both hands.",
           cue="Stand tall; squeeze the knee to the chest."))
_add(_pair("warrior_three", "Warrior III", "Virabhadrasana III", STANDING, 3,
           "balance", ("standing", "balance", "strength"), tracking="unreliable",
           tracking_note="Torso and lifted leg both point along the camera axis. The camera can see that the player is on one leg, but not whether the back leg is level or the hips are square -- which is the entire content of the pose. Scoring it would reward leaning forward on one leg.",
           instruction="Balance on the {side} leg, body and back leg level.",
           cue="One long line from the crown to the lifted heel."))

# ── guided tier: kneeling and quadruped ──────────────────────────────────────

_add(
    PoseSpec("child_pose", "Child's Pose", "Balasana", KNEELING, 1, "stretch",
             ("floor", "rest", "hips"),
             instruction="Kneel, sit back on your heels and fold forward.",
             cue="Arms stretched ahead, forehead down, breathe into the back."),
    PoseSpec("cat", "Cat Pose", "Marjaryasana", QUADRUPED, 1, "dynamic",
             ("floor", "spine"),
             instruction="On hands and knees, round the back and drop the head.",
             cue="Exhale as you round."),
    PoseSpec("cow", "Cow Pose", "Bitilasana", QUADRUPED, 1, "dynamic",
             ("floor", "spine"),
             instruction="Let the belly drop, lift the chest and tailbone.",
             cue="Inhale as you lift."),
    PoseSpec("table", "Table Top", "Bharmanasana", QUADRUPED, 1, "dynamic",
             ("floor", "neutral"),
             instruction="Hands under shoulders, knees under hips, back level.",
             cue="A flat table from the crown to the tail."),
    PoseSpec("downward_dog", "Downward-Facing Dog", "Adho Mukha Svanasana",
             INVERTED, 2, "hatha", ("floor", "strength", "hamstrings"),
             instruction="Hands and feet down, lift the hips into an upside-down V.",
             cue="Long arms and back; heels reaching down."),
    PoseSpec("plank", "Plank", "Phalakasana", PRONE, 2, "balance",
             ("floor", "strength", "core"),
             instruction="From hands and knees, step both feet back to a straight line.",
             cue="Hips level with the shoulders, not sagging."),
    PoseSpec("sphinx", "Sphinx", "Salamba Bhujangasana", PRONE, 1, "stretch",
             ("floor", "spine"),
             instruction="Lie on your front, forearms down, lift the chest.",
             cue="Elbows under the shoulders; shoulders away from the ears."),
    PoseSpec("cobra", "Cobra", "Bhujangasana", PRONE, 2, "stretch",
             ("floor", "spine"),
             instruction="Hands under the shoulders, lift the chest gently.",
             cue="Keep the lift small and the neck long."),
)
_add(_pair("low_lunge", "Low Lunge", "Anjaneyasana", KNEELING, 1, "stretch",
           ("floor", "hips"),
           instruction="{side} foot forward, back knee down, sink the hips.",
           cue="Ease forward until you feel the front of the back thigh."))
_add(_pair("low_lunge_reach", "Low Lunge with Reach", "Anjaneyasana", KNEELING,
           2, "stretch", ("floor", "hips", "spine"),
           instruction="From Low Lunge, sweep both arms overhead.",
           cue="Lift the chest as the hips sink."))
_add(_pair("half_split", "Half Split", "Ardha Hanumanasana", KNEELING, 2,
           "stretch", ("floor", "hamstrings"),
           instruction="Straighten the {side} leg, hips back over the knee.",
           cue="Flex the front foot; fold only as far as is comfortable."))
_add(_pair("lizard", "Lizard Preparation", "Utthan Pristhasana", KNEELING, 3,
           "stretch", ("floor", "hips"),
           instruction="{side} foot outside the hands, back knee down.",
           cue="Stay on the hands; do not force the hips down."))
_add(_pair("pigeon_prep", "Pigeon Preparation", "Eka Pada Rajakapotasana",
           KNEELING, 3, "stretch", ("floor", "hips"),
           instruction="{side} shin forward across the mat, back leg long.",
           cue="Keep the hips level; a folded blanket under the hip helps."))
_add(_pair("bird_dog", "Bird Dog", "Dandayamana Bharmanasana", QUADRUPED, 2,
           "balance", ("floor", "core", "balance"),
           instruction="Reach the {side} arm forward and the other leg back.",
           cue="Keep the hips square and the back flat."))
_add(_pair("thread_needle", "Thread the Needle", "Parsva Balasana", QUADRUPED,
           2, "stretch", ("floor", "shoulders", "spine"),
           instruction="Slide the {side} arm under the other, shoulder to the mat.",
           cue="Let the hips stay high over the knees."))
_add(_pair("side_plank", "Side Plank", "Vasisthasana", PRONE, 3, "balance",
           ("floor", "strength", "core"),
           instruction="From Plank, roll onto the {side} hand and stack the feet.",
           cue="Lift the hips; drop the bottom knee if you need to."))

# ── guided tier: seated ──────────────────────────────────────────────────────

_add(
    PoseSpec("easy_seat", "Easy Seat", "Sukhasana", SEATED, 1, "stretch",
             ("floor", "seated", "centering"),
             instruction="Sit cross-legged, facing forward, spine tall.",
             cue="Hands on the knees, shoulders relaxed."),
    PoseSpec("easy_seat_breath", "Seated Breathing", "Sukhasana", SEATED, 1,
             "relax", ("floor", "seated", "breathing"),
             instruction="Sit tall and let the breath slow.",
             cue="Nothing to do but breathe."),
    PoseSpec("butterfly", "Butterfly", "Baddha Konasana", SEATED, 1, "stretch",
             ("floor", "seated", "hips"),
             instruction="Soles of the feet together, knees falling open.",
             cue="Sit up tall before you fold at all."),
    PoseSpec("seated_forward_fold", "Seated Forward Fold", "Paschimottanasana",
             SEATED, 2, "stretch", ("floor", "seated", "hamstrings"),
             instruction="Legs long in front, fold forward over them.",
             cue="Lead with the chest, not the head."),
    PoseSpec("boat", "Boat Preparation", "Navasana", SEATED, 3, "balance",
             ("floor", "seated", "core"),
             instruction="Sit, lean back and lift both feet, shins level.",
             cue="Keep the chest lifted and the back long."),
    PoseSpec("staff", "Staff Pose", "Dandasana", SEATED, 1, "dynamic",
             ("floor", "seated", "neutral"),
             instruction="Sit with both legs straight in front, back tall.",
             cue="Press the backs of the knees down gently."),
)
_add(_pair("seated_side_stretch", "Seated Side Stretch", "Parsva Sukhasana",
           SEATED, 1, "stretch", ("floor", "seated", "spine"),
           instruction="Sitting tall, reach the {side} arm up and over.",
           cue="Keep both sitting bones down."))
_add(_pair("seated_twist", "Seated Twist", "Ardha Matsyendrasana", SEATED, 2,
           "stretch", ("floor", "seated", "spine"),
           instruction="Sitting tall, turn gently to the {side}.",
           cue="Lengthen up first, then turn; keep it gentle."))
_add(_pair("seated_hamstring", "Seated Hamstring Stretch", "Janu Sirsasana",
           SEATED, 2, "stretch", ("floor", "seated", "hamstrings"),
           instruction="{side} leg long, other foot to the inner thigh, fold over.",
           cue="Fold from the hip, not the waist."))

# ── guided tier: supine ──────────────────────────────────────────────────────

_add(
    PoseSpec("knees_to_chest", "Knees to Chest", "Apanasana", SUPINE, 1,
             "stretch", ("floor", "supine", "spine"),
             instruction="On your back, hug both knees in to your chest.",
             cue="Let the lower back soften into the floor."),
    PoseSpec("bridge", "Bridge Pose", "Setu Bandhasana", SUPINE, 2, "hatha",
             ("floor", "supine", "strength", "spine"),
             instruction="Feet flat and close to the hips, lift the hips up.",
             cue="Press through the whole foot; keep the knees parallel."),
    PoseSpec("happy_baby", "Happy Baby", "Ananda Balasana", SUPINE, 2,
             "stretch", ("floor", "supine", "hips"),
             instruction="On your back, take the outer feet and open the knees wide.",
             cue="Keep the head and shoulders on the floor."),
    PoseSpec("savasana", "Final Relaxation", "Savasana", SUPINE, 1, "relax",
             ("floor", "supine", "rest"),
             instruction="Lie flat on your back, legs long, arms by your sides.",
             cue="Nothing to hold. Let the floor take your weight."),
)
_add(_pair("supine_twist", "Supine Twist", "Supta Matsyendrasana", SUPINE, 1,
           "stretch", ("floor", "supine", "spine"),
           instruction="On your back, let both knees fall to the {side}.",
           cue="Arms wide in a T; turn the head the other way."))
_add(_pair("figure_four", "Figure Four", "Supta Kapotasana", SUPINE, 2,
           "stretch", ("floor", "supine", "hips"),
           instruction="On your back, cross the {side} ankle over the other knee.",
           cue="Draw the lower thigh in; keep the head down."))


POSES: dict[str, PoseSpec] = {spec.id: spec for spec in _SPECS}


def get(pose_id: str) -> PoseSpec:
    try:
        return POSES[pose_id]
    except KeyError:
        raise KeyError(f"there is no v2 pose called {pose_id!r}") from None


def scored() -> list[PoseSpec]:
    return [p for p in _SPECS if p.scored]


def guided() -> list[PoseSpec]:
    return [p for p in _SPECS if not p.scored]


def _validate() -> None:
    """Fail at import, for the same reason `lesson.py` does.

    A catalog that contradicts itself is worth catching here rather than in a
    generation manifest three steps downstream, where the symptom is a missing
    video file and the cause is a typo.
    """
    seen: set[str] = set()
    for spec in _SPECS:
        if spec.id in seen:
            raise ValueError(f"duplicate pose id {spec.id!r}")
        seen.add(spec.id)
        if spec.band not in BANDS:
            raise ValueError(f"{spec.id} has unknown hold band {spec.band!r}")
        if spec.bilateral and spec.mirror_id not in {s.id for s in _SPECS}:
            raise ValueError(f"{spec.id} is bilateral but {spec.mirror_id} "
                             f"is not in the catalog")
        if spec.side and not spec.id.endswith(("_left", "_right")):
            raise ValueError(f"{spec.id} has a side but no _left/_right suffix")
        # A floor pose can never be scored, whatever else it claims.
        if spec.family in FLOOR_FAMILIES and (spec.existing or spec.needs_table):
            raise ValueError(f"{spec.id} is a floor pose and cannot be scored")
        # And a guided pose needs no bone table -- that is what guided means.
        if not spec.scored and spec.needs_table:
            raise ValueError(f"{spec.id} is guided but claims a bone table")
        if spec.tracking == "unreliable" and spec.scored:
            raise ValueError(f"{spec.id} is unreliable yet marked scored")


_validate()
