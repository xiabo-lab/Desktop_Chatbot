"""Did that hand go through that fruit.

Section 16 is emphatic about the shape of this and it is worth restating why.
The obvious test — is the wrist inside the fruit right now — fails constantly
at 30 Hz. A hand moving at a very ordinary 3 m/s covers about 100 mm between
pose frames, which on this screen is roughly 250 px, and a watermelon is 110 px
across. **The hand is simply never inside the fruit on the frame the player
sliced it.** A player who swings hard and connects perfectly gets nothing, and
swinging harder makes it worse, which is the least forgivable failure a motion
game can have.

So the test is the *path*: the segment from where the hand was on the previous
pose frame to where it is now, against the fruit. And because the fruit is
moving too — a watermelon at the top of its arc is doing 400 px/s, and near the
bottom nearer 1200 — the honest test is against the fruit's path as well.

Doing both is no harder than doing one. Work in the fruit's frame of reference:
subtract the fruit's movement from the hand's, and a moving circle becomes a
stationary one at the origin. What is left is the classic segment-versus-circle
problem, which has a closed form and no iteration.

Pure arithmetic, no numpy, no game objects — it takes numbers and returns
numbers. That is what lets the awkward cases be tested directly: the slash that
passes clean through with neither endpoint inside, the tangent, the hand that
starts inside, the zero-length segment of a hand that has not moved.
"""

from __future__ import annotations

import math

#: Half the width of the blade, in screen pixels, added to a target's radius.
#:
#: **The blade used to be a line with no thickness**, and the drawn one was
#: 18 px across at its head — so a swipe whose bright centre passed a hair to
#: one side of a grape scored nothing, which reads as the game not registering
#: a hit that plainly happened. This is what makes the thing being tested the
#: same shape as the thing being drawn: a capsule of this half-width swept
#: along the hand's path, which is exactly the tapered streak in `drawTrail`.
#:
#: 54 px, because the trail's bloom is now 108 px across at its widest and half
#: of that is its reach from the centre line. Moving the two together is the
#: point — a collision radius the player cannot see is a difficulty setting they
#: cannot learn, and one the player *can* see but which does not cut is worse
#: still, because it looks like broken tracking rather than like a near miss.
#:
#: **Doubled from 27 along with the drawn blade.** The blade is measured
#: against the bloom rather than against the bright core inside it, and that is
#: deliberate: the bloom is what the eye reads as the extent of the thing, so
#: it is what the player aims by. A reach set to the core instead would be a
#: blade that visibly overlaps a fruit and does not cut it.
#:
#: This makes fruit easier to hit and, by `game._blade_reach`, does **not**
#: make bombs easier to hit — a hazard is still judged by its own edge. That
#: asymmetry is the whole reason the widening is safe to do: it can only give.
BLADE_HALF_WIDTH = 54.0


def segment_hits_circle(x1: float, y1: float, x2: float, y2: float,
                        cx: float, cy: float, radius: float) -> bool:
    """Does the segment (x1,y1)-(x2,y2) come within `radius` of (cx,cy)?

    The whole point of the module in one function: **neither endpoint needs to
    be inside the circle.** A slash that passes clean through is a hit.
    """
    return closest_distance(x1, y1, x2, y2, cx, cy) <= radius


def closest_distance(x1: float, y1: float, x2: float, y2: float,
                     cx: float, cy: float) -> float:
    """Shortest distance from a point to a line *segment*.

    Segment, not line — that distinction is the whole correctness of this. The
    infinite line through a slash across the top of the screen passes near a
    fruit at the bottom; the segment does not. Using the line would slice fruit
    the player's hand never went near, in a way that looks like the game
    cheating for you.
    """
    dx, dy = x2 - x1, y2 - y1
    length_squared = dx * dx + dy * dy

    if length_squared <= 1e-12:
        # A hand that did not move. Degenerate to a point test rather than
        # dividing by zero — this happens on every frame a hand is coasting
        # (see `pose_filter.HandFilter._coast`), so it is the common case
        # rather than an edge case.
        return math.hypot(cx - x1, cy - y1)

    # Projection of the centre onto the segment, clamped to its ends.
    t = ((cx - x1) * dx + (cy - y1) * dy) / length_squared
    t = min(1.0, max(0.0, t))
    return math.hypot(cx - (x1 + t * dx), cy - (y1 + t * dy))


def slash_hits_fruit(hand_from: tuple[float, float],
                     hand_to: tuple[float, float],
                     fruit_from: tuple[float, float],
                     fruit_to: tuple[float, float],
                     radius: float) -> bool:
    """Did a hand's path cross a fruit's path, over the same interval?

    Both are moving, so the test is done in the fruit's frame of reference:
    subtract the fruit's displacement from the hand's and the moving circle
    becomes a stationary one at the origin. Exact for the linear motion both
    have over one frame, and no more expensive than ignoring the fruit's
    movement would be.

    Ignoring it is not good enough, incidentally, and the case that proves it
    is the most satisfying shot in the game: a fruit rising fast into a hand
    swinging down across it. Treating the fruit as stationary at either end of
    the interval can miss a hit that plainly happened, because at neither
    instant were the two in the same place — only in between.
    """
    hx1, hy1 = hand_from
    hx2, hy2 = hand_to
    fx1, fy1 = fruit_from
    fx2, fy2 = fruit_to

    # Relative to the fruit's start, with the fruit's own motion removed from
    # the hand's. The circle is then fixed at the origin for the interval.
    rx1, ry1 = hx1 - fx1, hy1 - fy1
    rx2, ry2 = hx2 - fx2, hy2 - fy2
    return segment_hits_circle(rx1, ry1, rx2, ry2, 0.0, 0.0, radius)


def slash_angle(hand_from: tuple[float, float],
                hand_to: tuple[float, float]) -> float:
    """Direction of the slash in radians, for drawing the cut and the halves.

    Purely cosmetic — the two halves of a sliced fruit fly apart perpendicular
    to this — so a hand that did not move gets 0 rather than an error.
    """
    dx = hand_to[0] - hand_from[0]
    dy = hand_to[1] - hand_from[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.atan2(dy, dx)
