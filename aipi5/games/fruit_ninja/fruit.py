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

import random
from dataclasses import dataclass, field

#: The play area, matching `display.width` x `display.height`.
WIDTH, HEIGHT = 1280, 800

#: Downward acceleration, px/s². Chosen with the launch speeds below to give a
#: 2.2-2.7 s arc that peaks near the top of the screen: long enough to line a
#: swing up, short enough that the screen does not fill with fruit.
GRAVITY = 1000.0

#: Fruit are launched from just off the bottom edge so they appear to be thrown
#: into view rather than materialising at it.
SPAWN_Y = HEIGHT + 60

#: How far in from the sides a fruit may be launched. Fruit thrown from the
#: very corner spend their whole arc leaving the screen.
SPAWN_MARGIN = 160

#: Launch speeds, px/s. The vertical range is what makes some fruit reach the
#: top and others peak halfway, which is most of the variety in the game.
LAUNCH_VY = (-1350.0, -1080.0)
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
    FruitKind("watermelon", "#2e8b3d", "#f2536a", 60, 10, "🍉",
              shape="melon", juice="#ff3b5c", wetness=1.55, sound="heavy",
              weight=1.0, launch=0.94, spin=0.7),
    FruitKind("apple", "#c62828", "#ffe9c9", 44, 10, "🍎",
              shape="round", juice="#e53935", wetness=1.0, sound="crisp",
              weight=1.5, launch=1.0, spin=1.0),
    FruitKind("orange", "#ef6c1a", "#ffb74d", 45, 10, "🍊",
              shape="citrus", juice="#ff9420", wetness=1.25, sound="juicy",
              weight=1.4, launch=1.0, spin=0.9),
    FruitKind("lime", "#5fae2e", "#d6f57a", 38, 15, "🍈",
              shape="citrus", juice="#7ed321", wetness=1.15, sound="sharp",
              weight=1.1, launch=1.06, spin=1.1),
    FruitKind("banana", "#e9c229", "#fff3c4", 48, 10, "🍌",
              shape="banana", juice="#ffe066", wetness=0.8, sound="soft",
              weight=1.2, launch=0.98, spin=1.35),
    FruitKind("pearl", "#dfe6ef", "#ffffff", 34, 20, "🫧",
              shape="pearl", juice="#cfe8ff", wetness=0.55, sound="polished",
              weight=0.6, launch=1.1, spin=1.2),
    FruitKind("grape", "#7b3fa0", "#c9a0e0", 32, 20, "🍇",
              shape="grape", juice="#9b51e0", wetness=0.9, sound="pop",
              weight=1.0, launch=1.12, spin=1.4),
    FruitKind("strawberry", "#d81b52", "#ff8fa8", 35, 15, "🍓",
              shape="strawberry", juice="#ff4d7d", wetness=1.05, sound="soft",
              weight=1.2, launch=1.05, spin=1.15),
    FruitKind("kiwi", "#8d6e3a", "#a5d84a", 38, 15, "🥝",
              shape="kiwi", juice="#8bc34a", wetness=1.1, sound="crisp",
              weight=1.0, launch=1.02, spin=1.0),
    FruitKind("dragon", "#e0399b", "#fff0f7", 48, 25, "🐉",
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
BOMB = FruitKind("bomb", "#22262b", "#ff7043", 42, 0, "💣",
                 shape="bomb", juice="#ff7043", wetness=1.0, sound="bomb")

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
        # Sideways drift takes a fruit out of play permanently too, and a
        # generous margin means one that is briefly off the edge at the top of
        # its arc is not deleted mid-flight.
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
    #: **Both divided by three, for three times the fruit.** The rate is the
    #: lever rather than the number thrown per throw: the doubles and triples
    #: below are chances *per throw*, so tripling the throws triples the count
    #: exactly, everywhere in the round, without touching the ramp's shape or
    #: making the clumps three times bigger. Nine fruit arriving together would
    #: be a different game; the same nine spread across the second they were
    #: always going to occupy is this one, three times as busy.
    #:
    #: The ratio between the two is kept at a third, because that is what makes
    #: the closing stretch read as a climb rather than as a constant.
    interval: float = 0.383
    floor: float = 0.127
    #: Seconds of play by which the floor is reached. Chosen against the
    #: Ultimate rather than against the round: full tilt should arrive shortly
    #: before the warning, so the player is at their busiest when the screen
    #: clears for the dragon fruit.
    ramp_over: float = 85.0
    #: Chance a given throw is a bomb, once bombs start appearing.
    bomb_chance: float = 0.14
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
    #: No new fruit in the last moment of a round — see `Session.tick`. A fruit
    #: launched with less than this left cannot be reached before the whistle.
    dead_air: float = 1.4

    _next_at: float = 0.0
    _counter: int = field(default=0, repr=False)
    _random: random.Random = field(default_factory=random.Random, repr=False)

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

        bomb = (elapsed >= self.bomb_after and rng.random() < self.bomb_chance)
        kind = BOMB if bomb else self._pick()

        x = rng.uniform(SPAWN_MARGIN, WIDTH - SPAWN_MARGIN)
        # Scaled per kind, so a grape is flicked and a watermelon is lobbed.
        # Only the vertical component is scaled: `launch` is about how high it
        # goes and therefore how long there is to reach it, and scaling the
        # sideways drift too would make the fast fruit also the ones that leave
        # the screen, which is a different and worse kind of difficulty.
        vy = rng.uniform(*LAUNCH_VY) * kind.launch
        vx = rng.uniform(*LAUNCH_VX)
        # Nudge fruit launched near an edge back towards the middle, so they
        # arc into the play area rather than straight out of it.
        if x < WIDTH * 0.25:
            vx = abs(vx)
        elif x > WIDTH * 0.75:
            vx = -abs(vx)

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
