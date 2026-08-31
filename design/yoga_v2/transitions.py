"""Human-scale routes between the approved Yoga Coach endpoint poses.

An endpoint pose can be anatomically correct while a straight interpolation
to the next endpoint is not.  A person does not morph from standing to lying
down: they fold or lunge, bring support to the mat, sit, and then roll down.
This module supplies those shared stations for every course edge.

The stations are themselves owner-approved poses.  No unreviewed skeleton is
introduced, and course authors do not need to hand-maintain hundreds of pose
pairs.  Opposite-side poses also pass through a neutral station so the limbs
do not sweep through one another while changing sides.
"""

from __future__ import annotations

from collections import deque

from design.yoga_v2 import catalog
from design.yoga_v2.catalog import (FOLD, INVERTED, KNEELING, LUNGE, PRONE,
                                    QUADRUPED, SEATED, STANDING, SUPINE, WIDE)


# Neighbours are ordered deliberately.  When two routes are equally short we
# prefer hands-to-mat/Tabletop over lowering through an unsupported squat.
_GRAPH: dict[str, tuple[str, ...]] = {
    STANDING: (FOLD, LUNGE, WIDE),
    WIDE: (FOLD, LUNGE, STANDING),
    FOLD: (STANDING, WIDE, LUNGE, QUADRUPED, INVERTED),
    LUNGE: (STANDING, WIDE, FOLD, KNEELING, INVERTED),
    INVERTED: (FOLD, LUNGE, QUADRUPED, PRONE, KNEELING),
    QUADRUPED: (FOLD, INVERTED, PRONE, KNEELING, SEATED),
    KNEELING: (LUNGE, INVERTED, QUADRUPED, SEATED),
    PRONE: (INVERTED, QUADRUPED),
    SEATED: (QUADRUPED, KNEELING, SUPINE),
    SUPINE: (SEATED,),
}


# Each station is a reviewed, drawable pose.  The lunge station is selected
# by side below; all other stations are bilateral or neutral.
_STATION: dict[str, str] = {
    STANDING: "mountain",
    WIDE: "star",
    FOLD: "forward_fold",
    LUNGE: "low_lunge_left",
    INVERTED: "downward_dog",
    QUADRUPED: "table",
    KNEELING: "child_pose",
    SEATED: "easy_seat",
    PRONE: "plank",
    SUPINE: "savasana",
}


# Entering one of these floor orientations through its neutral station avoids
# a final direct roll/twist into a specialised shape.  The reverse rule makes
# the body gather itself before leaving the floor orientation.
_ORIENTED_FLOOR = {QUADRUPED, KNEELING, SEATED, PRONE, SUPINE}


# Switching left/right inside a family must unload the active side first.
# Lunges return all the way to Mountain; kneeling side changes use Tabletop.
_SIDE_NEUTRAL: dict[str, str] = {
    STANDING: "mountain",
    WIDE: "star",
    FOLD: "forward_fold",
    LUNGE: "mountain",
    INVERTED: "downward_dog",
    QUADRUPED: "table",
    KNEELING: "table",
    SEATED: "easy_seat",
    PRONE: "plank",
    SUPINE: "savasana",
}


ACTION: dict[str, str] = {
    "mountain": "Return to Mountain and steady both feet",
    "star": "Return to a neutral wide stance",
    "forward_fold": "Fold forward and bring the hands toward the mat",
    "wide_leg_fold": "Fold from the wide stance and bring the hands down",
    "upward_salute": "Sweep both arms overhead before changing body height",
    "low_lunge_left": "Step the left foot into a low lunge",
    "low_lunge_right": "Step the right foot into a low lunge",
    "downward_dog": "Plant both palms and move through Downward Dog",
    "table": "Lower both knees and come to Tabletop",
    "child_pose": "Lower the hips into Child's Pose",
    "easy_seat": "Bring the hips down into Easy Seat",
    "staff": "Uncross the legs and extend both legs into Staff Pose",
    "plank": "Plant the palms and extend into Plank",
    "knees_to_chest": "Roll onto the back and gather the knees in",
    "savasana": "Lower the whole back into a neutral resting position",
}


def _family_route(start: str, finish: str) -> list[str]:
    """Return the shortest ordered family route, including both ends."""
    if start == finish:
        return [start]
    queue = deque([(start, [start])])
    seen = {start}
    while queue:
        family, route = queue.popleft()
        for neighbour in _GRAPH[family]:
            if neighbour in seen:
                continue
            next_route = [*route, neighbour]
            if neighbour == finish:
                return next_route
            seen.add(neighbour)
            queue.append((neighbour, next_route))
    raise ValueError(f"no transition route from {start!r} to {finish!r}")


