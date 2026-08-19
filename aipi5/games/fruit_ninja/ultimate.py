"""The Ultimate Dragon Fruit: the last twenty seconds of every round.

Its own module rather than a flag on `Fruit`, because it shares almost nothing
with a thrown fruit and putting it there would have added an "unless this one"
to every rule in the file. A thrown fruit is launched, arcs, is cut once and is
gone. This one is placed, hovers, is cut thirty times and then keeps going.
The only thing the two have in common is a radius.

**The hard part is not the counting, it is refusing hits that are not real.**
A wrist resting inside a fruit the size of a dinner plate is reported thirty
times a second, and a naive "is the hand inside it" test hands the player five
hundred points for holding still. Section 21 is about that, and it takes three
separate gates, each of which lets through something the others do not:

    movement    the hand must have travelled far enough this frame
    path        the segment must actually cross the fruit
    cooldown    per hand, so one hand cannot hit twice in three milliseconds

The movement gate alone is not enough: a hand jittering fast in place inside
the fruit passes it. The cooldown alone is not enough either — it would still
award eight hits a second to a hand held perfectly still. Together they mean
the only way to score is to keep swinging through it, which is the thing the
player is being asked to do.

**The cooldowns are per hand and there is no global one**, which is section 22
and is not a detail: the intended way to play this is to alternate, and a
global cooldown of the length one hand needs would halve what two hands can do.

Pure, like everything else in this game that has rules: time arrives as an
argument, there is no clock and no randomness, and a whole thirty-hit sequence
is a few lines in a test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aipi5.games.fruit_ninja import collision
from aipi5.games.fruit_ninja.fruit import DRAGON, HEIGHT, WIDTH

#: How long it stays. Section 16: twenty seconds left to ten.
DURATION_S = 10.0

#: Valid cuts to complete it. Section 20.
HITS_REQUIRED = 30

#: How big, against an ordinary dragon fruit. Section 19 asks for 1.5x-2x;
#: 1.8x is 86 px of radius, which is a target a person a metre and a half back
#: can hit repeatedly without aiming, and that is the point — the challenge is
#: how fast you can swing, not whether you can find it.
SCALE = 1.8

#: Where it sits, as a fraction of the play area. Slightly above centre because
#: the player's hands are at chest height and the screen's vertical middle is
#: below that: a target at 0.5 is one they have to reach *down* to, thirty
#: times, and shoulders tire.
CENTRE = (0.5, 0.44)

#: How far it drifts, in pixels, and how fast. Section 18 wants it to bob
#: rather than sit dead still, but it must stay easy to attack continuously —
#: so this is a slow lissajous a few tens of pixels wide, not a wander. The two
#: rates are deliberately not a simple ratio, which is what stops the path
#: closing into an obvious repeating loop.
BOB_X, BOB_Y = 34.0, 26.0
BOB_RATE_X, BOB_RATE_Y = 0.55, 0.83

#: How long each hand must wait between hits. Section 21 suggests 80-150 ms.
#:
#: 110 ms, from the physical limit rather than the middle of the range: a hand
#: swinging back and forth through a target manages about four passes a second
#: at a pace anybody can sustain, which is 250 ms per hand, and the shortest
#: real interval seen from a very fast player is around 140 ms. 110 leaves room
#: under that and still rejects the 33 ms repeat a stationary hand produces.
#: With two hands alternating, thirty hits needs about four seconds of the ten.
HAND_COOLDOWN_S = 0.11

#: How far a hand must move on a frame for the hit to count, in normalised
#: frame widths. Below `game.MIN_SLASH_SPEED` in effect — that gate is a speed
#: and this one is a distance, and both matter: a slow hand dragged right
#: through the fruit is a real cut, and a fast twitch that goes nowhere is not.
#:
#: 0.035 is about 45 px on this screen, comfortably above the two or three
#: pixels of jitter a still hand shows and well under the ~120 px a deliberate
#: jab covers between pose frames.
MIN_TRAVEL = 0.035

#: Where the bonus band stops being generous. See `hit_value`.
BONUS_TAPER_AT = 40

def hit_value(hits: int) -> int:
    """What the `hits`-th cut is worth. Section 23's bands.

    1-based: the first cut of the round is `hit_value(1)`. Rising rather than
    flat because thirty identical hits is a chore, and because the bands are
    what make the last ten before completion feel like the run-in they are.

    **The tail was added after the first real person played it**, and the
    number that forced it is worth recording: they landed **88 hits**, and the
    58 of those past thirty were worth +30 each, so the Ultimate alone scored
    2710 of a 3628-point round. Section 24 asks for extra cuts to keep paying
    and section 23 requires that the Ultimate not dominate the final score, and
    at 88 hits those two are in direct conflict — the plan's +30 assumed a
    player would manage thirty-five or forty, not ninety.

    Ten past the requirement still pay full price, which is the reward section
    24 is for. After that a cut is worth **ten: what an ordinary apple is
    worth**, on the reasoning that this is precisely what the player would be
    scoring if the dragon fruit were not on the screen. Nothing is taken away
    and there is no cap; the marginal cut simply stops being worth three fruit.
    """
    if hits <= 9:
        return 10
    if hits <= 19:
        return 15
    if hits <= 29:
        return 20
    if hits < BONUS_TAPER_AT:
        return 30
    return 10


#: The completion bonus. Section 23.
COMPLETE_BONUS = 500

#: Damage stages, as the hit count that starts each. Section 25 — the page
#: draws cracks and glow from this rather than being told what to draw.
STAGES = (0, 10, 20, HITS_REQUIRED)


def damage_stage(hits: int) -> int:
    """0 to 3, for the page's crack overlays."""
    stage = 0
    for index, threshold in enumerate(STAGES):
        if hits >= threshold:
            stage = index
    return stage


