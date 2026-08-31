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

from aipi5.games.fruit_ninja import collision, ultimate
from aipi5.games.fruit_ninja.fruit import HEIGHT, WIDTH, Fruit, Spawner
from aipi5.games.fruit_ninja.ultimate import UltimateDragon
from aipi5.motion import geometry

log = logging.getLogger(__name__)

#: How long one round lasts. The game is a fixed clock, a score, and — since
#: the bomb was rebuilt — three lives that only a bomb can take.
#:
#: **The clock and the lives are not the same rule wearing two hats, and the
#: split is the whole design.** A *dropped* fruit costs no life and no time,
#: which is what keeps the round honest: doing nothing costs exactly as much as
#: trying and missing, so there is never a reason not to swing at something
#: awkward. A *sliced bomb* costs a life, which is the one thing on the screen
#: the player is asked to steer around and the only place a life can go. Lives
#: therefore measure judgement and the clock measures the round, and neither
#: punishes the other's mistake.
#:
#: The fixed length is what makes two scores comparable and a high score worth
#: having; a round that ends early now means one thing only, and it is a thing
#: the player did `MAX_LIVES` times on purpose.
#:
#: Two minutes as of the Ultimate upgrade, up from one. The extra minute is not
#: more of the same: it is what makes room for a round to have a *shape* — a
#: minute and a half of ordinary play that ramps, then a climax, then a short
#: coda. A sixty-second round with a ten-second Ultimate in it would have been
#: a sixth of the game spent on one fruit.
ROUND_SECONDS = 120.0

#: Where the Ultimate sits in the round, as seconds remaining. Section 16.
#:
#: Expressed as time *left* rather than time elapsed, which is the reading that
#: matches the promise: the Ultimate is the last fifteen seconds of the round,
#: whatever the round has been. It was load-bearing when a bomb could move the
#: clock and the two were genuinely different quantities; it is now the same
#: number said the right way round, and left alone because the *screen* counts
#: down and a constant the screen can be checked against is worth more than one
#: that has to be subtracted first.
#:
#: **The last fifteen seconds are the Ultimate, all of them.** `ULTIMATE_START_AT`
#: and `ultimate.DURATION_S` are equal on purpose, so the dragon fruit is born
#: at the fifteen second mark and runs out with the round. Nothing follows it.
#:
#: **Nothing can jump this window any more, and it is worth saying why the five
#: seconds are still five seconds.** A bomb used to take five seconds off the
#: clock, which meant the countdown did not pass *through* the warning window
#: so much as land somewhere in or beyond it — the window was made exactly one
#: bomb wide so that a single one could not clear it, and the two constants
#: were arithmetic on `BOMB_PENALTY_S` rather than two chosen numbers. The
#: bomb costs a life and a score now and does not touch the clock at all, so
#: the countdown is monotone in real time and every window is unjumpable
#: including a one-second one.
#:
#: The five seconds stay because five seconds is a good warning: long enough to
#: finish the swing in progress and look up, short enough not to be a lull. It
#: is now a choice about pacing rather than a defence against the clock, and
#: `_advance_phase` below is deliberately still written to survive a clock that
#: skips — the edge-once transitions cost nothing and the guarantee they give
#: outlives whatever the bomb does next.
ULTIMATE_WARNING_AT = 20.0
ULTIMATE_START_AT = 15.0

#: How many lives a round starts with, and the ceiling a heart fruit heals up
#: to.
#:
#: Five, up from three, alongside halving the bomb rate — and the two together
#: are one decision about what a two-minute round should feel like. Three lives
#: against one bomb in seven meant a round could be over in forty seconds on
#: three unlucky swings, which is the failure mode a fixed-length round exists
#: to avoid: the score stops being comparable when half the games do not reach
#: the Ultimate.
#:
#: Five is also about what the corner can hold. The hearts are drawn at full
#: size in a single row above the clock, and a row that has to shrink or wrap
#: to fit is a readout the player has to *look* at rather than glance at.
MAX_LIVES = 5

