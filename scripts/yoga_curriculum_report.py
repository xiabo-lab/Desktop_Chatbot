#!/usr/bin/env python3
"""The §32 Phase 2 review table, and §30's validation, computed not claimed.

    python scripts/yoga_curriculum_report.py
    python scripts/yoga_curriculum_report.py --course beginner_01

Reads `design/yoga_v2/` and prints what a human has to sign off before any
video is generated: every course's pose count, hold time, transition time,
relaxation and total duration, plus the asset arithmetic that falls out of it.

Exits non-zero if any §30 rule is broken, so "the curriculum is valid" is a
thing that can be checked rather than asserted. It changes no game behaviour
and imports nothing from a running system except the shipping pose library,
which it reads to confirm every reused id really exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# This runs on the Windows box as well as the Pi, and a Windows console
# defaults to cp1252, which cannot encode a box-drawing character. Losing a
# report to its own rule lines would be a silly way to fail.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from design.yoga_v2 import catalog, courses
from design.yoga_v2.courses import (ARRIVAL, COURSES, LEVELS, RELAX,
                                    WARMUP, transition_between)

#: **15-20 minutes is the requirement. 17-19 is a preference, and nothing
#: more.** A course is never lengthened to reach it: a lesson that finishes
#: naturally at 15:30 is a better lesson than one padded to 18:00 by holding a
#: demanding pose longer than it deserves. The report marks the preferred band
#: so it can be seen, and never treats missing it as a fault.
DURATION_MIN, DURATION_MAX = 15 * 60, 20 * 60
PREFERRED_MIN, PREFERRED_MAX = 17 * 60, 19 * 60


def validate(course) -> list[str]:
    """§30, one rule per line returned. Empty means the course is sound."""
    problems: list[str] = []
    total = course.total_seconds

    if not (DURATION_MIN <= total <= DURATION_MAX):
        problems.append(f"duration {total / 60:.1f} min is outside 15-20")

    for step in course.steps:
        if step.pose_id not in catalog.POSES:
            problems.append(f"unknown pose {step.pose_id!r}")
            continue
        low, high = step.bounds
        if not (low <= step.hold_s <= high):
            problems.append(
                f"{step.pose_id} held {step.hold_s:.0f}s, outside its "
                f"{step.effective_band} band {low:.0f}-{high:.0f}s")

    # §11: both sides of anything asymmetric.
    sided = [s.pose_id for s in course.steps if catalog.get(s.pose_id).bilateral]
    for pose_id in set(sided):
        partner = catalog.get(pose_id).mirror_id
        if partner not in sided:
            problems.append(f"does {pose_id} but never {partner}")
        else:
            here = sum(s.hold_s for s in course.steps if s.pose_id == pose_id)
            there = sum(s.hold_s for s in course.steps if s.pose_id == partner)
            if abs(here - there) > 0.6:
                problems.append(
                    f"{pose_id} held {here:.0f}s but {partner} {there:.0f}s")

    # §4: the arc. Starts by arriving, warms up, cools down, ends at rest.
    segments = course.segments
    if not segments or segments[0] != ARRIVAL:
        problems.append("does not begin with an arrival")
    if WARMUP not in segments:
        problems.append("has no warm-up")
    if not segments or segments[-1] != RELAX:
        problems.append("does not end in relaxation")
    if course.steps and course.steps[-1].effective_band != "relax":
        problems.append(f"ends on {course.steps[-1].pose_id}, not a rest pose")
    if course.relaxation_seconds < 60:
        problems.append(f"only {course.relaxation_seconds:.0f}s of relaxation")

    # §139: never open with something demanding.
    if course.steps:
        first = course.steps[0].pose
        if first.level > 1 or "balance" in first.categories:
            problems.append(f"opens with {first.id}, which is not a settling pose")

    # No pose twice in a row -- §30's "no accidental duplicate steps".
    for a, b in zip(course.steps, course.steps[1:]):
        if a.pose_id == b.pose_id:
            problems.append(f"{a.pose_id} appears twice in a row")

    return problems


def show_course(course) -> None:
    print(f"\n── {course.name}  ({course.id}, {course.level}) "
          f"{'─' * max(0, 42 - len(course.name))}")
    print(f"   {course.theme}")
    print(f"   {'step':>3}  {'pose':26} {'segment':14} {'hold':>6} "
          f"{'br':>3} {'move':>6} {'scored':>7}")
    previous = "standing-neutral"
    for index, step in enumerate(course.steps, 1):
        move = transition_between(previous, step.pose.family)
        previous = step.pose.family
        print(f"   {index:3}  {step.pose_id:26} {step.segment:14} "
              f"{step.hold_s:5.0f}s {step.breaths:3} {move:5.1f}s "
              f"{'yes' if step.score_enabled else 'guided':>7}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--course", default="", help="print one course's steps")
    args = parser.parse_args()

    if args.course:
        if args.course not in COURSES:
            print(f"no course called {args.course!r}")
            return 2
        show_course(COURSES[args.course])
        problems = validate(COURSES[args.course])
        print("\n   " + ("\n   ".join(f"PROBLEM: {p}" for p in problems)
                         if problems else "valid"))
        return 1 if problems else 0

    # ── the catalog ──────────────────────────────────────────────────────────
    from aipi5.games.yoga.poses import POSES as SHIPPED
    scored, guided = catalog.scored(), catalog.guided()
    reuse = [p for p in scored if p.existing]
    fresh = [p for p in scored if p.needs_table]
    print("── pose catalog ────────────────────────────────────────────────")
    print(f"   {len(catalog.POSES)} poses: {len(scored)} scored, "
          f"{len(guided)} guided (floor)")
    print(f"   reusing shipped poses : {len(reuse)} of {len(SHIPPED)}")
    print(f"   new bone tables needed: {len(fresh)}")
    missing = [p.existing for p in reuse if p.existing not in SHIPPED]
    if missing:
        print(f"   BROKEN: reused ids not in the shipping library: {missing}")

    # ── the courses ──────────────────────────────────────────────────────────
    print("\n── the 21 courses ──────────────────────────────────────────────")
    print(f"   {'course':22} {'lvl':4} {'poses':>5} {'scored':>6} {'hold':>7} "
          f"{'move':>7} {'relax':>6} {'total':>8}")
    failures: dict[str, list[str]] = {}
    off_target = 0
    for level in LEVELS:
        for course in courses.by_level(level):
            problems = validate(course)
            if problems:
                failures[course.id] = problems
            total = course.total_seconds
            preferred = PREFERRED_MIN <= total <= PREFERRED_MAX
            flag = "  *" if preferred else ""
            if not preferred:
                off_target += 1
            print(f"   {course.name[:22]:22} {level[:3]:4} "
                  f"{len(course.steps):5} {course.scored_steps:6} "
                  f"{course.hold_seconds / 60:6.1f}m "
                  f"{course.transition_seconds / 60:6.1f}m "
                  f"{course.relaxation_seconds:5.0f}s "
                  f"{total / 60:7.1f}m{flag}")

    # ── the assets that fall out of it ───────────────────────────────────────
    used = {s.pose_id for c in COURSES.values() for s in c.steps}
    pairs = set()
    for course in COURSES.values():
        for a, b in zip(course.steps, course.steps[1:]):
            pairs.add((a.pose_id, b.pose_id))
    families = {catalog.get(p).family for p in used}
    mirrorable = {p for p in used if catalog.get(p).side == "right"}
    print("\n── assets implied ──────────────────────────────────────────────")
    print(f"   distinct poses used      : {len(used)} of {len(catalog.POSES)}")
    print(f"   of those, right-hand      : {len(mirrorable)} "
          f"(mirror the left clip, no new generation)")
    print(f"   unique ordered pose pairs: {len(pairs)}")
    print(f"   posture families spanned  : {len(families)}")
    print(f"   hub-and-spoke clips       : ~{len(used) - len(mirrorable)} "
          f"pose clips + ~{len(families) * 2} family bridges")
    print(f"   (against {len(pairs)} clips if every pair were generated)")

    # ── verdict ──────────────────────────────────────────────────────────────
    print("\n── validation ──────────────────────────────────────────────────")
    if failures:
        for cid, problems in failures.items():
            print(f"   {cid}:")
            for problem in problems:
                print(f"      - {problem}")
        print(f"\n   {len(failures)} of {len(COURSES)} courses FAILED")
        return 1
    print(f"   all {len(COURSES)} courses pass every §30 rule")
    print(f"   all inside the 15-20 min requirement; {len(COURSES) - off_target} "
          f"also land in the preferred 17-19 band (marked *)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
