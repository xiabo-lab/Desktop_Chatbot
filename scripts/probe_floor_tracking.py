#!/usr/bin/env python3
"""Can the camera see a body on the floor well enough to score it? Measure it.

The Yoga Coach is standing-only, and that is not a preference. `rig.measure`
divides all seven of its ratio metrics by `span` -- the distance between the
two shoulders -- and every one of them is an **image-axis** difference: `stack`
and both `foot_height`s and both `hand_height`s are raw `.y`, `stance` and
`reach` are raw `.x`. A body lying down has its own axis somewhere else
entirely, so those numbers stop meaning what they are named. Separately, a Brio
at desk height looking along the floor sees a lying person nearly edge-on, and
no capture setting fixes an angle.

The new curriculum wants Savasana, Child's Pose, Downward Dog and a dozen more
like them, and the question of whether those can be *scored* or only
*demonstrated* decides how the whole thing is built. This script is how that
question gets answered with numbers instead of argument.

**It measures four things per pose, per camera mode:**

    keypoints   how many of the seventeen the model finds, and how confident
                it is. If the model cannot see a folded body at all, nothing
                downstream matters and the answer is "demonstrate".
    span        the shoulder distance the scorer normalises by, against
                `rig.MIN_SPAN`. Below that, `measure` returns nothing at all
                and the player is told to step into view -- forever.
    metrics     which of the seventeen `measure` produces, and which come out
                degenerate (a `stack` of 2.07 standing and 0.02 lying down is
                not a measurement, it is the absence of one).
    body frame  the interesting one. The same joints, re-measured in the
                *body's* own axis rather than the image's: rotate so the
                hip-to-shoulder direction is up, and take the scale from the
                torso when the shoulders are too foreshortened to supply it.
                This is computed here, offline, from joints already captured --
                so the question "would rewriting `measure` fix it?" is answered
                before anybody rewrites `measure`.

**And one regression check.** Moving the camera to see the floor changes what
it sees of a standing player, so `--standing` re-scores Mountain, Warrior II
and Tree at the new placement. A floor result bought by breaking the poses that
already work is not a result.

Run on the Pi with the assistant stopped -- that service owns both the camera
and the accelerator:

    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user stop aipi5
    ~/AIPI5/.venv/bin/python scripts/probe_floor_tracking.py \\
        --placement raised-tilted --countdown 12 \\
        --poses savasana childs-pose downward-dog easy-seat cow supine-twist \\
        --standing warrior_two_left
    systemctl --user restart aipi5      # restart, not start

Somebody has to be *in* each pose while it runs, so the whole list is walked in
one go on one lease, with an **audible** countdown between poses -- whoever is
holding Savasana cannot see a terminal. Two low beeps mean "get into the next
one", three quick ones mean "hold still now", two mean "done, relax", and three
high ones mean the run is over.

Run it once per camera placement, and give the desk and the raised position
different `--placement` names: re-running with the same one overwrites its JSON.

The maths half of this needs no hardware at all:

    python scripts/probe_floor_tracking.py --selftest

This is a development tool. Nothing imports it.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipi5.core import config as config_mod
from aipi5.games.yoga import rig
from aipi5.games.yoga.poses import POSES
from aipi5.motion import hailo_pose
from aipi5.motion.camera_lease import CameraLease, CameraLeaseError
from aipi5.motion.pose_types import KEYPOINT_NAMES
from aipi5.vision.camera import Camera

#: The candidate capture modes, and why each is on the list.
#:
#: `640x480@120` is what AI Motion runs today. It is a horizontal *crop* of the
#: sensor's 16:9 field rather than a rescale -- `config/aipi5.yaml` records that
#: it was verified by photographing one scene at both sizes -- so it gives up a
#: quarter of the width. The two 16:9 rows buy that width back, and the frame
#: rate they cost is worth nothing to yoga, which has no blade to smooth.
#:
#: Resolution above the model's 640-square buys **no** keypoint precision:
#: every frame is letterboxed to 640x640 before the accelerator sees it. The
#: 1080p row is here to prove that on this device rather than to assert it.
DEFAULT_MODES = (
    "640x480@120:MJPG",
    "1280x720@30:MJPG",
    "1920x1080@30:MJPG",
)

#: The seven ratio metrics -- the ones that carry an image-axis assumption and
#: therefore the ones that break off-vertical. The ten angle metrics are
#: differences of directions and survive a rotation, which is exactly why the
#: body-frame experiment below only has to rebuild these.
RATIO_METRICS = ("stack", "stance", "reach",
                 "left_hand_height", "right_hand_height",
                 "left_foot_height", "right_foot_height")

#: A ratio this close to zero is not a measurement. `STANDING_METRICS` puts
#: `stack` at 2.07 and the foot heights at -2.07; a pose that reports 0.02 is
#: reporting that the body axis and the image axis have parted company.
DEGENERATE = 0.15

#: Shoulder widths per torso length, taken from the coach's own rig so the
#: torso can stand in for the shoulders without changing the unit. Everything
#: `scoring.BASE_TOLERANCE` says about a ratio is in shoulder widths.
SPAN_PER_TORSO = 2.0 * rig.RIG["shoulder_half"] / rig.RIG["spine"]


#: Roughly what the torso should be doing in each pose, in degrees away from
#: straight up. Used only to catch a capture that measured the wrong shape --
#: somebody still standing when the cue said Downward Dog, which is otherwise
#: indistinguishable from a real result in a table of numbers.
#:
#: Wide bands on purpose. This is a sanity check, not a score.
EXPECTED_TORSO: dict[str, tuple[float, float]] = {
    "savasana": (60.0, 180.0),        # lying flat: axis near horizontal
    "supine-twist": (55.0, 180.0),
    "childs-pose": (45.0, 180.0),     # folded right over
    "downward-dog": (30.0, 110.0),    # hips high, torso a long diagonal
    "cow": (55.0, 130.0),             # spine horizontal
    "easy-seat": (0.0, 30.0),         # sitting upright
}


def parse_mode(text: str) -> tuple[int, int, int, str]:
    """`WxH@FPS:FOURCC`, the same spelling `bench_motion.py` uses."""
    spec, _, fourcc = text.partition(":")
    size, _, fps = spec.partition("@")
    width, _, height = size.partition("x")
    return int(width), int(height), int(fps or 30), (fourcc or "MJPG").upper()


def body_frame(joints: dict[str, tuple[float, float]]) -> dict | None:
    """The seven ratios re-measured in the body's own axis.

    `rig.measure` asks "how far above the shoulders is the wrist, in shoulder
    widths", and answers it with a subtraction along the image's y. That is the
    right question and the wrong axis the moment the body is not upright. Here
    the same question is asked along the **hip-to-shoulder** direction, which
    is the body's own up whichever way it happens to be lying.

    Two changes, and they are the whole experiment:

    - **rotate** every joint so the hip-to-shoulder direction points up, then
      take the same differences. `stack` becomes torso-relative rather than
      image-relative, and the foot and hand heights come back.
    - **scale** in shoulder widths still, but measured off the torso when the
      shoulders are unreliable. Shoulder width is the one length a *frontal*
      body never changes, and a body foreshortened by an oblique camera changes
      it a great deal; the torso is the more robust ruler in exactly those
      cases. It is converted back into shoulder widths through the coach's own
      proportion (`SPAN_PER_TORSO`) rather than used raw, because **every ratio
      tolerance in `scoring.py` is expressed in shoulder widths** and a ruler
      that silently changes unit would invalidate all of them.

    Returns None when the joints needed to define the axis are missing, which
    is itself a result worth recording.

    Takes the midpoints from the table when they are there and derives them
    from the four corner joints when they are not, so the same function reads
    both ends: `forward_kinematics` hands over `hip_mid`/`shoulder_mid`
    directly, and `player_joints` -- whose `LANDMARK_FOR` carries only the head
    and the twelve limb joints -- never does.
    """
    hip = joints.get("hip_mid")
    shoulder = joints.get("shoulder_mid")
    if hip is None or shoulder is None:
        left_hip, right_hip = joints.get("left_hip"), joints.get("right_hip")
        left_sh, right_sh = joints.get("left_shoulder"), joints.get("right_shoulder")
        if not (left_hip and right_hip and left_sh and right_sh):
            return None
        hip = ((left_hip[0] + right_hip[0]) / 2, (left_hip[1] + right_hip[1]) / 2)
        shoulder = ((left_sh[0] + right_sh[0]) / 2, (left_sh[1] + right_sh[1]) / 2)

    axis_x, axis_y = shoulder[0] - hip[0], shoulder[1] - hip[1]
    torso = math.hypot(axis_x, axis_y)
    if torso < 1e-6:
        return None

    span = 0.0
    left_sh, right_sh = joints.get("left_shoulder"), joints.get("right_shoulder")
    if left_sh and right_sh:
        span = math.dist(left_sh, right_sh)
    # The shoulders are believed while they are plausible for this torso;
    # below that they are foreshortened, and the torso is asked instead and
    # converted into the same unit.
    from_torso = torso * SPAN_PER_TORSO
    scale = span if span >= max(rig.MIN_SPAN, 0.5 * from_torso) else from_torso
    if scale < 1e-6:
        return None

    # Rotate so the body axis is "up" (negative y, matching the image
    # convention `rig.py` uses, where 90 degrees is down).
    ux, uy = axis_x / torso, axis_y / torso          # body up, as a unit vector
    def to_body(point):
        dx, dy = point[0] - hip[0], point[1] - hip[1]
        along = dx * ux + dy * uy                     # towards the shoulders
        across = dx * (-uy) + dy * ux                 # the body's left/right
        return across, -along

    placed = {name: to_body(p) for name, p in joints.items()
              if isinstance(p, tuple)}
    out: dict[str, float] = {}
    sh = placed.get("shoulder_mid")
    if sh is None and left_sh and right_sh:
        a, b = to_body(left_sh), to_body(right_sh)
        sh = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    hp = (0.0, 0.0)

    for side in ("left", "right"):
        wrist = placed.get(f"{side}_wrist")
        if wrist is not None and sh is not None:
            out[f"{side}_hand_height"] = (sh[1] - wrist[1]) / scale
        ankle = placed.get(f"{side}_ankle")
        if ankle is not None:
            out[f"{side}_foot_height"] = (hp[1] - ankle[1]) / scale

    left_ankle, right_ankle = placed.get("left_ankle"), placed.get("right_ankle")
    if left_ankle and right_ankle:
        out["stance"] = abs(left_ankle[0] - right_ankle[0]) / scale
        out["stack"] = ((left_ankle[1] + right_ankle[1]) / 2 - hp[1]) / scale
    left_wrist, right_wrist = placed.get("left_wrist"), placed.get("right_wrist")
    if left_wrist and right_wrist:
        out["reach"] = abs(left_wrist[0] - right_wrist[0]) / scale

    # The body axis itself, in the same convention as `measure`'s `torso`:
    # degrees away from straight up, image coordinates. This survives a span
    # far too small for `measure` to say anything, so it is the one number
    # that can always answer "what shape is this person actually in".
    out["_axis_deg"] = math.degrees(math.atan2(axis_y, axis_x)) + 90.0
    while out["_axis_deg"] > 180.0:
        out["_axis_deg"] -= 360.0
    while out["_axis_deg"] < -180.0:
        out["_axis_deg"] += 360.0
    out["_scale"] = scale
    out["_span"] = span
    out["_torso"] = torso
    out["_used_torso"] = float(torso > span)
    return out


def spread(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "sd": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def capture(lease, pose_engine, seconds: float, confidence: float,
            window: str = "", label: str = "") -> dict:
    """Everything measurable about one person holding one shape."""
    started = time.monotonic()
    frames = persons = 0
    keypoint_conf: dict[str, list[float]] = {n: [] for n in KEYPOINT_NAMES}
    keypoint_seen: dict[str, int] = {n: 0 for n in KEYPOINT_NAMES}
    spans: list[float] = []
    measured: dict[str, list[float]] = {}
    recovered: dict[str, list[float]] = {}
    empty_measure = 0
    no_axis = 0
    crowd: list[int] = []
    tracks: dict[int, int] = {}
    edge_frames = 0
    best_frame = None
    best_person = None

    while time.monotonic() - started < seconds:
        frame, captured_at = lease.frame()
        if frame is None:
            continue
        frames += 1
        result = pose_engine.infer(frame, captured_at=captured_at)
        if not result.persons:
            continue
        persons += 1
        person = max(result.persons, key=lambda p: p.area)
        # Who got measured, and whether it stayed the same body.
        #
        # This room has other people in it, and the primary person is chosen by
        # bounding-box area -- so somebody walking between the subject and the
        # lens silently becomes the subject. A run where the track id changed,
        # or where more than one person was in shot, is a run to look at before
        # believing. Recorded rather than guarded against, because the honest
        # fix is an empty room, not a cleverer heuristic.
        crowd.append(len(result.persons))
        # A body against the edge of the picture is a body only partly in it,
        # and the joints outside are invented rather than measured. This is
        # what catches "the subject was lying out of shot and the model found
        # something else".
        bx, by, bw, bh = person.box
        # Left, right and top only. A body doing floor work sits ON the bottom
        # of the picture by definition -- the floor is down there -- so
        # counting the bottom edge marks every good floor capture as invalid,
        # which is exactly what it did the first time.
        if bx <= 0.02 or by <= 0.02 or bx + bw >= 0.98:
            edge_frames += 1
        if person.track_id:
            tracks[person.track_id] = tracks.get(person.track_id, 0) + 1
        if best_person is None or person.confidence > best_person.confidence:
            best_person, best_frame = person, frame.copy()

        for name, point in person.keypoints.items():
            keypoint_conf[name].append(point.confidence)
            if point.confidence >= confidence:
                keypoint_seen[name] += 1

        if window:
            import cv2
            title, lines = guide_for(label)
            cv2.imshow(window, guide_screen(frame, person, list(result.persons),
                                            confidence, title, lines, 0.0, True,
                                            label))
            cv2.waitKey(1)

        joints = rig.player_joints(person, confidence)
        left_sh, right_sh = joints.get("left_shoulder"), joints.get("right_shoulder")
        if left_sh and right_sh:
            spans.append(math.dist(left_sh, right_sh))

        metrics = rig.measure(joints)
        if not metrics:
            empty_measure += 1
        for name, value in metrics.items():
            measured.setdefault(name, []).append(value)

        alt = body_frame(joints)
        if alt is None:
            no_axis += 1
        else:
            for name, value in alt.items():
                recovered.setdefault(name, []).append(value)

    dominant = max(tracks.values()) / persons if tracks and persons else 0.0
    return {
        "frames": frames,
        "person_rate": round(persons / frames, 3) if frames else 0.0,
        "people_in_shot": {"max": max(crowd) if crowd else 0,
                           "mean": round(statistics.fmean(crowd), 2) if crowd else 0.0},
        "same_body_rate": round(dominant, 3),
        "at_frame_edge_rate": round(edge_frames / persons, 3) if persons else 0.0,
        "keypoints": {
            name: {"seen_rate": round(keypoint_seen[name] / persons, 3)
                   if persons else 0.0,
                   "confidence": spread(keypoint_conf[name])}
            for name in KEYPOINT_NAMES},
        "span": spread(spans),
        "span_ok_rate": (round(sum(1 for s in spans if s >= rig.MIN_SPAN)
                               / len(spans), 3) if spans else 0.0),
        "measure_empty_rate": round(empty_measure / persons, 3) if persons else 1.0,
        "metrics": {n: spread(v) for n, v in sorted(measured.items())},
        "body_frame": {n: spread(v) for n, v in sorted(recovered.items())},
        "body_frame_no_axis_rate": round(no_axis / persons, 3) if persons else 1.0,
        "_best_frame": best_frame,
        "_best_person": best_person,
    }


def verdict(row: dict, label: str = "") -> tuple[str, list[str]]:
    """Scoreable, marginal or demonstrate-only -- and the reason.

    "Nobody was in shot" is kept apart from every other answer on purpose. An
    empty room produces the same zeros as a body the model cannot see, and the
    two mean opposite things: one is a finding, the other is a run to throw
    away and repeat. Reporting a missed cue as DEMONSTRATE-ONLY would quietly
    decide the curriculum on a mistake.
    """
    reasons: list[str] = []

    # Two ways a capture can be worthless rather than informative, and both
    # have to be said before any number in the row is quoted.
    if row.get("at_frame_edge_rate", 0.0) > 0.5:
        return "INVALID — body against the frame edge", [
            f"the measured body touched the edge of the picture in "
            f"{row['at_frame_edge_rate'] * 100:.0f}% of frames, so it was only "
            f"partly in shot. Re-aim and repeat; the numbers below mean nothing."]
    axis = (row.get("body_frame", {}).get("_axis_deg") or {}).get("mean")
    band = EXPECTED_TORSO.get(label or "")
    if axis is not None and band is not None and not (band[0] <= abs(axis) <= band[1]):
        return "INVALID — that is not the pose", [
            f"the torso sat {abs(axis):.0f}° from vertical; {label} should be "
            f"{band[0]:.0f}-{band[1]:.0f}°. Either the pose was not held or the "
            f"capture caught somebody else."]

    if row.get("people_in_shot", {}).get("max", 1) > 1:
        reasons.append(f"up to {row['people_in_shot']['max']} people in shot — "
                       f"the largest box is what got measured")
    # Only meaningful when the model actually assigns track ids. It does not
    # in this path -- every person comes back with track_id 0 -- so an absent
    # figure here means "not measured", never "the body changed".
    if row.get("same_body_rate") not in (None, 0.0) and row["same_body_rate"] < 0.9:
        reasons.append(f"the measured body changed identity: one track holds "
                       f"only {row['same_body_rate'] * 100:.0f}% of frames")
    if row["person_rate"] < 0.05:
        return "NO PERSON IN SHOT — rerun", [
            "the model found nobody in any frame; check the cue timing, the "
            "framing and that somebody is actually in the pose"]
    if row["person_rate"] < 0.9:
        reasons.append(f"model finds a person in only "
                       f"{row['person_rate'] * 100:.0f}% of frames")
    if row["span_ok_rate"] < 0.9:
        reasons.append(f"shoulder span is under MIN_SPAN in "
                       f"{(1 - row['span_ok_rate']) * 100:.0f}% of frames")
    if row["measure_empty_rate"] > 0.1:
        reasons.append(f"measure() returns nothing in "
                       f"{row['measure_empty_rate'] * 100:.0f}% of frames")

    degenerate = [n for n in RATIO_METRICS
                  if n in row["metrics"] and abs(row["metrics"][n]["mean"]) < DEGENERATE]
    if degenerate:
        reasons.append(f"image-axis ratios degenerate: {', '.join(degenerate)}")

    saved = [n for n in degenerate
             if n in row["body_frame"] and abs(row["body_frame"][n]["mean"]) >= DEGENERATE]
    if saved:
        reasons.append(f"body-frame normalisation recovers: {', '.join(saved)}")

    weak = [n for n, v in row["keypoints"].items() if v["seen_rate"] < 0.6]
    if weak:
        reasons.append(f"{len(weak)} keypoints below 60% visibility "
                       f"({', '.join(sorted(weak)[:4])}"
                       f"{'...' if len(weak) > 4 else ''})")

    if row["person_rate"] < 0.5 or row["measure_empty_rate"] > 0.5:
        return "DEMONSTRATE-ONLY", reasons
    if not degenerate and row["span_ok_rate"] >= 0.9 and not weak:
        return "SCOREABLE", reasons
    if saved and len(saved) == len(degenerate):
        return "SCOREABLE WITH BODY-FRAME", reasons
    return "MARGINAL", reasons


#: How big another person has to be, relative to the subject, before they could
#: plausibly be picked as the subject instead.
RIVAL_SHARE = 0.40

#: Drawn thicker and labelled on the live view: these are the ones that decide
#: whether the whole body is in frame, so they are the ones worth aiming at.
SKELETON = (
    ("left_shoulder", "right_shoulder"), ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"), ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"), ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"), ("left_hip", "right_hip"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
)


def overlay(frame, person, people, issues, confidence: float):
    """The frame with the skeleton, the rivals and the verdict drawn on it.

    Shared by the live window and the saved still, so what gets looked at
    afterwards is exactly what was on screen at the time.
    """
    import cv2
    canvas = frame.copy()
    height, width = canvas.shape[:2]

    # Everyone else the model found, in thin grey, so a rival is visible as a
    # rival rather than as a mystery line in the log.
    for other in people:
        if other is person:
            continue
        x, y, w, h = other.box
        cv2.rectangle(canvas, (int(x * width), int(y * height)),
                      (int((x + w) * width), int((y + h) * height)),
                      (140, 140, 140), 1)
        cv2.putText(canvas, f"other {other.area * 100:.0f}%",
                    (int(x * width), max(12, int(y * height) - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (140, 140, 140), 1, cv2.LINE_AA)

    if person is not None:
        def at(name):
            point = person.point(name)
            if point is None or point.confidence < confidence:
                return None
            return int(point.x * width), int(point.y * height)

        for a, b in SKELETON:
            pa, pb = at(a), at(b)
            if pa and pb:
                cv2.line(canvas, pa, pb, (0, 210, 0), 2, cv2.LINE_AA)
        for name in KEYPOINT_NAMES:
            point = person.point(name)
            if point is None:
                continue
            spot = (int(point.x * width), int(point.y * height))
            good = point.confidence >= confidence
            essential = name in ESSENTIAL
            cv2.circle(canvas, spot, 6 if essential else 3,
                       (0, 220, 0) if good else (0, 90, 230), -1)
            if essential and not good:
                cv2.putText(canvas, name, (spot[0] + 8, spot[1] - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 90, 230), 1,
                            cv2.LINE_AA)
        x, y, w, h = person.box
        cv2.rectangle(canvas, (int(x * width), int(y * height)),
                      (int((x + w) * width), int((y + h) * height)),
                      (0, 220, 0) if not issues else (0, 150, 230), 2)

    banner = 64
    cv2.rectangle(canvas, (0, 0), (width, banner),
                  (0, 120, 0) if not issues else (0, 40, 120), -1)
    if not issues:
        text = "READY  -  whole body in frame"
    else:
        text = issues[0][:78]
    cv2.putText(canvas, text, (16, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                (255, 255, 255), 2, cv2.LINE_AA)
    # A floor line at the bottom eighth: if the mat is not under it, the camera
    # is aimed too high, which is the single most common fault here.
    cv2.line(canvas, (0, height - height // 8), (width, height - height // 8),
             (60, 200, 255), 1)
    cv2.putText(canvas, "aim so the mat sits above this line",
                (16, height - height // 8 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (60, 200, 255), 1, cv2.LINE_AA)
    return canvas


def annotate(frame, person, path: Path) -> bool:
    """The frame the numbers came from, with the joints drawn on it.

    A table saying eleven keypoints were found does not say whether they were
    found in the *right places*, and a picture does.
    """
    try:
        import cv2
    except ImportError:
        return False
    height, width = frame.shape[:2]
    canvas = frame.copy()
    for name, point in person.keypoints.items():
        x, y = int(point.x * width), int(point.y * height)
        good = point.confidence >= 0.45
        cv2.circle(canvas, (x, y), 5, (0, 220, 0) if good else (0, 90, 220), -1)
        cv2.putText(canvas, f"{name[:12]} {point.confidence:.2f}", (x + 7, y - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32,
                    (0, 220, 0) if good else (0, 90, 220), 1, cv2.LINE_AA)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), canvas)
    return True


def standing_check(lease, pose_engine, seconds: float, confidence: float,
                   pose_id: str) -> dict:
    """Re-score a pose that already works, at the camera's new placement."""
    from aipi5.games.yoga import scoring
    target = POSES[pose_id]
    started = time.monotonic()
    accuracies: list[float] = []
    while time.monotonic() - started < seconds:
        frame, captured_at = lease.frame()
        if frame is None:
            continue
        result = pose_engine.infer(frame, captured_at=captured_at)
        if not result.persons:
            continue
        person = max(result.persons, key=lambda p: p.area)
        joints = rig.player_joints(person, confidence)
        metrics = rig.measure(joints)
        if not metrics:
            continue
        assessment = scoring.assess(target, metrics, tolerance_scale=1.05)
        if assessment.tracked:
            accuracies.append(assessment.accuracy)
    return {"pose": pose_id, "accuracy": spread(accuracies)}


