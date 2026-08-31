"""The 21 courses, and the timing model that gives each one a real duration.

Seven Beginner, seven Intermediate, seven Advanced, every one of them built on
the same arc §4 asks for:

    A arrival     30-60 s   settle and start breathing
    B warm-up     2-4 min   mobility before anything demanding
    C main        8-12 min  the work, intensity peaking in the middle
    D cool-down   2-4 min   step down, come to the floor
    E relaxation  1-2 min   Savasana, and finish calmer than the peak

These are **original sequences built from established sequencing principles** --
warm the spine before bending it, open the hips before standing on one leg,
come down through something gentle before stopping, and never open with a
balance. They are not transcribed from the supplied PDF or the PNG, both of
which are somebody else's copyrighted work, and §2 asks for the same thing.

Nothing here is therapeutic or diagnostic. It is a general wellness class.
"""

from __future__ import annotations

from dataclasses import dataclass

from design.yoga_v2 import catalog
from design.yoga_v2.catalog import (FOLD, INVERTED, KNEELING, LUNGE, PRONE,
                                    QUADRUPED, SEATED, STANDING, SUPINE, WIDE,
                                    PoseSpec, breaths_for)

# ── the transition model ─────────────────────────────────────────────────────
#
# §17 wants transition time to depend on how far the body has to travel, and
# §16 wants transitions to stay inside a movement family where they can. Both
# fall out of one number per family: **how far off the floor it is.** Moving
# between two families costs a base plus the distance between their heights, so
# Warrior II to Triangle is cheap and standing to Savasana is not, without a
# hand-written table of pairs that would need 100 entries and would rot.

HEIGHT: dict[str, float] = {
    STANDING: 4.0, WIDE: 4.0, LUNGE: 3.5, FOLD: 3.5,
    INVERTED: 2.5, QUADRUPED: 1.5, KNEELING: 1.5,
    SEATED: 1.0, PRONE: 0.5, SUPINE: 0.0,
}

TRANSITION_BASE = 3.0
TRANSITION_PER_STEP = 1.6
TRANSITION_MIN, TRANSITION_MAX = 2.5, 8.0

#: The still look at the finished pose before anybody is asked to move. Carried
#: over from the shipping game, where it was added because nobody can copy a
#: movement they have not seen the end of.
PREVIEW_S = 2.5

#: §25's SETTLE: time to arrive in the shape before scoring starts. The
#: shipping game has no such phase -- HOLDING begins the instant the coach
#: stops -- which is why a player is marked down for the half second it takes
#: them to stop moving.
SETTLE_S = 1.5

#: §32's countdown before the first pose, and the per-pose score card, both
#: carried over from the shipping lesson.
COUNTDOWN_S = 4.0
RESULT_S = 2.2


def transition_between(a: str, b: str) -> float:
    """Seconds to move from one posture family to another."""
    gap = abs(HEIGHT[a] - HEIGHT[b])
    return min(TRANSITION_MAX,
               max(TRANSITION_MIN, TRANSITION_BASE + TRANSITION_PER_STEP * gap))


