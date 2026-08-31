"""What gets thrown, how fast, and how often.

Screen pixels throughout — 1280x800, the panel's own coordinates — rather than
the normalised 0-1 the pose service speaks. Hand positions are converted once,
on the way in (`geometry.to_screen`), and after that everything here is in the
space a person is actually looking at. The alternative was tempting and wrong:
the screen is 16:10, so a circle in normalised coordinates is an ellipse on
glass, and a "radius" would mean two different distances depending on which way
the hand was moving.

**No sprites and no sound files are shipped.** Section 10: the original Hailo
community project is MIT and its fruit are drawn as coloured shapes, which is
where the colours below come from; nothing from the commercial game is used or
imitated. Each fruit is a colour, a radius and a name, and the browser draws
it. That keeps the repository free of artwork with a licence to argue about,
and it is genuinely what the reference did too.

**That decision survived the ten-fruit expansion, and the reason is the UI
server.** `aipi5/ui/server.py` serves exactly one file from a fixed path and
says so — there is no static directory, no MIME table and no path-traversal
defence, because it has never needed any. Shipping sprite sheets would mean
building all three, plus a preloader and about fifteen megabytes on an `scp`
deploy, to draw shapes a canvas draws for nothing. So each kind carries a
`shape` here and the page has a routine per shape: a banana is a crescent, a
watermelon a rind-and-flesh wedge, a grape a cluster. They are distinguishable
at a glance from across the room, which is the actual requirement, and the
repository still has no artwork to license. See `ASSET_LICENSES.md`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

#: The play area, matching `display.width` x `display.height`.
WIDTH, HEIGHT = 1280, 800

#: How much bigger every thrown object is than the radii the game shipped with.
#:
#: A multiplier rather than ten rewritten numbers, because the *relative* sizes
#: are the tuning that matters — a watermelon is an easy target and a grape is
#: not — and that relation is what a table of ten hand-scaled literals loses the
#: first time somebody adjusts one of them. It is also what `EDGE_MARGIN` and
#: `ceiling_speed` need: a bigger fruit has less room above it before its own
#: rim leaves the screen, so the launch bounds below are derived from the
#: radius rather than written down next to it.
SIZE = 1.5

#: Downward acceleration, px/s². Chosen with the launch speeds below to give a
#: 1.7-2.5 s arc that peaks just under the top of the screen: long enough to
#: line a swing up, short enough that the screen does not fill with fruit.
GRAVITY = 1000.0

#: Fruit are launched from just off the bottom edge so they appear to be thrown
#: into view rather than materialising at it.
SPAWN_Y = HEIGHT + 60

#: How far in from the sides a fruit may be launched. Fruit thrown from the
#: very corner spend their whole arc leaving the screen.
SPAWN_MARGIN = 160

#: How close a fruit's *rim* may come to the top or the side of the screen.
#:
#: Every thrown object stays inside the play area on three sides, and it is
#: arranged at launch rather than by clamping mid-flight: a fruit that is
#: stopped at an edge has visibly stopped obeying gravity, and worse, the page
#: extrapolates between snapshots with the same closed form the simulation
#: integrates (see `Fruit.advance`), so any rule the page does not also know
#: about is a rule the drawn fruit and the collidable one disagree over. A
#: launch the arithmetic below has already bounded needs no such rule.
EDGE_MARGIN = 24.0

#: Launch speed as a fraction of the fastest this fruit could be thrown without
#: its top leaving the screen — see `ceiling_speed`. A range rather than a
#: number because which fruit reach the top and which peak halfway is most of
#: the variety in the game, and a fraction rather than a speed because the
#: ceiling depends on the radius: a 90 px watermelon runs out of room sooner
#: than a 48 px grape, so one absolute range cannot be right for both.
LAUNCH_FRACTION = (0.74, 0.98)
LAUNCH_VX = (-170.0, 170.0)

#: Degrees per second. Cosmetic.
SPIN = (-180.0, 180.0)

#: A fruit below this has left the game. Not `HEIGHT` — a fruit whose centre is
#: exactly at the bottom edge is still half visible, and vanishing then looks
#: like it was deleted rather than dropped.
GONE_Y = HEIGHT + 120


@dataclass(frozen=True)
class FruitKind:
    """One type of fruit: what it is called, what it looks like, how it flies.

    Everything that makes one fruit feel unlike another is a field here rather
    than a branch somewhere else, so that adding an eleventh is one line and
    not a hunt through three files for the places that said `if watermelon`.
    """

    name: str
    #: Drawn by the page. Two colours so a slice can show a lighter inside.
    colour: str
    flesh: str
    radius: float
    points: int = 10
    #: What the page shows on the fruit when a colour-emoji font is present.
    #: The procedural shape underneath is what actually identifies it — see the
    #: module docstring — so a device without the font loses nothing but a
    #: garnish.
    glyph: str = ""
    #: Which drawing routine the page uses. One of the names in `SHAPES`.
    shape: str = "round"
    #: The juice. Separate from `flesh` because the two are genuinely
    #: different: a watermelon's flesh is pink and its juice sprays red, and a
    #: pearl's flesh is white while its juice reads as pale blue. This is the
    #: colour of the splat, the droplets and the score popup.
    juice: str = "#ff5a5a"
    #: How wet the splash is: a multiplier on particle count and splat size.
    #: A watermelon at 1.5 throws half again as much as an apple; a pearl at
    #: 0.55 barely marks the screen.
    wetness: float = 1.0
    #: Which synthesised voice the page plays. See `playSliceSound`.
    sound: str = "crisp"
    #: Relative chance of being thrown. Not uniform, because ten equally likely
    #: fruit make every round look like every other one: the common three are
    #: what the round is mostly made of and the rare ones are what makes a
    #: particular round memorable.
    weight: float = 1.0
    #: Multipliers on the launch velocity and the spin. A grape is flicked
    #: harder and spins faster than a watermelon is lobbed, which is most of
    #: what makes a screen with both on it read as having two kinds of target
    #: rather than two colours of one.
    launch: float = 1.0
    spin: float = 1.0


#: Section 6's ten. Radii differ enough to matter — a watermelon is an easy
#: target and a grape is not — and the points follow the difficulty rather than
#: the fruit, so the small fast ones are worth going for.
#:
#: Dragon fruit is deliberately the rarest of the nine ordinary fruit
#: (section 10: "less common during normal gameplay"), which is what keeps the
#: Ultimate at the end of the round feeling like the same fruit turning up
#: enormous rather than like an unrelated object.
KINDS: tuple[FruitKind, ...] = (
    FruitKind("watermelon", "#2e8b3d", "#f2536a", 60 * SIZE, 10, "🍉",
              shape="melon", juice="#ff3b5c", wetness=1.55, sound="heavy",
              weight=1.0, launch=0.94, spin=0.7),
    FruitKind("apple", "#c62828", "#ffe9c9", 44 * SIZE, 10, "🍎",
              shape="round", juice="#e53935", wetness=1.0, sound="crisp",
              weight=1.5, launch=1.0, spin=1.0),
    FruitKind("orange", "#ef6c1a", "#ffb74d", 45 * SIZE, 10, "🍊",
              shape="citrus", juice="#ff9420", wetness=1.25, sound="juicy",
              weight=1.4, launch=1.0, spin=0.9),
    FruitKind("lime", "#5fae2e", "#d6f57a", 38 * SIZE, 15, "🍈",
              shape="citrus", juice="#7ed321", wetness=1.15, sound="sharp",
              weight=1.1, launch=1.06, spin=1.1),
    FruitKind("banana", "#e9c229", "#fff3c4", 48 * SIZE, 10, "🍌",
              shape="banana", juice="#ffe066", wetness=0.8, sound="soft",
              weight=1.2, launch=0.98, spin=1.35),
    FruitKind("pearl", "#dfe6ef", "#ffffff", 34 * SIZE, 20, "🫧",
              shape="pearl", juice="#cfe8ff", wetness=0.55, sound="polished",
              weight=0.6, launch=1.1, spin=1.2),
    FruitKind("grape", "#7b3fa0", "#c9a0e0", 32 * SIZE, 20, "🍇",
              shape="grape", juice="#9b51e0", wetness=0.9, sound="pop",
              weight=1.0, launch=1.12, spin=1.4),
    FruitKind("strawberry", "#d81b52", "#ff8fa8", 35 * SIZE, 15, "🍓",
              shape="strawberry", juice="#ff4d7d", wetness=1.05, sound="soft",
              weight=1.2, launch=1.05, spin=1.15),
    FruitKind("kiwi", "#8d6e3a", "#a5d84a", 38 * SIZE, 15, "🥝",
              shape="kiwi", juice="#8bc34a", wetness=1.1, sound="crisp",
              weight=1.0, launch=1.02, spin=1.0),
    FruitKind("dragon", "#e0399b", "#fff0f7", 48 * SIZE, 25, "🐉",
              shape="dragon", juice="#ff2fa0", wetness=1.35, sound="exotic",
              weight=0.35, launch=0.97, spin=0.85),
)

#: By name, for the Ultimate — which is a dragon fruit and should not restate
#: its own colours — and for tests that want one particular kind.
BY_NAME: dict[str, FruitKind] = {kind.name: kind for kind in KINDS}

#: The dragon fruit the Ultimate is an enormous version of.
DRAGON = BY_NAME["dragon"]

#: Section 26. Deliberately last, deliberately its own kind rather than a flag
#: on a fruit, so that nothing about scoring or slicing has to ask "unless".
#:
#: Scaled with everything else. A hazard that stayed the old size while the
#: fruit around it grew by half would be the smallest object on the screen,
#: which is the opposite of what the bomb's whole visual treatment is for.
BOMB = FruitKind("bomb", "#22262b", "#ff7043", 42 * SIZE, 0, "💣",
                 shape="bomb", juice="#ff7043", wetness=1.0, sound="bomb")

#: The heart fruit: slicing it gives a life back. See `game.MAX_LIVES` and
#: `game.Session._cut`, which own the effect — this file only says what the
#: thing looks like and how it flies.
#:
#: **Drawn from paths like everything else, and deliberately not a copy of the
#: picture it was asked for.** The reference handed over was a heart container
#: from a well-known adventure game: a red heart inside an ornate gold frame.
#: The gold-framed red heart is the part that carries the meaning — it is what
#: says "this is a life" from across the room — and that is what the page draws
#: (`SHAPES.heart`). The particular scrollwork is not, and no pixels of it are
#: in this repository. See `ASSET_LICENSES.md`.
#:
#: Its own kind rather than an eleventh fruit, for the reason the ice cube is
#: one: how often it turns up has to be a rate the round can reason about — it
#: is pinned to the bomb rate, at a third of it — rather than a weight
#: competing with the fruit.
#:
#: It scores like a fruit as well as healing, for the same reason the ice does:
#: a reward that broke a streak would ask the player to choose between two
#: rewards.
#:
#: It barely spins. Every other object here tumbles, and a heart that tumbles
#: is a heart that spends half its arc upside down, which is the one
#: orientation this shape does not survive — it stops reading as a heart and
#: starts reading as an unfamiliar red blob. `spin=0.22` is a slow wobble that
#: still says the object is in flight.
HEART = FruitKind("heart", "#d81b2f", "#ff7b8c", 42 * SIZE, 10, "❤️",
                  shape="heart", juice="#ff2d55", wetness=1.1, sound="heart",
                  launch=1.0, spin=0.22)

#: The ice cube: slicing it halves every fruit's speed for five seconds. See
#: `game.SLOW_FACTOR` and `game.SLOW_SECONDS`, which own the effect — this file
#: only says what the thing looks like and how it flies.
#:
#: Its own kind rather than an eleventh entry in `KINDS`, for the same reason
#: the bomb is: how often it turns up has to be a rate the round can reason
#: about (`Spawner.ice_after`, `Spawner.ice_chance`) rather than a weight
#: competing with the fruit, and a power-up that could be the *only* thing
#: thrown in a quiet stretch is a different game.
#:
#: It still scores like a fruit — points, streak, combo — because a power-up
#: that breaks a streak to grant a bonus asks the player to choose between two
#: rewards, and there is nothing in this game that wants that choice.
ICE = FruitKind("ice", "#8ad8ff", "#eafaff", 38 * SIZE, 10, "🧊",
                shape="ice", juice="#bfefff", wetness=0.7, sound="ice",
                launch=1.04, spin=0.8)

#: Cumulative weights, built once. `random.choices` would do this on every
#: call; a fruit is chosen a few hundred times a round and the list never
#: changes, so it is built here instead.
_WEIGHTS: tuple[float, ...] = tuple(kind.weight for kind in KINDS)
_TOTAL_WEIGHT: float = sum(_WEIGHTS)


@dataclass
class Fruit:
    """One thrown object, mid-flight.

    Carries `previous_x`/`previous_y` for the same reason a hand does: the
    collision test needs where it *was* as well as where it is, because both
    ends of the problem are moving. See `collision.slash_hits_fruit`.
    """

    kind: FruitKind
    x: float
    y: float
    vx: float
    vy: float
    spin: float
    angle: float = 0.0
    previous_x: float = 0.0
    previous_y: float = 0.0
    sliced: bool = False
    #: When it was sliced, so the page can animate the halves for a moment
    #: before the object is dropped.
    sliced_at: float = 0.0
    slice_angle: float = 0.0
    #: Monotonically increasing, so the page can tell a new fruit from a
    #: recycled one and animate entrances without guessing.
    id: int = 0

    def __post_init__(self) -> None:
        if not self.previous_x and not self.previous_y:
            self.previous_x, self.previous_y = self.x, self.y

    @property
    def is_bomb(self) -> bool:
        return self.kind is BOMB

    @property
    def is_ice(self) -> bool:
        return self.kind is ICE

    @property
    def is_heart(self) -> bool:
        return self.kind is HEART

    def advance(self, dt: float) -> None:
        """Integrate one step. `dt` in seconds — never a frame count.

        Section 24: physics must not be tied to the frame rate. The reference
        implementation added a constant to the velocity every frame, which
        means the game plays differently on a fast display than a slow one and
        differently again when something else on the Pi steals a moment.
        """
        self.previous_x, self.previous_y = self.x, self.y
        # The closed form for constant acceleration, not Euler in either
        # flavour. `y += v*dt` alone — with the velocity updated before or
        # after — leaves an error of `g*t*dt/2`, which is *step-size
        # dependent*: measured here, a fruit simulated for one second at 30 Hz
        # ends 12 px away from the same fruit at 120 Hz. Small, invisible, and
        # exactly the frame-rate coupling section 24 asks to be rid of, so it
        # is not worth keeping for the sake of one saved multiply.
        self.x += self.vx * dt
        self.y += self.vy * dt + 0.5 * GRAVITY * dt * dt
        self.vy += GRAVITY * dt
        self.angle = (self.angle + self.spin * dt) % 360.0

    @property
    def missed(self) -> bool:
        """Fell off the bottom without being sliced. Section 27: a lost life.

        Only counts when it is on the way *down*. A fruit is launched from
        below the bottom edge, so without this every single spawn would be a
        missed fruit on the frame it appeared.
        """
        return not self.sliced and self.y > GONE_Y and self.vy > 0

    @property
    def gone(self) -> bool:
        """Off the screen entirely and not coming back."""
        if self.y > GONE_Y and self.vy > 0:
            return True
        # Sideways drift takes a fruit out of play permanently too. Nothing
        # `Spawner` throws can reach this any more — see `EDGE_MARGIN`, which
        # bounds the drift at launch — but a `Fruit` can be constructed
        # anywhere by a test or by a future mode, and the generous margin means
        # one that is briefly off the edge is not deleted mid-flight.
        return self.x < -300 or self.x > WIDTH + 300

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.name,
            "glyph": self.kind.glyph,
            "colour": self.kind.colour,
            "flesh": self.kind.flesh,
            "shape": self.kind.shape,
            # The juice travels with the fruit rather than being looked up on
            # the page, so that the ten colours live in exactly one file. A
            # table duplicated in JavaScript is a table that will one day
            # disagree with this one about what colour a kiwi is.
            "juice": self.kind.juice,
            "wet": self.kind.wetness,
            "sound": self.kind.sound,
            "r": self.kind.radius,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            # Sent so the page can extrapolate between server updates rather
            # than stepping. Section 24: render at 60, simulate at whatever.
            "vx": round(self.vx, 1),
            "vy": round(self.vy, 1),
            "angle": round(self.angle, 1),
            "sliced": self.sliced,
            "cut": round(self.slice_angle, 3) if self.sliced else 0.0,
        }


@dataclass
class Spawner:
    """Decides when to throw something, and what.

    **Difficulty ramps on seconds elapsed, not on score.** It used to be score,
    and the reasoning was that score means "this person is doing well" while
    time punishes somebody who is struggling. That reasoning belonged to a game
    with lives, where a bad run ended early; in a fixed minute it is wrong in
    both directions. Ramping on score gives the best player the busiest screen
    and leaves a beginner throwing one fruit at a time for a whole minute, and
    it makes two rounds incomparable — the same minute is a different game
    depending on how it went, so the high score measures the ramp as much as
    the player.

    On the clock, every round is the same shape: quiet enough at the start to
    find your hands, and thick enough at the end that the last fifteen seconds
    are where a score is really made.

    **The thresholds below were stretched by about 1.8x when the round went
    from sixty seconds to a hundred and twenty**, not doubled. Doubling them
    would have kept the shape of the old round and simply played it slower;
    leaving them alone would have hit full tilt at the forty-five second mark
    and then held it flat for over a minute, which stops reading as a climb.
    1.8x reaches the floor at around eighty-five seconds — just before the
    Ultimate warning at ninety-eight — so the busiest normal stretch is the one
    that runs straight into it.
    """

    #: Seconds between throws at the start of a round, and the floor it ramps
    #: down to by the end. The floor is a third of the opening interval, so the
    #: closing stretch throws roughly three times as much fruit.
    #:
    #: **Both doubled, for half the fruit**, now that every fruit is half again
    #: as big. The two changes are one change: at the old rate a screen of
    #: 1.5x fruit is a wall rather than a set of targets, and a swing that
    #: cannot miss is not a swing. Halving the *rate* rather than the clump
    #: sizes is the same lever the tripling used, in reverse — the doubles and
    #: triples below are chances *per throw*, so this halves the count exactly,
    #: everywhere in the round, without touching the ramp's shape or thinning
    #: out the flurries that are the most interesting thing in it.
    #:
    #: The ratio between the two is kept at a third, because that is what makes
    #: the closing stretch read as a climb rather than as a constant.
    interval: float = 0.766
    floor: float = 0.254
    #: Seconds of play by which the floor is reached. Chosen against the
    #: Ultimate rather than against the round: full tilt should arrive shortly
    #: before the warning, so the player is at their busiest when the screen
    #: clears for the dragon fruit.
    ramp_over: float = 85.0
    #: Chance a given throw is a bomb, once bombs start appearing.
    #:
    #: **Halved, from 0.14, when the bomb stopped costing seconds and started
    #: costing lives.** The two changes belong together. At one bomb in seven
    #: the hazard was priced as an interruption — five seconds, annoying,
    #: survivable, and a thing that could reasonably happen a dozen times in a
    #: round. It is now the only way to lose, and a losing condition that
    #: arrives that often stops being a mistake the player made and starts
    #: being weather.
    #:
    #: A proportion rather than a rate, still: this is a share of what is on
    #: screen, so it holds its meaning through the difficulty ramp instead of
    #: making the busy closing stretch disproportionately lethal.
    #:
    #: It does not touch the ice cube, whose 6% is rolled separately and so
    #: throws exactly as many cubes per round as before. The two are now within
    #: a hair of each other in frequency, which is a change in how the round
    #: reads — the ice used to be the rare one of the pair and now they are
    #: about as common as each other — but not a change in how much ice there
    #: is. See `ice_chance`.
    bomb_chance: float = 0.07
    #: No bombs for the opening seconds, so the round teaches the game before
    #: it starts punishing. Section 26: bombs must not hold up the basic thing
    #: working. Unstretched — ten seconds is how long it takes to find your
    #: hands, which does not depend on how long the round is.
    bomb_after: float = 10.0
    #: After this many seconds, throws sometimes come in pairs — and later, in
    #: threes. This is most of what "more fruit as time goes by" feels like:
    #: a shorter gap alone reads as a faster metronome, while two at once reads
    #: as the game getting harder.
    double_after: float = 25.0
    double_chance: float = 0.34
    triple_after: float = 62.0
    triple_chance: float = 0.22
    #: What share of the bombs thrown are heart fruit, and when the first one
    #: may appear.
    #:
    #: A share rather than a rate of its own, because the requirement is a
    #: ratio: one heart for every three bombs. Written as a rate it would be
    #: two numbers that have to be edited together, and the first change to
    #: `bomb_chance` would silently make the game easier or harder in a way
    #: nobody asked for. Written as a share it stays true by construction — see
    #: `heart_chance`, which does the one piece of arithmetic that turns it
    #: into a probability the sequential roll in `_make` actually honours.
    #:
    #: The bomb's gate rather than the ice cube's, because a heart thrown
    #: before any bomb could have been is a heart nobody needs: there is
    #: nothing to heal, so it is an ordinary ten-point fruit wearing the one
    #: symbol on the screen that is supposed to mean something.
    heart_share: float = 1.0 / 3.0
    heart_after: float = 10.0
    #: Chance a given throw is an ice cube, and how long the round waits before
    #: any are thrown.
    #:
    #: 6%, and left alone when the bomb's 14% was halved to 7%. The rate is
    #: about the *effect*, not about the bomb: five seconds of half speed is
    #: the strongest thing a player can be handed, and one arriving every few
    #: seconds would make the slow the normal state and full speed the
    #: surprise. At this rate and the halved throw rate above, a two-minute
    #: round throws roughly a dozen, which is a handful of moments rather than
    #: a mode — and that is as true now as it was when bombs were twice as
    #: common.
    #:
    #: It is still the rarer of the two once the roll order is accounted for
    #: (the ice is only offered the throws the bomb and the heart declined), but
    #: only just, where it used to be less than half as likely. That is a
    #: consequence of the bomb moving, not a decision about the ice.
    #:
    #: Eight seconds rather than the bomb's ten, so the first one lands while
    #: the round is still quiet enough to see what it did.
    ice_chance: float = 0.06
    ice_after: float = 8.0
    #: No new fruit in the last moment of a round — see `Session.tick`. A fruit
    #: launched with less than this left cannot be reached before the whistle.
    dead_air: float = 1.4

    _next_at: float = 0.0
    _counter: int = field(default=0, repr=False)
    _random: random.Random = field(default_factory=random.Random, repr=False)

    @property
    def heart_chance(self) -> float:
        """The probability the *heart* roll needs to hit the share above.

        Not `bomb_chance * heart_share`, and the difference is the whole reason
        this is a property. `_make` rolls in sequence — bomb first, and the
        heart is only offered the throws the bomb declined — so a heart rolled
        at one third of the bomb's rate would land on one third of *86%* of the
        throws and come out at 0.29 hearts per bomb rather than 0.33. Dividing
        by what is left restores it exactly, which is what makes
        `test_one_heart_for_every_three_bombs` a statement about the game
        rather than about the order two `if`s happen to be written in.
        """
        remaining = 1.0 - self.bomb_chance
        if remaining <= 0:
            return 0.0
        return self.bomb_chance * self.heart_share / remaining

    def seed(self, value: int) -> None:
        """Make a session reproducible. Used by the tests, never in play."""
        self._random = random.Random(value)

    def due(self, now: float, elapsed: float) -> list[Fruit]:
        """Whatever should be thrown at `now`. Usually nothing.

        `elapsed` is seconds of *play* — `Session.elapsed`, which excludes
        paused time and includes the seconds a bomb took away.
        """
        if self._next_at == 0.0:
            # First call. A short delay so the game does not open with a fruit
            # already halfway up the screen before the player has looked up.
            self._next_at = now + 0.6
            return []
        if now < self._next_at:
            return []

        progress = min(1.0, max(0.0, elapsed) / self.ramp_over)
        gap = self.interval + (self.floor - self.interval) * progress
        self._next_at = now + gap

        thrown = [self._make(elapsed)]
        if (elapsed >= self.double_after
                and self._random.random() < self.double_chance):
            thrown.append(self._make(elapsed))
        if (elapsed >= self.triple_after
                and self._random.random() < self.triple_chance):
            thrown.append(self._make(elapsed))
        return thrown

    def _pick(self) -> FruitKind:
        """One fruit, by weight. Section 10's "spawn probability" varies.

        Linear over ten entries rather than `bisect` over a cumulative list:
        ten comparisons a few hundred times a round is nothing, and the obvious
        version is the one that stays right when somebody adds an eleventh.
        """
        target = self._random.random() * _TOTAL_WEIGHT
        for kind, weight in zip(KINDS, _WEIGHTS):
            target -= weight
            if target <= 0:
                return kind
        return KINDS[-1]

    def _make(self, elapsed: float) -> Fruit:
        self._counter += 1
        rng = self._random

        # Bomb first, then the heart, then ice, then a fruit. Rolled in
        # sequence rather than from one number so each rate means what it says
        # on its own: adding the ice cube did not change how often a bomb is
        # thrown, and neither will the next thing that is not a fruit.
        #
        # The heart goes directly after the bomb because it is the only one of
        # the three whose rate is *defined against* another — see
        # `heart_chance`, which is written for exactly this position in the
        # chain. Moving it below the ice would change what it means.
        if elapsed >= self.bomb_after and rng.random() < self.bomb_chance:
            kind = BOMB
        elif elapsed >= self.heart_after and rng.random() < self.heart_chance:
            kind = HEART
        elif elapsed >= self.ice_after and rng.random() < self.ice_chance:
            kind = ICE
        else:
            kind = self._pick()

        radius = kind.radius
        x = rng.uniform(SPAWN_MARGIN, WIDTH - SPAWN_MARGIN)

        # Scaled per kind, so a grape is flicked and a watermelon is lobbed.
        # Only the vertical component is scaled: `launch` is about how high it
        # goes and therefore how long there is to reach it, and scaling the
        # sideways drift too would make the fast fruit also the ones that leave
        # the screen, which is a different and worse kind of difficulty.
        #
        # The scale is applied to the *fraction* of the ceiling rather than to
        # a speed, and the top of the band is capped at the ceiling itself.
        # Capping the fraction rather than the resulting speed is what keeps
        # the fast kinds from all being thrown at exactly the same speed: a
        # clamp applied afterwards piles every grape onto the ceiling and the
        # variety that `launch` exists to create disappears at precisely the
        # kinds it matters most for.
        ceiling = ceiling_speed(radius)
        low = LAUNCH_FRACTION[0] * kind.launch
        high = min(1.0, LAUNCH_FRACTION[1] * kind.launch)
        vy = -ceiling * rng.uniform(min(low, high), high)

        vx = rng.uniform(*LAUNCH_VX)
        # Nudge fruit launched near an edge back towards the middle, so they
        # arc into the play area rather than straight out of it.
        if x < WIDTH * 0.25:
            vx = abs(vx)
        elif x > WIDTH * 0.75:
            vx = -abs(vx)
        # And then hold it to whatever drift actually fits in the time this
        # fruit has left. The nudge above only fixes the *direction*; a fruit
        # launched 200 px from the edge at 170 px/s still leaves through the
        # side well before it comes down, which is the case this bounds.
        seconds = flight_seconds(vy, radius)
        room_right = max(0.0, WIDTH - EDGE_MARGIN - radius - x)
        room_left = max(0.0, x - EDGE_MARGIN - radius)
        vx = max(-room_left / seconds, min(room_right / seconds, vx))

        return Fruit(kind=kind, x=x, y=SPAWN_Y, vx=vx, vy=vy,
                     spin=rng.uniform(*SPIN) * kind.spin, id=self._counter)

    def reset(self) -> None:
        self._next_at = 0.0
        self._counter = 0


def apex_height(vy: float) -> float:
    """How high a fruit launched at `vy` will get. For tests and for tuning."""
    return (vy * vy) / (2 * GRAVITY)


def airborne_seconds(vy: float) -> float:
    """How long it stays up. Symmetric, so twice the time to apex."""
    return 2 * abs(vy) / GRAVITY


def ceiling_speed(radius: float) -> float:
    """The fastest a fruit of `radius` may be thrown and still stay on screen.

    The apex is `vy²/2g` above the launch point, so the room available is the
    distance from `SPAWN_Y` down to where the fruit's *top* would touch the
    margin — its own radius plus `EDGE_MARGIN` below the edge — and this is
    that inverted.

    Rounded down by construction: a fruit thrown at exactly this speed has its
    rim `EDGE_MARGIN` from the top for one instant and never crosses it.
    """
    room = max(0.0, SPAWN_Y - radius - EDGE_MARGIN)
    return math.sqrt(2 * GRAVITY * room)


def flight_seconds(vy: float, radius: float) -> float:
    """How long a fruit launched at `vy` is in play, up and back down.

    Not `airborne_seconds`: that is the time to return to the height it was
    thrown from, and a fruit keeps falling from `SPAWN_Y` to `GONE_Y` after
    that. The difference is about a tenth of a second and it is the tenth in
    which a fast sideways drift leaves the screen, which is exactly what the
    horizontal bound in `Spawner._make` is computed against.

    The positive root of `½gt² + vy·t - (GONE_Y - SPAWN_Y + radius) = 0`, with
    the radius included so the bound is on the fruit's rim rather than on its
    centre.
    """
    drop = GONE_Y - SPAWN_Y + radius
    speed = abs(vy)
    return (speed + math.sqrt(speed * speed + 2 * GRAVITY * drop)) / GRAVITY
