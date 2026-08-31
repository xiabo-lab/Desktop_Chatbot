#!/usr/bin/env python3
"""Write the JSON catalogs §12 and §18 ask for, from the Python source.

    python -m design.yoga_v2.export

Four files, all artefacts and none of them hand-edited:

    pose_catalog.json                every shape, with its metadata
    course_catalog.json              the 21 courses, step by step
    transition_catalog.json          the posture-family graph and its costs
    animation_generation_manifest.json   exactly what has to be generated

The manifest is the point of the exercise. §18 says build the asset list before
generating hundreds of clips, and §19 says do not generate a left and a right
version of anything that can be mirrored. Both fall out of the catalog rather
than being counted by hand, so the number cannot drift from the curriculum.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from design.yoga_v2 import catalog, courses                       # noqa: E402
from design.yoga_v2.catalog import BANDS, breaths_for             # noqa: E402
from design.yoga_v2.courses import COURSES, HEIGHT, transition_between  # noqa: E402
from design.yoga_v2.freeze import fingerprint as _fp  # noqa: E402

FINGERPRINT = _fp()

HERE = Path(__file__).resolve().parent

#: Frames in a transition clip, and in a hold loop. The transition figure is
#: what the shipping clips already use and is proven on the Pi; the hold loop
#: is §15's 3-6 s at 10 fps, taken at the short end and played forwards then
#: backwards so a 12-frame file covers a 2.4 s cycle without a seam.
TRANSITION_FRAMES = 7
HOLD_LOOP_FRAMES = 12

#: Measured from the shipping clips: 2.8 MB for 20 clips of 7 keyed, cropped
#: frames is about 20 kB a frame.
KB_PER_FRAME = 20


def pose_catalog() -> dict:
    out = {}
    for spec in catalog.POSES.values():
        low, high = spec.hold_bounds
        out[spec.id] = {
            "id": spec.id,
            "name": spec.name,
            "sanskrit_name": spec.sanskrit,
            "family": spec.family,
            "transition_group": spec.family,
            "category": list(spec.categories),
            "level": spec.level,
            "bilateral": spec.bilateral,
            "side": spec.side or "center",
            "mirror_supported": bool(spec.side),
            "mirror_of": (spec.mirror_id if spec.side == "right" else None),
            "default_hold_sec": spec.default_hold,
            "min_hold_sec": low,
            "max_hold_sec": high,
            "breaths": breaths_for(spec.default_hold),
            "hold_band": spec.band,
            "score_enabled": spec.scored,
            "normalisation": spec.normalisation,
            "tracking_profile": spec.existing or None,
            "animation_id": spec.id,
            "reuses_shipped_pose": spec.existing or None,
            "needs_bone_table": spec.needs_table,
            "instruction": spec.instruction,
            "cue": spec.cue,
        }
    return {"count": len(out), "poses": out}


def course_catalog() -> dict:
    out = {}
    for course in COURSES.values():
        steps = []
        previous = catalog.STANDING
        for step in course.steps:
            move = transition_between(previous, step.pose.family)
            previous = step.pose.family
            steps.append({
                "pose": step.pose_id,
                "side": step.pose.side or "center",
                "segment": step.segment,
                "hold_sec": step.hold_s,
                "transition_sec": move,
                "breaths": step.breaths,
                "hold_band": step.effective_band,
                "score_enabled": step.score_enabled,
                "instruction": step.pose.instruction,
                "cue": step.pose.cue,
            })
        out[course.id] = {
            "id": course.id,
            "name": course.name,
            "level": course.level,
            "theme": course.theme,
            "target_duration_sec": round(course.total_seconds),
            "hold_sec": round(course.hold_seconds),
            "transition_sec": round(course.transition_seconds),
            "relaxation_sec": round(course.relaxation_seconds),
            "pose_count": len(course.steps),
            "scored_steps": course.scored_steps,
            "segments": list(course.segments),
            "poses": steps,
        }
    return {"count": len(out), "courses": out}


def transition_catalog() -> dict:
    """The family graph, plus every pair the 21 courses actually ask for."""
    families = sorted(HEIGHT)
    matrix = {a: {b: transition_between(a, b) for b in families} for a in families}

    pairs: dict[str, dict] = {}
    for course in COURSES.values():
        for first, second in zip(course.steps, course.steps[1:]):
            key = f"{first.pose_id}->{second.pose_id}"
            if key in pairs:
                pairs[key]["used_in"] += 1
                continue
            a, b = first.pose.family, second.pose.family
            pairs[key] = {
                "from": first.pose_id, "to": second.pose_id,
                "from_family": a, "to_family": b,
                "seconds": transition_between(a, b),
                "same_family": a == b,
                "used_in": 1,
            }
    return {
        "families": {name: {"height": HEIGHT[name]} for name in families},
        "seconds_between_families": matrix,
        "model": ("base 3.0s plus 1.6s per unit of height difference, "
                  "clamped to 2.5-8.0s"),
        "pair_count": len(pairs),
        "pairs": pairs,
    }


#: The pose every family routes through. A clip runs between a pose and its
#: family's hub, played forwards to enter and backwards to leave -- the trick
#: the shipping engine already uses -- and the hubs connect to each other
#: through a small set of bridges. That is what turns 258 ordered pairs into
#: about eighty clips.
HUB = {
    catalog.STANDING: "mountain", catalog.FOLD: "forward_fold",
    catalog.WIDE: "star", catalog.LUNGE: "mountain",
    catalog.INVERTED: "downward_dog", catalog.QUADRUPED: "table",
    catalog.KNEELING: "child_pose", catalog.SEATED: "easy_seat",
    catalog.PRONE: "plank", catalog.SUPINE: "savasana",
}

#: Hub-to-hub moves. Each is a real journey a class makes -- standing to the
#: floor, hands-and-knees to sitting -- and each is one clip played both ways.
BRIDGES = [
    ("mountain", "star"), ("mountain", "forward_fold"),
    ("mountain", "downward_dog"), ("forward_fold", "downward_dog"),
    ("downward_dog", "table"), ("table", "child_pose"),
    ("table", "plank"), ("child_pose", "easy_seat"),
    ("easy_seat", "savasana"), ("mountain", "savasana"),
]


def _job(kind, pose, start=None, end=None, frames=0, loop=False, note=""):
    spec = catalog.get(pose)
    mirrored = spec.side == "right"
    return {
        "id": f"{kind}:{pose}" if kind != "transition" else f"{kind}:{start}->{end}",
        "kind": kind,
        "pose": pose,
        "start_pose": start,
        "end_pose": end,
        "side": spec.side or "center",
        "frames": frames,
        "duration_sec": round(frames / 10.0, 1) if frames else None,
        "fps": 10,
        "resolution": "1280x800",
        "loop": loop,
        "mirrorable": spec.bilateral,
        "mirror_of": spec.mirror_id if mirrored else None,
        "reference": spec.existing or None,
        "output": (f"assets/yoga/clips/{start}__{end}/" if kind == "transition"
                   else f"assets/yoga/loops/{pose}/" if kind == "hold_loop"
                   else f"assets/yoga/coach/{pose}.webp"),
        "status": "pending",
        "qa": "pending",
        "note": note,
    }


#: The smallest set that proves the 1280x800 @ 10 fps system on the Pi.
#:
#: Chosen so that **nothing in it is blocked on a bone table**: every newly
#: generated pose here is guided, and a guided pose needs no rig, no targets and
#: no tolerance tuning. The three scored poses in the set are ones whose artwork
#: already ships, which is also what makes it a genuine reuse test.
REPRESENTATIVE_CASES = [
    ("scored standing pose", "warrior_two_left",
     "shipped still and clip, reused unchanged"),
    ("guided standing pose", "warrior_three_left",
     "new artwork, no bone table -- guided poses need none"),
    ("floor / kneeling pose", "child_pose", "new artwork, floor placement rule"),
    ("floor / supine pose", "savasana", "new artwork, the widest figure"),
    ("left/right mirroring", "warrior_three_right",
     "drawn from the left clip at runtime; zero generation, must be verified"),
    ("same-family direct transition", "star->triangle_left",
     "WIDE to WIDE, 3.0s -- the shortcut the old engine could not make"),
    ("large standing-to-floor transition", "mountain->savasana",
     "STANDING to SUPINE, 8.0s -- the longest bridge in the graph"),
    ("normal hold loop", "warrior_two_left",
     "12-frame ping-pong over shipped artwork"),
    ("long relaxation hold loop", "savasana",
     "the same loop has to survive 115s of Savasana without reading as a cycle"),
    ("reused + new in one movement", "mountain->savasana",
     "shipped Mountain artwork at one end, new Savasana at the other"),
]


def representative(transitions, loops, stills) -> dict:
    """Pick the jobs that cover every case, and nothing else."""
    want_clips = {"star->triangle_left", "mountain->savasana",
                  "mountain->warrior_three_left", "child_pose->easy_seat"}
    want_loops = {"warrior_two_left", "savasana", "child_pose"}
    want_stills = {"warrior_three_left", "child_pose", "savasana"}

    picked_clips = [j for j in transitions
                    if f'{j["start_pose"]}->{j["end_pose"]}' in want_clips]
    picked_loops = [j for j in loops if j["pose"] in want_loops]
    picked_stills = [j for j in stills if j["pose"] in want_stills]
    frames = (sum(j["frames"] for j in picked_clips)
              + sum(j["frames"] for j in picked_loops))
    return {
        "purpose": ("Prove the pipeline and the Pi before generating the "
                    "full library. 32 Phase 6."),
        "cases_covered": [{"case": c, "asset": a, "why": w}
                          for c, a, w in REPRESENTATIVE_CASES],
        "blocked_on_bone_tables": False,
        "stills": [j["id"] for j in picked_stills],
        "transitions": [j["id"] for j in picked_clips],
        "hold_loops": [j["id"] for j in picked_loops],
        "totals": {
            "stills": len(picked_stills),
            "transition_clips": len(picked_clips),
            "hold_loops": len(picked_loops),
            "frames": frames,
            "estimated_disk_kb": frames * KB_PER_FRAME,
            "share_of_full_library": f"{frames / 988 * 100:.0f}%",
        },
        "verify_on_pi": [
            "1280x800 output", "10 fps playback with no stall",
            "visual quality", "coach identity across new and reused assets",
            "transition continuity at both ends",
            "hold loop seamless over a full relaxation hold",
            "mirrored pose stays centred", "CPU", "RAM and browser memory",
            "preload behaviour and first-play latency",
            "pose tracking still at its measured rate while all this runs",
        ],
    }


def generation_manifest() -> dict:
    """Every asset that has to exist, and which of them already do."""
    used = sorted({s.pose_id for c in COURSES.values() for s in c.steps})
    sources = [p for p in used if catalog.get(p).side != "right"]
    mirrors = [p for p in used if catalog.get(p).side == "right"]

    stills, transitions, loops = [], [], []
    for pose in sources:
        spec = catalog.get(pose)
        if not spec.existing:
            stills.append(_job("still", pose, frames=1,
                               note="new artwork" if spec.scored is False
                               else "needs a bone table first"))
        loops.append(_job("hold_loop", pose, frames=HOLD_LOOP_FRAMES, loop=True,
                          note="ping-ponged; 12 frames cover a 2.4 s cycle"))
        hub = HUB[spec.family]
        if pose != hub:
            # A shipped clip runs *Mountain* to the pose. It is only reusable
            # when this pose's hub is also Mountain -- a WIDE pose now routes
            # through Star, and the old Mountain->Warrior II clip is the wrong
            # movement for that spoke however good the artwork is.
            reusable = bool(spec.existing) and hub == "mountain"
            transitions.append(_job(
                "transition", pose, start=hub, end=pose,
                frames=TRANSITION_FRAMES,
                note=("reuse the shipped Mountain clip" if reusable
                      else "new: the shipped clip runs from Mountain, not "
                           f"from {hub}" if spec.existing else "new")))
    for a, b in BRIDGES:
        transitions.append(_job("transition", b, start=a, end=b,
                                frames=TRANSITION_FRAMES,
                                note="family bridge, played both ways"))

    new_stills = [j for j in stills]
    new_clips = [j for j in transitions
                 if not j["note"].startswith("reuse the shipped")]
    frames = sum(j["frames"] for j in new_clips) + sum(j["frames"] for j in loops)
    return {
        "frozen_fingerprint": FINGERPRINT,
        "poses_used": len(used),
        "source_poses": len(sources),
        "mirror_only": len(mirrors),
        "totals": {
            "stills_to_generate": len(new_stills),
            "transition_clips_total": len(transitions),
            "transition_clips_reused": len(transitions) - len(new_clips),
            "transition_clips_new": len(new_clips),
            "hold_loops": len(loops),
            "frames_to_generate": frames,
            "estimated_disk_kb": frames * KB_PER_FRAME,
        },
        "notes": [
            "A right-hand pose is the left-hand clip mirrored at draw time; "
            "it costs no generated frame (19).",
            "One clip per pose to its family hub, played forwards to enter and "
            "backwards to leave, plus ten hub-to-hub bridges.",
            "Hold loops ping-pong, so 12 frames cover a 2.4 s cycle with no "
            "seam; 15 asks for 3-6 s.",
            "RAM stays bounded by the shipping LRU, which holds three clips.",
        ],
        "representative_set": representative(transitions, loops, stills),
        "stills": stills,
        "transitions": transitions,
        "hold_loops": loops,
        "mirrors": [{"pose": p, "from": catalog.get(p).mirror_id,
                     "generated": False} for p in mirrors],
    }


def main() -> int:
    for name, payload in (
            ("pose_catalog.json", pose_catalog()),
            ("course_catalog.json", course_catalog()),
            ("transition_catalog.json", transition_catalog()),
            ("animation_generation_manifest.json", generation_manifest())):
        path = HERE / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        print(f"  {name:38} {path.stat().st_size / 1024:7.1f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