@dataclass(frozen=True)
class Step:
    pose_id: str
    hold_s: float
    segment: str
    #: Overrides the pose's own band for this step. A pose's right duration is
    #: not a property of the pose -- §5 lists "location within the sequence"
    #: among the things it depends on, and Mountain proves the point: held at
    #: the start of a class it is a 20-second hatha pose, and passed through in
    #: the middle of a half-salutation it is an eight-second breath. Same shape,
    #: different job, different band.
    band: str = ""

    @property
    def pose(self) -> PoseSpec:
        return catalog.get(self.pose_id)

    @property
    def effective_band(self) -> str:
        """The band this step is actually judged against.

        An explicit `band` wins. Otherwise a hold that falls inside §5's
        dynamic range **is** dynamic movement, by that section's own
        definition -- three to fifteen seconds, one to three breaths, "usually
        animated movements rather than static holds". Mountain at eight seconds
        in a half-salutation needs no annotation to be understood as a moment
        passed through.

        Relax poses are excluded from that rule deliberately. Nobody flows
        through Savasana, so a short Savasana is a mistake worth reporting
        rather than a movement worth accepting.
        """
        if self.band:
            return self.band
        low, _, high = catalog.BANDS["dynamic"]
        # Only shapes that genuinely appear inside a flow can be passed
        # through. A fifteen-second Warrior III is a hard balance held for a
        # short time -- which is what §5 asks for -- not a moment in a
        # salutation, and calling it "dynamic" would hide it from the very
        # check that is meant to keep balances short.
        if self.pose.band in ("hatha", "stretch") and low <= self.hold_s <= high:
            return "dynamic"
        return self.pose.band

    @property
    def bounds(self) -> tuple[float, float]:
        low, _, high = catalog.BANDS[self.effective_band]
        return low, high

    @property
    def breaths(self) -> int:
        return breaths_for(self.hold_s)

    @property
    def score_enabled(self) -> bool:
        return self.pose.scored


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    level: str
    theme: str
    steps: tuple[Step, ...]

    @property
    def transitions(self) -> list[float]:
        """One per step: the move that gets *into* it."""
        out, previous = [], STANDING
        for step in self.steps:
            out.append(transition_between(previous, step.pose.family))
            previous = step.pose.family
        return out

    @property
    def hold_seconds(self) -> float:
        return sum(step.hold_s for step in self.steps)

    @property
    def transition_seconds(self) -> float:
        return sum(self.transitions)

    @property
    def overhead_seconds(self) -> float:
        """Preview, settle and the score card, which are real time too."""
        return len(self.steps) * (PREVIEW_S + SETTLE_S) + \
            max(0, len(self.steps) - 1) * RESULT_S

    @property
    def total_seconds(self) -> float:
        return (COUNTDOWN_S + self.hold_seconds + self.transition_seconds
                + self.overhead_seconds)

    @property
    def relaxation_seconds(self) -> float:
        return sum(s.hold_s for s in self.steps if s.effective_band == "relax")

    @property
    def segments(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.segment not in seen:
                seen.append(step.segment)
        return tuple(seen)

    @property
    def scored_steps(self) -> int:
        return sum(1 for s in self.steps if s.score_enabled)


ARRIVAL, WARMUP, MAIN, COOLDOWN, RELAX = (
    "Arrival", "Warm-up", "Main sequence", "Cool-down", "Relaxation")


def _steps(segment: str, *entries) -> list[Step]:
    """`"pose"` for the catalog's default hold, `("pose", 25)` to override."""
    out = []
    for entry in entries:
        if isinstance(entry, tuple):
            pose_id, hold = entry
        else:
            pose_id, hold = entry, catalog.get(entry).default_hold
        out.append(Step(pose_id, float(hold), segment))
    return out


def _flow(segment: str, *entries) -> list[Step]:
    """Steps that are passed *through* rather than held.

    A half-salutation is one continuous movement, not six poses that each
    happen to be brief, and §35 asks for exactly that distinction. Marking the
    steps `dynamic` is what lets Mountain appear at eight seconds without the
    validator objecting that Mountain is a twenty-second pose -- because here
    it is not one.
    """
    return [Step(s.pose_id, s.hold_s, s.segment, "dynamic")
            for s in _steps(segment, *entries)]


def _both(segment: str, base: str, hold=None) -> list[Step]:
    """A sided pose, left then right. §11: two sides of one exercise."""
    out = []
    for side in ("left", "right"):
        pose_id = f"{base}_{side}"
        chosen = hold if hold is not None else catalog.get(pose_id).default_hold
        out.append(Step(pose_id, float(chosen), segment))
    return out


def _course(cid, name, level, theme, *groups) -> Course:
    steps: list[Step] = []
    for group in groups:
        steps.extend(group)
    return Course(cid, name, level, theme, tuple(steps))


# ═══ BEGINNER ════════════════════════════════════════════════════════════════
#
# Every hold below is chosen, not solved for. The first version of this file
# set durations by scaling toward an 18-minute target and clamping into §5's
# bands, which produced legal numbers and some silly ones -- a 42-second Chair,
# a 40-second Warrior II in an intermediate class, fifteen identical Savasanas
# at the band ceiling. The rule now is the other way round: the pose, its
# level, its job in the sequence and the fatigue around it decide the duration,
# and the clock lands where it lands inside 15-20 minutes.

BEGINNER = [
    _course("beginner_01", "Full Body Reset", "beginner",
            "Gentle full-body mobility, from standing down to the floor.",
            _steps(ARRIVAL, ("mountain", 18), ("mountain_breath", 36)),
            _steps(WARMUP, ("cactus_arms", 12), ("shoulder_opener", 24)),
            _both(WARMUP, "neck_tilt", 22),
            _both(WARMUP, "side_bend", 22),
            # A half-salutation: passed through, not held.
            _steps(WARMUP, ("upward_salute", 9), ("forward_fold", 24),
                   ("half_lift", 7), ("forward_fold", 12), ("mountain", 8)),
            _steps(MAIN, ("table", 6), ("cow", 8), ("cat", 8), ("cow", 8),
                   ("cat", 8), ("child_pose", 34)),
            _both(MAIN, "low_lunge", 30),
            _steps(MAIN, ("downward_dog", 22), ("child_pose", 26)),
            _steps(COOLDOWN, ("easy_seat", 26)),
            _both(COOLDOWN, "seated_side_stretch", 24),
            _steps(COOLDOWN, ("knees_to_chest", 24)),
            _both(COOLDOWN, "supine_twist", 30),
            _steps(RELAX, ("savasana", 80))),

    _course("beginner_02", "Morning Mobility", "beginner",
            "Wake the whole body gently, without demanding anything of it.",
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 34)),
            _steps(WARMUP, ("upward_salute", 10), ("gentle_back_reach", 12)),
            _both(WARMUP, "side_bend", 22),
            _both(WARMUP, "standing_twist", 12),
            _steps(WARMUP, ("forward_fold", 24), ("half_lift", 7),
                   ("forward_fold", 12), ("mountain", 8)),
            _both(MAIN, "low_lunge", 30),
            _steps(MAIN, ("downward_dog", 22), ("table", 6), ("cow", 8),
                   ("cat", 8), ("cow", 8), ("cat", 8)),
            _both(MAIN, "bird_dog", 18),
            _steps(MAIN, ("child_pose", 32)),
            _steps(COOLDOWN, ("easy_seat", 26), ("butterfly", 30),
                   ("seated_forward_fold", 28)),
            _both(COOLDOWN, "seated_twist", 24),
            _both(COOLDOWN, "figure_four", 28),
            _steps(RELAX, ("savasana", 75))),

    _course("beginner_03", "Hips and Lower Body", "beginner",
            "Hip, leg and lower-body mobility, built up slowly.",
            _steps(ARRIVAL, ("mountain", 18), ("mountain_breath", 32)),
            _steps(WARMUP, ("upward_salute", 9), ("forward_fold", 24),
                   ("half_lift", 7), ("mountain", 8)),
            _both(WARMUP, "side_bend", 22),
            _both(MAIN, "low_lunge", 30),
            # Warriors at a beginner's length: long enough to find the shape,
            # short enough that the second side is not worse than the first.
            _both(MAIN, "warrior_one", 26),
            _steps(MAIN, ("forward_fold", 12)),
            _both(MAIN, "warrior_two", 28),
            _both(MAIN, "triangle", 26),
            _steps(MAIN, ("star", 18), ("goddess", 24), ("wide_leg_fold", 30)),
            _steps(COOLDOWN, ("easy_seat", 22), ("butterfly", 32)),
            _both(COOLDOWN, "seated_hamstring", 28),
            _both(COOLDOWN, "figure_four", 30),
            _steps(RELAX, ("savasana", 85))),

    _course("beginner_04", "Back and Shoulder Release", "beginner",
            "For a body that has been sitting: spine, shoulders and neck.",
            _steps(ARRIVAL, ("easy_seat", 24), ("easy_seat_breath", 40)),
            _both(WARMUP, "neck_tilt", 24),
            _both(WARMUP, "seated_side_stretch", 26),
            _steps(WARMUP, ("table", 6), ("cow", 8), ("cat", 8), ("cow", 8),
                   ("cat", 8)),
            _both(MAIN, "thread_needle", 30),
            _steps(MAIN, ("child_pose", 30), ("downward_dog", 20)),
            _both(MAIN, "low_lunge_reach", 28),
            _steps(MAIN, ("sphinx", 26), ("child_pose", 26)),
            _both(COOLDOWN, "seated_twist", 26),
            _steps(COOLDOWN, ("knees_to_chest", 26)),
            _both(COOLDOWN, "supine_twist", 32),
            _steps(RELAX, ("savasana", 90))),

    _course("beginner_05", "Balance Basics", "beginner",
            "Introduce standing balance, once the legs are properly warm.",
            _steps(ARRIVAL, ("mountain", 18), ("mountain_breath", 32)),
            _steps(WARMUP, ("upward_salute", 9), ("forward_fold", 22),
                   ("half_lift", 7), ("mountain", 8), ("weight_shift", 14)),
            _both(WARMUP, "side_bend", 22),
            # Balance ladder: held short on purpose. A beginner's Tree gets
            # worse after fifteen seconds, and scoring the decline teaches
            # nothing.
            _both(MAIN, "standing_knee", 15),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "tree_heart", 16),
            _steps(MAIN, ("chair", 20), ("forward_fold", 12)),
            _both(MAIN, "warrior_one", 26),
            _both(MAIN, "warrior_two", 28),
            _both(MAIN, "triangle", 26),
            _steps(COOLDOWN, ("downward_dog", 20), ("child_pose", 30),
                   ("butterfly", 30), ("knees_to_chest", 26)),
            _both(COOLDOWN, "supine_twist", 28),
            _steps(RELAX, ("savasana", 80))),

    _course("beginner_06", "Gentle Flow", "beginner",
            "Breath-connected movement, with the shapes already familiar.",
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 14),
                   ("half_lift", 6), ("forward_fold", 10), ("upward_salute", 8),
                   ("mountain", 8), ("upward_salute", 8), ("forward_fold", 14),
                   ("half_lift", 6), ("mountain", 8)),
            _both(MAIN, "low_lunge", 26),
            _steps(MAIN, ("downward_dog", 20)),
            _both(MAIN, "warrior_two", 28),
            _both(MAIN, "reverse_warrior", 20),
            _steps(MAIN, ("forward_fold", 12)),
            _both(MAIN, "triangle", 26),
            _steps(COOLDOWN, ("child_pose", 30), ("easy_seat", 24)),
            _both(COOLDOWN, "seated_side_stretch", 24),
            _both(COOLDOWN, "figure_four", 28),
            _steps(COOLDOWN, ("knees_to_chest", 26)),
            _steps(RELAX, ("savasana", 80))),

    _course("beginner_07", "Relax and Recover", "beginner",
            "Low intensity throughout; the softest class in the set.",
            _steps(ARRIVAL, ("easy_seat", 26), ("easy_seat_breath", 45)),
            _both(WARMUP, "neck_tilt", 26),
            _steps(WARMUP, ("shoulder_opener", 28), ("table", 6), ("cow", 8),
                   ("cat", 8), ("cow", 8), ("cat", 8), ("child_pose", 40)),
            _both(MAIN, "low_lunge", 32),
            _steps(MAIN, ("butterfly", 35), ("seated_forward_fold", 32)),
            _both(MAIN, "seated_hamstring", 30),
            _both(COOLDOWN, "figure_four", 32),
            _steps(COOLDOWN, ("knees_to_chest", 28)),
            _both(COOLDOWN, "supine_twist", 32),
            # A restorative class earns the long rest.
            _steps(RELAX, ("savasana", 115))),
]

