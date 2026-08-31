#!/usr/bin/env python3
"""Export everything the runtime 3D coach needs into one JSON file.

The 3D coach is drawn in the browser from data, not from pictures, so this is
the whole of its asset pipeline -- a hundred kilobytes of numbers in place of
the ninety-seven megabytes of generated video the 2D route needed.

Four things go in, and where each comes from matters:

  rig / poses     `aipi5.games.yoga` -- the *scoring* tables, unmodified. The
                  coach shows the player the same numbers the player is judged
                  against, so the two cannot drift apart.
  poses3d         `design.yoga_v2.poses3d` -- the guided tier, which has no
                  scoring table because it is never scored.
  catalog         `design.yoga_v2.catalog` -- names, cues, families and the
                  frozen scored/guided classification.
  courses         `design.yoga_v2.courses` -- the 21 frozen lessons.

Run it after touching any of those:

    python scripts/build_yoga_v3_data.py
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "design"))

OUT = ROOT / "aipi5" / "ui" / "web" / "assets" / "yoga" / "v3" / "rigdata.json"


def main() -> int:
    from aipi5.games.yoga import poses as game_poses
    from aipi5.games.yoga import rig
    from yoga_v2 import catalog, courses, poses3d, transitions

    data: dict = {}

    # The rig proportions, so the fallback stick figure and the 3D coach agree
    # about what a body is shaped like.
    data["rig"] = {name: round(float(value), 4)
                   for name, value in vars(rig).items()
                   if name.islower() and isinstance(value, float)}
    if not data["rig"]:
        data["rig"] = dict(getattr(rig, "LENGTHS", {}))

    balance_support = {
        "tree_heart_left": "left", "tree_heart_right": "right",
        "tree_overhead_left": "left", "tree_overhead_right": "right",
        "half_moon_left": "left", "half_moon_right": "right",
        "hand_to_toe_left": "left", "hand_to_toe_right": "right",
    }

    def scored_terminals(pose) -> dict[str, str]:
        """Standing scored poses use a planted sole, including balances."""
        support = balance_support.get(pose.id)
        sides = (support,) if support else ("left", "right")
        out = {f"{side}_foot": "floor" for side in sides}
        if pose.id.startswith("tree_"):
            out[f"{'right' if support == 'left' else 'left'}_foot"] = "relax"
        elif pose.id.startswith("half_moon_"):
            # The lifted ankle is dorsiflexed so its sole stays vertical and
            # the heel reaches away; a pointed toe reads as a dance arabesque.
            out[f"{'right' if support == 'left' else 'left'}_foot"] = "flex"
        elif pose.id.startswith("hand_to_toe_"):
            out[f"{'right' if support == 'left' else 'left'}_foot"] = "flex"
        if pose.id in {"warrior_two_left", "warrior_two_right"}:
            out["left_hand"] = "palm_down"
            out["right_hand"] = "palm_down"
        if pose.id == "star":
            out["left_hand"] = "palm_forward"
            out["right_hand"] = "palm_forward"
        return out

    def scored_terminal3d(pose_id: str) -> dict[str, list[float]]:
        """Foot turnout belongs to the ankle, not the scored leg angles."""
        support = balance_support.get(pose_id)
        planted = (support,) if support else ("left", "right")
        out = {f"{side}_foot": [30, 0, 0] for side in planted}
        mirrored = {
            "warrior_one_left":  ("left", "right"),
            "warrior_two_left":  ("left", "right"),
            "reverse_warrior_left": ("left", "right"),
            "side_angle_left":   ("left", "right"),
            "triangle_left":     ("left", "right"),
            "pyramid_left":      ("left", "right"),
            "warrior_one_right": ("right", "left"),
            "warrior_two_right": ("right", "left"),
            "reverse_warrior_right": ("right", "left"),
            "side_angle_right":  ("right", "left"),
            "triangle_right":    ("right", "left"),
            "pyramid_right":     ("right", "left"),
        }
        if pose_id in mirrored:
            front, back = mirrored[pose_id]
            sign = -1 if front == "left" else 1
            out.update({f"{front}_foot": [30, sign * 18, 0],
                        f"{back}_foot": [30, -sign * 42, 0]})
        if pose_id in {"star", "wide_leg_fold"}:
            out.update({"left_foot": [30, -12, 0],
                        "right_foot": [30, 12, 0]})
        return out

    data["poses"] = {
        pose.id: {"name": pose.name, "side": pose.side or "",
                  "scales": dict(getattr(pose, "scales", {}) or {}),
                  "table": {k: float(v) for k, v in pose.table.items()},
                  "terminals": scored_terminals(pose),
                  **({"terminal3d": scored_terminal3d(pose.id)}
                     if scored_terminal3d(pose.id) else {}),
                  **({"camera": poses3d.SCORED_CAMERAS[pose.id]}
                     if pose.id in poses3d.SCORED_CAMERAS else {})}
        # The *authored* tables only. `POSES` now also contains the guided
        # poses and the five promoted from this very file, and exporting those
        # back into it would be a loop that grows a little each run.
        for pose in game_poses._POSES
    }

    data["poses3d"] = poses3d.POSES3D
    data["views"] = poses3d.VIEWS
    data["stand"] = poses3d.STAND

    data["catalog"] = {
        spec.id: {"name": spec.name, "sanskrit": spec.sanskrit,
                  "family": spec.family, "band": spec.band,
                  "level": spec.level, "side": spec.side or "",
                  "bilateral": spec.bilateral, "scored": spec.scored,
                  "instruction": spec.instruction, "cue": spec.cue,
                  "existing": spec.existing or ""}
        for spec in catalog.POSES.values()
    }

    data["courses"] = {
        course.id: {"name": course.name, "level": course.level,
                    "steps": [{"pose": step.pose_id, "segment": step.segment,
                               "hold": round(step.hold_s, 1),
                               "band": step.effective_band,
                               # The reviewed move time, plus everything the
                               # curriculum counted around it: the preview, the
                               # settle, and the beat where the last pose is
                               # acknowledged. The shipping game has no settle
                               # or result *phase* -- both are drawn over the
                               # transition -- so that time is spent arriving
                               # instead. Which is where it belongs: it is what
                               # gives the camera room to finish moving before
                               # the hold begins, and leaving it out put nine
                               # of the twenty-one classes under their floor.
                               "transition": round(
                                   courses.transition_between(
                                       prev.pose.family, step.pose.family)
                                   + courses.PREVIEW_S + courses.SETTLE_S
                                   + courses.RESULT_S, 1)
                               if prev is not None else
                               round(courses.PREVIEW_S + courses.SETTLE_S, 1),
                               "scored": step.pose.scored}
                              for prev, step in zip((None, *course.steps),
                                                    course.steps)]}
        for course in courses.COURSES.values()
    }

    # Straight interpolation is only safe between nearby shapes.  Larger
    # changes travel through owner-approved, human-scale stations (Fold,
    # Tabletop, Easy Seat, and so on), shared by the game and review page.
    course_edges = {
        (previous, step["pose"])
        for course in data["courses"].values()
        for previous, step in zip(
            ("mountain", *(s["pose"] for s in course["steps"])),
            course["steps"])
    }
    data["transition_paths"] = {
        f"{source}>{target}": transitions.path_between(source, target)
        for source, target in sorted(course_edges)
    }
    data["transition_actions"] = {
        f"{source}>{target}": transitions.action_between(source, target)
        for source, target in sorted(course_edges)
        if transitions.action_between(source, target)
    }

    # Invalid anatomy must never become a drawable production asset.  The
    # checker distinguishes natural declared contact from core penetration and
    # covers the complete scored + guided catalog; make it a build gate rather
    # than a report somebody has to remember to run.
    from yoga_v2 import check3d
    anatomy_faults = []
    for name, pose in sorted(check3d.pose_library().items()):
        anatomy_faults.extend(check3d.report(name, pose, verbose=False))
    if anatomy_faults:
        print("  ANATOMY FAILED")
        print("\n".join(anatomy_faults))
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=None, sort_keys=True,
                              separators=(",", ":")) + "\n", encoding="utf-8")

    # Every pose a course asks for must be drawable, or the coach silently
    # falls back to standing and nobody notices until a lesson is running.
    wanted = {step["pose"] for c in data["courses"].values() for step in c["steps"]}
    wanted |= set(data["catalog"])
    drawable = set(data["poses"]) | set(data["poses3d"])
    missing = sorted(wanted - drawable)
    unused = sorted(set(data["poses3d"]) - wanted)

    print(f"  {OUT.relative_to(ROOT)}  {OUT.stat().st_size / 1024:.0f} kB")
    print(f"  catalog {len(data['catalog'])}  courses {len(data['courses'])}"
          f"  scored tables {len(data['poses'])}"
          f"  authored in 3D {len(data['poses3d'])}")
    if missing:
        print(f"  MISSING ({len(missing)}): {', '.join(missing)}")
    if unused:
        print(f"  authored but unused ({len(unused)}): {', '.join(unused)}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