def _station(family: str, source_id: str, target_id: str,
             role: str = "route") -> str:
    if family == LUNGE:
        target_side = catalog.get(target_id).side
        source_side = catalog.get(source_id).side
        side = (source_side if role == "source" else target_side
                if role == "target" else
                target_side if catalog.get(target_id).family == LUNGE
                else source_side)
        if side == "right":
            return "low_lunge_right"
    if family == KNEELING:
        kneeling_id = (source_id if catalog.get(source_id).family == KNEELING
                       else target_id)
        if kneeling_id.startswith(("low_lunge", "lizard", "half_split",
                                   "pigeon_prep")):
            return ("low_lunge_right" if catalog.get(kneeling_id).side == "right"
                    else "low_lunge_left")
    return _STATION[family]


def path_between(source_id: str, target_id: str) -> list[str]:
    """Return reviewed waypoint pose ids between two course endpoints."""
    if source_id == target_id:
        return []
    source = catalog.get(source_id)
    target = catalog.get(target_id)

    def finish(route: list[str]) -> list[str]:
        cleaned: list[str] = []
        for pose_id in route:
            if pose_id in (source_id, target_id) or pose_id in cleaned:
                continue
            cleaned.append(pose_id)

        # Tabletop is the shared supported-floor neutral. It makes both hands
        # and both knees carry the body before the coach changes between Dog,
        # Plank, prone, and kneeling orientations. Staff similarly unfolds the
        # legs before a supported floor pose turns into a seated one.
        supported = {INVERTED, QUADRUPED, KNEELING, PRONE}
        full = [source_id, *cleaned, target_id]
        bridged = [full[0]]
        for pose_id in full[1:]:
            previous = bridged[-1]
            a = catalog.get(previous).family
            b = catalog.get(pose_id).family
            additions: list[str] = []
            pair = {previous, pose_id}
            if pair == {"mountain", "forward_fold"}:
                additions = ["upward_salute"]
            elif ("mountain" in pair and any(
                    p.startswith(("side_bend_", "gentle_back_reach"))
                    for p in pair)):
                additions = ["upward_salute"]
            elif pair == {"star", "forward_fold"}:
                additions = ["wide_leg_fold"]
            elif pair == {"forward_fold", "table"}:
                additions = ["downward_dog"]
            elif ("forward_fold" in pair and any(
                    p.startswith("low_lunge_") for p in pair)):
                additions = ["downward_dog"]
            elif a == SUPINE and b == SUPINE and "savasana" not in pair:
                additions = ["savasana"]
            elif previous == "staff" and b == SUPINE and pose_id != "savasana":
                additions = ["savasana"]
            elif a == SUPINE and pose_id == "staff" and previous != "savasana":
                additions = ["savasana"]
            elif a != b and a in supported and b in supported \
                    and "table" not in (previous, pose_id):
                additions = ["table"]
            elif a != b and a in supported and b == SEATED:
                additions = ([] if previous == "table" and pose_id == "staff"
                             else ["staff"] if previous == "table"
                             else ["table", "staff"])
            elif a != b and a == SEATED and b in supported:
                additions = ([] if previous == "staff" and pose_id == "table"
                             else ["staff"] if pose_id == "table"
                             else ["staff", "table"])
            for addition in additions:
                if addition not in (bridged[-1], pose_id):
                    bridged.append(addition)
            bridged.append(pose_id)

        result: list[str] = []
        for pose_id in bridged[1:-1]:
            if pose_id not in (source_id, target_id) and pose_id not in result:
                result.append(pose_id)
        return result

    # A low kneeling lunge is the bridge between standing and the mat. Put the
    # hands down in a fold before lowering the back knee (and reverse it on the
    # way up) instead of sinking vertically through space.
    lunge_kneel = lambda pose_id: pose_id.startswith(
        ("low_lunge", "lizard", "half_split", "pigeon_prep"))
    upright = {STANDING, WIDE}
    floor = {QUADRUPED, KNEELING, SEATED, PRONE, SUPINE}
    fold = "wide_leg_fold" if source.family == WIDE else "forward_fold"
    rise_fold = "wide_leg_fold" if target.family == WIDE else "forward_fold"
    if source.family in upright and target.family in floor:
        start = (["mountain"] if source.family == STANDING
                 else ["star"])
        if target.family == QUADRUPED:
            route = [*start, fold, "table"]
        elif target.family == KNEELING:
            route = ([*start, fold, _station(KNEELING, source_id, target_id,
                                             "target")]
                     if lunge_kneel(target_id)
                     else [*start, fold, "table", "child_pose"])
        elif target.family == SEATED:
            route = [*start, fold, "table", "staff"]
        elif target.family == PRONE:
            route = [*start, fold, "table", "plank"]
        else:
            route = [*start, fold, "table", "staff",
                     "savasana"]
        return finish(route)
    if source.family in floor and target.family in upright:
        arrival = (["mountain"] if target.family == STANDING else ["star"])
        if source.family == QUADRUPED:
            route = ["table", rise_fold, *arrival]
        elif source.family == KNEELING:
            route = ([_station(KNEELING, source_id, target_id, "source"), rise_fold,
                      *arrival] if lunge_kneel(source_id)
                     else ["child_pose", "table", rise_fold, *arrival])
        elif source.family == SEATED:
            route = (["staff", "table", rise_fold, *arrival]
                     if source_id == "staff"
                     else ["easy_seat", "staff", "table", rise_fold,
                           *arrival])
        elif source.family == PRONE:
            route = ["plank", "table", rise_fold, *arrival]
        else:
            route = ["savasana", "staff", "table",
                     rise_fold, *arrival]
        return finish(route)

    # Every substantial change inside one family unloads first. This is more
    # conservative than treating two approved endpoints as proof that the
    # straight line between them is safe: elbows, thighs, and wrists can still
    # sweep through the torso along that line.
    if source.family == target.family:
        if source.family == SEATED:
            if "staff" in (source_id, target_id):
                return []
            neutral = "staff" if "easy_seat" in (source_id, target_id) \
                else "easy_seat"
        else:
            neutral = _SIDE_NEUTRAL[source.family]
        return finish([neutral])

    # Upward Salute already has both arms clear of the torso, so it is the
    # safe entry/exit for a fold. Returning to Mountain first would lower the
    # arms only to sweep them back past the body during the fold.
    if ({source_id, target_id} == {"upward_salute", "forward_fold"}):
        return []

    # A standing lunge changes which foot carries the body. Before moving to a
    # neutral or wide standing shape, put the hands down, bring the feet
    # together, and rise through Mountain. Reverse those actions on entry.
    other_upright = {STANDING, WIDE, FOLD}
    if source.family == LUNGE and target.family in other_upright:
        route = [_station(LUNGE, source_id, target_id, "source"),
                 "forward_fold"]
        if target.family == STANDING:
            route.append("mountain")
        elif target.family == WIDE:
            route.extend(("mountain", "star"))
        return finish(route)
    if target.family == LUNGE and source.family in other_upright:
        start = (["star", "mountain"] if source.family == WIDE else
                 ["mountain"] if source.family == STANDING else [])
        return finish([*start, "forward_fold",
                       _station(LUNGE, source_id, target_id, "target")])

    # From a high lunge to sitting or lying down, put both hands and the back
    # knee down in Tabletop rather than detouring through Child's Pose. This
    # keeps the thighs apart while the hips turn to sit. Reverse it on entry.
    if source.family == LUNGE and target.family in {SEATED, SUPINE}:
        route = [_station(LUNGE, source_id, target_id, "source"), "table",
                 "staff"]
        if target.family == SUPINE:
            route.append("savasana")
        return finish(route)
    if target.family == LUNGE and source.family in {SEATED, SUPINE}:
        route = (["savasana"] if source.family == SUPINE else [])
        route.extend(("staff", "table",
                      _station(LUNGE, source_id, target_id, "target")))
        return finish(route)

    if source.family == SEATED and target.family == SUPINE:
        route = ([] if source_id == "staff" else ["easy_seat", "staff"])
        route.append("savasana")
        return finish(route)
    if source.family == SUPINE and target.family == SEATED:
        route = ["savasana", "staff"]
        if target_id != "staff":
            route.append("easy_seat")
        return finish(route)

    supported_floor = {INVERTED, QUADRUPED, KNEELING, PRONE}
    if source.family in supported_floor and target.family == SUPINE:
        return finish([_station(source.family, source_id, target_id, "source"),
                       "table", "staff", "savasana"])
    if source.family == SUPINE and target.family in supported_floor:
        return finish(["savasana", "staff", "table",
                       _station(target.family, source_id, target_id, "target")])

    route = _family_route(source.family, target.family)
    if len(route) == 1:
        return []

    # Unload the source before changing family. A Warrior lowers into Low
    # Lunge; a balance returns to Mountain; a supported floor pose returns to
    # Tabletop/Plank/Easy Seat/Knees-to-Chest as appropriate.
    waypoints: list[str] = [
        _station(source.family, source_id, target_id, "source")]

    # The family route provides the main human actions: fold, lower the knees,
    # sit, and roll.  End families are endpoints, not intermediate stations.
    for family in route[1:-1]:
        waypoints.append(_station(family, source_id, target_id))

    # Enter the destination through its neutral station before the final pose.
    waypoints.append(_station(target.family, source_id, target_id, "target"))

    # Crossed seated legs unfold before the torso rolls to or rises from the
    # mat. Staff keeps both thighs parallel; Savasana is the neutral supine
    # station, so neither leg has to sweep vertically through the belly.
    if target.family == SUPINE and "savasana" in waypoints:
        waypoints.insert(waypoints.index("savasana"), "staff")
    if source.family == SUPINE and "savasana" in waypoints:
        waypoints.insert(waypoints.index("savasana") + 1, "staff")

    return finish(waypoints)


def action_between(source_id: str, target_id: str) -> list[str]:
    """Plain-language action cues for the generated waypoint route."""
    return [ACTION[pose_id] for pose_id in path_between(source_id, target_id)]