# ═══ INTERMEDIATE ════════════════════════════════════════════════════════════

INTERMEDIATE = [
    _course("intermediate_01", "Full Body Flow", "intermediate",
            "A complete standing sequence, one side fully before the other.",
            _steps(ARRIVAL, ("mountain", 15), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 20),
                   ("half_lift", 6), ("forward_fold", 12), ("mountain", 8)),
            _both(WARMUP, "side_bend", 20),
            _steps(WARMUP, ("downward_dog", 22)),
            _steps(MAIN, ("low_lunge_left", 24), ("warrior_one_left", 30),
                   ("warrior_two_left", 32), ("reverse_warrior_left", 20),
                   ("side_angle_left", 30), ("triangle_left", 28),
                   ("downward_dog", 16)),
            _steps(MAIN, ("low_lunge_right", 24), ("warrior_one_right", 30),
                   ("warrior_two_right", 32), ("reverse_warrior_right", 20),
                   ("side_angle_right", 30), ("triangle_right", 28)),
            _steps(MAIN, ("chair", 25), ("downward_dog", 16)),
            _both(COOLDOWN, "pigeon_prep", 32),
            _steps(COOLDOWN, ("child_pose", 26), ("seated_forward_fold", 28)),
            _steps(RELAX, ("savasana", 90))),

    _course("intermediate_02", "Strength and Stability", "intermediate",
            "Standing strength, core and a first backbend.",
            _steps(ARRIVAL, ("mountain", 15), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("chair", 25),
                   ("forward_fold", 22), ("half_lift", 6), ("plank", 16),
                   ("downward_dog", 24)),
            _both(MAIN, "high_lunge", 26),
            _both(MAIN, "warrior_one", 30),
            _steps(MAIN, ("forward_fold", 12)),
            _both(MAIN, "warrior_two", 32),
            _both(MAIN, "side_angle", 30),
            _steps(MAIN, ("downward_dog", 16)),
            _steps(MAIN, ("goddess", 28)),
            _both(MAIN, "tree_heart", 20),
            _steps(MAIN, ("child_pose", 22)),
            _steps(MAIN, ("plank", 18), ("boat", 18), ("bridge", 30)),
            _steps(COOLDOWN, ("knees_to_chest", 26), ("child_pose", 30)),
            _both(COOLDOWN, "supine_twist", 28),
            _steps(RELAX, ("savasana", 90))),

    _course("intermediate_03", "Hip Mobility Flow", "intermediate",
            "Deep but controlled hip work, one side at a time.",
            _steps(ARRIVAL, ("easy_seat", 22), ("easy_seat_breath", 32)),
            _steps(WARMUP, ("table", 6), ("cow", 8), ("cat", 8), ("cow", 8),
                   ("cat", 8), ("downward_dog", 22)),
            _steps(MAIN, ("low_lunge_left", 28), ("lizard_left", 32),
                   ("warrior_two_left", 30), ("reverse_warrior_left", 20),
                   ("side_angle_left", 30), ("wide_leg_fold", 28)),
            _steps(MAIN, ("low_lunge_right", 28), ("lizard_right", 32),
                   ("warrior_two_right", 30), ("reverse_warrior_right", 20),
                   ("side_angle_right", 30), ("wide_leg_fold", 28)),
            _steps(COOLDOWN, ("butterfly", 32)),
            _both(COOLDOWN, "figure_four", 30),
            _both(COOLDOWN, "seated_twist", 26),
            _steps(RELAX, ("savasana", 90))),

    _course("intermediate_04", "Balance and Focus", "intermediate",
            "Balance built in stages, with a reset between the hard ones.",
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 20),
                   ("half_lift", 6), ("mountain", 8), ("chair", 25)),
            _both(WARMUP, "standing_knee", 16),
            # Balance is the point of this course, so the section is long --
            # but a neutral reset every two poses keeps the last balance from
            # being scored on accumulated fatigue rather than on balance.
            _both(MAIN, "tree_heart", 20),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "tree_overhead", 18),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "warrior_two", 30),
            _both(MAIN, "triangle", 28),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "warrior_three", 15),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "half_moon", 17),
            _steps(COOLDOWN, ("child_pose", 30), ("butterfly", 30),
                   ("easy_seat_breath", 32)),
            _both(COOLDOWN, "figure_four", 28),
            _steps(RELAX, ("savasana", 85))),

    _course("intermediate_05", "Spine and Posture", "intermediate",
            "Mobility through the whole spine, in both directions, gently.",
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 30)),
            _both(WARMUP, "side_bend", 22),
            _both(WARMUP, "standing_twist", 12),
            _steps(WARMUP, ("forward_fold", 22), ("half_lift", 7),
                   ("table", 6), ("cow", 8), ("cat", 8), ("cow", 8), ("cat", 8)),
            _both(MAIN, "crescent_moon", 26),
            _both(MAIN, "bird_dog", 20),
            _steps(MAIN, ("sphinx", 28), ("cobra", 24), ("child_pose", 28),
                   ("downward_dog", 20)),
            _both(MAIN, "low_lunge_reach", 28),
            _both(MAIN, "warrior_one", 28),
            _steps(MAIN, ("bridge", 28)),
            _both(COOLDOWN, "seated_twist", 26),
            _both(COOLDOWN, "supine_twist", 30),
            _steps(RELAX, ("savasana", 90))),

    _course("intermediate_06", "Slow Hatha", "intermediate",
            "Fewer poses, held with attention, paced by the breath.",
            # "Slow" is not "everything longer". The pace comes from a long
            # settled arrival, unhurried transitions, and a recovery breath
            # between sides -- not from pushing every hold to its ceiling.
            # Warrior II sits at 34s: inside the 30-40 range that suits this
            # class, and deliberately not at the 45s band maximum.
            _steps(ARRIVAL, ("mountain", 20), ("mountain_breath", 40)),
            _steps(WARMUP, ("upward_salute", 12), ("forward_fold", 28),
                   ("half_lift", 8), ("mountain", 10)),
            _both(WARMUP, "side_bend", 24),
            _steps(MAIN, ("chair", 25), ("forward_fold", 14)),
            _both(MAIN, "warrior_one", 32),
            _steps(MAIN, ("mountain_breath", 30)),
            _both(MAIN, "warrior_two", 34),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "triangle", 32),
            _both(MAIN, "side_angle", 32),
            _steps(MAIN, ("downward_dog", 24), ("child_pose", 30)),
            _both(MAIN, "tree_heart", 20),
            _steps(COOLDOWN, ("butterfly", 32), ("seated_forward_fold", 30)),
            _steps(RELAX, ("savasana", 100))),

    _course("intermediate_07", "Evening Unwind", "intermediate",
            "Begins moderate and steps down to the floor for good.",
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 32)),
            _steps(WARMUP, ("upward_salute", 9), ("forward_fold", 22),
                   ("half_lift", 7), ("mountain", 8)),
            _both(WARMUP, "side_bend", 22),
            _both(MAIN, "low_lunge", 28),
            _both(MAIN, "warrior_two", 28),
            _both(MAIN, "triangle", 26),
            _steps(MAIN, ("downward_dog", 20), ("child_pose", 30)),
            _steps(COOLDOWN, ("butterfly", 32)),
            _both(COOLDOWN, "seated_hamstring", 30),
            _both(COOLDOWN, "figure_four", 30),
            _steps(COOLDOWN, ("knees_to_chest", 26)),
            _both(COOLDOWN, "supine_twist", 30),
            _steps(RELAX, ("savasana", 110))),
]

