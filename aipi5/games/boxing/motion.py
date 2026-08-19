"""Upper-body pose analysis for boxing.

This is the intermediate layer between AIPI5's existing pose tracker and game
rules.  It is intentionally pure Python and accepts ordinary ``PersonPose``
objects, so recorded or synthetic poses can test it without a camera.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math

from aipi5.games.boxing.config import MOTION, MotionTuning
from aipi5.motion import geometry

#: How far back a wrist is compared against, in seconds.
#:
#: **Every punch threshold in `config.MotionTuning` is a distance, and a
#: distance only means something over a stated time.** They were measured
#: against the previous *frame* at a pose rate of about thirty, so
#: `punch_travel` of 0.065 shoulder widths was implicitly "in 33 ms". When the
#: pose pipeline was sped up to about forty-five frames a second the frames got
#: closer together, the distance between two of them shrank by a third, and the
#: same punch stopped clearing the same threshold — a latency improvement
#: quietly making the game harder to play, which is the worst possible shape
#: for a regression because it looks like the tracking got worse.
#:
#: So the comparison is against where the wrist was 33 ms ago, interpolated
#: between the two samples that bracket it, whatever the frame rate is. The
#: constants keep the meaning they were measured with, and a faster pipeline
#: now buys what it should: the *decision* arrives sooner, on fresher data.
PUNCH_WINDOW_S = 1 / 30

#: How long the neutral stance takes to follow a player who has drifted, as a
#: time constant rather than a per-frame weight — same reason, same fix. 0.94 s
#: is what the old 3.5%-per-frame blend came to at thirty frames a second.
NEUTRAL_TAU_S = 0.94


@dataclass(frozen=True)
class BoxingAction:
    name: str
    side: str = ""
    target: str = ""
    confidence: float = 0.0
    speed: float = 0.0
    acceleration: float = 0.0
    trajectory: tuple[float, float, float, float] = ()

    def as_dict(self) -> dict:
        data = {
            "name": self.name, "side": self.side, "target": self.target,
            "confidence": round(self.confidence, 2),
            "speed": round(self.speed, 2),
            "acceleration": round(self.acceleration, 2),
        }
        if self.trajectory:
            data["trajectory"] = [round(value, 4) for value in self.trajectory]
        return data


@dataclass(frozen=True)
class _WristSample:
    """Where one wrist was, and how extended the arm was, at one instant."""

    at: float
    x: float
    y: float
    extension: float


def _interpolate(track, wanted: float) -> _WristSample:
    """The wrist as it was at `wanted`, between the two samples around it.

    Interpolated rather than "the nearest sample", because nearest quantises
    the comparison window to the frame interval — which is the very thing this
    is trying to stop mattering. Clamped to the oldest sample held, so the
    first frames after a hand appears compare against the little history there
    is instead of inventing some.
    """
    if track[0].at >= wanted:
        return track[0]
    for older, newer in zip(track, list(track)[1:]):
        if newer.at >= wanted:
            gap = newer.at - older.at
            if gap <= 1e-9:
                return newer
            t = (wanted - older.at) / gap
            return _WristSample(
                at=wanted,
                x=older.x + t * (newer.x - older.x),
                y=older.y + t * (newer.y - older.y),
                extension=older.extension + t * (newer.extension - older.extension),
            )
    return track[-1]


@dataclass
class MotionResult:
    actions: list[BoxingAction] = field(default_factory=list)
    guard: str = "none"
    posture: dict = field(default_factory=dict)
    hands: dict = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return {action.name for action in self.actions}

    def first(self, *names: str) -> BoxingAction | None:
        wanted = set(names)
        return next((action for action in self.actions
                     if action.name in wanted), None)

    def as_dict(self) -> dict:
        return {
            "actions": [action.as_dict() for action in self.actions],
            "gesture": self.actions[0].name if self.actions else self.guard,
            "confidence": round(max((action.confidence for action in self.actions),
                                    default=1.0 if self.guard != "none" else 0.0), 2),
            "guard": self.guard,
            "posture": self.posture,
            "hands": self.hands,
        }


class BoxingMotionAnalyzer:
    """Classify punches and defensive movement from upper-body landmarks."""

    def __init__(self, tuning: MotionTuning = MOTION):
        self.tuning = tuning
        self.reset()

    def reset(self) -> None:
        self._last_time: float | None = None
        #: A third of a second of wrist history per side, so the comparison
        #: window below is a duration and not a frame count.
        self._track: dict[str, deque] = {"left": deque(), "right": deque()}
        self._last_speed = {"left": 0.0, "right": 0.0}
        self._cooldown = {"left": -1e9, "right": -1e9,
                          "dodge": -1e9, "duck": -1e9,
                          "lean": -1e9, "parry": -1e9}
        self._neutral: dict[str, float] = {}
        self._last_result = MotionResult()

    @staticmethod
    def _distance(first, second) -> float:
        return math.hypot(first.x - second.x, first.y - second.y)

    def _points(self, person) -> dict | None:
        if person is None:
            return None
        names = ("nose", "left_shoulder", "right_shoulder",
                 "left_elbow", "right_elbow", "left_wrist", "right_wrist")
        points = {name: person.point(name) for name in names}
        required = ("nose", "left_shoulder", "right_shoulder")
        if any(points[name] is None or
               points[name].confidence < self.tuning.confidence
               for name in required):
            return None
        return points

    def calibrate(self, person, dt: float = PUNCH_WINDOW_S) -> bool:
        """Blend a neutral stance from one credible upper-body pose.

        `dt` is how long since the last blend. It defaults to one frame at the
        rate this was tuned at, so an outside caller behaves exactly as before.
        """
        person = person.shaped() if person is not None else None
        points = self._points(person)
        if points is None:
            return False
        left, right, nose = (points["left_shoulder"],
                             points["right_shoulder"], points["nose"])
        span = self._distance(left, right)
        if span < 0.055:
            return False
        sample = {
            "x": (left.x + right.x) / 2,
            "y": (left.y + right.y) / 2,
            "nose_y": nose.y,
            "span": span,
        }
        if not self._neutral:
            self._neutral = sample
        else:
            # A time constant, not a per-frame weight. The old flat 0.035 meant
            # the neutral chased the player 1.7x faster once the pose rate went
            # up, which eats exactly the offset a dodge is measured against.
            alpha = 1.0 - math.exp(-max(0.0, dt) / NEUTRAL_TAU_S)
            for name, value in sample.items():
                self._neutral[name] += alpha * (value - self._neutral[name])
        return True

    def update(self, person, timestamp: float, hands=None,
               incoming: dict | None = None) -> MotionResult:
        # Everything below is a shape — angles, and distances divided by the
        # shoulder span — so it is measured in the reference camera aspect
        # rather than the camera's own. `hands` comes from the shared filter in
        # camera space and gets the same factor applied by hand, because a
        # `Hand` is not a `PersonPose` and must not start pretending to be one:
        # Fruit Ninja steers a blade with it and wants camera space exactly.
        shape_scale = (geometry.shape_scale(person.aspect)
                       if person is not None else 1.0)
        person = person.shaped() if person is not None else None
        points = self._points(person)
        if points is None:
            self._last_time = timestamp
            self._last_result = MotionResult(posture={"tracked": False})
            return self._last_result

        left_shoulder, right_shoulder = (points["left_shoulder"],
                                         points["right_shoulder"])
        span = self._distance(left_shoulder, right_shoulder)
        if span < 0.055:
            return MotionResult(posture={"tracked": False})
        if not self._neutral:
            self.calibrate(person)

        dt = 0.0 if self._last_time is None else timestamp - self._last_time
        dt = max(1 / 240, min(0.20, dt)) if dt > 0 else 0.0
        self._last_time = timestamp

        centre_x = (left_shoulder.x + right_shoulder.x) / 2
        centre_y = (left_shoulder.y + right_shoulder.y) / 2
        nose = points["nose"]
        dx = (centre_x - self._neutral.get("x", centre_x)) / span
        dy = (centre_y - self._neutral.get("y", centre_y)) / span
        nose_dy = (nose.y - self._neutral.get("nose_y", nose.y)) / span
        scale_change = span / max(0.001, self._neutral.get("span", span)) - 1.0
        torso_angle = math.degrees(math.atan2(
            right_shoulder.y - left_shoulder.y,
            right_shoulder.x - left_shoulder.x))
        lane = "left" if dx <= -0.16 else "right" if dx >= 0.16 else "middle"

        result = MotionResult(posture={
            "tracked": True,
            "centre": [round(centre_x, 4), round(centre_y, 4)],
            "head": [round(nose.x, 4), round(nose.y, 4)],
            "shoulder_width": round(span, 4),
            "torso_angle": round(torso_angle, 1),
            "offset_x": round(dx, 2),
            "offset_y": round(dy, 2),
            "lane": lane,
            "scale_change": round(scale_change, 2),
        })

        # A dodge is torso movement, not merely turning the head.  Each motion
        # has a short latch so a held pose reports one action rather than one
        # per camera frame.
        if timestamp - self._cooldown["dodge"] >= 0.45:
            if dx <= -self.tuning.dodge_distance:
                result.actions.append(BoxingAction("dodge_left", confidence=min(1.0, -dx)))
                self._cooldown["dodge"] = timestamp
            elif dx >= self.tuning.dodge_distance:
                result.actions.append(BoxingAction("dodge_right", confidence=min(1.0, dx)))
                self._cooldown["dodge"] = timestamp
        if (nose_dy >= self.tuning.duck_distance and dy >= self.tuning.duck_distance * 0.55
                and timestamp - self._cooldown["duck"] >= 0.55):
            result.actions.append(BoxingAction("duck", confidence=min(1.0, nose_dy)))
            self._cooldown["duck"] = timestamp
        if (scale_change <= -self.tuning.lean_scale_drop
                and nose_dy < self.tuning.duck_distance * 0.65
                and timestamp - self._cooldown["lean"] >= 0.65):
            result.actions.append(BoxingAction("lean_back", confidence=min(1.0, -scale_change * 3)))
            self._cooldown["lean"] = timestamp

        near_face: dict[str, bool] = {}
        for side in ("left", "right"):
            wrist = points[f"{side}_wrist"]
            elbow = points[f"{side}_elbow"]
            shoulder = points[f"{side}_shoulder"]
            if (wrist is None or elbow is None or
                    min(wrist.confidence, elbow.confidence) < self.tuning.confidence):
                continue

            # Prefer the shared filtered wrists used by Fruit Ninja.  The raw
            # landmark is a fallback for synthetic tests and debug playback.
            filtered = hands.get(f"{side}_wrist") if isinstance(hands, dict) else None
            live = filtered is not None and filtered.live
            x = filtered.x * shape_scale if live else wrist.x
            y = filtered.y if live else wrist.y
            direct = math.hypot(x - shoulder.x, y - shoulder.y)
            limb = self._distance(shoulder, elbow) + math.hypot(x - elbow.x, y - elbow.y)
            extension = direct / max(0.001, limb)

            # Against where this wrist was `PUNCH_WINDOW_S` ago, not against
            # the previous frame — see that constant for why the difference is
            # the whole of this. History older than twice the window is of no
            # further use and is dropped rather than allowed to grow.
            track = self._track[side]
            track.append(_WristSample(timestamp, x, y, extension))
            while len(track) > 2 and timestamp - track[1].at >= 2 * PUNCH_WINDOW_S:
                track.popleft()
            before = _interpolate(track, timestamp - PUNCH_WINDOW_S)
            window = max(1e-3, timestamp - before.at)

            travel = math.hypot(x - before.x, y - before.y)
            speed = travel / (window * span)
            acceleration = (speed - self._last_speed[side]) / window
            extension_gain = extension - before.extension
            # The *current* shoulder against the *old* wrist, deliberately: a
            # player stepping forward must not read as an arm extending.
            radial_before = math.hypot(before.x - shoulder.x,
                                       before.y - shoulder.y)
            radial_gain = (direct - radial_before) / span
            confidence = min(wrist.confidence, elbow.confidence,
                             shoulder.confidence)
            result.hands[side] = {
                "x": round(x, 4), "y": round(y, 4),
                "px": round(before.x, 4), "py": round(before.y, 4),
                "speed": round(speed, 2),
                "acceleration": round(acceleration, 2),
                "extension": round(extension, 2),
            }

            face_distance = math.hypot(x - nose.x, y - nose.y) / span
            near_face[side] = face_distance <= self.tuning.guard_radius

            enough_motion = (speed >= self.tuning.punch_speed and
                             travel / span >= self.tuning.punch_travel)
            extending = (radial_gain >= 0.025 or
                         extension_gain >= self.tuning.punch_extension_gain)
            straightening = (extension >= self.tuning.punch_extension and
                              extension_gain >= self.tuning.punch_extension_gain and
                              speed >= self.tuning.punch_speed * 0.55)
            if ((enough_motion and extending) or straightening) and \
                    timestamp - self._cooldown[side] >= self.tuning.punch_cooldown_s:
                horizontal = abs(x - before.x)
                vertical = abs(y - before.y)
                hook = (horizontal > self.tuning.hook_horizontal_ratio *
                        max(0.008, vertical) and extension < 0.90)
                target = "head" if y <= centre_y + 0.28 * span else "body"
                name = f"{side}_{'hook' if hook else 'punch'}"
                result.actions.append(BoxingAction(
                    name, side=side, target=target,
                    confidence=min(1.0, confidence * min(1.35, speed / self.tuning.punch_speed)),
                    speed=speed, acceleration=acceleration,
                    trajectory=(before.x, before.y, x, y)))
                self._cooldown[side] = timestamp

            self._last_speed[side] = speed

        if near_face.get("left") and near_face.get("right"):
            result.guard = "two_hand_guard"
        elif near_face.get("left"):
            result.guard = "left_block"
        elif near_face.get("right"):
            result.guard = "right_block"

        # A parry is intentionally movement-gated.  A stationary hand near the
        # face remains a block even inside the timing window.
        if incoming and 0.0 <= float(incoming.get("impact_in", 99)) <= \
                float(incoming.get("parry_window", self.tuning.parry_window_s)):
            moving = [(side, detail) for side, detail in result.hands.items()
                      if detail["speed"] >= self.tuning.parry_speed]
            if moving and timestamp - self._cooldown["parry"] >= 0.35:
                target_y = nose.y if incoming.get("target") == "head" else centre_y + span
                side, detail = min(moving, key=lambda item:
                    math.hypot(item[1]["x"] - centre_x,
                               item[1]["y"] - target_y))
                distance = math.hypot(detail["x"] - centre_x,
                                      detail["y"] - target_y) / span
                if distance <= self.tuning.parry_radius:
                    result.actions.insert(0, BoxingAction(
                        "parry", side=side, target=str(incoming.get("target", "head")),
                        confidence=min(1.0, detail["speed"] / 2.5),
                        speed=detail["speed"], acceleration=detail["acceleration"],
                        trajectory=(detail["px"], detail["py"],
                                    detail["x"], detail["y"])))
                    self._cooldown["parry"] = timestamp

        # Slowly follow ordinary stance drift, never a decisive dodge/duck.
        if abs(dx) < 0.10 and abs(nose_dy) < 0.10 and abs(scale_change) < 0.08:
            self.calibrate(person, dt)
        self._last_result = result
        return result

    def describe(self) -> dict:
        return self._last_result.as_dict()
