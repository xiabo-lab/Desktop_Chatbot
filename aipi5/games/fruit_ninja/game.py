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

#: How long one round lasts. Section 27 originally asked for three lives; the
#: game is a fixed clock and the score at the end of it.
#:
#: **The change is not cosmetic — it changes what the game rewards.** With
#: lives, a dropped fruit ended the game a third sooner, so the safe play was
#: to ignore anything awkward and wait for an easy one. With a clock, doing
#: nothing costs exactly as much as trying and missing, so there is never a
#: reason not to swing. It also makes every round the same length, which is
#: what makes two scores comparable and a high score worth having.
#:
#: Two minutes as of the Ultimate upgrade, up from one. The extra minute is not
#: more of the same: it is what makes room for a round to have a *shape* — a
#: minute and a half of ordinary play that ramps, then a climax, then a short
#: coda. A sixty-second round with a ten-second Ultimate in it would have been
#: a sixth of the game spent on one fruit.
ROUND_SECONDS = 120.0

#: Where the Ultimate sits in the round, as seconds remaining. Section 16.
#:
#: Expressed as time *left* rather than time elapsed, and that is load-bearing
#: rather than a style: a bomb takes five seconds off the clock, so elapsed and
#: remaining are not two views of one number. A player who sliced three bombs
#: still gets their Ultimate with twenty seconds on the clock, which is what
#: the screen promised them, instead of fifteen seconds after the warning has
#: already gone.
ULTIMATE_WARNING_AT = 22.0
ULTIMATE_START_AT = 20.0

#: What slicing a bomb costs, in seconds off the clock. Section 26's bomb
#: cannot cost a life any more, and it has to cost *something* or it is a free
#: extra target.
#:
#: Five, chosen against the clock rather than in the abstract: it is a twelfth
#: of the round, which is enough to be worth avoiding and not enough to end a
#: good run. A score penalty was the alternative and is worse — it can make the
#: number on screen go backwards, which reads as the game taking something away
#: rather than as a mistake costing time.
BOMB_PENALTY_S = 5.0

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
    #: Seconds left, recomputed every tick. Held rather than derived from
    #: `started_at` because bombs take time off it, so it is genuinely its own
    #: quantity and not a function of the wall clock.
    time_left: float = ROUND_SECONDS
    score: int = 0
    best: int = 0
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
    #: Guards the once-per-round guarantee against a clock that can be pushed
    #: back over a threshold. A bomb takes five seconds off, so `time_left`
    #: crosses 20 exactly once — but nothing in the rules *promises* that, and
    #: a second dragon fruit in one round would be a much worse bug than a
    #: redundant boolean.
    ultimate_spawned: bool = False

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
        self.combo = self.best_combo = 0
        self._combo_until = 0.0

        self.phase = Phase.NORMAL
        self.dragon = None
        self.ultimate_hits = self.ultimate_points = 0
        self.ultimate_done = False
        self.ultimate_spawned = False

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
        # happened and the page does not play the game-over phrase over the
        # top of the Ultimate's own ending.
        self._finalise_ultimate(now)
        self.state = State.OVER
        self.ended_at = now
        self.events.append({"name": "game-over"})
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

        for item in self.fruit:
            if not item.sliced:
                item.advance(dt)
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
        if self.time_left > self.spawner.dead_air and not self.in_ultimate:
            for spawned in self.spawner.due(now, self.elapsed):
                self.fruit.append(spawned)

        if self.time_left <= 0:
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
        return self.duration >= ULTIMATE_WARNING_AT + ultimate.DURATION_S

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
        phase — a two-second warning and a `MAX_STEP_S` of 0.1 s means it takes
        twenty stalled frames, but the version that cannot go wrong costs
        nothing over the version that can.
        """
        previous = self.phase
        left = self.time_left

        if not self.has_ultimate:
            return
        if left > ULTIMATE_WARNING_AT:
            wanted = Phase.NORMAL
        elif left > ULTIMATE_START_AT:
            wanted = Phase.WARNING
        elif self.dragon is not None and not self.dragon.expired:
            wanted = Phase.ULTIMATE
        elif not self.ultimate_spawned:
            # The clock is inside the Ultimate window and no dragon fruit has
            # ever been made. This is the frame it appears on.
            wanted = Phase.ULTIMATE
        else:
            wanted = Phase.FINAL

        if wanted is previous:
            return
        self.phase = wanted

        if wanted is Phase.WARNING:
            # Section 17. The sound and the banner, and nothing else — the
            # player keeps full control through the warning.
            self.events.append({"name": "ultimate-warning"})
            log.info("Game: ultimate warning")
        elif wanted is Phase.ULTIMATE:
            self.dragon = UltimateDragon(born_at=now)
            self.ultimate_spawned = True
            self.events.append({"name": "ultimate-spawn"})
            log.info("Game: ultimate dragon fruit spawned")
        elif wanted is Phase.FINAL:
            self._finalise_ultimate(now)

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
            # Section 26, in the timed game: a bomb costs seconds rather than a
            # life, and breaks the streak. It does not subtract score, because
            # a number that goes backwards reads as the game taking something
            # away rather than as a mistake costing time — and the clock is
            # already the thing the player is watching.
            self.bombs_hit += 1
            self.time_left = max(0.0, self.time_left - BOMB_PENALTY_S)
            self.streak = 0
            self.events.append({"name": "bomb", "x": round(item.x, 1),
                                "y": round(item.y, 1)})
            log.info("Game: bomb sliced — %.0fs off the clock, %.0fs left",
                     BOMB_PENALTY_S, self.time_left)
            return

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
                # A dropped fruit costs no time, only the points it was worth
                # and the streak. That is the whole point of a timed round:
                # with lives, an awkward fruit was better ignored than
                # attempted, because a failed swing ended the game a third
                # sooner. On a clock, doing nothing costs exactly what trying
                # and missing costs, so there is never a reason not to swing.
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
        paused time and *does* count the seconds a bomb took away. It is what
        the spawner ramps on, which means a player who slices a bomb gets the
        difficulty of the time they have used rather than the time they have
        sat there — the penalty is the lost clock, not a harder game.
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
            "time_left": round(self.time_left, 2),
            "duration": round(self.duration, 1),
            "streak": self.streak,
            "combo": self.combo,
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
