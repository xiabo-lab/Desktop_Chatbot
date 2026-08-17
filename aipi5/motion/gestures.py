"""Whole-body gestures derived from the existing Hailo pose skeleton.

Fruit Ninja's start controls used to run a second, browser-side MediaPipe
model to distinguish an open palm from a fist. Closed fists are easily hidden
from a front-facing camera, so the reliable signal is now the six joints the
pose model already tracks well: shoulders, elbows, and wrists.

The detector is independent of screen orientation. It projects each wrist
onto the line from the labelled left shoulder to the labelled right shoulder,
so it works before or after mirroring and does not assume which anatomical
side appears at the smaller x coordinate.
"""

from __future__ import annotations

import math


JOINTS = (
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
)


def _distance(first, second) -> float:
    return math.hypot(first.x - second.x, first.y - second.y)


def arms_crossed(person, threshold: float = 0.35) -> bool:
    """True when both forearms make a deliberate X across the upper chest.

    Wrist order alone is insufficient: hands briefly pass each other during
    normal play. The wrists must cross the torso centre, remain near the
    opposite shoulders, sit above their elbows, and be at roughly the same
    height. All distances scale with shoulder width, so player size and camera
    distance do not change the gesture.
    """
    if person is None:
        return False
    points = {name: person.point(name) for name in JOINTS}
    if any(point is None or point.confidence < threshold
           for point in points.values()):
        return False

    left_shoulder = points["left_shoulder"]
    right_shoulder = points["right_shoulder"]
    left_elbow = points["left_elbow"]
    right_elbow = points["right_elbow"]
    left_wrist = points["left_wrist"]
    right_wrist = points["right_wrist"]

    axis_x = right_shoulder.x - left_shoulder.x
    axis_y = right_shoulder.y - left_shoulder.y
    span = math.hypot(axis_x, axis_y)
    if span < 0.06:
        return False

    def projection(point) -> float:
        return (((point.x - left_shoulder.x) * axis_x
                 + (point.y - left_shoulder.y) * axis_y) / (span * span))

    # Each wrist has crossed clearly into the opposite half of the torso.
    if projection(left_wrist) < 0.58 or projection(right_wrist) > 0.42:
        return False

    # Elbows stay outside while the forearms cross inward. This rejects hands
    # simply clasped in the middle of the chest.
    if projection(left_elbow) > 0.72 or projection(right_elbow) < 0.28:
        return False

    # A real X is across the chest, with each hand approaching the opposite
    # shoulder. Low crossed hands at the waist are common between swings and
    # must not start a new round.
    if _distance(left_wrist, right_shoulder) > 1.30 * span:
        return False
    if _distance(right_wrist, left_shoulder) > 1.30 * span:
        return False
    if _distance(left_wrist, right_shoulder) + 0.08 * span >= \
            _distance(left_wrist, left_shoulder):
        return False
    if _distance(right_wrist, left_shoulder) + 0.08 * span >= \
            _distance(right_wrist, right_shoulder):
        return False

    # Wrists should rise from the elbows and form one recognisable crossing,
    # not one high hand and one low hand caught in unrelated movement.
    if left_wrist.y > left_elbow.y + 0.25 * span:
        return False
    if right_wrist.y > right_elbow.y + 0.25 * span:
        return False
    if abs(left_wrist.y - right_wrist.y) > 0.75 * span:
        return False
    return True


class CrossedArmsGesture:
    """Debounce an X pose and require a fresh gesture after each round.

    A continuous hold is intentional; one crossing pose frame is ordinary
    gameplay. Small keypoint dropouts receive a short grace window. Entering
    Game Over while already crossed does not immediately restart: the player
    must lower their arms, then make a new X.
    """

    def __init__(self, *, hold_s: float = 0.65, release_s: float = 0.25,
                 lost_grace_s: float = 0.16, confidence: float = 0.35):
        self.hold_s = hold_s
        self.release_s = release_s
        self.lost_grace_s = lost_grace_s
        self.confidence = confidence
        self.reset()

    def reset(self) -> None:
        self.phase = ""
        self.crossed = False
        self.armed = True
        self._crossed_since = 0.0
        self._last_crossed = 0.0
        self._release_since = 0.0

    def update(self, person, phase, now: float) -> bool:
        phase = str(getattr(phase, "value", phase))
        crossed = arms_crossed(person, self.confidence)
        actionable = phase in ("ready", "over")

        if phase != self.phase:
            first = not self.phase
            self.phase = phase
            self._crossed_since = 0.0
            self._last_crossed = now if crossed else 0.0
            self._release_since = now if actionable and not crossed else 0.0
            # The first Start screen may accept the pose immediately. A later
            # state transition requires release, preventing an X held at zero
            # seconds from instantly pressing Play Again.
            self.armed = bool(actionable and first)

        if not actionable:
            self.crossed = crossed
            self._crossed_since = 0.0
            return False

        if crossed:
            self.crossed = True
            self._last_crossed = now
            self._release_since = 0.0
            if not self.armed:
                return False
            if not self._crossed_since:
                self._crossed_since = now
                return False
            if now - self._crossed_since >= self.hold_s:
                self.armed = False
                return True
            return False

        # One missed joint must not erase a nearly complete hold.
        if (self._crossed_since and self._last_crossed
                and now - self._last_crossed <= self.lost_grace_s):
            self.crossed = True
            return False

        self.crossed = False
        self._crossed_since = 0.0
        if not self.armed:
            if not self._release_since:
                self._release_since = now
            elif now - self._release_since >= self.release_s:
                self.armed = True
        return False

    def describe(self, now: float) -> dict:
        progress = 0.0
        if self.armed and self.crossed and self._crossed_since:
            progress = min(1.0, (now - self._crossed_since) / self.hold_s)
        return {
            "name": "arms-crossed-x",
            "armed": self.armed,
            "crossed": self.crossed,
            "progress": round(progress, 2),
            "hold_ms": round(self.hold_s * 1000),
        }
