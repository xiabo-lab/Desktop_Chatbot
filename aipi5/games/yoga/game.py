"""One yoga session: a lesson, a clock, and a body being compared to a coach.

The same shape as `BoxingSession` and for the same reason — the manager holds
the camera and the accelerator, calls `tick_pose` once per shared pose frame,
and this decides what any of it means. Nothing here opens a camera, loads a
model or draws anything.

**Two clocks, and they measure different things.** `time_left` is twenty
minutes of wall time and is the promise the game made; `hold_left` is how much
of the current pose is still owed and only moves while the player is actually
in the pose. Folding them into one would break whichever promise was folded
into the other: a hold timer that ran regardless would credit somebody standing
still, and a lesson clock that stopped would turn a twenty-minute class into
however long the hardest pose took.

**A pose that is not being found is left, not failed.** Every step carries a
wall-clock deadline as well as a hold, so a player who cannot get into Half
Moon spends about forty seconds being told what to fix and then moves on with
whatever they earned. Nothing in this game ends a session early, and nothing
makes the player press anything: the only input after the crossed arms is the
player's own body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import math

from aipi5.games.fruit_ninja.game import State
from aipi5.games.yoga.lesson import (
    COUNTDOWN_SECONDS, DEADLINE_ALLOWANCE, DEADLINE_FACTOR, DIFFICULTIES,
    LESSON_SECONDS, RESULT_SECONDS, Lesson, get_course, get_lesson)
from aipi5.games.yoga.poses import POSES, get as get_pose
from aipi5.games.yoga.rig import measure, player_joints
from aipi5.games.yoga.scoring import (
    Assessment, Calibration, FeedbackPicker, Stability, assess, band,
    correction, pose_score)

#: Weight on each new accuracy sample. The number the hold timer is gated on
#: must not be the raw per-frame one: at thirty frames a second a single
#: mis-detected ankle would stop the clock, and the player would see a hold
#: that stutters for reasons they cannot see or fix. Four frames of smoothing
#: is about 130 ms, which is below what anybody notices and above what one bad
#: frame can do.
ACCURACY_SMOOTHING = 0.25

#: Once the player is in the pose, they may drop this far below the threshold
#: without the hold stopping. Coming out of a pose is a movement, not a step
#: change, and a bare threshold makes the timer flicker for the whole second it
#: takes somebody to wobble and recover.
HOLD_HYSTERESIS = 0.06

#: Accuracy at or above this counts as being in the pose well enough that the
#: correction line gives way to praise.
PRAISE_ACCURACY = 0.86

#: How close somebody has to be to Mountain Pose before their body is used to
#: calibrate limb lengths. Deliberately not strict — the point is to exclude
#: somebody who is mid-Warrior, not to require a perfect Tadasana.
CALIBRATION_ACCURACY = 0.62


log = logging.getLogger("aipi5.games.yoga")

class Phase(str, Enum):
    """Where one *pose* is, inside `State.PLAYING`.

    Separate from `State` for the reason `fruit_ninja.Phase` is separate: they
    answer different questions, and pausing during a hold must not lose the
    hold. An enum rather than a set of time comparisons because each change is
    an edge that has to fire exactly once — one chime per pose, one score per
    pose — and `if hold_left <= 0` is true for every frame afterwards.
    """

    COUNTDOWN = "countdown"
    #: The finished pose, held still, with its name and nothing else. The
    #: player is being shown where this is going before being asked to go
    #: there; no instruction is spoken and nothing is scored.
    PREVIEW = "preview"
    #: The coach is moving into the pose, through the transition frames, and
    #: the instruction is on screen and spoken. Still nothing scored.
    TRANSITION = "transition"
    HOLDING = "holding"


@dataclass
class PoseResult:
    """What one pose was worth, kept for the end-of-session summary."""

    pose_id: str
    name: str
    side: str
    segment: str
    score: int
    accuracy: float
    held_s: float
    required_s: float
    stability: float

    def as_dict(self) -> dict:
        return {
            "pose": self.pose_id,
            "name": self.name,
            "side": self.side,
            "segment": self.segment,
            "score": self.score,
            "band": band(self.score),
            "accuracy": round(100 * self.accuracy),
            "held": round(self.held_s, 1),
            "required": round(self.required_s, 1),
        }


@dataclass
class YogaSession:
    """One visit to the Yoga Coach: pick a difficulty, follow the class."""

    best: int = 0
    difficulty: str = ""
    course_id: str = ""
    state: State = State.READY
    score: int = 0
    events: list[dict] = field(default_factory=list)

    lesson: Lesson | None = None
    duration: float = LESSON_SECONDS
    time_left: float = LESSON_SECONDS
    step_index: int = 0
    phase: Phase = Phase.COUNTDOWN
    #: Wall clock at which the current phase is due to end. For HOLDING this is
    #: the deadline that moves the class on regardless, not the hold itself.
    phase_until: float = 0.0
    phase_started: float = 0.0
    hold_left: float = 0.0

    results: list[PoseResult] = field(default_factory=list)

    calibration: Calibration = field(default_factory=Calibration)
    feedback: FeedbackPicker = field(default_factory=FeedbackPicker)

    #: Where a spoken instruction goes, injected by the manager so this file
    #: never reaches for the assistant's voice. `None` in tests and whenever
    #: the device has no speaker, and the class then reads exactly the same on
    #: screen — the instruction has always been written down as well.
    speak: object = None

    _last_tick: float = -1.0
    #: Until when the coach is believed to be talking. Estimated rather than
    #: reported: piper is handed the line and returns immediately, and the only
    #: consumer is the page ducking its music under her voice, which does not
    #: need the truth to the millisecond.
    _speaking_until: float = 0.0
    _paused_at: float = 0.0
    _accuracy: float = 0.0
    _assessment: Assessment = field(default_factory=Assessment)
    _metrics: dict = field(default_factory=dict)
    _holding: bool = False
    _reached: bool = False
    _stability: Stability = field(default_factory=Stability)
    _quality_sum: float = 0.0
    _quality_frames: int = 0
    _held_s: float = 0.0
    _last_result: PoseResult | None = None
    #: Wall clock until which the score just earned stays on screen. Not a
    #: phase of its own, and that is the difference between a class that runs
    #: to twenty minutes and one that runs to twenty-one and a half: forty
    #: poses times a two-second results card is a minute and a half of a
    #: twenty-minute lesson spent looking at a number. The card is drawn over
    #: the start of the next pose's transition, where the coach is already
    #: moving and the player has nothing to do yet.
    _result_until: float = 0.0
    _previous_pose_id: str = "mountain"
    #: Chosen once per pose so a forty-second hold is not congratulated in four
    #: different ways while nothing about it has changed.
    _praise: str = ""

    # ── selection and lifecycle ──────────────────────────────────────

    @property
    def startable(self) -> bool:
        return self.difficulty in DIFFICULTIES and self.lesson is not None

    @property
    def gesture_phase(self) -> str:
        """What `CrossedArmsGesture` is told the game is doing.

        "select" while no difficulty has been chosen, so an X held over the
        difficulty sheet does not start a lesson nobody has picked. Exactly
        what Boxing does with its mode sheet.
        """
        if self.state is State.READY and not self.startable:
            return "select"
        return self.state.value

    def select_difficulty(self, difficulty: str) -> None:
        if difficulty not in DIFFICULTIES:
            raise ValueError("choose Beginner, Intermediate or Advanced")
        if self.state is State.PLAYING:
            raise ValueError("the difficulty cannot change during a lesson")
        self.difficulty = difficulty
        self.lesson = get_lesson(difficulty)
        self.course_id = self.lesson.course_id
        self.state = State.READY

    def select_course(self, course_id: str) -> None:
        """Choose one of the twenty-one authored classes."""
        if self.state is State.PLAYING:
            raise ValueError("the course cannot change during a lesson")
        lesson = get_course(course_id)
        self.course_id = lesson.course_id
        self.difficulty = lesson.difficulty
        self.lesson = lesson
        self.state = State.READY

    @property
    def next_pose_id(self) -> str:
        """The pose after this one, or "" at the end of the class.

        Sent to the page for one reason: it fetches that pose's transition
        drawings during the current hold, which is forty seconds of warning
        for a file it would otherwise ask for at the instant it is needed.
        """
        following = self.step_index + 1
        if self.lesson is None or following >= len(self.lesson.steps):
            return ""
        return self.lesson.steps[following].pose_id

    @property
    def total_poses(self) -> int:
        return len(self.lesson.steps) if self.lesson else 0

    @property
    def step(self):
        if self.lesson is None or self.step_index >= len(self.lesson.steps):
            return None
        return self.lesson.steps[self.step_index]

    @property
    def pose(self):
        step = self.step
        return step.pose if step is not None else get_pose("mountain")

    def start(self, now: float) -> None:
        if not self.startable:
            raise ValueError("choose a yoga course first")
        self.lesson = get_lesson(self.difficulty, self.course_id or None)
        self.state = State.PLAYING
        self.score = 0
        self.duration = self.lesson.total_seconds
        self.time_left = self.duration
        self.step_index = 0
        self.results.clear()
        self.events.clear()
        self._last_tick = now
        self._last_result = None
        self._previous_pose_id = "mountain"
        # The calibration is deliberately *not* reset: it was gathered while
        # the player stood on the ready screen, which is the whole reason that
        # screen waits for a player at all, and throwing it away here would
        # mean the first two poses of every lesson are scored against the
        # coach's legs rather than the player's.
        self.feedback.reset()
        self._enter(Phase.COUNTDOWN, now, COUNTDOWN_SECONDS)
        self.events.append({"name": "yoga-start", "difficulty": self.difficulty,
                            "course": self.course_id})

    def pause(self, now: float) -> bool:
        if self.state is not State.PLAYING:
            return False
        self.state = State.PAUSED
        self._paused_at = now
        return True

    def resume(self, now: float) -> bool:
        if self.state is not State.PAUSED:
            return False
        # Every deadline in this session is wall-clock, so a pause has to be
        # added back to all of them or the lesson resumes with the current pose
        # already expired.
        paused = max(0.0, now - self._paused_at)
        self.phase_until += paused
        self.phase_started += paused
        self.state = State.PLAYING
        self._last_tick = now
        return True

    def finish(self, now: float) -> None:
        if self.state is State.OVER:
            return
        # A pose in progress when the clock runs out still counts for what was
        # earned. Ending mid-hold and recording nothing would mean the final
        # pose of every completed lesson was silently free.
        if self.state is State.PLAYING and self.phase is Phase.HOLDING:
            self._score_pose(now)
        self.state = State.OVER
        self.score = self.overall_score
        if self.score > self.best:
            self.best = self.score
        self.events.append({"name": "yoga-over", "score": self.score})

    # ── the class ────────────────────────────────────────────────────

    def _enter(self, phase: Phase, now: float, seconds: float) -> None:
        self.phase = phase
        self.phase_started = now
        self.phase_until = now + seconds

    def _begin_step(self, now: float) -> None:
        step = self.step
        if step is None:
            self.finish(now)
            return
        self.hold_left = step.hold_s
        self._accuracy = 0.0
        self._assessment = Assessment()
        self._holding = False
        self._reached = False
        self._stability = Stability()
        self._quality_sum = 0.0
        self._quality_frames = 0
        self._held_s = 0.0
        self._praise = self.feedback.praise()
        self.feedback.reset()
        self._enter(Phase.PREVIEW, now, self.lesson.preview_s)
        self.events.append({
            "name": "yoga-pose", "pose": step.pose_id,
            "title": step.pose.name, "index": self.step_index + 1,
            "total": self.total_poses, "hold": step.hold_s,
            "segment": step.segment})

    #: Words a second, and the breath either side of a spoken line.
    SPEECH_RATE = 2.6
    SPEECH_TAIL = 0.7

    def _say(self, text: str, now: float) -> None:
        """Say one line, and remember roughly how long it will take.

        Non-blocking on purpose: this is called from the pose loop, and a
        class whose animation stops while the coach talks is worse than one
        with no voice at all.
        """
        text = (text or "").strip()
        if not text:
            return
        words = max(1, len(text.split()))
        self._speaking_until = now + min(
            8.0, words / self.SPEECH_RATE + self.SPEECH_TAIL)
        if self.speak is None:
            return
        try:
            self.speak(text)
        except Exception:
            log.warning("the coach could not speak", exc_info=True)

    def _score_pose(self, now: float) -> None:
        step = self.step
        if step is None:
            return
        if not step.scored:
            # Nothing to score, and nothing to put on the card. The pose still
            # ends and the class still moves on; it simply leaves no mark in
            # the results, so the overall score stays an average of the poses
            # that were genuinely measured.
            self._previous_pose_id = step.pose_id
            self.events.append({
                "name": "yoga-pose-complete", "pose": step.pose_id,
                "title": step.pose.name, "guided": True,
                "complete": self._held_s >= step.hold_s - 0.05})
            return
        quality = (self._quality_sum / self._quality_frames
                   if self._quality_frames else 0.0)
        coverage = (self._held_s / step.hold_s) if step.hold_s > 0 else 1.0
        stability = self._stability.value() if self._reached else 0.0
        score = pose_score(quality, coverage, stability)
        result = PoseResult(
            pose_id=step.pose_id, name=step.pose.name, side=step.pose.side,
            segment=step.segment, score=score, accuracy=quality,
            held_s=min(self._held_s, step.hold_s), required_s=step.hold_s,
            stability=stability)
        self.results.append(result)
        self._last_result = result
        self._result_until = now + RESULT_SECONDS
        self.score = self.overall_score
        self._previous_pose_id = step.pose_id
        self.events.append({
            "name": "yoga-pose-complete", "pose": step.pose_id,
            "title": step.pose.name, "score": score, "band": band(score),
            "accuracy": round(100 * quality),
            "complete": self._held_s >= step.hold_s - 0.05})

    def _advance(self, now: float) -> None:
        self._score_pose(now)
        self.step_index += 1
        if self.step_index >= self.total_poses:
            self.finish(now)
            return
        self._begin_step(now)

    def tick_pose(self, now: float, person, pose_timestamp: float) -> None:
        """Consume one shared pose frame. Runs on the pose thread; never blocks.

        `pose_timestamp` is capture time and is deliberately not used for the
        clocks: a yoga hold is a promise to the player about wall-clock seconds,
        and a pose pipeline that drops to twenty frames a second must not turn a
        fifteen-second Tree Pose into a twenty-second one. It stays in the
        signature because it is what a future stability measure over raw joint
        motion would have to differentiate against.
        """
        joints = player_joints(person, self._confidence)
        self._metrics = measure(joints)

        if self.state is State.READY:
            # Standing in front of the camera waiting to begin is the best
            # calibration sample this game will ever get: the player is upright,
            # still, and being asked to be.
            #
            # The same frame answers the other question the ready screen has to
            # ask, and it is a question only this game asks: `PoseService`
            # reports a player ready once both shoulders are visible, which is
            # all Fruit Ninja and Boxing need, and yoga needs ankles. Somebody
            # standing at Fruit Ninja distance is "ready" and has no legs, so
            # the framing advice is worked out here against Mountain Pose
            # rather than by changing what readiness means for every game.
            self._assessment = assess(POSES["mountain"], self._metrics,
                                      tolerance_scale=1.35, check_mirror=False)
            self._calibrate()
            return
        if self.state is not State.PLAYING:
            return

        dt = 0.0 if self._last_tick < 0 else max(0.0, now - self._last_tick)
        # The same clamp `Session.tick` uses, for the same reason: a thread that
        # was starved for a second must not take a second off the lesson in one
        # step.
        dt = min(dt, 0.25)
        self._last_tick = now
        self.time_left = max(0.0, self.time_left - dt)

        if self.phase is Phase.COUNTDOWN:
            self._calibrate()
            if now >= self.phase_until:
                self._begin_step(now)
        elif self.phase is Phase.PREVIEW:
            # Watched, not measured. The player is looking at the pose they are
            # about to be asked for, and the pose stream is still read so the
            # tracking numbers do not arrive cold at the hold.
            self._evaluate(now, dt, counting=False)
            if now >= self.phase_until:
                self._enter(Phase.TRANSITION, now, self.lesson.movement_s)
                self._say(self.pose.instruction, now)
                self.events.append({"name": "yoga-move", "pose": self.pose.id})
        elif self.phase is Phase.TRANSITION:
            self._evaluate(now, dt, counting=False)
            if now >= self.phase_until:
                step = self.step
                deadline = step.hold_s * DEADLINE_FACTOR + DEADLINE_ALLOWANCE
                self._enter(Phase.HOLDING, now, deadline)
        elif self.phase is Phase.HOLDING:
            self._evaluate(now, dt, counting=True)
            if self.hold_left <= 0.0 or now >= self.phase_until:
                self._advance(now)

        if self.time_left <= 0.0 and self.state is State.PLAYING:
            self.finish(now)

    @property
    def _confidence(self) -> float:
        # Lower than the wrist threshold Fruit Ninja steers a blade with. A
        # yoga pose needs ankles and knees, which sit at the bottom of the
        # frame and behind trousers, and refusing them at 0.45 would mean
        # refusing to score the half of the body the pose is about. A joint
        # this game is unsure of costs accuracy through `MIN_COVERAGE` rather
        # than being quietly guessed.
        return 0.30

    def _calibrate(self) -> bool:
        if not self._metrics:
            return False
        standing = POSES["mountain"]
        # Uncalibrated on purpose: the point is to find out how this body is
        # built, and asking the question through the answer would only ever
        # confirm whatever it already believed.
        check = assess(standing, self._metrics, tolerance_scale=1.35,
                       check_mirror=False)
        if not check.tracked or check.accuracy < CALIBRATION_ACCURACY:
            return False
        return self.calibration.add(self._metrics, standing.targets)

    def _evaluate(self, now: float, dt: float, *, counting: bool) -> None:
        pose = self.pose
        step = self.step
        if step is not None and step.pose_id in ("mountain", "mountain_breath"):
            self._calibrate()

        if step is not None and not step.scored:
            self._follow(now, dt, counting=counting)
            return

        self._assessment = assess(
            pose, self._metrics,
            tolerance_scale=self.lesson.tolerance_scale,
            calibration=self.calibration)

        if not self._assessment.tracked:
            # Losing the player is not the same as the player being wrong. The
            # accuracy decays towards zero rather than dropping to it, so that
            # walking behind a chair for half a second does not read as having
            # abandoned the pose, and no frame of it is folded into the score.
            self._accuracy *= 0.85
            self._holding = False
            self.feedback.update(now, self._assessment.advice, urgent=True)
            return

        target = self._assessment.accuracy
        self._accuracy += ACCURACY_SMOOTHING * (target - self._accuracy)

        threshold = self.lesson.hold_threshold
        if self._holding:
            threshold -= HOLD_HYSTERESIS
        close = self._accuracy >= threshold and not self._assessment.wrong_side

        if counting:
            self._quality_sum += self._accuracy
            self._quality_frames += 1
            self._stability.add(self._accuracy, close)
            if close:
                if not self._reached:
                    self._reached = True
                    self.events.append({"name": "yoga-in-pose",
                                        "pose": step.pose_id if step else ""})
                spent = min(dt, self.hold_left)
                self.hold_left = max(0.0, self.hold_left - spent)
                self._held_s += spent
        self._holding = close

        if close and self._accuracy >= PRAISE_ACCURACY:
            self.feedback.update(now, self._praise)
            return
        message = correction(pose, self._metrics, self._assessment,
                             self.calibration)
        self.feedback.update(now, message,
                             urgent=self._assessment.wrong_side)

    def _follow(self, now: float, dt: float, *, counting: bool) -> None:
        """A guided pose: demonstrated, cued, and not measured.

        Fifty-five of the ninety-one poses are on the floor, seated, prone or
        supine, where the seventeen numbers `rig.measure` produces stop meaning
        anything -- plus Chair, which is switched off by choice so the coach can
        show a real squat instead of a scoreable fake one.

        The class still has to *run* through them, and the hold has to end. So
        the clock is the only thing counted: hold time is spent by the second
        rather than earned by being close to a target. What must not happen is
        the pose being marked anyway -- an empty bone table scores everybody
        perfect, and a class that hands out a hundred per cent for lying on the
        floor is worse than one that says nothing.
        """
        self._assessment = None
        self._accuracy = 0.0
        self._holding = counting
        if counting:
            if not self._reached:
                self._reached = True
                step = self.step
                self.events.append({"name": "yoga-in-pose",
                                    "pose": step.pose_id if step else ""})
            spent = min(dt, self.hold_left)
            self.hold_left = max(0.0, self.hold_left - spent)
            self._held_s += spent
        # The cue, held on screen for as long as the pose is. There is no
        # correction to give: nothing is being measured to correct against.
        self.feedback.update(now, self.pose.cue)

    # ── results ──────────────────────────────────────────────────────

    @property
    def overall_score(self) -> int:
        if not self.results:
            return 0
        return int(round(sum(r.score for r in self.results) / len(self.results)))

    @property
    def scored_poses(self) -> int:
        """How many of this class the player is actually marked on."""
        return sum(1 for step in self.lesson.steps if step.scored)

    @property
    def average_accuracy(self) -> int:
        if not self.results:
            return 0
        return int(round(100 * sum(r.accuracy for r in self.results)
                         / len(self.results)))

    @property
    def total_hold_s(self) -> float:
        return sum(r.held_s for r in self.results)

    def summary(self) -> dict:
        """The Yoga Complete panel. Section: end-of-session result."""
        completed = len(self.results)
        best = max(self.results, key=lambda r: r.score, default=None)
        worst = min(self.results, key=lambda r: r.score, default=None)
        overall = self.overall_score
        return {
            "overall": overall,
            "band": band(overall),
            "average_accuracy": self.average_accuracy,
            "completed": completed,
            # The poses that were *marked*. A class of thirty with seventeen
            # guided poses in it is not seventeen poses the player failed, and
            # a summary that said "13 of 30" would read exactly like one.
            "total": self.scored_poses,
            "poses_shown": self.total_poses,
            "hold_seconds": round(self.total_hold_s),
            "required_seconds": round(sum(r.required_s for r in self.results)),
            "best": best.as_dict() if best else None,
            "lowest": worst.as_dict() if worst else None,
            # Every pose, for the scrolling breakdown. Small: forty entries of
            # six short fields is a few kilobytes, and it is sent once.
            "poses": [r.as_dict() for r in self.results],
        }

    # ── what the page reads ──────────────────────────────────────────

    def _framing(self) -> str:
        """What the ready screen says about where the player is standing."""
        if self.state is not State.READY:
            return ""
        if self._assessment.tracked:
            return ""
        return self._assessment.advice or "Step into view of the camera"

    def _countdown(self, now: float) -> str:
        if self.state is not State.PLAYING or self.phase is not Phase.COUNTDOWN:
            return ""
        remaining = self.phase_until - now
        if remaining > 3.0:
            return "READY"
        if remaining > 0:
            return str(max(1, math.ceil(remaining)))
        return "BEGIN"

    def _rig(self, now: float) -> dict:
        """The coach's bones now, and the bones being tweened from.

        The page does the interpolation because it draws at sixty frames a
        second and this runs at thirty; sending joint *positions* instead and
        letting the page blend those would make the coach's limbs stretch and
        shrink through every transition, which is exactly the thing an angle
        rig exists to avoid.
        """
        pose = self.pose
        previous = POSES.get(self._previous_pose_id, pose)
        blend = 1.0
        if self.state is State.PLAYING and self.phase is Phase.TRANSITION:
            span = max(0.001, self.phase_until - self.phase_started)
            blend = min(1.0, max(0.0, (now - self.phase_started) / span))
        return {
            "pose": pose.id,
            "from": previous.id,
            "blend": round(blend, 3),
            "bones": {name: round(value, 1)
                      for name, value in pose.table.items()},
            "from_bones": {name: round(value, 1)
                           for name, value in previous.table.items()},
            "scales": pose.scales,
            "from_scales": previous.scales,
        }

    def snapshot(self, now: float) -> dict:
        step = self.step
        pose = self.pose
        data = {
            "kind": "yoga",
            "state": self.state.value,
            "difficulty": self.difficulty,
            "lesson": self.lesson.name if self.lesson else "",
            "course": self.lesson.course_id if self.lesson else "",
            "scored_poses": self.scored_poses if self.lesson else 0,
            "score": self.score,
            "best": self.best,
            "time_left": round(self.time_left, 1),
            "duration": round(self.duration, 1),
            "phase": self.phase.value if self.state is State.PLAYING else "",
            "countdown": self._countdown(now),
            "accuracy": round(100 * self._accuracy),
            "tracked": self._assessment.tracked if self._assessment else True,
            "holding": self._holding,
            "next_pose": self.next_pose_id,
            # For the page's music ducking, and for nothing else.
            "speaking": now < self._speaking_until,
            "wrong_side": bool(self._assessment and self._assessment.wrong_side),
            "feedback": self.feedback.message,
            "calibration": self.calibration.as_dict(),
            "framing": self._framing(),
            "rig": self._rig(now),
        }
        if step is not None:
            data["pose"] = {
                "id": pose.id,
                "name": pose.name,
                "sanskrit": pose.sanskrit,
                "instruction": pose.instruction,
                "cue": pose.cue,
                "segment": step.segment,
                "side": pose.side,
                # What the screen shows instead of an accuracy dial. The page
                # has no other way to know: a guided pose looks exactly like a
                # scored one that nobody is managing to do.
                "scored": step.scored,
                "index": self.step_index + 1,
                "total": self.total_poses,
                "hold": round(step.hold_s),
                "hold_left": round(self.hold_left, 1),
                "hold_progress": round(
                    1.0 - self.hold_left / step.hold_s, 3) if step.hold_s else 1.0,
            }
        if self._last_result is not None and now < self._result_until:
            data["last"] = self._last_result.as_dict()
        if self.state is State.OVER:
            data["summary"] = self.summary()
        return data

    def take_events(self) -> list[dict]:
        events, self.events = self.events[-40:], []
        return events