# ═══ ADVANCED ════════════════════════════════════════════════════════════════
#
# "Advanced" means more balance, longer flows and stronger holds -- never
# inversions, deep binds or anything where bad alignment is dangerous and the
# camera cannot see it. §10 is explicit, and this catalog has no Headstand,
# Shoulderstand, Handstand, Forearm Stand or arm balance to offer.

ADVANCED = [
    _course("advanced_01", "Strong Full Body Flow", "advanced",
            "The complete standing sequence, both sides, with strength.",
            _steps(ARRIVAL, ("mountain", 14), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("chair", 26),
                   ("forward_fold", 18), ("half_lift", 6), ("plank", 18),
                   ("downward_dog", 20)),
            _steps(MAIN, ("high_lunge_left", 26), ("warrior_one_left", 30),
                   ("warrior_two_left", 32), ("reverse_warrior_left", 20),
                   ("side_angle_left", 30), ("triangle_left", 28),
                   ("warrior_three_left", 15), ("downward_dog", 16)),
            _steps(MAIN, ("high_lunge_right", 26), ("warrior_one_right", 30),
                   ("warrior_two_right", 32), ("reverse_warrior_right", 20),
                   ("side_angle_right", 30), ("triangle_right", 28),
                   ("warrior_three_right", 15), ("downward_dog", 16)),
            _both(COOLDOWN, "pigeon_prep", 30),
            _steps(COOLDOWN, ("child_pose", 26), ("seated_forward_fold", 26)),
            _steps(RELAX, ("savasana", 80))),

    _course("advanced_02", "Balance Challenge", "advanced",
            "The hardest balances in the library, with recovery between each.",
            _steps(ARRIVAL, ("mountain", 15), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 20),
                   ("half_lift", 6), ("chair", 28), ("mountain", 8)),
            _both(WARMUP, "standing_knee", 15),
            _both(MAIN, "tree_overhead", 20),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "warrior_three", 16),
            _steps(MAIN, ("forward_fold", 16)),
            _both(MAIN, "half_moon", 18),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "hand_to_toe", 16),
            _steps(MAIN, ("forward_fold", 14), ("plank", 20)),
            _both(MAIN, "side_plank", 15),
            _steps(COOLDOWN, ("child_pose", 30), ("butterfly", 30)),
            _both(COOLDOWN, "figure_four", 28),
            _steps(COOLDOWN, ("knees_to_chest", 26)),
            _both(COOLDOWN, "supine_twist", 28),
            _steps(RELAX, ("savasana", 85))),

    _course("advanced_03", "Strength Flow", "advanced",
            "Standing strength, core and hips, with the legs unloaded between.",
            _steps(ARRIVAL, ("mountain", 14), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("chair", 28),
                   ("forward_fold", 20), ("plank", 20), ("downward_dog", 22)),
            _both(MAIN, "high_lunge", 28),
            _both(MAIN, "warrior_one", 30),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "warrior_two", 32),
            _both(MAIN, "warrior_three", 16),
            _steps(MAIN, ("downward_dog", 16)),
            _steps(MAIN, ("plank", 22)),
            _both(MAIN, "side_plank", 16),
            _steps(MAIN, ("boat", 20), ("bridge", 30)),
            _steps(COOLDOWN, ("knees_to_chest", 28)),
            _both(COOLDOWN, "figure_four", 28),
            _steps(COOLDOWN, ("child_pose", 30)),
            _steps(RELAX, ("savasana", 85))),

    _course("advanced_04", "Deep Mobility", "advanced",
            "Range of motion, controlled, with nothing forced.",
            _steps(ARRIVAL, ("easy_seat", 20), ("easy_seat_breath", 30)),
            _steps(WARMUP, ("table", 6), ("cow", 8), ("cat", 8), ("cow", 8),
                   ("cat", 8), ("downward_dog", 20)),
            _steps(MAIN, ("low_lunge_left", 26), ("lizard_left", 32),
                   ("half_split_left", 30), ("pyramid_left", 28),
                   ("triangle_left", 28), ("side_angle_left", 28),
                   ("wide_leg_fold", 26)),
            _steps(MAIN, ("low_lunge_right", 26), ("lizard_right", 32),
                   ("half_split_right", 30), ("pyramid_right", 28),
                   ("triangle_right", 28), ("side_angle_right", 28),
                   ("wide_leg_fold", 26)),
            _steps(COOLDOWN, ("butterfly", 32)),
            _both(COOLDOWN, "seated_hamstring", 30),
            _both(COOLDOWN, "figure_four", 28),
            _steps(RELAX, ("savasana", 85))),

    _course("advanced_05", "Power Hatha", "advanced",
            "Few shapes, long holds, and recovery earned rather than assumed.",
            # Warrior II keeps its 40 seconds, which is the point of the
            # course -- but only because a fold or a Dog sits either side of
            # it. Cumulative load is the thing being managed here, not the
            # length of any single hold.
            _steps(ARRIVAL, ("mountain", 16), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 10), ("forward_fold", 22),
                   ("half_lift", 8), ("downward_dog", 22)),
            _steps(MAIN, ("chair", 28), ("forward_fold", 14)),
            _both(MAIN, "high_lunge", 30),
            _steps(MAIN, ("downward_dog", 16)),
            _both(MAIN, "warrior_one", 34),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "warrior_two", 40),
            _steps(MAIN, ("downward_dog", 16)),
            _both(MAIN, "side_angle", 34),
            _both(MAIN, "triangle", 32),
            _steps(MAIN, ("forward_fold", 14)),
            _both(MAIN, "tree_overhead", 22),
            _both(MAIN, "warrior_three", 16),
            _steps(COOLDOWN, ("child_pose", 28), ("seated_forward_fold", 28)),
            _steps(RELAX, ("savasana", 85))),

    _course("advanced_06", "Continuous Flow", "advanced",
            "The most vinyasa-like: shorter holds, more movement.",
            _steps(ARRIVAL, ("mountain", 14), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 16),
                   ("half_lift", 6), ("plank", 14), ("downward_dog", 16)),
            _steps(MAIN, ("high_lunge_left", 22), ("warrior_two_left", 26),
                   ("reverse_warrior_left", 16), ("side_angle_left", 26),
                   ("triangle_left", 24), ("downward_dog", 14),
                   ("high_lunge_right", 22), ("warrior_two_right", 26),
                   ("reverse_warrior_right", 16), ("side_angle_right", 26),
                   ("triangle_right", 24), ("downward_dog", 14)),
            _steps(MAIN, ("chair", 24), ("forward_fold", 16), ("plank", 16),
                   ("downward_dog", 16)),
            _both(MAIN, "warrior_three", 14),
            _both(COOLDOWN, "pigeon_prep", 28),
            _steps(COOLDOWN, ("child_pose", 26), ("butterfly", 28)),
            _steps(RELAX, ("savasana", 85))),

    _course("advanced_07", "Complete Practice", "advanced",
            "The culmination: build, peak, and a real recovery afterwards.",
            # Build -> peak -> recover, deliberately. The peak is the Half Moon
            # pair; everything before it climbs to that and everything after it
            # comes down, rather than holding the intensity flat to the end.
            _steps(ARRIVAL, ("mountain", 15), ("mountain_breath", 30)),
            _steps(WARMUP, ("upward_salute", 8), ("forward_fold", 18),
                   ("half_lift", 6), ("mountain", 8)),
            _both(WARMUP, "side_bend", 20),
            _steps(WARMUP, ("chair", 24), ("plank", 16), ("downward_dog", 18)),
            # build
            _steps(MAIN, ("warrior_one_left", 28), ("warrior_two_left", 30),
                   ("side_angle_left", 28), ("triangle_left", 26),
                   ("downward_dog", 14)),
            _steps(MAIN, ("warrior_one_right", 28), ("warrior_two_right", 30),
                   ("side_angle_right", 28), ("triangle_right", 26),
                   ("downward_dog", 14)),
            # peak
            _both(MAIN, "tree_overhead", 18),
            _steps(MAIN, ("mountain", 10)),
            _both(MAIN, "half_moon", 17),
            # recover
            _steps(MAIN, ("forward_fold", 16), ("child_pose", 28)),
            _steps(MAIN, ("bridge", 26)),
            _both(COOLDOWN, "pigeon_prep", 28),
            _steps(COOLDOWN, ("seated_forward_fold", 26)),
            _both(COOLDOWN, "supine_twist", 26),
            _steps(RELAX, ("savasana", 105))),
]


COURSES: dict[str, Course] = {
    course.id: course for course in BEGINNER + INTERMEDIATE + ADVANCED}

LEVELS = ("beginner", "intermediate", "advanced")


def by_level(level: str) -> list[Course]:
    return [c for c in COURSES.values() if c.level == level]