def selftest() -> int:
    """Prove the body-frame maths with no camera, no Hailo and no person.

    Everything `body_frame` claims is a statement about geometry, and geometry
    can be checked against the coach's own poses on any machine. Three
    properties, and the third is the one that decides the design:

      1. **Upright, it must agree with `measure` exactly.** Where the body axis
         is the image axis the two are the same measurement, so any difference
         is a bug in this file rather than a finding.
      2. **Rotated, it must not move.** Rotating a pose bodily in the image
         plane is what lying down does to the geometry.
      3. **On a leaning pose the two legitimately differ**, and by how much is
         the price of switching: it is the lean moving out of the seven ratios
         and into `torso` alone.
    """
    def rotate(joints, degrees):
        radians = math.radians(degrees)
        cos, sin = math.cos(radians), math.sin(radians)
        return {n: (p[0] * cos - p[1] * sin, p[0] * sin + p[1] * cos)
                for n, p in joints.items()}

    def lean(pose):
        return rig.measure(rig.forward_kinematics(pose.table, pose.scales))["torso"]

    upright = [i for i, p in POSES.items() if abs(lean(p)) < 15.0]
    leaning = [i for i, p in POSES.items() if abs(lean(p)) >= 15.0]
    failures = 0

    worst, where = 0.0, ""
    for pose_id in upright:
        pose = POSES[pose_id]
        joints = rig.forward_kinematics(pose.table, pose.scales)
        m, b = rig.measure(joints), body_frame(joints)
        for name in RATIO_METRICS:
            if name in m and name in b and abs(m[name] - b[name]) > worst:
                worst, where = abs(m[name] - b[name]), f"{pose_id}.{name}"
    ok = worst < 0.02
    failures += not ok
    print(f"1. upright agreement over {len(upright)} poses: worst {worst:.5f} "
          f"at {where or 'n/a'}  [{'PASS' if ok else 'FAIL'}]")

    drift = 0.0
    for pose_id in POSES:
        pose = POSES[pose_id]
        joints = rig.forward_kinematics(pose.table, pose.scales)
        base = body_frame(joints)
        for degrees in (45, 90, 180):
            turned = body_frame(rotate(joints, degrees))
            drift = max(drift, max(abs(turned[n] - base[n])
                                   for n in RATIO_METRICS
                                   if n in turned and n in base))
    ok = drift < 1e-6
    failures += not ok
    print(f"2. rotation invariance over all {len(POSES)} poses: worst drift "
          f"{drift:.7f}  [{'PASS' if ok else 'FAIL'}]")

    print(f"3. cost on the {len(leaning)} leaning poses (not errors — the lean "
          f"moving into `torso`):")
    rows = []
    for pose_id in leaning:
        pose = POSES[pose_id]
        joints = rig.forward_kinematics(pose.table, pose.scales)
        m, b = rig.measure(joints), body_frame(joints)
        rows.append((max(abs(m[n] - b[n]) for n in RATIO_METRICS
                         if n in m and n in b), pose_id, lean(pose)))
    for shift, pose_id, angle in sorted(rows, reverse=True)[:6]:
        print(f"     {pose_id:22} lean {angle:+4.0f}°   ratio shift {shift:5.2f}")
    print("   => normalisation must be chosen per pose, not switched globally.")
    return 1 if failures else 0


