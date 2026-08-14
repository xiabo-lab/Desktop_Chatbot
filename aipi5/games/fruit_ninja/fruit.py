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
    """One type of fruit: what it is called, what it looks like, how big."""

    name: str
    #: Drawn by the page. Two colours so a slice can show a lighter inside.
    colour: str
    flesh: str
    radius: float
    points: int = 10
    #: What the page shows on the fruit. Emoji rather than an image file, which
    #: is the whole of section 10's licensing problem solved by not having one.
    glyph: str = ""


#: Section 25's five, and no more for V1. Radii differ enough to matter: a
#: watermelon is an easy target worth the same as a strawberry that is not.
KINDS: tuple[FruitKind, ...] = (
    FruitKind("watermelon", "#2e8b3d", "#f2536a", 58, 10, "🍉"),
    FruitKind("apple", "#c62828", "#ffe9c9", 44, 10, "🍎"),
    FruitKind("orange", "#ef6c1a", "#ffb74d", 44, 10, "🍊"),
    FruitKind("banana", "#e9c229", "#fff3c4", 46, 10, "🍌"),
    FruitKind("strawberry", "#d81b52", "#ff8fa8", 34, 10, "🍓"),
)

#: Section 26. Deliberately last, deliberately its own kind rather than a flag
#: on a fruit, so that nothing about scoring or slicing has to ask "unless".
BOMB = FruitKind("bomb", "#22262b", "#ff7043", 42, 0, "💣")


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

    Difficulty is a function of score and nothing else — not of time, and not
    of lives. Score is what the player is being rewarded for and it is the only
    signal here that means "this person is doing well"; ramping on elapsed time
    instead punishes somebody who is struggling by speeding up while they miss.
    """

    #: Seconds between throws at the start, and the floor it ramps down to.
    interval: float = 1.15
    floor: float = 0.42
    #: Score at which the floor is reached.
    ramp_over: int = 900
    #: Chance a given throw is a bomb, once bombs start appearing.
    bomb_chance: float = 0.14
    #: No bombs until this score, so the first half-minute teaches the game
    #: before it starts punishing. Section 26: bombs must not hold up the
    #: basic thing working.
    bomb_after: int = 120
    #: Chance of throwing two at once, once the player is going well.
    double_after: int = 400
    double_chance: float = 0.3

    _next_at: float = 0.0
    _counter: int = field(default=0, repr=False)
    _random: random.Random = field(default_factory=random.Random, repr=False)

    def seed(self, value: int) -> None:
        """Make a session reproducible. Used by the tests, never in play."""
        self._random = random.Random(value)

    def due(self, now: float, score: int) -> list[Fruit]:
        """Whatever should be thrown at `now`. Usually nothing."""
        if self._next_at == 0.0:
            # First call. A short delay so the game does not open with a fruit
            # already halfway up the screen before the player has looked up.
            self._next_at = now + 0.6
            return []
        if now < self._next_at:
            return []

        progress = min(1.0, max(0, score) / self.ramp_over)
        gap = self.interval + (self.floor - self.interval) * progress
        self._next_at = now + gap

        thrown = [self._make(score)]
        if (score >= self.double_after
                and self._random.random() < self.double_chance):
            thrown.append(self._make(score))
        return thrown

    def _make(self, score: int) -> Fruit:
        self._counter += 1
        rng = self._random

        bomb = (score >= self.bomb_after and rng.random() < self.bomb_chance)
        kind = BOMB if bomb else rng.choice(KINDS)

        x = rng.uniform(SPAWN_MARGIN, WIDTH - SPAWN_MARGIN)
        vy = rng.uniform(*LAUNCH_VY)
        vx = rng.uniform(*LAUNCH_VX)
        # Nudge fruit launched near an edge back towards the middle, so they
        # arc into the play area rather than straight out of it.
        if x < WIDTH * 0.25:
            vx = abs(vx)
        elif x > WIDTH * 0.75:
            vx = -abs(vx)

        return Fruit(kind=kind, x=x, y=SPAWN_Y, vx=vx, vy=vy,
                     spin=rng.uniform(*SPIN), id=self._counter)

    def reset(self) -> None:
        self._next_at = 0.0
        self._counter = 0


def apex_height(vy: float) -> float:
    """How high a fruit launched at `vy` will get. For tests and for tuning."""
    return (vy * vy) / (2 * GRAVITY)


def airborne_seconds(vy: float) -> float:
    """How long it stays up. Symmetric, so twice the time to apex."""
    return 2 * abs(vy) / GRAVITY
