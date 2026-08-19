"""Comparing a body against a pose, and saying one useful sentence about it.

Three things happen here, and they are separate on purpose because they fail
differently: how close the player is *right now* (`assess`), which single
correction is worth putting on screen (`FeedbackPicker`), and how steady they
were over the whole hold (`Stability`). A yoga pose is not scored on any one of
those alone — the spec's own examples make the point, since 94 and 61 are both
reachable by somebody whose skeleton passed through the right shape at some
moment.

**Nothing is scored to the pixel and nothing is scored to the degree.** Every
metric has a tolerance band inside which it is simply correct, and the score
falls off linearly outside it rather than dropping off a cliff. Real people
have different femurs, different shoulders and different ideas about how far
their hamstrings go, and a game that told a beginner their Warrior II was 71%
because their front knee was at 96 degrees instead of 90 would be measuring the
wrong thing accurately.

**The mirror check is the one thing here that is not a tolerance.** Doing a
beautiful Warrior II on the wrong side is not a slightly wrong pose, it is a
different pose, and it scores well against the mirrored target while scoring
badly against the real one. That is a signal rather than a nuisance: comparing
against both targets costs one extra pass over twenty numbers and turns the
most common mistake in a follow-along class into a sentence that fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from aipi5.games.yoga.rig import (
    ANGLE_METRICS, STANDING_METRICS, angular_distance, mirrored)

#: How far a joint may be from the coach and still be simply correct. Degrees
#: for the angles, shoulder-widths for the ratios.
#:
#: These are not measurement error; they are the width of the population.
#: Shoulders and elbows are loose because arm placement is where people vary
#: most and where it matters least; the torso is tight because it is the one
#: thing that is either upright or is not, and it is what "keep your body tall"
#: actually means.
BASE_TOLERANCE: dict[str, float] = {
    "left_elbow": 26.0, "right_elbow": 26.0,
    "left_shoulder": 22.0, "right_shoulder": 22.0,
    "left_hip": 18.0, "right_hip": 18.0,
    "left_knee": 20.0, "right_knee": 20.0,
    "torso": 13.0,
    "head": 24.0,
    # Tighter than they look, and they can be: `Calibration` has already taken
    # the player's own limb lengths out of these, so what is left really is
    # how far they are from the pose rather than how far their legs are from
    # the coach's.
    "stance": 0.40,
    "stack": 0.22,
    "reach": 0.50,
    "left_hand_height": 0.38, "right_hand_height": 0.38,
    "left_foot_height": 0.34, "right_foot_height": 0.34,
}

#: Ratio metrics that scale with how long the player's legs are, and with how
#: long their arms are. Everything else on the body is an angle and needs no
#: such correction.
LEG_RATIOS: frozenset[str] = frozenset({
    "stack", "stance", "left_foot_height", "right_foot_height"})
ARM_RATIOS: frozenset[str] = frozenset({
    "reach", "left_hand_height", "right_hand_height"})

#: What each metric is worth before the pose has its say. Elbows and head are
#: low because a slightly bent elbow is still the pose; hips, knees and torso
#: are the pose.
BASE_WEIGHT: dict[str, float] = {
    "left_elbow": 0.55, "right_elbow": 0.55,
    "left_shoulder": 1.00, "right_shoulder": 1.00,
    "left_hip": 1.00, "right_hip": 1.00,
    "left_knee": 1.00, "right_knee": 1.00,
    "torso": 1.10,
    "head": 0.35,
    "stance": 0.85,
    "stack": 0.85,
    "reach": 0.55,
    "left_hand_height": 0.70, "right_hand_height": 0.70,
    "left_foot_height": 0.70, "right_foot_height": 0.70,
}

#: What share of a pose's total weight its `emphasis` joints carry between
#: them, once it declares any.
#:
#: A share rather than a multiplier, and the difference is the whole reason
#: this is not a one-line `weight *= 2`. A Neck Release is *entirely* about one
#: joint out of seventeen; multiplying that joint by two still leaves a body
#: standing perfectly still scoring in the high nineties, because fifteen
#: unchanged metrics are all agreeing that it is standing perfectly still. A
#: share says "this pose is sixty-two percent about these joints" no matter how
#: many joints that is, which is the thing the pose actually means.
EMPHASIS_SHARE = 0.62

#: `relax` does not quite zero a metric, because a knee that is wildly wrong
#: should still cost something in a pose that is not about knees.
RELAX_FACTOR = 0.25

#: A metric a pose leaves exactly where standing leaves it keeps only this much
#: of its weight, rising to all of it once the pose moves that metric by a full
#: tolerance band or more. Automatic, and it means a pose does not have to list
#: every joint it is *not* about in order to be scored on the ones it is.
NEUTRAL_FLOOR = 0.45

#: Where the linear fall-off outside the tolerance band reaches zero, as a
#: multiple of the tolerance. 2.6 means a joint 40 degrees out of a 20-degree
#: band still scores about a third rather than nothing, which is what keeps a
#: nearly-right pose feeling nearly right.
FALLOFF = 1.6

#: Metrics whose *magnitude* is the pose and whose sign is anatomy. A knee
#: bends one way; being told to bend it "the other way" would be nonsense.
MAGNITUDE_METRICS: frozenset[str] = frozenset({
    "left_elbow", "right_elbow", "left_knee", "right_knee"})

#: How much of the body has to be measurable before a frame is scored at all.
#: Below this the player is too close, half out of frame, or turned side-on,
#: and the honest answer is to say so rather than to score the visible half.
MIN_COVERAGE = 0.55

#: How much better the mirrored target has to fit before the player is told
#: they are on the wrong side. Generous, because a symmetric-ish pose scores
#: nearly the same both ways and telling somebody to swap sides when they are
#: already correct is the worst thing this game could do.
MIRROR_MARGIN = 0.12

#: What to say, per metric, when there is too little of it and when there is
#: too much. Poses override these where the generic wording would be vague —
#: see `Pose.cues`.
PHRASES: dict[str, tuple[str, str]] = {
    "left_elbow": ("Bend your left arm more", "Straighten your left arm"),
    "right_elbow": ("Bend your right arm more", "Straighten your right arm"),
    "left_shoulder": ("Take your left arm further out",
                      "Bring your left arm in closer"),
    "right_shoulder": ("Take your right arm further out",
                       "Bring your right arm in closer"),
    "left_hip": ("Take your left leg further out",
                 "Bring your left leg in closer"),
    "right_hip": ("Take your right leg further out",
                  "Bring your right leg in closer"),
    "left_knee": ("Bend your left knee more", "Straighten your left leg"),
    "right_knee": ("Bend your right knee more", "Straighten your right leg"),
    "torso": ("Lean further into the pose", "Straighten your back"),
    "head": ("Follow the coach with your head", "Bring your head back to centre"),
    "stance": ("Step your feet wider", "Bring your feet closer together"),
    "stack": ("Sink lower into the pose", "Lift your hips a little higher"),
    "reach": ("Reach your hands further apart", "Bring your hands closer"),
    "left_hand_height": ("Raise your left arm higher", "Lower your left arm"),
    "right_hand_height": ("Raise your right arm higher", "Lower your right arm"),
    "left_foot_height": ("Lift your left foot higher", "Lower your left foot"),
    "right_foot_height": ("Lift your right foot higher", "Lower your right foot"),
}

#: Said when there is nothing to correct. Rotated so a long hold does not stare
#: back with the same two words for forty seconds.
PRAISE: tuple[str, ...] = (
    "Excellent — hold it!",
    "That's the shape. Breathe.",
    "Beautiful. Stay here.",
    "Steady and strong — hold.",
)

#: Said when the pose is close but not yet inside the band.
NEARLY = "Almost there — settle into it"


def weights(pose) -> dict[str, float]:
    """What each metric is worth in this pose.

    Three things in order: the base weight, how far this pose moves the metric
    away from simply standing there, and finally the pose's own emphasis
    normalised to a fixed share of the total.
    """
    band = tolerances(pose)
    table: dict[str, float] = {}
    for name, base in BASE_WEIGHT.items():
        weight = base
        neutral = STANDING_METRICS.get(name)
        target = pose.targets.get(name)
        if neutral is not None and target is not None:
            distance = _error(name, target, neutral) / band[name]
            weight *= NEUTRAL_FLOOR + (1.0 - NEUTRAL_FLOOR) * min(1.0, distance)
        if name in pose.relax:
            weight *= RELAX_FACTOR
        table[name] = weight

    chosen = [name for name in pose.emphasis if table.get(name)]
    if not chosen:
        return table
    rest = sum(value for name, value in table.items() if name not in chosen)
    focus = sum(table[name] for name in chosen)
    if rest <= 0.0 or focus <= 0.0:
        return table
    wanted = rest * EMPHASIS_SHARE / (1.0 - EMPHASIS_SHARE)
    factor = wanted / focus
    for name in chosen:
        table[name] *= factor
    return table


def tolerances(pose, scale: float = 1.0) -> dict[str, float]:
    factor = pose.tolerance_scale * scale
    return {name: value * factor for name, value in BASE_TOLERANCE.items()}


def _error(name: str, value: float, target: float) -> float:
    if name in ANGLE_METRICS:
        return angular_distance(value, target)
    return abs(value - target)


def _metric_score(error: float, tolerance: float) -> float:
    if error <= tolerance:
        return 1.0
    return max(0.0, 1.0 - (error - tolerance) / (FALLOFF * tolerance))


def _too_much(name: str, value: float, target: float) -> bool:
    """Which of the metric's two phrases applies."""
    if name not in ANGLE_METRICS:
        return value > target
    if name in MAGNITUDE_METRICS:
        return abs(value) > abs(target)
    # A signed angle the player has taken the wrong way is not "too much" of
    # anything; they need to be told to go the intended way.
    if abs(target) >= 8.0 and value * target < 0:
        return False
    return abs(value) > abs(target)