@dataclass
class UltimateDragon:
    """The multi-hit dragon fruit, from spawn to explosion.

    Created when the phase begins and dropped when it ends; nothing survives
    between rounds, which is section 40's reset requirement solved by not
    having any state to reset — `Session.start` throws the whole object away.
    """

    #: When it appeared, on the same monotonic clock everything else uses.
    born_at: float
    #: Seconds it has been alive. Advanced by `advance` from the session's
    #: clamped `dt`, not from a wall clock, so a stalled frame cannot make it
    #: expire early and a paused game freezes it.
    age: float = 0.0
    hits: int = 0
    #: Set the moment the thirtieth hit lands, so the page can show the banner
    #: once rather than every frame after.
    completed: bool = False
    completed_at: float = 0.0
    #: Points earned from this fruit alone, for the end-of-round breakdown.
    points: int = 0

    #: Where it is now, in screen pixels. Recomputed each step from `age`
    #: rather than integrated, so it cannot drift.
    x: float = field(default=WIDTH * CENTRE[0])
    y: float = field(default=HEIGHT * CENTRE[1])
    previous_x: float = field(default=WIDTH * CENTRE[0])
    previous_y: float = field(default=HEIGHT * CENTRE[1])

    #: Last time each hand scored, by hand name. Section 22: separate, so
    #: alternating hands are never throttled by each other.
    _cooldowns: dict[str, float] = field(default_factory=dict, repr=False)
    #: Set on any frame a hit landed, for the page's shake and flash. Drained
    #: by the session along with the sound events.
    struck_at: float = 0.0

    @property
    def radius(self) -> float:
        return DRAGON.radius * SCALE

    @property
    def time_left(self) -> float:
        return max(0.0, DURATION_S - self.age)

    @property
    def expired(self) -> bool:
        return self.age >= DURATION_S

    @property
    def stage(self) -> int:
        return damage_stage(self.hits)

    # ── movement ─────────────────────────────────────────────────────

    def advance(self, dt: float) -> None:
        """Bob. The only thing it does when nobody is hitting it.

        `previous_x`/`previous_y` are kept for the same reason a thrown fruit
        keeps them: the collision test is a moving segment against a moving
        circle, and this circle moves. Slowly — but the test is written once
        and costs nothing extra, so there is no reason to special-case a target
        that happens to be nearly still.
        """
        self.previous_x, self.previous_y = self.x, self.y
        self.age += dt
        phase = self.age
        self.x = WIDTH * CENTRE[0] + BOB_X * math.sin(phase * BOB_RATE_X * math.tau)
        self.y = HEIGHT * CENTRE[1] + BOB_Y * math.sin(phase * BOB_RATE_Y * math.tau)

    # ── being hit ────────────────────────────────────────────────────

    def attempt(self, hand, from_point, to_point, now: float) -> int:
        """Score one hand's swing against this fruit. Returns points, or 0.

        Every rejection is silent and cheap; this runs twice per pose frame for
        the whole ten seconds. The gates are ordered by cost, so the two that
        are a subtraction and a comparison run before the one that is a square
        root.
        """
        # Section 21's cooldown, per hand. Checked first because it rejects
        # most of the frames of a genuinely fast attack, and it is one dict
        # lookup.
        last = self._cooldowns.get(hand.name, 0.0)
        if last and now - last < HAND_COOLDOWN_S:
            return 0

        # Section 21's movement requirement. `hand.travel` is the distance
        # covered since the previous accepted sample, which is exactly the
        # segment about to be tested — so a hand that has not moved is rejected
        # here rather than by a path test that would happily report that a
        # zero-length segment at the centre of the fruit intersects it.
        if hand.travel < MIN_TRAVEL:
            return 0

        # The blade's own half-width, as everywhere else — the dragon fruit is
        # a target and gets the same swept capsule an apple does. It changes
        # far less here than it does for a grape: 27 px onto an 86 px radius is
        # a third more reach, against nearly double for the smallest fruit.
        if not collision.slash_hits_fruit(
                from_point, to_point,
                (self.previous_x, self.previous_y), (self.x, self.y),
                self.radius + collision.BLADE_HALF_WIDTH):
            return 0

        self._cooldowns[hand.name] = now
        self.hits += 1
        self.struck_at = now
        gained = hit_value(self.hits)

        if self.hits == HITS_REQUIRED:
            # Section 27: the banner and the bonus land on the hit itself, not
            # when the fruit is finally removed. The player has to know they
            # did it while they are still swinging, or the reward arrives after
            # the moment it was for.
            self.completed = True
            self.completed_at = now
            gained += COMPLETE_BONUS

        self.points += gained
        return gained

    # ── publishing ───────────────────────────────────────────────────

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "r": round(self.radius, 1),
            "hits": self.hits,
            "required": HITS_REQUIRED,
            "stage": self.stage,
            "completed": self.completed,
            "time_left": round(self.time_left, 2),
            "duration": DURATION_S,
            "colour": DRAGON.colour,
            "flesh": DRAGON.flesh,
            "juice": DRAGON.juice,
            "points": self.points,
        }
