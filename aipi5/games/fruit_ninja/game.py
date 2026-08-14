"""One session of Fruit Slice: the state machine, the score, and the lives.

**The simulation is authoritative and lives here, in Python.** The page draws
it and does not decide anything. That is a deliberate departure from the Hailo
reference, which runs the game in a Pygame process and passes fruit positions
back for an overlay, and the reason is that this project's screen is a browser
— every other page in AIPI5 is — so a second windowing toolkit would be a
second thing fighting for the display on a Wayland kiosk that has exactly one
surface.

Keeping the simulation in Python buys three things beyond that:

* Collision is tested against the pose stream directly, at the moment the pose
  arrives, rather than after a round trip to the page and back. That is the
  lowest-latency place it can possibly happen.
* Every rule in sections 24 to 28 is testable with no browser — see
  `tests/test_fruit_ninja.py`, which plays whole games in a few milliseconds.
* The page can be reloaded mid-game without losing the score.

What the page does own is *drawing at 60 Hz*, which it does by extrapolating
each fruit from the position and velocity in the last snapshot. Section 24's
"interpolation may be used between pose frames", and it is what lets the
simulation tick at the pose rate without anything looking like it is stepping.

**`tick` never reads a clock.** Time arrives as an argument, from the pose
frame that triggered it, which is what makes a fixed-step test of a two-minute
game run instantly and deterministically.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from aipi5.games.fruit_ninja import collision
from aipi5.games.fruit_ninja.fruit import HEIGHT, WIDTH, Fruit, Spawner
from aipi5.motion import geometry

log = logging.getLogger(__name__)

#: Section 27.
STARTING_LIVES = 3

#: How long a sliced fruit stays in the state so the page can animate its
#: halves flying apart. Purely visual; it cannot be hit again.
SLICE_LINGER_S = 0.55

#: The longest step the simulation will take in one go. A tab that was
#: backgrounded, a pose frame that arrived late after a stall — without this,
#: one enormous `dt` teleports every fruit off the screen and costs the player
#: every life at once. Clamped rather than subdivided because this game has no
#: state that depends on the path taken, only on where things end up.
MAX_STEP_S = 0.1

#: A slash slower than this does not cut. Resting a hand on a fruit should not
#: slice it — section 15 is about not cutting with *uncertain* data, and this
#: is the other half: not cutting with data that is certain and stationary.
#: Normalised widths per second, measured against a hand held deliberately
#: still (well under 0.15) and a gentle deliberate swipe (above 1.0).
MIN_SLASH_SPEED = 0.35


class State(str, Enum):
    """Where a session is. `str` so it serialises without a converter."""

    #: Waiting for a player to be detected and to press Start. Section 20.
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    OVER = "over"


@dataclass
class Session:
    """One game, from the start screen to game over.

    Not thread-safe on its own. `tick` is called from the pose thread and the
    control methods from HTTP handlers, so `aipi5/games/manager.py` — which
    owns the only instance — holds a lock around all of them.
    """

    lives: int = STARTING_LIVES
    score: int = 0
    best: int = 0
    state: State = State.READY
    fruit: list[Fruit] = field(default_factory=list)
    spawner: Spawner = field(default_factory=Spawner)
    #: Slashes to draw, as screen-space segments. Drained by the page each
    #: poll; capped so a page that stops reading cannot grow this without
    #: bound.
    slashes: list[dict] = field(default_factory=list)
    #: Set when something happened that the page should react to with a sound.
    #: Section 37's five effects, played by the browser.
    events: list[str] = field(default_factory=list)

    started_at: float = 0.0
    ended_at: float = 0.0
    #: -1 rather than 0 for "has not ticked yet", because 0.0 is a perfectly
    #: real time. Truth-testing this field made the first tick of a session
    #: whose clock started at zero silently do nothing — invisible against
    #: `time.monotonic()`, which is never 0, and immediately fatal to any test
    #: that starts its own clock at the obvious place.
    _last_tick: float = -1.0
    sliced_total: int = 0
    missed_total: int = 0
    bombs_hit: int = 0
    #: Consecutive fruit sliced without a miss, for the combo readout.
    streak: int = 0
    best_streak: int = 0

    # ── control ──────────────────────────────────────────────────────

    def start(self, now: float) -> None:
        """Begin, or begin again. Section 28's Play Again is this."""
        self.lives = STARTING_LIVES
        self.score = 0
        self.fruit.clear()
        self.slashes.clear()
        self.events.clear()
        self.spawner.reset()
        self.state = State.PLAYING
        self.started_at = now
        self.ended_at = 0.0
        self._last_tick = now
        self.sliced_total = self.missed_total = self.bombs_hit = 0
        self.streak = self.best_streak = 0

    def pause(self, now: float) -> bool:
        if self.state is not State.PLAYING:
            return False
        self.state = State.PAUSED
        return True

    def resume(self, now: float) -> bool:
        """Carry on. The clock restarts from now, not from the pause.

        Without resetting `_last_tick`, the first step after a two-minute pause
        would be a two-minute `dt`. `MAX_STEP_S` would clamp it to a tenth of a
        second, so nothing would break — but every fruit would still jump, and
        the fix belongs here where the intent is obvious rather than in a
        clamp that happens to cover it.
        """
        if self.state is not State.PAUSED:
            return False
        self.state = State.PLAYING
        self._last_tick = now
        return True

    def finish(self, now: float) -> None:
        if self.state is State.OVER:
            return
        self.state = State.OVER
        self.ended_at = now
        self.events.append("game-over")
        if self.score > self.best:
            self.best = self.score
        log.info("Game: Fruit Ninja over — score %d, best %d, %d sliced, "
                 "%d missed", self.score, self.best, self.sliced_total,
                 self.missed_total)

    # ── the simulation ───────────────────────────────────────────────

    def tick(self, now: float, hands=None) -> None:
        """Advance to `now` and test `hands` against everything in flight.

        `hands` are `pose_filter.Hand` objects in normalised camera space; they
        are converted to screen pixels here, once, by the same function the
        page's cursors use. Doing it here rather than in the pose service keeps
        `PoseFrame` free of any opinion about how big the screen is.
        """
        if self.state is not State.PLAYING:
            return

        dt = now - self._last_tick if self._last_tick >= 0 else 0.0
        self._last_tick = now
        if dt <= 0:
            return
        dt = min(dt, MAX_STEP_S)

        for item in self.fruit:
            if not item.sliced:
                item.advance(dt)

        if hands:
            self._slice(now, hands, dt)

        self._retire(now)

        for spawned in self.spawner.due(now, self.score):
            self.fruit.append(spawned)

        if self.lives <= 0:
            self.finish(now)

    def _slice(self, now: float, hands, dt: float) -> None:
        """Test each live hand's path against each unsliced fruit."""
        for hand in hands:
            if not hand.live or hand.reappeared:
                # `reappeared` is the first frame back after the hand was lost.
                # Its segment is zero-length by construction, but skipping it
                # outright says why: a hand that vanished on one side of the
                # screen and returned on the other must not slice everything
                # in between.
                continue
            if hand.speed(dt) < MIN_SLASH_SPEED:
                continue

            from_point = geometry.to_screen(hand.previous_x, hand.previous_y,
                                            WIDTH, HEIGHT, clamp=False)
            to_point = geometry.to_screen(hand.x, hand.y, WIDTH, HEIGHT,
                                          clamp=False)

            cut_any = False
            for item in self.fruit:
                if item.sliced:
                    continue
                if collision.slash_hits_fruit(
                        from_point, to_point,
                        (item.previous_x, item.previous_y), (item.x, item.y),
                        item.kind.radius):
                    self._cut(item, from_point, to_point, now)
                    cut_any = True

            if cut_any:
                self.slashes.append({
                    "hand": hand.name,
                    "x1": round(from_point[0], 1), "y1": round(from_point[1], 1),
                    "x2": round(to_point[0], 1), "y2": round(to_point[1], 1),
                    "at": round(now, 3),
                })
                # A page that stopped polling must not be able to grow this
                # without bound. The newest are the ones worth drawing.
                del self.slashes[:-12]

    def _cut(self, item: Fruit, from_point, to_point, now: float) -> None:
        item.sliced = True
        item.sliced_at = now
        item.slice_angle = collision.slash_angle(from_point, to_point)

        if item.is_bomb:
            # Section 26. A bomb costs a life and breaks the streak; it does
            # not subtract score, because a negative score is a worse thing to
            # show somebody than a stalled one.
            self.bombs_hit += 1
            self.lives -= 1
            self.streak = 0
            self.events.append("bomb")
            log.info("Game: bomb sliced — %d lives left", self.lives)
            return

        self.streak += 1
        self.best_streak = max(self.best_streak, self.streak)
        self.sliced_total += 1
        # Combos are worth more, gently. Section 25 says keep V1 simple, so
        # this is the one embellishment: a bonus point per fruit in the streak
        # beyond the second, capped, which rewards a good run without letting
        # the score run away.
        bonus = min(10, max(0, self.streak - 2))
        self.score += item.kind.points + bonus
        self.events.append("slice")

    def _retire(self, now: float) -> None:
        """Drop what has left play, and charge for the fruit that got away."""
        kept: list[Fruit] = []
        for item in self.fruit:
            if item.sliced:
                if now - item.sliced_at < SLICE_LINGER_S:
                    kept.append(item)
                continue
            if item.missed:
                # Section 27. A bomb that falls off the bottom is a *good*
                # outcome and must not cost anything — the player correctly
                # left it alone.
                if not item.is_bomb:
                    self.lives -= 1
                    self.missed_total += 1
                    self.streak = 0
                    self.events.append("miss")
                continue
            if item.gone:
                continue
            kept.append(item)
        self.fruit = kept

    # ── publishing ───────────────────────────────────────────────────

    def snapshot(self, now: float) -> dict:
        """What the page draws. Small enough to send thirty times a second."""
        return {
            "state": self.state.value,
            "score": self.score,
            "best": self.best,
            "lives": max(0, self.lives),
            "streak": self.streak,
            "fruit": [item.as_dict() for item in self.fruit],
            "slashes": self.slashes[-6:],
            "elapsed": round(now - self.started_at, 1) if self.started_at else 0.0,
            "stats": {
                "sliced": self.sliced_total,
                "missed": self.missed_total,
                "bombs": self.bombs_hit,
                "best_streak": self.best_streak,
            },
        }

    def take_events(self) -> list[str]:
        """Drain the sound queue. Called once per publish, by one reader."""
        events, self.events = self.events, []
        return events


class HighScores:
    """The best score, on this device. Section 28: no cloud.

    One small JSON file, read at start and written when a record falls.
    Failures are logged and ignored — a game that will not start because a
    score file is unwritable would be a worse device than one that forgets.
    """

    def __init__(self, path):
        self.path = path
        self._scores: dict = {}
        self.load()

    def load(self) -> None:
        try:
            self._scores = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._scores, dict):
                self._scores = {}
        except FileNotFoundError:
            self._scores = {}
        except (OSError, ValueError) as exc:
            log.warning("could not read the high scores at %s: %s",
                        self.path, exc)
            self._scores = {}

    def best(self, game: str) -> int:
        try:
            return int(self._scores.get(game, 0))
        except (TypeError, ValueError):
            return 0

    def record(self, game: str, score: int) -> bool:
        """Remember `score` if it beats what is there. True if it did."""
        if score <= self.best(game):
            return False
        self._scores[game] = int(score)
        self._scores.setdefault("updated", 0)
        self._scores["updated"] = int(time.time())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._scores, indent=2),
                                 encoding="utf-8")
        except OSError as exc:
            log.warning("could not save the high score: %s", exc)
        return True