#: What slicing a bomb costs: one life of `MAX_LIVES`, and this many points.
#:
#: **The clock is no longer part of it, and that reversed an earlier decision
#: on purpose.** The bomb used to take five seconds, and the argument against a
#: score penalty was that a number going backwards reads as the game taking
#: something away rather than as a mistake costing time. That argument was
#: right about the *feeling* and wrong about which feeling this game wants: a
#: penalty the player is supposed to steer around should be felt, and a clock
#: that drops five seconds in one frame was read by the first person who saw it
#: as the countdown being broken — which is the honest reading, because a
#: countdown that jumps is broken. A life and a score are both things a player
#: can watch themselves lose.
#:
#: Twenty-five is the most any single fruit is worth (the dragon), so a bomb
#: costs a very good cut. It is clamped at zero rather than allowed to go
#: negative — a negative score is a different game — which means the opening
#: seconds are the one stretch where a bomb costs only a life, and that is
#: fine: the bomb cannot be thrown for the first ten of them anyway.
BOMB_PENALTY_POINTS = 25

#: How long a sliced fruit stays in the state so the page can animate its
#: halves flying apart. Purely visual; it cannot be hit again.
SLICE_LINGER_S = 0.55

#: What slicing an ice cube does: every fruit in flight moves at this fraction
#: of its speed, for this many seconds.
#:
#: **It is a scale on `dt`, not on the velocities** — see `Session.tick`. The
#: two are indistinguishable while the effect is running and completely
#: different when it ends: halving `vx` and `vy` and doubling them again five
#: seconds later gives back a *different* trajectory from the one the fruit was
#: on, because gravity has been adding to a halved velocity the whole time, so
#: every fruit on screen would visibly jump at the moment the ice wore off.
#: Slowing time instead means the arc is the same arc, walked more slowly, and
#: nothing changes at either end except the rate.
#:
#: The round's own clock is deliberately *not* slowed. Five seconds of half
#: speed is meant to be five seconds the player can use, and a slow that also
#: slowed the countdown would hand out nothing at all — the same fruit, the
#: same time to reach them, at half the frame-to-frame distance.
SLOW_FACTOR = 0.5
SLOW_SECONDS = 5.0

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

#: How long after a slice another one still counts towards the same combo.
#: Section 13.
#:
#: 0.7 s, which is a little longer than one swing. Two fruit taken by a single
#: slash are simultaneous and would count at any window at all; what this
#: number really decides is whether a *backhand* return counts, and it should —
#: cutting left-to-right and immediately right-to-left is the most satisfying
#: thing in the game and reads as one action. Much longer and an ordinary busy
#: stretch is permanently "in combo", which means nothing.
COMBO_WINDOW_S = 0.7

#: The smallest combo worth announcing. Two fruit at once is common enough that
#: a banner for it would be on screen most of the round.
COMBO_ANNOUNCE = 3


def _blade_reach(item: Fruit) -> float:
    """How far either side of the hand's path this swing actually cuts.

    Added to the target's radius, so the blade is a swept capsule rather than a
    line with no thickness — see `collision.BLADE_HALF_WIDTH` for why the
    tested shape has to match the drawn one.

    **A bomb gets none of it, and that is deliberate.** Widening the blade is
    meant to stop a good swing scoring nothing; letting the same widening set
    off a bomb the player steered around would take with one hand what it gives
    with the other, and turn "the blade got thicker" into "the round got
    shorter". Generous towards a reward and exact towards a hazard is the
    convention players read as fair rather than as inconsistent — and a bomb is
    the one object on screen that is *aimed away from*, so its edge is the one
    the player is already judging by eye.
    """
    return 0.0 if item.is_bomb else collision.BLADE_HALF_WIDTH


class State(str, Enum):
    """Where a session is. `str` so it serialises without a converter."""

    #: Waiting for a player to be detected and to press Start. Section 20.
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    OVER = "over"


class Phase(str, Enum):
    """Where a *round* is, within `State.PLAYING`. Section 39.

    Separate from `State` because they answer different questions and change
    for different reasons: `State` is about whether the simulation is running
    at all, and this is about what the simulation is currently doing. Pausing
    during the Ultimate must not lose the Ultimate, which is exactly what
    folding the two into one enum would have made easy to get wrong.

    **An enum rather than the time checks it replaces.** The transitions below
    are each an edge that must fire exactly once — one warning sound, one
    dragon fruit — and `if time_left <= 22` is true for every frame of the next
    two seconds. Comparing the phase to what it was is the only version of this
    that cannot fire twice.
    """

    #: 120s down to 22s left. Ordinary play.
    NORMAL = "normal"
    #: 22s to 20s. The warning is up; nothing else has changed.
    WARNING = "warning"
    #: 20s to 10s. The dragon fruit is out and nothing new is thrown.
    ULTIMATE = "ultimate"
    #: 10s to 0s. Ordinary play again, for a short coda.
    FINAL = "final"