@dataclass
class Calibration:
    """The player's own proportions, learned while they stand still.

    The angle half of this game needs no calibration — an elbow at 90 degrees
    is 90 degrees on anybody. The ratio half does: `stack` asks how far the
    hips sit above the feet in shoulder widths, and a long-legged player
    standing perfectly upright measures 2.3 where a short-legged one measures
    1.9. Judged against the coach's 2.07 flat, one of them is permanently told
    to sink lower and the other to lift up, in every pose, forever.

    So the coach's Mountain Pose is compared against the *player's* Mountain
    Pose once, and the two ratios that fall out of it — how long their legs
    are and how long their arms are, relative to the coach — scale every ratio
    target afterwards. This is what makes the tolerance bands in
    `BASE_TOLERANCE` affordable: they only have to cover how wrong the pose is,
    not how differently the player is built.

    Samples are only taken from a body that is already standing more or less
    still and upright, because a calibration taken during Warrior II would
    teach the game that this player's legs are half as long as they are.
    """

    leg: float = 1.0
    arm: float = 1.0
    samples: int = 0

    #: Weight on each new sample. Slow, because this is a property of a person
    #: rather than of a moment and there is no hurry: the ready screen and the
    #: countdown alone supply a hundred frames before the first pose.
    alpha: float = 0.08
    #: A limb ratio outside this is not a differently proportioned person, it
    #: is a bad frame, and adopting it would be worse than not calibrating.
    floor: float = 0.72
    ceiling: float = 1.34

    def add(self, metrics: dict[str, float],
            standing: dict[str, float]) -> bool:
        """Fold one standing frame in. False if it was not usable."""
        pairs = []
        if "stack" in metrics and standing.get("stack"):
            pairs.append(("leg", metrics["stack"] / standing["stack"]))
        arms = [metrics[name] / standing[name]
                for name in ("left_hand_height", "right_hand_height")
                if name in metrics and standing.get(name)]
        if arms:
            pairs.append(("arm", sum(arms) / len(arms)))
        if not pairs:
            return False

        used = False
        for field_name, ratio in pairs:
            if not self.floor <= ratio <= self.ceiling:
                continue
            current = getattr(self, field_name)
            setattr(self, field_name,
                    ratio if not self.samples
                    else current + self.alpha * (ratio - current))
            used = True
        if used:
            self.samples += 1
        return used

    def factor(self, metric: str) -> float:
        if metric in LEG_RATIOS:
            return self.leg
        if metric in ARM_RATIOS:
            return self.arm
        return 1.0

    def as_dict(self) -> dict:
        return {"leg": round(self.leg, 3), "arm": round(self.arm, 3),
                "samples": self.samples}


