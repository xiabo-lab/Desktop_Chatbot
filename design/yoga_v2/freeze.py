"""The curriculum freeze: a fingerprint of what was approved.

Approved 2026-08-20. From here on the curriculum is an input to the animation
work rather than something the animation work is allowed to renegotiate — so
what matters is being able to tell, cheaply and at any later moment, whether
what is on disk is still what was signed off.

`fingerprint()` hashes every course id, step, hold, side and segment, plus each
pose's scored/guided state. It deliberately ignores prose: fixing a typo in a
`cue` does not break the freeze, but moving a hold by one second does.

    python -m design.yoga_v2.freeze          # print and check

A mismatch is not an error in itself — curricula get revised. It means the
approved fingerprint below must be updated *deliberately*, and whoever does it
knows they are changing something a human signed off.
"""

from __future__ import annotations

import hashlib

from design.yoga_v2 import catalog
from design.yoga_v2.courses import COURSES

#: What was approved on 2026-08-20. Regenerate only with a decision behind it.
APPROVED = "2dc9d27290eaa535"

APPROVED_SUMMARY = {
    "courses": 21,
    "steps": 605,
    "scored_poses": 37,
    "guided_poses": 54,
    "bone_tables_to_author": 5,
    "duration_range": "15:07-19:40",
}


def fingerprint() -> str:
    """A stable hash of every number a human looked at and approved."""
    parts: list[str] = []
    for cid in sorted(COURSES):
        course = COURSES[cid]
        parts.append(f"{cid}|{course.level}")
        for step in course.steps:
            parts.append(f"{step.pose_id}:{step.hold_s:.1f}:{step.segment}:"
                         f"{int(step.score_enabled)}")
    for pid in sorted(catalog.POSES):
        spec = catalog.POSES[pid]
        parts.append(f"{pid}={spec.family}/{spec.band}/{int(spec.scored)}/"
                     f"{spec.tracking}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def check() -> bool:
    return fingerprint() == APPROVED


def main() -> int:
    actual = fingerprint()
    print(f"  approved   {APPROVED}")
    print(f"  on disk    {actual}")
    if actual == APPROVED:
        print("\n  FROZEN — the curriculum on disk is what was approved.")
        for key, value in APPROVED_SUMMARY.items():
            print(f"    {key:24} {value}")
        return 0
    print("\n  CHANGED — the curriculum has moved since it was approved.")
    print("  If that was deliberate, update APPROVED in this file and say so.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