#: The joints that have to be visible before a run means anything. Feet and
#: head are the ones that decide "is the whole body in frame"; shoulders and
#: hips are what `span` and the body axis are built from, so without them there
#: is no measurement at all, only a detection.
ESSENTIAL = ("nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip",
             "left_knee", "right_knee", "left_ankle", "right_ankle")


def framing(lease, engine, seconds: float, confidence: float,
            out: Path, placement: str, live: bool = False,
            expect: str = "") -> dict:
    """Live framing check, before any data is collected.

    **A detection is not a person.** An empty room in this house already
    reported a person in 100% of frames, so every number below is worthless
    until somebody has looked at the picture and agreed that the thing the
    model found is the human being who intends to be measured. This prints a
    verdict a couple of times a second, beeps it so it can be heard from a mat,
    and saves an annotated frame to be looked at afterwards.

    Checks, in the order they go wrong in practice:

      how many        more than one person means the room contains something
                      the model likes the shape of. That is the false positive
                      to catch here rather than halfway through Savasana.
      whole body      every joint in `ESSENTIAL` above the confidence floor.
                      Feet cut off at the bottom of frame is the usual one.
      span            against `MIN_SPAN`, because below it `measure` returns
                      nothing at all and the whole run is "step into view".
      size in frame   how much of the picture the body occupies. Too small and
                      every keypoint is a couple of pixels of guesswork.
    """
    window = ""
    if live:
        try:
            import cv2
            window = "AIPI5 framing - press q when happy"
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN)
        except Exception as exc:  # noqa: BLE001
            print(f"  (no live window: {exc})")
            window = ""

    deadline = time.monotonic() + seconds
    spoke_at = 0.0
    best = None
    best_score = -1.0
    samples = 0
    ready_frames = 0
    print(f"\n  FRAMING CHECK — {seconds:.0f}s. Get where you intend to be "
          f"measured and adjust until it says READY.")
    print("  (one high beep = READY, two low = not yet)\n")

    while time.monotonic() < deadline:
        frame, captured_at = lease.frame()
        if frame is None:
            continue
        result = engine.infer(frame, captured_at=captured_at)
        samples += 1
        people = list(result.persons)
        issues: list[str] = []
        person = None
        if not people:
            issues.append("nobody found")
        else:
            person = max(people, key=lambda p: p.area)
            # Other people in the room are only a problem when one of them
            # could plausibly *become* the subject. The primary is whoever has
            # the largest box, so a child at the far end of a living room at a
            # twentieth of the area is no threat to that; another adult at half
            # of it is. Demanding an empty room would mean never getting a
            # READY in a house that has one.
            rivals = [p for p in people
                      if p is not person and p.area > RIVAL_SHARE * person.area]
            if rivals:
                others = ", ".join(f"{p.confidence:.2f}@{p.area * 100:.1f}%"
                                   for p in sorted(rivals, key=lambda p: -p.area)[:3])
                issues.append(f"{len(rivals)} other person(s) big enough to be "
                              f"mistaken for you ({others})")
            missing = [n for n in ESSENTIAL
                       if not person.visible(n, confidence)]
            if missing:
                issues.append(f"not visible: {', '.join(missing)}")
            shaped = person.shaped()
            left = shaped.point("left_shoulder")
            right = shaped.point("right_shoulder")
            span = (math.dist((left.x, left.y), (right.x, right.y))
                    if left and right else 0.0)
            if span < rig.MIN_SPAN:
                issues.append(f"span {span:.3f} below MIN_SPAN {rig.MIN_SPAN}")
            _, _, box_w, box_h = person.box
            if max(box_w, box_h) < 0.35:
                issues.append(f"body fills only {max(box_w, box_h) * 100:.0f}% "
                              f"of the frame — move closer or widen the view")
            # Is the body even in the shape being aimed for? Without this the
            # check happily confirms a standing person over and over while the
            # pose it is supposed to be framing is a lying one.
            joints = rig.player_joints(person, confidence)
            alt = body_frame(joints)
            axis = abs(alt["_axis_deg"]) if alt else None
            band = EXPECTED_TORSO.get(expect)
            if band and axis is not None and not (band[0] <= axis <= band[1]):
                issues.insert(0, f"torso is {axis:.0f}° from vertical — "
                                 f"{expect} needs {band[0]:.0f}-{band[1]:.0f}°"
                                 f" (are you actually in the pose?)")
            score = (len(ESSENTIAL) - len(missing)) + min(span, 0.2) * 10
            if not issues and score > best_score:
                best_score, best = score, (frame.copy(), person)
            if best is None and person is not None:
                best = (frame.copy(), person)

        if not issues:
            ready_frames += 1
        if window:
            import cv2
            cv2.imshow(window, overlay(frame, person, people, issues, confidence))
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                print("\n  (stopped from the window)")
                break
        now = time.monotonic()
        if now - spoke_at >= 2.0:
            spoke_at = now
            if issues:
                print(f"    not yet: {issues[0]}", flush=True)
                beep(2, hertz=440, ms=90)
            else:
                _, _, box_w, box_h = person.box
                joints = rig.player_joints(person, confidence)
                alt = body_frame(joints)
                axis = f"{abs(alt['_axis_deg']):.0f}deg" if alt else "?"
                print(f"    READY — one person, all {len(ESSENTIAL)} joints, "
                      f"box {box_w * 100:.0f}x{box_h * 100:.0f}% of frame, "
                      f"torso {axis} from vertical", flush=True)
                beep(1, hertz=1180, ms=160)

    if window:
        import cv2
        cv2.destroyWindow(window)
        cv2.waitKey(1)
    shot = out / f"{placement}-framing.png"
    saved = False
    if best is not None:
        saved = annotate(best[0], best[1], shot)
    rate = ready_frames / samples if samples else 0.0
    print(f"\n  framing ready in {rate * 100:.0f}% of {samples} frames")
    if saved:
        print(f"  annotated frame: {shot}")
    return {"ready_rate": round(rate, 3), "samples": samples,
            "annotated": str(shot) if saved else ""}


