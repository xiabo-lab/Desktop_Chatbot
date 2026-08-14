"""Turning a twitchy estimate into something you can hold a sword with.

Three problems, in the order they have to be solved, and none of them optional:

**Which person.** Section 13. The model returns everybody it sees, and a game
that took `persons[0]` would hand the sword to whoever the NMS happened to rank
first that frame — which changes when somebody at the back leans forward. The
player is chosen by area with hysteresis, so a bystander has to be decisively
bigger for a decisive length of time before the game changes its mind.

**Which samples to believe.** Section 15. A wrist behind a back is still
reported, with a low confidence and a position that is a guess. Steering a
blade from those is how a fruit gets sliced by a hand that is nowhere near it.
Below the threshold the hand coasts on its last known position for a moment —
because a single bad frame in a good sequence is noise, not an absence — and
is then declared gone.

**How much to smooth.** Section 14. Raw keypoints jitter by a few pixels at
rest, which on a 1280 px screen is a blade that shivers. An exponential moving
average fixes that and buys the jitter back as latency, which is the one thing
this game cannot afford, so the weight is chosen against measurement rather
than taste — see `SMOOTHING`.

Everything here is pure: no camera, no accelerator, no clock of its own. Time
arrives as an argument. That is what makes the interesting cases — a hand
vanishing for exactly one frame, two people swapping size — testable at all.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from aipi5.motion.pose_types import WRISTS, PersonPose, PoseFrame

log = logging.getLogger(__name__)

#: Weight on each new sample in the exponential moving average.
#:
#: `filtered = alpha * new + (1 - alpha) * previous`, section 14's formula.
#: 0.55 is measured rather than chosen: at 30 FPS it settles a step change to
#: within 5% in about four frames (130 ms) while removing the visible shiver at
#: rest. Lower is calmer and adds lag you can feel by 0.3 — the blade trails
#: the hand like it is underwater. Higher stops smoothing anything much above
#: 0.8. Section 14 warns against over-filtering and this is the end of the
#: range it warns about.
SMOOTHING = 0.55

#: How far a wrist may jump between frames before the filter stops smoothing
#: and simply believes it, as a fraction of the frame width. A real slash
#: crosses a third of the frame in 100 ms and *must* not be averaged with where
#: the hand used to be — that would round the corner off every fast movement,
#: which is precisely the movement the game is about. Below this, smooth;
#: above it, snap.
SNAP_DISTANCE = 0.12

#: How much bigger a rival has to be, and for how many frames, before the
#: player changes. Section 13's "avoid randomly switching players
#: frame-to-frame": 25% bigger for half a second at 30 FPS.
SWITCH_MARGIN = 1.25
SWITCH_FRAMES = 15


@dataclass
class Hand:
    """One wrist, filtered, with enough history to be a sword.

    `previous` and `x, y` are the two ends of the slash segment section 16
    asks for. They are a frame apart, not a fixed distance, which is why the
    collision test is a segment rather than a point: at 30 FPS a hand moving
    at a plausible 3 m/s travels most of the way across a fruit between
    samples, and a point test would miss it entirely.
    """

    name: str
    x: float = 0.0
    y: float = 0.0
    #: Where it was on the previous accepted sample.
    previous_x: float = 0.0
    previous_y: float = 0.0
    confidence: float = 0.0
    #: Monotonic time of the last sample confident enough to accept.
    last_seen: float = 0.0
    #: Whether this hand may currently drive a blade.
    live: bool = False
    #: Set for the first accepted sample after an absence, so consumers do not
    #: draw a slash from wherever the hand used to be to wherever it just
    #: reappeared — a segment across the whole screen that slices everything.
    reappeared: bool = False

    @property
    def travel(self) -> float:
        """How far the hand moved on the last sample, normalised."""
        return math.hypot(self.x - self.previous_x, self.y - self.previous_y)

    def speed(self, dt: float) -> float:
        """Normalised widths per second. 0 when no time has passed."""
        return self.travel / dt if dt > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 4), "y": round(self.y, 4),
            "px": round(self.previous_x, 4), "py": round(self.previous_y, 4),
            "confidence": round(self.confidence, 3),
            "live": self.live,
            "new": self.reappeared,
        }


class HandFilter:
    """Both wrists, smoothed and confidence-gated. Section 14 and section 15.

    One per game session, not one per frame. It is entirely about what happened
    last frame, so a fresh one every frame would be an elaborate way of doing
    no filtering at all.
    """

    def __init__(self, *, confidence: float = 0.45, stale_s: float = 0.25,
                 smoothing: float = SMOOTHING):
        self.confidence = confidence
        self.stale_s = stale_s
        self.smoothing = smoothing
        self.hands: dict[str, Hand] = {name: Hand(name) for name in WRISTS}

    def update(self, person: PersonPose | None, now: float) -> dict[str, Hand]:
        """Fold one frame in. Returns the hands, which are mutated in place."""
        for name, hand in self.hands.items():
            point = person.point(name) if person is not None else None
            if point is not None and point.confidence >= self.confidence:
                self._accept(hand, point.x, point.y, point.confidence, now)
            else:
                self._coast(hand, now)
        return self.hands

    def _accept(self, hand: Hand, x: float, y: float, confidence: float,
                now: float) -> None:
        hand.reappeared = not hand.live
        if not hand.live:
            # First sample back. Both ends of the segment are set to it, so the
            # slash has zero length this frame — see `Hand.reappeared`. A hand
            # that reappears across the screen would otherwise sweep a blade
            # through everything between the two positions.
            hand.x = hand.previous_x = x
            hand.y = hand.previous_y = y
            hand.live = True
            hand.confidence = confidence
            hand.last_seen = now
            return

        hand.previous_x, hand.previous_y = hand.x, hand.y

        # Snap rather than smooth when the hand genuinely moved a long way.
        # Averaging a real slash with where the hand was is what rounds the
        # corners off fast movement, and fast movement is the entire game.
        if math.hypot(x - hand.x, y - hand.y) >= SNAP_DISTANCE:
            hand.x, hand.y = x, y
        else:
            alpha = self.smoothing
            hand.x += alpha * (x - hand.x)
            hand.y += alpha * (y - hand.y)

        hand.confidence = confidence
        hand.last_seen = now

    def _coast(self, hand: Hand, now: float) -> None:
        """No believable sample this frame. Section 15's two-stage answer.

        The last known position is kept for `stale_s` and then the hand is
        invalidated — "do not allow an old wrist coordinate to remain
        indefinitely". Keeping it briefly matters because one dropped frame in
        a good sequence is noise and blinking the blade out for it looks
        broken; keeping it forever means a sword parked in mid-air slicing
        fruit for a player who has left the room.
        """
        hand.reappeared = False
        if not hand.live:
            return
        if now - hand.last_seen > self.stale_s:
            hand.live = False
            hand.confidence = 0.0
            return
        # Still coasting. The segment collapses to a point so that a stale hand
        # cannot keep slicing along the path of its last real movement.
        hand.previous_x, hand.previous_y = hand.x, hand.y

    @property
    def live_hands(self) -> list[Hand]:
        return [hand for hand in self.hands.values() if hand.live]

    def reset(self) -> None:
        for name in self.hands:
            self.hands[name] = Hand(name)


@dataclass
class PlayerSelector:
    """Which of the people in shot is the one playing. Section 13.

    Largest bounding box, with hysteresis. Area rather than centre distance
    because it is a proxy for *closest to the camera*, and the person playing a
    motion game is reliably the one standing nearest it — whereas "closest to
    centre" picks the person walking behind them through the middle of the
    room.
    """

    margin: float = SWITCH_MARGIN
    frames: int = SWITCH_FRAMES
    #: Area of whoever is currently the player, or 0.
    _current_area: float = 0.0
    _current_centre: float = 0.5
    _challenger_frames: int = 0
    _held: bool = False
    _seen: int = field(default=0, repr=False)

    def choose(self, frame: PoseFrame) -> PersonPose | None:
        """The player, or None when nobody qualifies."""
        if frame.empty:
            # Not reset immediately: people vanish for a frame all the time,
            # and forgetting the player every time they do would make the
            # hysteresis above pointless.
            self._seen += 1
            if self._seen > self.frames:
                self._held = False
                self._current_area = 0.0
                self._challenger_frames = 0
            return None
        self._seen = 0

        best = max(frame.persons, key=lambda person: person.area)
        if not self._held:
            self._take(best)
            return best

        # Whoever is nearest to the incumbent's last known position and size is
        # taken to *be* the incumbent. There is no tracking in this model, so
        # identity across frames is inferred, and position plus area is enough
        # for the one case that matters: telling the player from somebody
        # walking past behind them.
        incumbent = min(frame.persons,
                        key=lambda person: abs(person.centre_x - self._current_centre))
        if best is incumbent:
            self._take(best)
            self._challenger_frames = 0
            return best

        if best.area >= incumbent.area * self.margin:
            self._challenger_frames += 1
            if self._challenger_frames >= self.frames:
                log.info("AI Motion: the player changed")
                self._take(best)
                self._challenger_frames = 0
                return best
        else:
            self._challenger_frames = 0

        self._take(incumbent)
        return incumbent

    def _take(self, person: PersonPose) -> None:
        self._held = True
        self._current_area = person.area
        self._current_centre = person.centre_x

    def reset(self) -> None:
        self._held = False
        self._current_area = 0.0
        self._current_centre = 0.5
        self._challenger_frames = 0
        self._seen = 0


def readiness(person: PersonPose | None, confidence: float) -> dict:
    """Whether this person can start a game, and what to tell them if not.

    Section 21's quick calibration, which is deliberately not a calibration:
    Fruit Ninja needs an upper body and two hands, and demanding a full-body
    pose would push everybody a metre further back for nothing. The advice
    strings are what the start screen shows, so they say what to *do* rather
    than what is wrong.
    """
    if person is None:
        return {"ready": False, "player": False,
                "advice": "Step in front of the camera"}

    shoulders = person.upper_body_visible(confidence)
    wrists = [name for name in WRISTS if person.visible(name, confidence)]

    if not shoulders:
        return {"ready": False, "player": True,
                "advice": "Move back a little so your shoulders are visible"}
    if len(wrists) < 2:
        # Named singular or plural rather than "both hands need to be visible"
        # regardless, because a person holding one hand up and being told about
        # "both" cannot tell which one the camera has lost.
        missing = "Both hands need to be visible" if not wrists else \
            "Raise your other hand so the camera can see it"
        return {"ready": False, "player": True, "advice": missing}

    # A person filling the frame is standing too close to swing at anything —
    # the play area is their arm span, and they need room for it.
    if person.area > 0.62:
        return {"ready": False, "player": True,
                "advice": "Move back a little to give yourself room"}

    return {"ready": True, "player": True, "advice": "Raise your hands to play"}