@dataclass(frozen=True)
class Assessment:
    """One frame of one player against one pose."""

    #: 0.0 to 1.0. What the HUD shows as a percentage and what the hold timer
    #: is gated on.
    accuracy: float = 0.0
    #: False when too little of the body was measurable to say anything. The
    #: hold timer never advances on an untracked frame and the score never
    #: counts one, so standing out of shot is neither rewarded nor punished.
    tracked: bool = False
    #: What the same body scores against the pose done on the other side.
    mirror_accuracy: float = 0.0
    wrong_side: bool = False
    #: The metric furthest outside its band, weighted. "" when everything fits.
    worst: str = ""
    worst_error: float = 0.0
    #: Fraction of the pose's total weight that could actually be measured.
    coverage: float = 0.0
    #: Per-metric scores, for the debug overlay only.
    detail: dict[str, float] = field(default_factory=dict)
    #: Why nothing could be measured, written for the screen.
    advice: str = ""


def targets_for(pose, calibration: Calibration | None) -> dict[str, float]:
    """The coach's numbers, resized to this player's arms and legs."""
    if calibration is None or not calibration.samples:
        return dict(pose.targets)
    return {name: value * calibration.factor(name)
            for name, value in pose.targets.items()}


def assess(pose, metrics: dict[str, float], *, tolerance_scale: float = 1.0,
           calibration: Calibration | None = None,
           check_mirror: bool = True) -> Assessment:
    """Score one frame of the player against one pose.

    `metrics` comes from `rig.measure`; an empty dict means the pose model saw
    nobody usable and produces an untracked assessment rather than a zero,
    because those two mean opposite things to a hold timer.
    """
    if not metrics:
        return Assessment(advice="Step into view of the camera")

    targets = targets_for(pose, calibration)
    weight = weights(pose)
    band = tolerances(pose, tolerance_scale)

    total = 0.0
    scored = 0.0
    possible = 0.0
    detail: dict[str, float] = {}
    worst = ""
    worst_penalty = 0.0
    worst_error = 0.0

    for name, target in targets.items():
        w = weight.get(name, 0.0)
        if w <= 0.0:
            continue
        possible += w
        if name not in metrics:
            continue
        error = _error(name, metrics[name], target)
        score = _metric_score(error, band[name])
        detail[name] = round(score, 3)
        total += w
        scored += w * score
        penalty = w * (1.0 - score)
        if penalty > worst_penalty:
            worst, worst_penalty, worst_error = name, penalty, error

    coverage = total / possible if possible else 0.0
    if total <= 0.0 or coverage < MIN_COVERAGE:
        # Almost always the same physical situation: standing close enough for
        # the wrists but not for the ankles. Section 20's start screen asks for
        # the upper body; yoga needs the feet, and it is the only game here
        # that does.
        return Assessment(coverage=round(coverage, 3), detail=detail,
                          advice="Step back so the camera can see your feet")

    accuracy = scored / total

    mirror_accuracy = 0.0
    wrong_side = False
    if check_mirror and pose.side:
        flipped = assess(pose, mirrored(metrics),
                         tolerance_scale=tolerance_scale,
                         calibration=calibration, check_mirror=False)
        mirror_accuracy = flipped.accuracy
        wrong_side = mirror_accuracy - accuracy >= MIRROR_MARGIN

    return Assessment(
        accuracy=accuracy, tracked=True, mirror_accuracy=mirror_accuracy,
        wrong_side=wrong_side, worst=worst, worst_error=worst_error,
        coverage=round(coverage, 3), detail=detail)