#: How to get into each pose, for the person on the mat. Shown on the Pi's own
#: screen during the countdown, because being told to do Supine Twist is not
#: the same as knowing how, and a pose done wrong measures something real about
#: the wrong shape.
#:
#: Deliberately plain, and deliberately not therapeutic advice: these are
#: descriptions of a shape, for somebody who has chosen to get into it.
POSE_GUIDE: dict[str, tuple[str, tuple[str, ...]]] = {
    "savasana": ("SAVASANA  (lie flat on your back)", (
        "Lie on your back along the mat, head away from the clutter.",
        "Legs long, a little apart. Arms by your sides, palms up.",
        "Let the shoulders drop. Stay still and breathe.")),
    "childs-pose": ("CHILD'S POSE  (kneel and fold forward)", (
        "Kneel with big toes together and knees comfortably wide.",
        "Sit your hips back toward your heels.",
        "Fold forward, arms stretched out in front, forehead down.")),
    "downward-dog": ("DOWNWARD DOG  (hips high, upside-down V)", (
        "Hands and feet on the mat, hands shoulder-width apart.",
        "Lift the hips high so the body makes an upside-down V.",
        "Arms and back long; heels reaching toward the floor.")),
    "easy-seat": ("EASY SEAT  (sit cross-legged)", (
        "Sit cross-legged, facing the camera.",
        "Spine tall, crown of the head lifting.",
        "Hands resting on the knees, shoulders relaxed.")),
    "cow": ("COW  (hands and knees, belly drops)", (
        "Hands and knees: wrists under shoulders, knees under hips.",
        "Let the belly drop and lift the chest and tailbone.",
        "Look gently forward. Hold there -- do not rock into Cat.")),
    "supine-twist": ("SUPINE TWIST  (lie down, knees across)", (
        "Lie on your back and hug both knees in.",
        "Let the knees fall together to one side, toward the floor.",
        "Arms out wide in a T. Turn your head the other way.")),
}


