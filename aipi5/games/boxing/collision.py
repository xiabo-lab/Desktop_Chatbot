"""Small, pure swept-collision helpers used by boxing and its tests."""

from __future__ import annotations

import math


def point_segment_distance(point: tuple[float, float],
                           start: tuple[float, float],
                           end: tuple[float, float]) -> float:
    """Shortest distance from ``point`` to the closed segment start--end."""
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def swept_circle_hit(start: tuple[float, float], end: tuple[float, float],
                     centre: tuple[float, float], radius: float) -> bool:
    """True when a moving point crosses a circular hit region.

    Testing the whole path instead of only ``end`` prevents a fast wrist from
    tunnelling through a target between two camera frames.
    """
    return point_segment_distance(centre, start, end) <= max(0.0, radius)