def correction(pose, metrics: dict[str, float], assessment: Assessment,
               calibration: Calibration | None = None) -> str:
    """The one sentence worth showing. Short, imperative, and about one joint.

    Deliberately never a list. A player holding Warrior II cannot read three
    corrections and act on them, and the second-worst joint is very often
    downstream of the worst one anyway — a knee that is not bent enough makes
    the hips too high, and fixing the knee fixes both.
    """
    if not assessment.tracked:
        return assessment.advice
    if assessment.wrong_side:
        other = "right" if pose.side == "left" else "left"
        return f"Other side — lead with your {pose.side}, not your {other}"
    if not assessment.worst:
        return ""
    name = assessment.worst
    targets = targets_for(pose, calibration)
    if name not in metrics or name not in targets:
        return ""
    too_much = _too_much(name, metrics[name], targets[name])
    phrases = pose.cues.get(name) or PHRASES.get(name)
    if not phrases:
        return ""
    return phrases[1] if too_much else phrases[0]


class FeedbackPicker:
    """Stop the correction line from flickering between two joints.

    The worst joint changes from frame to frame while somebody is settling, and
    text that changes thirty times a second is text nobody reads. A new message
    has to be the worst one for a short while before it replaces the one on
    screen, and once shown it stays for long enough to be acted on.
    """

    def __init__(self, *, settle_s: float = 0.5, hold_s: float = 1.6):
        self.settle_s = settle_s
        self.hold_s = hold_s
        self.reset()

    def reset(self) -> None:
        self.message = ""
        self._candidate = ""
        self._candidate_since = 0.0
        self._shown_at = 0.0
        self._praise = 0

    def update(self, now: float, message: str, *, urgent: bool = False) -> str:
        if message == self.message:
            self._candidate = ""
            return self.message
        if urgent:
            # Wrong side, or nobody in frame. Both are conditions the player
            # cannot fix by adjusting what they are already doing, so they are
            # worth interrupting for.
            self.message, self._shown_at, self._candidate = message, now, ""
            return self.message
        if message != self._candidate:
            self._candidate, self._candidate_since = message, now
            return self.message
        if now - self._candidate_since < self.settle_s:
            return self.message
        if self.message and now - self._shown_at < self.hold_s:
            return self.message
        self.message, self._shown_at, self._candidate = message, now, ""
        return self.message

    def praise(self) -> str:
        """The next of the rotating well-done lines."""
        line = PRAISE[self._praise % len(PRAISE)]
        self._praise += 1
        return line