#: A stick figure of each pose, to show rather than describe.
#:
#: Points are (x, y) in a 0..1 panel, y downward, and the floor is the line at
#: the bottom. Side-on views, because that is how a yoga shape reads: from the
#: front, Child's Pose and Cow are both "a person crouching". Easy Seat is the
#: exception and is drawn from the front, which is how it is performed here.
#:
#: Hand-drawn rather than generated: the floor poses have no bone tables yet --
#: that is what this whole spike is deciding -- so there is nothing to render
#: from, and a diagram of the joints is clearer for copying than a photograph.
POSE_SKETCH: dict[str, tuple[dict, tuple, str]] = {
    "savasana": ({
        "head": (0.10, 0.55), "shoulder": (0.24, 0.60), "elbow": (0.34, 0.66),
        "wrist": (0.44, 0.68), "hip": (0.56, 0.62), "knee": (0.74, 0.64),
        "ankle": (0.90, 0.66)},
        (("head", "shoulder"), ("shoulder", "elbow"), ("elbow", "wrist"),
         ("shoulder", "hip"), ("hip", "knee"), ("knee", "ankle")),
        "flat on your back, whole body long on the floor"),
    "childs-pose": ({
        "wrist": (0.06, 0.66), "elbow": (0.18, 0.63), "shoulder": (0.34, 0.57),
        "head": (0.21, 0.63), "hip": (0.62, 0.46), "knee": (0.72, 0.64),
        "ankle": (0.88, 0.67)},
        (("wrist", "elbow"), ("elbow", "shoulder"), ("shoulder", "head"),
         ("shoulder", "hip"), ("hip", "knee"), ("knee", "ankle")),
        "kneel, sit back on heels, fold down, arms stretched forward"),
    "downward-dog": ({
        "wrist": (0.10, 0.72), "elbow": (0.20, 0.55), "shoulder": (0.32, 0.38),
        "head": (0.26, 0.49), "hip": (0.54, 0.16), "knee": (0.72, 0.45),
        "ankle": (0.88, 0.72)},
        (("wrist", "elbow"), ("elbow", "shoulder"), ("shoulder", "head"),
         ("shoulder", "hip"), ("hip", "knee"), ("knee", "ankle")),
        "hands and feet down, HIPS PUSHED HIGH, an upside-down V"),
    "easy-seat": ({
        "head": (0.50, 0.20), "shoulder": (0.50, 0.34),
        "shoulderL": (0.38, 0.34), "shoulderR": (0.62, 0.34),
        "elbowL": (0.32, 0.48), "elbowR": (0.68, 0.48),
        "wristL": (0.34, 0.58), "wristR": (0.66, 0.58),
        "hipL": (0.44, 0.58), "hipR": (0.56, 0.58),
        "kneeL": (0.26, 0.66), "kneeR": (0.74, 0.66),
        "ankleL": (0.47, 0.68), "ankleR": (0.53, 0.68)},
        (("head", "shoulder"), ("shoulderL", "shoulderR"),
         ("shoulderL", "elbowL"), ("elbowL", "wristL"),
         ("shoulderR", "elbowR"), ("elbowR", "wristR"),
         ("shoulderL", "hipL"), ("shoulderR", "hipR"), ("hipL", "hipR"),
         ("hipL", "kneeL"), ("kneeL", "ankleL"),
         ("hipR", "kneeR"), ("kneeR", "ankleR")),
        "sit cross-legged FACING the camera, back tall"),
    "cow": ({
        "wrist": (0.16, 0.72), "elbow": (0.16, 0.59), "shoulder": (0.18, 0.43),
        "head": (0.07, 0.37), "sag": (0.44, 0.56), "hip": (0.70, 0.41),
        "knee": (0.74, 0.59), "ankle": (0.87, 0.72)},
        (("wrist", "elbow"), ("elbow", "shoulder"), ("shoulder", "head"),
         ("shoulder", "sag"), ("sag", "hip"), ("hip", "knee"),
         ("knee", "ankle")),
        "on hands and KNEES, let the belly sag, chest and tail lift"),
    "supine-twist": ({
        "head": (0.10, 0.42), "shoulder": (0.26, 0.46),
        "elbowU": (0.28, 0.26), "wristU": (0.30, 0.12),
        "elbowD": (0.26, 0.64), "wristD": (0.26, 0.78),
        "hip": (0.56, 0.50), "knee": (0.68, 0.72), "ankle": (0.82, 0.74)},
        (("head", "shoulder"), ("shoulder", "elbowU"), ("elbowU", "wristU"),
         ("shoulder", "elbowD"), ("elbowD", "wristD"), ("shoulder", "hip"),
         ("hip", "knee"), ("knee", "ankle")),
        "on your back, arms in a T, both knees dropped to ONE side"),
}