@dataclass
class Session:
    """One game, from the start screen to game over.

    Not thread-safe on its own. `tick` is called from the pose thread and the
    control methods from HTTP handlers, so `aipi5/games/manager.py` — which
    owns the only instance — holds a lock around all of them.
    """

    #: How long this round lasts. A field rather than the constant so a future
    #: game — or a test — can run a ten-second round without patching a module.
    duration: float = ROUND_SECONDS
    #: Seconds left, counted down every tick by the same clamped `dt` the
    #: physics uses. Held rather than derived from `started_at` because it is
    #: not a function of the wall clock: a pause does not spend it and a stalled
    #: frame cannot take ten seconds off the player at once. It was also, until
    #: the bomb stopped costing time, a quantity the game itself could move.
    time_left: float = ROUND_SECONDS
    score: int = 0
    best: int = 0
    #: Lives left. Spent only on bombs and refilled only by heart fruit, up to
    #: `MAX_LIVES` — see `_cut`. A dropped fruit does not touch this.
    lives: int = MAX_LIVES
    state: State = State.READY
    fruit: list[Fruit] = field(default_factory=list)
    spawner: Spawner = field(default_factory=Spawner)
    #: Slashes to draw, as screen-space segments. Drained by the page each
    #: poll; capped so a page that stops reading cannot grow this without
    #: bound.
    slashes: list[dict] = field(default_factory=list)
    #: Set when something happened that the page should react to. Each entry is
    #: a dict rather than the bare string it used to be, because a slice now
    #: has to say *which fruit* — the sound and the splash colour both depend
    #: on it — and where it happened, for the score popup. The page reads
    #: `name` and ignores anything it does not recognise, so adding a field
    #: here never needs a matching change there.
    events: list[dict] = field(default_factory=list)

    # ── the Ultimate. Section 16 to 28. ──────────────────────────────

    #: Which part of the round this is. Never `WARNING` or later in a fresh
    #: session; `start()` puts it back to `NORMAL`.
    phase: Phase = Phase.NORMAL
    #: The dragon fruit, while it exists. `None` at every other moment,
    #: including after it has been finalised — which is what makes section 40's
    #: reset list a single assignment rather than eight.
    dragon: UltimateDragon | None = None
    #: What the last Ultimate finished with, kept after `dragon` is dropped so
    #: the game-over screen can report it. Zero when the round has not reached
    #: the Ultimate yet.
    ultimate_hits: int = 0
    ultimate_points: int = 0
    ultimate_done: bool = False
    #: Guards the once-per-round guarantee against a clock that crosses a
    #: threshold twice. Nothing moves `time_left` but time itself any more, so
    #: this cannot currently fire — and it stays, because a second dragon fruit
    #: in one round would be a much worse bug than a redundant boolean, and the
    #: thing that used to push the clock about was a gameplay rule rather than a
    #: law of nature.
    ultimate_spawned: bool = False
    #: Whether the warning has been given this round. Not derivable from the
    #: phase, which has moved on by the time it matters, and not from
    #: `ultimate_spawned`, which is set at the wrong moment. It exists so that
    #: a frame long enough to clear the whole five-second window still gets its
    #: warning — late, on the same frame as the fruit it was warning about,
    #: which is worth more than nothing at all. See `_advance_phase`.
    ultimate_warned: bool = False

    #: Seconds of half speed still owed, counted down by `tick` in seconds of
    #: play — so a pause does not spend it, for the same reason the round's own
    #: clock is a quantity rather than a function of the wall clock. Zero for
    #: almost all of a round. See `SLOW_FACTOR`.
    slow_left: float = 0.0

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
    #: Heart fruit sliced, whether or not there was a life to give back. The
    #: count the game-over screen reports, so it is the number of times the
    #: player *took* one rather than the number of times it helped.
    hearts_hit: int = 0
    #: Consecutive fruit sliced without a miss, for the combo readout.
    streak: int = 0
    best_streak: int = 0
    #: Fruit sliced inside the last `COMBO_WINDOW_S`, and when the window
    #: opened. Section 13's combo, which is a different quantity from `streak`
    #: and worth keeping separately: a streak is about not *missing* and can
    #: run for a minute, while a combo is about several fruit at once and is
    #: over in half a second. Conflating them would mean a careful player who
    #: never misses is permanently in a 40x combo.
    combo: int = 0
    _combo_until: float = 0.0
    best_combo: int = 0

    # ── control ──────────────────────────────────────────────────────

    def start(self, now: float) -> None:
        """Begin, or begin again. Section 28's Play Again is this.

        Section 40 lists eight pieces of Ultimate state that must not leak into
        the next round. Seven of them are fields of `UltimateDragon`, so
        dropping the object resets them all at once and there is no list here
        to fall out of step with the one in `ultimate.py`. The three that
        survive it are the *record* of what the last Ultimate did, which the
        game-over screen shows, and they are zeroed explicitly below.
        """
        self.time_left = self.duration
        self.score = 0
        self.lives = MAX_LIVES
        self.fruit.clear()
        self.slashes.clear()
        self.events.clear()
        self.spawner.reset()
        self.state = State.PLAYING
        self.started_at = now
        self.ended_at = 0.0
        self._last_tick = now
        self.sliced_total = self.missed_total = self.bombs_hit = 0
        self.hearts_hit = 0
        self.streak = self.best_streak = 0
        self.combo = self.best_combo = 0
        self._combo_until = 0.0
        self.slow_left = 0.0

        self.phase = Phase.NORMAL
        self.dragon = None
        self.ultimate_hits = self.ultimate_points = 0
        self.ultimate_done = False
        self.ultimate_spawned = False
        self.ultimate_warned = False

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
        # Before the state changes, so the events go out in the order they
        # happened.
        #
        # They are now the *same* frame rather than ten seconds apart: the
        # Ultimate runs to the whistle, so every round ends with the dragon
        # fruit bursting and the game-over phrase arriving together. The page
        # holds the phrase back when it sees both in one batch — see the
        # `game-over` case in `handleGameEvent` — which is the right place for
        # it, because it is a question about sound and not about the game.
        self._finalise_ultimate(now)
        self.state = State.OVER
        self.ended_at = now
        # Why it ended, because there are two ways now and the game-over screen
        # says different things about them. Derived from the lives rather than
        # passed in by the caller: `finish` is reached from the clock, from the
        # last bomb and from the Stop button, and a reason threaded through
        # three call sites is a reason that will one day be wrong at one of
        # them.
        self.events.append({"name": "game-over",
                            "reason": "lives" if self.lives <= 0 else "time"})
        if self.score > self.best:
            self.best = self.score
        log.info("Game: Fruit Ninja over (%s) — score %d, best %d, %d sliced, "
                 "%d missed, %d lives left",
                 "out of lives" if self.lives <= 0 else "time up",
                 self.score, self.best, self.sliced_total, self.missed_total,
                 self.lives)

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

        # The clock runs off the same clamped `dt` as the physics, not off
        # `now - started_at`. That is what keeps the round honest across a
        # pause: `resume` moves `_last_tick` forward, so the paused seconds are
        # never charged, and a stalled frame cannot take ten seconds off the
        # player at once.
        self.time_left = max(0.0, self.time_left - dt)

        # Before anything moves, so that the frame the dragon fruit appears on
        # is a frame it can already be hit on, and so that the spawn
        # suppression below is reading this frame's phase rather than the last
        # one's.
        self._advance_phase(now)

        # The ice cube's half speed, applied to the step rather than to the
        # velocities — see `SLOW_FACTOR`. Read before it is spent, so the frame
        # an ice cube's last fraction of a second falls on is still a slow one.
        slow = SLOW_FACTOR if self.slow_left > 0 else 1.0
        self.slow_left = max(0.0, self.slow_left - dt)

        for item in self.fruit:
            if not item.sliced:
                item.advance(dt * slow)
        # The dragon fruit is not slowed. It does not fly — it hovers, and the
        # bob is a decoration rather than a trajectory, so halving it makes the
        # target no easier and only makes the one moment of the round that is
        # supposed to feel frantic look like it has stalled. Nothing new is
        # thrown during the Ultimate either (see below), so the only way to be
        # in both states at once is to have sliced an ice cube in the second
        # before it began.
        if self.dragon is not None:
            self.dragon.advance(dt)

        if hands:
            self._slice(now, hands, dt)

        self._retire(now)

        # The combo lapses on its own rather than on the next slice, so the
        # HUD stops showing "5x" three seconds after the last one.
        if self.combo and now >= self._combo_until:
            self.combo = 0

        # Nothing new is thrown in the last moment of a round. A fruit launched
        # with half a second left cannot be reached before the whistle, and
        # watching one arc up as the clock hits zero reads as the game having
        # cheated rather than as bad luck.
        #
        # And nothing at all during the Ultimate. Section 29: the dragon fruit
        # is the only thing that should want the player's attention for those
        # ten seconds, and a bomb arriving mid-flurry would cost five seconds
        # for a swing they were making at something else. Fruit already in the
        # air when it began is left to finish its arc — deleting it would look
        # like the game confiscating a fruit the player was about to reach.
        if (self.time_left > self.spawner.dead_air and self.lives > 0
                and not self.in_ultimate):
            for spawned in self.spawner.due(now, self.elapsed):
                self.fruit.append(spawned)

        # The two ways a round ends, tested in the same place and after
        # everything else has happened. Out of lives is checked here rather
        # than inside `_cut` for the reason given there: a swing that takes the
        # last two bombs at once must be one game over, and the frame it
        # happens on should finish drawing the explosions it caused.
        if self.time_left <= 0 or self.lives <= 0:
            self.finish(now)

    @property
    def in_ultimate(self) -> bool:
        """Whether the dragon fruit currently owns the screen. Section 29."""
        return self.phase is Phase.ULTIMATE

    @property
    def has_ultimate(self) -> bool:
        """Whether this round is long enough to hold an Ultimate at all.

        Section 16 guarantees one in every 120-second game, and `duration` is a
        field precisely so a round need not be 120 seconds — the tests run
        ten-second games and a future mode may want a short one. Without this
        check such a round starts *inside* the Ultimate window and throws a
        dragon fruit on its first frame, which is not a shorter version of the
        game so much as a different one.
        """
        return self.duration > ULTIMATE_WARNING_AT

    def _finalise_ultimate(self, now: float) -> None:
        """Bank what the dragon fruit earned and take it off the screen.

        Called from exactly two places: the transition into `FINAL`, which is
        the ordinary end, and `finish`, which is what happens when the round
        runs out from under it. The second is not hypothetical — a bomb already
        in the air when the Ultimate began takes five seconds off the clock if
        it is sliced, and the phase deliberately follows the *dragon fruit's*
        age rather than the clock, so a player who does that gets their full
        ten seconds and the whistle goes during them.
        """
        dragon = self.dragon
        if dragon is None:
            return
        self.dragon = None
        self.ultimate_hits = dragon.hits
        self.ultimate_points = dragon.points
        self.ultimate_done = dragon.completed
        self.events.append({
            "name": "ultimate-end",
            "hits": dragon.hits,
            "completed": dragon.completed,
            "x": round(dragon.x, 1),
            "y": round(dragon.y, 1),
        })
        log.info("Game: ultimate ended — %d hits, %s, %d points",
                 dragon.hits, "complete" if dragon.completed else "incomplete",
                 dragon.points)

    def _advance_phase(self, now: float) -> None:
        """Move the round on, firing each transition exactly once.

        Written as "what should the phase be, given the clock" followed by
        "what changed", rather than as a chain of `if` statements that each do
        something. The difference matters when a frame is long enough to skip a
        phase, and the version that cannot go wrong costs nothing over the
        version that can.

        **The order of the branches is the guarantee.** `left <=
        ULTIMATE_START_AT` is tested before anything that could hold the dragon
        fruit back, so from the fifteen second mark onwards there is a dragon
        fruit on the screen no matter how the clock got there — counted down
        to, or arrived at in one long frame. The previous arrangement let the
        warning gate the spawn, and a warning that is owed time can outlive the
        window it was owed in; that is how a round could end having never shown
        the Ultimate at all.
        """
        previous = self.phase
        left = self.time_left

        if not self.has_ultimate:
            return

        if self.dragon is not None and not self.dragon.expired:
            wanted = Phase.ULTIMATE
        elif self.ultimate_spawned:
            # There has been a dragon fruit and it is gone. Unreachable while
            # `ultimate.DURATION_S` equals `ULTIMATE_START_AT` — the fruit and
            # the round run out together — and kept because it is what catches
            # the two being set apart, in either direction.
            wanted = Phase.FINAL
        elif left <= ULTIMATE_START_AT:
            wanted = Phase.ULTIMATE
        elif left <= ULTIMATE_WARNING_AT:
            wanted = Phase.WARNING
        else:
            wanted = Phase.NORMAL

        if wanted is previous:
            return
        self.phase = wanted

        if wanted is Phase.WARNING:
            # Section 17. The sound and the banner, and nothing else — the
            # player keeps full control through the warning.
            self._warn(now)
        elif wanted is Phase.ULTIMATE:
            # A warning that never happened, because the clock cleared the
            # whole window in one frame. Nothing in an ordinary round can do
            # that now that the bomb has stopped moving the clock — a frame
            # would have to be five seconds long, and `MAX_STEP_S` clamps it to
            # a tenth — and the notice is worth more late than not at all.
            if not self.ultimate_warned:
                self._warn(now)
            self.dragon = UltimateDragon(born_at=now)
            self.ultimate_spawned = True
            self.events.append({"name": "ultimate-spawn"})
            log.info("Game: ultimate dragon fruit spawned with %.1fs left",
                     left)
        elif wanted is Phase.FINAL:
            self._finalise_ultimate(now)

    def _warn(self, now: float) -> None:
        """Announce the Ultimate, once per round."""
        if self.ultimate_warned:
            return
        self.ultimate_warned = True
        self.events.append({"name": "ultimate-warning"})
        log.info("Game: ultimate warning")

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
                        item.kind.radius + _blade_reach(item)):
                    self._cut(item, from_point, to_point, now)
                    cut_any = True

            # The dragon fruit is tested by the same swing, with its own rules.
            # It does not set `cut_any`, because that flag is about whether to
            # record a slash segment for the debug overlay and a hand that is
            # only ever hitting the dragon fruit would otherwise fill it thirty
            # times a second for ten seconds.
            if self.dragon is not None:
                gained = self.dragon.attempt(hand, from_point, to_point, now)
                if gained:
                    self._dragon_hit(gained, now)

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

    def _dragon_hit(self, gained: int, now: float) -> None:
        """One valid cut on the dragon fruit landed. Sections 23, 26 and 27.

        The score goes on immediately and the streak is *not* touched. A streak
        is about consecutive thrown fruit, and letting thirty dragon hits run it
        to thirty would both cap out the combo bonus for free and leave the
        player's real streak destroyed the moment the Ultimate ended.
        """
        dragon = self.dragon
        if dragon is None:
            return
        self.score += gained
        self.events.append({
            "name": "ultimate-hit",
            "hits": dragon.hits,
            "points": gained,
            "stage": dragon.stage,
            "x": round(dragon.x, 1),
            "y": round(dragon.y, 1),
        })
        if dragon.completed and dragon.hits == ultimate.HITS_REQUIRED:
            # Exactly the completing hit, not every hit after it. Section 27
            # wants the banner the moment it happens; sections 24 and 25 want
            # play to carry on afterwards, so this fires once and the fruit
            # stays.
            self.events.append({
                "name": "ultimate-complete",
                "bonus": ultimate.COMPLETE_BONUS,
                "x": round(dragon.x, 1),
                "y": round(dragon.y, 1),
            })
            log.info("Game: ultimate complete — +%d bonus",
                     ultimate.COMPLETE_BONUS)

    def _cut(self, item: Fruit, from_point, to_point, now: float) -> None:
        item.sliced = True
        item.sliced_at = now
        item.slice_angle = collision.slash_angle(from_point, to_point)

        if item.is_bomb:
            # Section 26: a bomb costs a life and a score, breaks the streak,
            # and leaves the clock alone. `MAX_LIVES` of them end the round —
            # but not here. The state change happens at the bottom of `tick`, next to
            # the clock running out, so that one swing through two bombs is one
            # game over and not a `finish` called from inside the loop that is
            # still iterating the fruit it is finishing over.
            self.bombs_hit += 1
            self.lives = max(0, self.lives - 1)
            self.score = max(0, self.score - BOMB_PENALTY_POINTS)
            self.streak = 0
            # What it cost and what is left, both on the event, so the page can
            # animate the exact number that changed without keeping its own
            # copy of a constant that lives in this file.
            self.events.append({"name": "bomb",
                                "points": BOMB_PENALTY_POINTS,
                                "lives": self.lives, "max_lives": MAX_LIVES,
                                "x": round(item.x, 1), "y": round(item.y, 1)})
            log.info("Game: bomb sliced — -%d points, %d %s left",
                     BOMB_PENALTY_POINTS, self.lives,
                     "life" if self.lives == 1 else "lives")
            return

        if item.is_ice:
            # Refreshed rather than stacked. Two ice cubes in one flurry is a
            # perfectly ordinary thing to manage, and ten seconds of half speed
            # off the back of it is long enough to be most of the stretch that
            # follows — the reward for the second cube is that the five seconds
            # start again, which is what a player expects from every other
            # timed pickup they have ever seen.
            self.slow_left = SLOW_SECONDS
            self.events.append({"name": "ice", "seconds": SLOW_SECONDS,
                                "factor": SLOW_FACTOR,
                                "x": round(item.x, 1),
                                "y": round(item.y, 1)})
            log.info("Game: ice sliced — fruit at %.0f%% speed for %.0fs",
                     SLOW_FACTOR * 100, SLOW_SECONDS)
            # And then falls through to score exactly like a fruit.

        if item.is_heart:
            # One life back, and never more than the round started with. A
            # heart taken at full health is not refused and not wasted either:
            # it still scores, and the event says which of the two happened so
            # the page can say "+1 LIFE" or "FULL HEALTH" rather than flashing
            # a heart that did not change.
            #
            # Capping rather than banking a spare is deliberate. A stockpile
            # would make the last thirty seconds of a lucky round unloseable,
            # and the three hearts in the corner are the whole readout — a
            # fourth life that is not drawn anywhere is a rule the player
            # cannot see.
            self.hearts_hit += 1
            healed = self.lives < MAX_LIVES
            if healed:
                self.lives += 1
            self.events.append({"name": "heart", "healed": healed,
                                "lives": self.lives, "max_lives": MAX_LIVES,
                                "x": round(item.x, 1), "y": round(item.y, 1)})
            log.info("Game: heart sliced — %s, %d %s",
                     "life restored" if healed else "already full",
                     self.lives, "life" if self.lives == 1 else "lives")
            # And then falls through to score exactly like a fruit, for the
            # reason the ice cube does: a reward that broke a streak would ask
            # the player to choose between two rewards.

        self.streak += 1
        self.best_streak = max(self.best_streak, self.streak)
        self.sliced_total += 1
        # Combos are worth more, gently. Section 25 says keep V1 simple, so
        # this is the one embellishment: a bonus point per fruit in the streak
        # beyond the second, capped, which rewards a good run without letting
        # the score run away.
        bonus = min(10, max(0, self.streak - 2))
        gained = item.kind.points + bonus
        self.score += gained

        # Section 13's combo. The window is extended by each slice rather than
        # fixed from the first, so a sustained flurry stays one combo instead
        # of restarting every 0.7 s in the middle of it.
        self.combo = self.combo + 1 if now < self._combo_until else 1
        self._combo_until = now + COMBO_WINDOW_S
        self.best_combo = max(self.best_combo, self.combo)
        if self.combo >= COMBO_ANNOUNCE:
            self.events.append({"name": "combo", "count": self.combo,
                                "x": round(item.x, 1), "y": round(item.y, 1)})
        # Everything the page needs to make the slice *look* like that fruit —
        # the splash colour, how wet it is, which synthesised voice, and where
        # to float the number. Sent per event rather than looked up on the page
        # so the ten fruit are described in exactly one file.
        self.events.append({
            "name": "slice",
            "kind": item.kind.name,
            "sound": item.kind.sound,
            "juice": item.kind.juice,
            # Skin and flesh as well as juice, because the two halves the page
            # throws apart are drawn from the fruit's own colours and the fruit
            # itself is gone from the next snapshot. Sending them with the
            # event is what lets the halves outlive the object.
            "colour": item.kind.colour,
            "flesh": item.kind.flesh,
            "wet": item.kind.wetness,
            "r": item.kind.radius,
            "points": gained,
            "streak": self.streak,
            "cut": round(item.slice_angle, 3),
            "x": round(item.x, 1),
            "y": round(item.y, 1),
        })

    def _retire(self, now: float) -> None:
        """Drop what has left play, and charge for the fruit that got away."""
        kept: list[Fruit] = []
        for item in self.fruit:
            if item.sliced:
                if now - item.sliced_at < SLICE_LINGER_S:
                    kept.append(item)
                continue
            if item.missed:
                # A dropped fruit costs no time and no life, only the points it
                # was worth and the streak — and that survived the lives coming
                # back, because it is what the lives are *for*. If a drop cost
                # one, an awkward fruit would be better ignored than attempted:
                # a failed swing would end the round sooner than not swinging
                # at all, and the safe play would be to stand still and wait
                # for something easy. Lives are spent on bombs, which are the
                # one thing on the screen a player chooses to touch.
                #
                # A bomb reaching the floor is still a *good* outcome — the
                # player correctly left it alone — and does not break a streak.
                if not item.is_bomb:
                    self.missed_total += 1
                    self.streak = 0
                    self.events.append({"name": "miss", "x": round(item.x, 1)})
                continue
            if item.gone:
                continue
            kept.append(item)
        self.fruit = kept

    # ── publishing ───────────────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        """Seconds of play so far, from the clock rather than the wall.

        `duration - time_left`, not `now - started_at`, so it does not count
        paused time and is not thrown off by a stalled frame the step clamp
        shortened. It is what the spawner ramps on, so the difficulty follows
        the seconds actually played.

        It used to also carry the seconds a bomb took away, which was the
        interesting half of this docstring and is gone with the time penalty.
        """
        return max(0.0, self.duration - self.time_left)

    def snapshot(self, now: float) -> dict:
        """What the page draws. Small enough to send thirty times a second.

        `time_left` is rounded to two places rather than one now that the page
        shows minutes and seconds: at one decimal the extrapolated clock and
        the authoritative one can disagree by enough to flicker the last digit
        of `1:07` back and forth on the boundary.
        """
        return {
            "state": self.state.value,
            "phase": self.phase.value,
            "score": self.score,
            "best": self.best,
            # Both, so the HUD can draw three hearts with one of them spent
            # without knowing how many a round starts with. The page never
            # hard-codes `MAX_LIVES`; it draws whatever it is told.
            "lives": self.lives,
            "max_lives": MAX_LIVES,
            "time_left": round(self.time_left, 2),
            "duration": round(self.duration, 1),
            "streak": self.streak,
            "combo": self.combo,
            # The ice cube's remaining half speed, and the factor itself. Both,
            # because the page extrapolates fruit between snapshots with the
            # same closed form this module integrates (see `drawFruit`) and it
            # cannot do that correctly without knowing the rate time is running
            # at — a page that drew at full speed through a slow would run
            # every fruit ahead of where it is and snap it back thirty times a
            # second. Sending the seconds left as well as the factor lets it
            # split a frame that straddles the end of the effect.
            "slow": round(self.slow_left, 2),
            "slow_factor": SLOW_FACTOR,
            "fruit": [item.as_dict() for item in self.fruit],
            # Absent rather than null when there is no Ultimate on screen, so
            # the page's test is `if (game.dragon)` and never has to know the
            # difference between "not yet" and "finished".
            **({"dragon": self.dragon.as_dict()} if self.dragon else {}),
            "slashes": self.slashes[-6:],
            "elapsed": round(self.elapsed, 1),
            "stats": {
                "sliced": self.sliced_total,
                "missed": self.missed_total,
                "bombs": self.bombs_hit,
                "hearts": self.hearts_hit,
                "lives": self.lives,
                "best_streak": self.best_streak,
                "best_combo": self.best_combo,
                "ultimate_hits": self.ultimate_hits,
                "ultimate_points": self.ultimate_points,
                "ultimate_done": self.ultimate_done,
            },
        }

    def take_events(self) -> list[dict]:
        """Drain the event queue. Called once per publish, by one reader.

        Capped on the way out. A page that stopped reading — a reload, a
        backgrounded tab — must not be able to make this grow for the rest of
        the round, and the newest are the ones still worth reacting to: a
        splash for a fruit cut four seconds ago would land somewhere the fruit
        no longer is. Thirty is comfortably more than one pose frame can
        generate, so nothing is ever dropped from a reader that is keeping up.
        """
        events, self.events = self.events[-30:], []
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
        """Remember `score` if it beats what is there. True if it did.

        A nameless game is refused rather than filed under the empty string.
        There is one caller that can reach here without a name — a pose frame
        arriving while the manager is being torn down — and it is fixed at
        source, but this is the file that has to stay clean and the guard is
        one line. It also keeps `""` out of `best()`, which iterates nothing
        and would otherwise report a high score for a game nobody named.
        """
        if not game or score <= self.best(game):
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