@dataclass
class Stability:
    """How steady the hold was, by Welford over the accuracy series.

    Steadiness is measured on the *score* rather than on joint positions, and
    that is the useful definition here rather than the convenient one: a
    balance that is genuinely still and a balance that is being caught and
    recovered thirty times have similar mean accuracy and completely different
    variance, and the second one is the one that has not been learned yet.

    Breaks are counted separately because variance under-reports them: falling
    out of Tree Pose once and getting straight back in barely moves the
    standard deviation and is the single most informative thing that happened.
    """

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    breaks: int = 0
    _holding: bool = False
    _started: bool = False

    def add(self, accuracy: float, holding: bool) -> None:
        self.count += 1
        delta = accuracy - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (accuracy - self.mean)
        if holding:
            self._started = True
        elif self._started and self._holding:
            self.breaks += 1
        self._holding = holding

    @property
    def deviation(self) -> float:
        if self.count < 2:
            return 0.0
        return math.sqrt(max(0.0, self.m2 / (self.count - 1)))

    def value(self) -> float:
        """0.0 to 1.0. One clean hold is 1.0; a scramble is near zero."""
        if self.count < 2:
            return 1.0 if self.count else 0.0
        spread = min(1.0, self.deviation / 0.16)
        stumbles = min(1.0, self.breaks / 4.0)
        return max(0.0, 1.0 - 0.7 * spread - 0.3 * stumbles)


#: How the 0-100 numbers are read out loud. The boundaries are the spec's own
#: examples: 94 Excellent, 86 Great, 74 Good, 61 Keep Practicing.
BANDS: tuple[tuple[int, str], ...] = (
    (90, "Excellent"),
    (80, "Great"),
    (70, "Good"),
    (0, "Keep Practicing"),
)


def band(score: float) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return BANDS[-1][1]


#: What the three parts of a pose score are worth. Quality is how close the
#: shape was, coverage is how much of the asked-for hold was actually credited,
#: stability is how still it was. Coverage carries nearly a third on purpose —
#: the spec is explicit that a pose held correctly for the whole time must beat
#: a pose that was briefly perfect, and one frame of a beautiful Triangle with
#: no hold behind it tops out at fifty.
QUALITY_WEIGHT = 0.50
COVERAGE_WEIGHT = 0.30
STABILITY_WEIGHT = 0.20


def pose_score(quality: float, coverage: float, stability: float) -> int:
    value = (QUALITY_WEIGHT * max(0.0, min(1.0, quality))
             + COVERAGE_WEIGHT * max(0.0, min(1.0, coverage))
             + STABILITY_WEIGHT * max(0.0, min(1.0, stability)))
    return int(round(100 * value))