def draw_sketch(canvas, label: str):
    """Paint the pose diagram into the corner of the live view."""
    import cv2
    entry = POSE_SKETCH.get(label)
    if entry is None:
        return canvas
    points, bones, caption = entry
    height, width = canvas.shape[:2]
    panel_w, panel_h = int(width * 0.36), int(height * 0.52)
    x0, y0 = width - panel_w - 16, height - panel_h - 16

    box = canvas[y0:y0 + panel_h, x0:x0 + panel_w]
    cv2.rectangle(box, (0, 0), (panel_w, panel_h), (250, 250, 245), -1)
    cv2.rectangle(box, (0, 0), (panel_w - 1, panel_h - 1), (60, 60, 60), 2)

    def at(name):
        px, py = points[name]
        return int(px * panel_w), int(py * panel_h)

    # The floor, so "on the floor" is unmistakable.
    cv2.line(box, (int(0.02 * panel_w), int(0.76 * panel_h)),
             (int(0.98 * panel_w), int(0.76 * panel_h)), (150, 150, 150), 2)
    for a, b in bones:
        cv2.line(box, at(a), at(b), (170, 60, 30), 6, cv2.LINE_AA)
    for name in points:
        cv2.circle(box, at(name), 6, (40, 40, 40), -1)
    head = points.get("head")
    if head:
        cv2.circle(box, at("head"), 15, (170, 60, 30), 3, cv2.LINE_AA)
    cv2.putText(box, "COPY THIS SHAPE", (10, 22), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (40, 40, 40), 1, cv2.LINE_AA)
    # Wrap on words, to a width the panel can actually hold: at this font a
    # character is about eight pixels, and a caption clipped mid-word is worse
    # than no caption.
    per_line = max(14, int(panel_w / 8.4))
    words, lines, current = caption.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 <= per_line:
            current = f"{current} {word}".strip()
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    for i, chunk in enumerate(lines[:3]):
        cv2.putText(box, chunk, (10, panel_h - 44 + i * 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (40, 40, 40), 1, cv2.LINE_AA)
    return canvas


def guide_for(label: str) -> tuple[str, tuple[str, ...]]:
    if label in POSE_GUIDE:
        return POSE_GUIDE[label]
    pose = POSES.get(label)
    if pose is not None:
        return (f"{pose.name.upper()}  (standing)", (pose.instruction, pose.cue))
    return (label.replace("-", " ").upper(), ())


def guide_screen(frame, person, people, confidence: float, title: str,
                 lines: tuple[str, ...], countdown_left: float, capturing: bool,
                 label_key: str = ""):
    """The live view with the pose, the instructions and the clock on it."""
    import cv2
    canvas = overlay(frame, person, people, [] if capturing else ["_"], confidence)
    height, width = canvas.shape[:2]

    # Replace the overlay's banner with the pose title and the clock.
    cv2.rectangle(canvas, (0, 0), (width, 132),
                  (0, 90, 0) if capturing else (70, 40, 0), -1)
    cv2.putText(canvas, title[:52], (16, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.95,
                (255, 255, 255), 2, cv2.LINE_AA)
    for index, line in enumerate(lines[:3]):
        cv2.putText(canvas, line[:78], (16, 74 + index * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (215, 235, 215), 1,
                    cv2.LINE_AA)
    if capturing:
        cv2.putText(canvas, "HOLD STILL", (width - 250, 44),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 255, 120), 3,
                    cv2.LINE_AA)
    else:
        cv2.putText(canvas, f"{int(math.ceil(countdown_left))}",
                    (width - 130, 60), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                    (120, 220, 255), 4, cv2.LINE_AA)
    return draw_sketch(canvas, label_key)


def beep(count: int = 1, hertz: int = 880, ms: int = 140) -> None:
    """An audible cue, because the person being measured is on a mat.

    Whoever is holding Savasana cannot see a terminal, and a countdown they
    cannot hear is a countdown that gets missed -- which shows up later as a
    run where the model found nobody and nobody knows why. Best effort: if
    there is no `aplay`, or no sink, the run carries on silently.
    """
    import array
    import subprocess
    import wave
    try:
        rate = 22050
        samples = array.array("h")
        for index in range(int(rate * ms / 1000)):
            angle = 2.0 * math.pi * hertz * index / rate
            # A short fade at each end, so the cue does not click.
            fade = min(1.0, index / 200.0,
                       (int(rate * ms / 1000) - index) / 200.0)
            samples.append(int(12000 * fade * math.sin(angle)))
        path = Path("/tmp/probe-beep.wav")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(samples.tobytes())
        for _ in range(count):
            subprocess.run(["aplay", "-q", str(path)], timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def countdown(seconds: float, label: str, lease=None, engine=None,
              window: str = "", confidence: float = 0.45) -> None:
    """Time to get into the pose, with the pose shown on the Pi's screen.

    When there is a live window the camera keeps running through the countdown,
    so the person can see their own skeleton and fix the framing *before* the
    seconds that get measured, rather than finding out afterwards that a foot
    was out of shot.
    """
    title, lines = guide_for(label)
    print(f"\n  Get into {title}", flush=True)
    for line in lines:
        print(f"      {line}", flush=True)
    beep(2, hertz=660)
    started = time.monotonic()
    spoken = None
    while True:
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            break
        whole = int(math.ceil(left))
        if whole != spoken:
            spoken = whole
            print(f"    {whole}...", flush=True)
            if whole <= 3:
                beep(1, hertz=660, ms=90)
        if window and lease is not None and engine is not None:
            frame, captured_at = lease.frame()
            if frame is not None:
                import cv2
                result = engine.infer(frame, captured_at=captured_at)
                people = list(result.persons)
                person = max(people, key=lambda p: p.area) if people else None
                cv2.imshow(window, guide_screen(frame, person, people,
                                                confidence, title, lines,
                                                left, False, label))
                cv2.waitKey(1)
        else:
            time.sleep(0.1)
    print("    GO -- hold still.\n", flush=True)
    beep(1, hertz=1180, ms=260)


def show(pose_label: str, mode: str, row: dict) -> None:
    call, reasons = verdict(row, pose_label)
    print(f"\n  {pose_label} @ {mode}")
    print(f"    frames {row['frames']}, person in {row['person_rate'] * 100:.0f}%")
    print(f"    span mean {row['span'].get('mean', 0):.3f} "
          f"(MIN_SPAN {rig.MIN_SPAN}), ok in {row['span_ok_rate'] * 100:.0f}%")
    print(f"    measure() empty in {row['measure_empty_rate'] * 100:.0f}%")
    if row["metrics"]:
        parts = [f"{n}={row['metrics'][n]['mean']:+.2f}"
                 for n in RATIO_METRICS if n in row["metrics"]]
        print(f"    image-axis ratios : {'  '.join(parts) or 'none'}")
    if row["body_frame"]:
        parts = [f"{n}={row['body_frame'][n]['mean']:+.2f}"
                 for n in RATIO_METRICS if n in row["body_frame"]]
        print(f"    body-frame ratios : {'  '.join(parts) or 'none'}")
    print(f"    --> {call}")
    for reason in reasons:
        print(f"        - {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect", default="",
                        help="the pose the framing check should insist on, "
                             "e.g. savasana. Without it the check will happily "
                             "confirm a standing body.")
    parser.add_argument("--live", action="store_true",
                        help="show a full-screen live view with the skeleton "
                             "and verdict on the Pi's own display")
    parser.add_argument("--framing", type=float, default=0.0,
                        help="run a live framing check for this many seconds "
                             "and exit. Do this before collecting anything.")
    parser.add_argument("--selftest", action="store_true",
                        help="prove the body-frame maths offline and exit; "
                             "needs no camera, no accelerator and no person")
    parser.add_argument("--poses", nargs="+", default=[],
                        help="what the person will do, in order, e.g. "
                             "savasana childs-pose downward-dog. One lease "
                             "and one countdown per pose.")
    parser.add_argument("--placement", default="unknown",
                        help="where the camera is, e.g. desk or raised-tilted")
    parser.add_argument("--modes", nargs="*", default=list(DEFAULT_MODES))
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--countdown", type=float, default=8.0)
    parser.add_argument("--confidence", type=float, default=None,
                        help="keypoint confidence (default: motion config)")
    parser.add_argument("--standing", default="",
                        help="also re-score this pose id, e.g. warrior_two_left")
    parser.add_argument("--out", default="design/yoga-v2/spike")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.poses and not args.framing:
        parser.error("--poses is required unless --selftest or --framing")

    settings = config_mod.load(args.config)
    confidence = (args.confidence if args.confidence is not None
                  else settings.motion.keypoint_confidence)

    identity = hailo_pose.identify()
    if not identity.get("available"):
        print("FAIL: no Hailo device. Stop the aipi5 service first.")
        return 1

    camera = Camera(settings.camera)
    camera.open()
    try:
        engine = hailo_pose.HailoPose(
            settings.motion.pose_model,
            score_threshold=settings.motion.person_confidence,
            iou_threshold=settings.motion.iou)
    except hailo_pose.PoseUnavailable as exc:
        print(f"FAIL: {exc}")
        camera.close()
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_window = ""
    report = {"poses": args.poses, "placement": args.placement,
              "confidence": confidence, "runs": {}}

    if args.framing:
        print(f"── framing check at placement '{args.placement}' ──")
    else:
        print(f"── {len(args.poses)} pose(s) at placement '{args.placement}', "
              f"{len(args.modes)} mode(s) ──")
        print(f"   {' → '.join(args.poses)}")
    for mode in args.modes:
        width, height, fps, fourcc = parse_mode(mode)
        try:
            lease = CameraLease(camera, width=width, height=height, fps=fps,
                                fourcc=fourcc)
        except TypeError:
            lease = CameraLease(camera, width=width, height=height, fps=fps)
        try:
            lease.acquire()
        except CameraLeaseError as exc:
            print(f"  {mode}: {exc}")
            continue
        try:
            time.sleep(0.6)                    # let exposure settle
            if args.live and not args.framing and not run_window:
                try:
                    import cv2
                    run_window = "AIPI5 yoga probe"
                    cv2.namedWindow(run_window, cv2.WINDOW_NORMAL)
                    cv2.setWindowProperty(run_window, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_FULLSCREEN)
                except Exception as exc:  # noqa: BLE001
                    print(f"  (no live window: {exc})")
                    run_window = ""
            if args.framing:
                report["framing"] = framing(lease, engine, args.framing,
                                            confidence, out, args.placement,
                                            live=args.live, expect=args.expect)
                break
            # One lease for the whole list. Re-acquiring between poses would
            # cost a mode renegotiation and an exposure settle each time, and
            # the person on the mat would be waiting through both.
            for pose_label in args.poses:
                countdown(args.countdown, pose_label, lease, engine,
                          run_window, confidence)
                row = capture(lease, engine, args.seconds, confidence,
                              run_window, pose_label)
                frame, person = row.pop("_best_frame"), row.pop("_best_person")
                if frame is not None and person is not None:
                    shot = (out / f"{args.placement}-{pose_label}-"
                                  f"{mode.replace(':', '-')}.png")
                    if annotate(frame, person, shot):
                        row["annotated"] = str(shot)
                row["capture"] = lease.describe()
                report["runs"].setdefault(mode, {})[pose_label] = row
                show(pose_label, mode, row)
                beep(2, hertz=520, ms=110)      # that one is done, relax

            if args.standing:
                if args.standing not in POSES:
                    print(f"    (no pose called {args.standing!r}; skipping)")
                else:
                    countdown(args.countdown, args.standing, lease, engine,
                              run_window, confidence)
                    check = standing_check(lease, engine, args.seconds,
                                           confidence, args.standing)
                    report["runs"][mode]["_standing"] = check
                    print(f"    standing regression {args.standing}: "
                          f"accuracy mean "
                          f"{check['accuracy'].get('mean', 0):.3f}")
                    beep(2, hertz=520, ms=110)
        finally:
            lease.release()

    beep(3, hertz=1180, ms=200)                 # the whole run is over
    if run_window:
        import cv2
        cv2.destroyWindow(run_window)
        cv2.waitKey(1)
    engine.close()
    camera.close()

    path = out / f"{args.placement}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(f"\n  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
