"""Three twenty-minute classes, written down rather than shuffled.

A yoga sequence is not a playlist. Warm the spine before you bend it, open the
hips before you stand on one leg, and come down through something gentle before
you stop — that is what makes twenty minutes of poses a class instead of
twenty minutes of poses. So these are authored lists, in order, and the game
never picks a pose at random.

Each lesson runs **warm-up, foundations, standing strength, peak, cooldown**,
and within every one of those the poses get harder rather than staying level:
Beginner meets its first balance nine minutes in, after four minutes of warm-up
and five of grounded standing work, because Tree Pose taught cold is Tree Pose
nobody can do. `tests/test_yoga.py` checks that shape mechanically rather than
trusting this docstring.

The structure follows the conventions Tummee documents for beginner classes —
a full-body warm-up, a standing sequence, a progression into Tree Pose, hip
opening, chest opening and a cooldown — but every pose choice, hold, transition
and piece of artwork here is this project's own.

What a class here does *not* contain is a floor. Every pose is standing and
square to the camera, because those are the only poses a webcam on a desk can
see well enough to mark — `poses.py` has the argument. So the closing rest is
Mountain Breath rather than Savasana, and Sun Salutation contributes only its
standing half.

**Timing.** A step costs its hold plus the lesson's transition, and the lesson
clock is twenty minutes of wall time regardless: a player who cannot find a
pose does not get a longer class, they get a shorter one, and the class still
ends with the cooldown they were promised because the last steps are the cheap
ones. The totals below land at 19-20 minutes for somebody who is finding the
poses, which leaves room for the countdown and the between-pose results.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipi5.games.yoga.poses import POSES, get

#: The wall clock every lesson runs against, in seconds. The lessons are
#: written to finish just inside it.
LESSON_SECONDS = 1200.0

#: How long the coach is shown moving into a pose, and the pose name and hold
#: are on screen, before the hold timer starts. Beginners get longer because
#: the instruction has to be read as well as watched.
#:
#: Also the tween time for the coach animation — see `yoga.js`.
TRANSITION_SECONDS: dict[str, float] = {
    "beginner": 8.0, "intermediate": 6.0, "advanced": 5.0}

#: How much every tolerance band in `scoring.py` is stretched or squeezed. This
#: is the main thing difficulty means: the same Warrior II, judged generously
#: or judged closely.
TOLERANCE_SCALE: dict[str, float] = {
    "beginner": 1.35, "intermediate": 1.05, "advanced": 0.88}

#: The accuracy the player has to reach before the hold timer starts counting
#: down. Not the same knob as the tolerance: one decides what "correct" means,
#: this decides how much of the body has to be correct at once.
HOLD_THRESHOLD: dict[str, float] = {
    "beginner": 0.58, "intermediate": 0.64, "advanced": 0.70}

#: How long a pose may occupy the wall clock before the class moves on without
#: it, as a multiple of the hold plus a fixed allowance. Without this a player
#: who never finds Half Moon spends the rest of their twenty minutes in it.
DEADLINE_FACTOR = 1.6
DEADLINE_ALLOWANCE = 5.0

#: How long the score for a finished pose stays on screen before the next one
#: begins to be demonstrated.
RESULT_SECONDS = 2.2

#: Seconds of "3, 2, 1" after the crossed arms and before the first pose.
COUNTDOWN_SECONDS = 4.0

DIFFICULTIES: tuple[str, ...] = ("beginner", "intermediate", "advanced")


@dataclass(frozen=True)
class Step:
    pose_id: str
    hold_s: float
    segment: str

    @property
    def pose(self):
        return get(self.pose_id)


@dataclass(frozen=True)
class Lesson:
    difficulty: str
    name: str
    blurb: str
    steps: tuple[Step, ...]

    @property
    def transition_s(self) -> float:
        return TRANSITION_SECONDS[self.difficulty]

    @property
    def tolerance_scale(self) -> float:
        return TOLERANCE_SCALE[self.difficulty]

    @property
    def hold_threshold(self) -> float:
        return HOLD_THRESHOLD[self.difficulty]

    @property
    def total_seconds(self) -> float:
        """What the class costs a player who finds every pose promptly."""
        return sum(step.hold_s + self.transition_s for step in self.steps)

    @property
    def hold_seconds(self) -> float:
        return sum(step.hold_s for step in self.steps)

    @property
    def segments(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.segment not in seen:
                seen.append(step.segment)
        return tuple(seen)


def _segment(name: str, *pairs: tuple[str, float]) -> tuple[Step, ...]:
    return tuple(Step(pose_id, float(hold), name) for pose_id, hold in pairs)


WARMUP = "Warm-up"
FOUNDATION = "Foundations"
STRENGTH = "Standing Strength"
PEAK = "Peak"
COOLDOWN = "Cooldown"


BEGINNER = Lesson(
    "beginner", "Beginner",
    "Gentle standing poses, long instructions, generous scoring.",
    _segment(
        WARMUP,
        ("mountain", 25), ("neck_tilt_left", 18), ("neck_tilt_right", 18),
        ("cactus_arms", 20), ("shoulder_opener", 20), ("upward_salute", 20),
        ("side_bend_left", 20), ("side_bend_right", 20),
        ("forward_fold", 22), ("mountain", 15),
    ) + _segment(
        FOUNDATION,
        ("chair", 18), ("mountain", 12), ("star", 20), ("goddess", 20),
        ("warrior_two_left", 22), ("mountain", 12), ("warrior_two_right", 22),
        ("wide_leg_fold", 22), ("mountain", 12),
    ) + _segment(
        STRENGTH,
        ("warrior_one_left", 20), ("warrior_one_right", 20),
        ("triangle_left", 20), ("triangle_right", 20),
        ("side_angle_left", 18), ("side_angle_right", 18),
        ("mountain", 12),
        ("crescent_moon_left", 18), ("crescent_moon_right", 18),
    ) + _segment(
        PEAK,
        ("tree_heart_left", 20), ("mountain", 10), ("tree_heart_right", 20),
        ("tree_overhead_left", 16), ("tree_overhead_right", 16),
        ("chair", 18), ("goddess", 20), ("cactus_arms", 18),
    ) + _segment(
        COOLDOWN,
        ("forward_fold", 28), ("side_bend_left", 18), ("side_bend_right", 18),
        ("cactus_arms", 20), ("upward_salute", 18), ("mountain", 20),
        ("mountain_breath", 55),
    ),
)


INTERMEDIATE = Lesson(
    "intermediate", "Intermediate",
    "Longer holds, deeper ranges, and balance on both sides.",
    _segment(
        WARMUP,
        ("mountain", 20), ("cactus_arms", 20), ("upward_salute", 20),
        ("side_bend_left", 22), ("side_bend_right", 22),
        ("shoulder_opener", 20), ("forward_fold", 25), ("star", 22),
    ) + _segment(
        FOUNDATION,
        ("chair", 24), ("goddess", 26), ("wide_leg_fold", 26),
        ("warrior_two_left", 28), ("warrior_two_right", 28), ("mountain", 12),
    ) + _segment(
        STRENGTH,
        ("warrior_one_left", 26), ("warrior_one_right", 26),
        ("triangle_left", 26), ("triangle_right", 26),
        ("side_angle_left", 24), ("side_angle_right", 24),
        ("crescent_moon_left", 20), ("crescent_moon_right", 20),
    ) + _segment(
        PEAK,
        ("tree_overhead_left", 26), ("tree_overhead_right", 26),
        ("chair", 24), ("goddess", 28),
        ("warrior_two_left", 24), ("warrior_two_right", 24),
        ("hand_to_toe_left", 20), ("hand_to_toe_right", 20),
        ("cactus_arms", 22),
    ) + _segment(
        COOLDOWN,
        ("forward_fold", 30), ("wide_leg_fold", 21),
        ("neck_tilt_left", 16), ("neck_tilt_right", 16),
        ("side_bend_left", 20), ("side_bend_right", 20),
        ("upward_salute", 20), ("mountain", 20), ("mountain_breath", 65),
    ),
)


ADVANCED = Lesson(
    "advanced", "Advanced",
    "Deep holds, close scoring, and the balance poses at the peak.",
    _segment(
        WARMUP,
        ("mountain", 18), ("upward_salute", 20), ("cactus_arms", 20),
        ("side_bend_left", 22), ("side_bend_right", 22),
        ("forward_fold", 28), ("star", 24), ("shoulder_opener", 20),
    ) + _segment(
        FOUNDATION,
        ("chair", 30), ("goddess", 34), ("wide_leg_fold", 32),
        ("warrior_two_left", 34), ("warrior_two_right", 34),
    ) + _segment(
        STRENGTH,
        ("warrior_one_left", 30), ("warrior_one_right", 30),
        ("triangle_left", 32), ("triangle_right", 32),
        ("side_angle_left", 34), ("side_angle_right", 34),
    ) + _segment(
        PEAK,
        ("tree_overhead_left", 28), ("tree_overhead_right", 28),
        ("half_moon_left", 28), ("half_moon_right", 28),
        ("hand_to_toe_left", 26), ("hand_to_toe_right", 26),
        ("goddess", 28), ("chair", 24), ("cactus_arms", 24),
    ) + _segment(
        COOLDOWN,
        ("forward_fold", 30), ("wide_leg_fold", 26),
        ("crescent_moon_left", 22), ("crescent_moon_right", 22),
        ("side_bend_left", 20), ("side_bend_right", 20),
        ("upward_salute", 20), ("mountain", 20), ("mountain_breath", 55),
    ),
)


LESSONS: dict[str, Lesson] = {
    lesson.difficulty: lesson for lesson in (BEGINNER, INTERMEDIATE, ADVANCED)}


def get_lesson(difficulty: str) -> Lesson:
    try:
        return LESSONS[difficulty]
    except KeyError:
        raise ValueError("choose Beginner, Intermediate or Advanced") from None


def _validate() -> None:
    """Fail at import rather than fifteen minutes into somebody's practice.

    A mistyped pose id in a sequence is otherwise a blank coach at minute
    thirteen on a device with no console in front of it, and the sequences are
    the part of this game most likely to be edited by somebody who is thinking
    about yoga rather than about Python.
    """
    for lesson in (BEGINNER, INTERMEDIATE, ADVANCED):
        for step in lesson.steps:
            if step.pose_id not in POSES:
                raise KeyError(
                    f"{lesson.difficulty} asks for a pose that does not "
                    f"exist: {step.pose_id!r}")
        # Both sides of everything. A class that works one hip and not the
        # other is not a class, and it is an easy thing to leave out.
        sided = [step.pose_id for step in lesson.steps
                 if POSES[step.pose_id].side]
        for pose_id in set(sided):
            partner = POSES[pose_id].mirror_id
            if partner not in sided:
                raise ValueError(
                    f"{lesson.difficulty} does {pose_id} but never {partner}")


_validate()
