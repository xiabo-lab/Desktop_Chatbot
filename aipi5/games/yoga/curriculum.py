"""The 21 courses and the 91 poses, read from the file the coach is drawn from.

There is one copy of this data and both halves of the game read it. That is the
whole point of the module. The browser loads `rigdata.json` to *draw* the
coach; this loads the same bytes to run the class, so a pose the coach can show
and a pose the lesson can ask for are the same set by construction. The
alternative -- a Python list here and a JSON file there, kept in step by hand --
is the arrangement that produces a blank coach at minute thirteen on a device
with no console in front of it.

`scripts/build_yoga_v3_data.py` writes the file, out of `design/yoga_v2`. Run it
after touching the catalog or the courses.

What comes from here:

  GUIDED    the 59 poses with no bone table, as `Pose` objects with
            `scored = False`. They are demonstrated and never marked.
  COURSES   21 hand-timed classes, seven per level.

The 32 *scored* poses do not come from here. They stay authored in `poses.py`
with their tuned tolerances and their emphasis lists, because those are the
numbers a player is judged against and they were measured rather than written.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

#: The same file the browser fetches. Deliberately not a second copy.
DATA = (pathlib.Path(__file__).resolve().parents[3]
        / "aipi5" / "ui" / "web" / "assets" / "yoga" / "v3" / "rigdata.json")


def _load() -> dict:
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except FileNotFoundError:  # pragma: no cover - a broken deployment
        raise FileNotFoundError(
            f"the yoga curriculum is missing: {DATA}. Run "
            "`python scripts/build_yoga_v3_data.py`.") from None


_DATA = _load()

#: Every pose the curriculum names, whether it is scored or not.
CATALOG: dict[str, dict] = _DATA["catalog"]

#: The tables the coach is posed from, for the poses that have no scored table
#: in `poses.py`.
DEMONSTRATED: dict[str, dict] = _DATA["poses3d"]

#: How much a table promoted from the 3D library is loosened before it is used
#: to mark anybody.
#:
#: Five standing poses were frozen as scored and never had a *scoring* table
#: authored -- only a demonstration one. Rather than quietly drop them to
#: guided, the demonstration table is used, which is the right table by
#: definition: it is the shape the coach is showing. What it has not had is a
#: session in front of the camera deciding how close is close enough, so it
#: marks generously until it does. Being marked kindly on a pose is recoverable;
#: being marked harshly against a tolerance nobody checked is what makes people
#: stop doing the class.
UNTUNED_TOLERANCE = 1.25


@dataclass(frozen=True)
class CourseStep:
    pose_id: str
    hold_s: float
    segment: str
    band: str
    #: The reviewed move time for *this* step, not a flat per-level constant.
    #: A course is timed pose by pose -- standing to the floor is a longer
    #: journey than standing to standing -- and using an average instead put
    #: fourteen of the twenty-one classes under their fifteen-minute floor.
    transition_s: float = 0.0

    @property
    def scored(self) -> bool:
        return bool(CATALOG[self.pose_id]["scored"])


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    level: str
    steps: tuple[CourseStep, ...]

    @property
    def hold_seconds(self) -> float:
        return sum(step.hold_s for step in self.steps)

    @property
    def transition_seconds(self) -> float:
        return sum(step.transition_s for step in self.steps)

    @property
    def scored_steps(self) -> int:
        return sum(1 for step in self.steps if step.scored)

    @property
    def segments(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.segment not in seen:
                seen.append(step.segment)
        return tuple(seen)


COURSES: dict[str, Course] = {
    course_id: Course(
        id=course_id, name=body["name"], level=body["level"],
        steps=tuple(CourseStep(step["pose"], float(step["hold"]),
                               step["segment"], step.get("band", ""),
                               float(step.get("transition", 0.0)))
                    for step in body["steps"]))
    for course_id, body in sorted(_DATA["courses"].items())
}

#: Course ids by level, in the order they were authored.
BY_LEVEL: dict[str, tuple[str, ...]] = {
    level: tuple(c.id for c in COURSES.values() if c.level == level)
    for level in ("beginner", "intermediate", "advanced")
}


def promoted_poses(pose_class, scored_ids):
    """Scored poses whose table comes from the 3D library rather than `poses.py`.

    Only for catalog poses marked scored that `poses.py` has no table for. The
    coach and the scorer still read the same numbers, which is the invariant
    that matters; the numbers simply came from the demonstration side.
    """
    made = {}
    for pose_id, entry in CATALOG.items():
        if pose_id in scored_ids or not entry["scored"]:
            continue
        shown = DEMONSTRATED.get(pose_id)
        if not shown or shown.get("root") or shown.get("plane"):
            # A pose the coach only shows side-on or lying down has no frontal
            # table to promote, and the camera could not measure it anyway.
            continue
        made[pose_id] = pose_class(
            id=pose_id,
            name=entry["name"],
            sanskrit=entry.get("sanskrit", ""),
            instruction=entry.get("instruction", ""),
            cue=entry.get("cue", ""),
            level=int(entry.get("level", 1)),
            family=entry.get("family", ""),
            table={k: float(v) for k, v in shown["bones"].items()},
            side=entry.get("side", ""),
            tolerance_scale=UNTUNED_TOLERANCE,
            scored=True,
        )
    return made


def guided_poses(pose_class, scored_ids):
    """Build `Pose` objects for everything the catalog has and `poses.py` has not.

    They carry no bone table, and `Pose.targets` refuses rather than returning
    nonsense for them -- a guided pose that quietly scored against an empty
    table would mark every player perfect, which is worse than not scoring.
    """
    made = {}
    for pose_id, entry in CATALOG.items():
        if pose_id in scored_ids:
            continue
        made[pose_id] = pose_class(
            id=pose_id,
            name=entry["name"],
            sanskrit=entry.get("sanskrit", ""),
            instruction=entry.get("instruction", ""),
            cue=entry.get("cue", ""),
            level=int(entry.get("level", 1)),
            family=entry.get("family", ""),
            table={},
            side=entry.get("side", ""),
            scored=False,
        )
    return made
