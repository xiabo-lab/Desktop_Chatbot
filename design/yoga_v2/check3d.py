#!/usr/bin/env python3
"""Check the 3D pose library by arithmetic instead of by eye.

Every pose in `poses3d` is a flat picture, and a flat picture can be run
through the same forward kinematics `rig.py` uses. That means the questions
that actually decide whether a pose reads -- are both hands on the floor, is
the knee under the hip, is the raised leg level -- have numeric answers, and do
not need a screenshot and a round trip to the Pi to ask.

What it will not tell you is whether a pose looks *good*. It tells you whether
the body it describes is geometrically the body it claims to be, which is the
part that was going wrong: Tabletop with the hips a hand's width too low is a
mistake of eleven centimetres, invisible in a thumbnail and obvious once she is
on the mat at full size.

    python -m yoga_v2.check3d            # every pose, only the complaints
    python -m yoga_v2.check3d --all      # every pose, with its measurements
    python -m yoga_v2.check3d table cat  # just these
"""

from __future__ import annotations

import argparse
import math
import sys

if __package__ in (None, ""):
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
else:
    import pathlib

# `python -m yoga_v2.check3d` is documented to run from design/.  Add the
# repository root as well so that pass includes the 32 scored production poses
# instead of silently checking only the 60 authored 3D entries.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from yoga_v2 import poses3d

#: The same bone lengths `rig.py` uses, in units of the spine. Kept as a copy
#: rather than an import so this file can be read on its own, and checked
#: against the original below so the copy cannot rot.
RIG = {"spine": 1.00, "neck": 0.40, "upper_arm": 0.64, "forearm": 0.56,
       "thigh": 0.88, "shin": 0.86, "shoulder_half": 0.42, "hip_half": 0.30}

#: Metres of real coach per spine unit, so the numbers below read as parts of
#: a body rather than as abstract units. The VRM stands 1.71 m and a standing
#: table measures 3.14 units from sole to crown, so one unit is 0.545 m.
METRE = 1.71 / 3.14

#: How far off the floor a joint may be and still count as resting on it. A
#: hand is not a point -- the palm is a few centimetres across, and the ankle
#: joint sits above the sole -- so exact contact is the wrong test.
TOUCH = 0.16


def joints(table: dict, side_on: bool = False) -> dict[str, tuple[float, float]]:
    """Where every joint lands, hips at the origin, y downward as in the rig.

    `side_on` is the one thing a flat picture has to be told. Seen from the
    side, the two shoulders sit one behind the other rather than apart, and a
    checker that kept them apart would put the near knee half a metre above the
    far one and then complain that the near one was floating.
    """

    def step(start, angle, length):
        radians = math.radians(angle)
        return (start[0] + math.cos(radians) * length,
                start[1] + math.sin(radians) * length)

    hip = (0.0, 0.0)
    out = {"hip": hip}
    out["neck_base"] = step(hip, table["spine"], RIG["spine"])
    out["head"] = step(out["neck_base"], table["neck"], RIG["neck"])
    # The shoulder line is square across the spine unless a pose says otherwise.
    across = table["spine"] + 90
    half_shoulder = 0.0 if side_on else RIG["shoulder_half"]
    half_hip = 0.0 if side_on else RIG["hip_half"]
    out["left_shoulder"] = step(out["neck_base"], across + 180, half_shoulder)
    out["right_shoulder"] = step(out["neck_base"], across, half_shoulder)
    out["left_hip"] = step(hip, across + 180, half_hip)
    out["right_hip"] = step(hip, across, half_hip)
    for side in ("left", "right"):
        out[f"{side}_elbow"] = step(out[f"{side}_shoulder"],
                                    table[f"{side}_upper_arm"], RIG["upper_arm"])
        out[f"{side}_hand"] = step(out[f"{side}_elbow"],
                                   table[f"{side}_forearm"], RIG["forearm"])
        out[f"{side}_knee"] = step(out[f"{side}_hip"],
                                   table[f"{side}_thigh"], RIG["thigh"])
        out[f"{side}_foot"] = step(out[f"{side}_knee"],
                                   table[f"{side}_shin"], RIG["shin"])
    return out


def measure(pose: dict) -> dict:
    """Heights above the ground, in metres, for the parts that touch it."""
    points = joints(pose["bones"], side_on=bool(pose.get("root")
                                                and not pose.get("plane")))
    floor = max(y for _, y in points.values())   # y grows downward
    height = {name: (floor - y) * METRE for name, (_, y) in points.items()}
    span = (max(x for x, _ in points.values())
            - min(x for x, _ in points.values())) * METRE
    return {"height": height, "tall": max(height.values()), "long": span,
            "points": points, "floor": floor}


#: What each pose claims about itself, as the joints that should be on the
#: floor. Only poses with a real contact story are listed; a standing pose has
#: nothing interesting to say here.
GROUNDED = {
    "table": ("left_knee", "right_knee", "left_hand", "right_hand"),
    "cat": ("left_knee", "right_knee", "left_hand", "right_hand"),
    "cow": ("left_knee", "right_knee", "left_hand", "right_hand"),
    "bird_dog_left": ("right_knee", "left_hand"),
    "bird_dog_right": ("left_knee", "right_hand"),
    "thread_needle_left": ("right_knee", "left_knee", "right_hand"),
    "thread_needle_right": ("right_knee", "left_knee", "left_hand"),
    "child_pose": ("left_knee", "right_knee", "left_hand", "right_hand", "head"),
    "downward_dog": ("left_foot", "right_foot", "left_hand", "right_hand"),
    "plank": ("left_foot", "right_foot", "left_hand", "right_hand"),
    "side_plank_left": ("left_foot", "left_hand"),
    "side_plank_right": ("right_foot", "right_hand"),
    "sphinx": ("left_foot", "right_foot", "left_hand", "right_hand", "hip"),
    "cobra": ("left_foot", "right_foot", "left_hand", "right_hand", "hip"),
    "low_lunge_left": ("left_foot", "right_knee"),
    "low_lunge_right": ("right_foot", "left_knee"),
    "low_lunge_reach_left": ("left_foot", "right_knee"),
    "low_lunge_reach_right": ("right_foot", "left_knee"),
    "half_split_left": ("left_foot", "right_knee", "left_hand", "right_hand"),
    "half_split_right": ("right_foot", "left_knee", "left_hand", "right_hand"),
    "lizard_left": ("left_foot", "right_knee", "left_hand", "right_hand"),
    "lizard_right": ("right_foot", "left_knee", "left_hand", "right_hand"),
    "pigeon_prep_left": ("left_knee", "right_knee", "left_hand", "right_hand"),
    "pigeon_prep_right": ("left_knee", "right_knee", "left_hand", "right_hand"),
    "bridge": ("head", "left_shoulder", "right_shoulder",
               "left_elbow", "right_elbow", "left_hand", "right_hand",
               "left_foot", "right_foot"),
    "chair": ("left_foot", "right_foot"),
    "staff": ("hip", "left_foot", "right_foot"),
    "seated_forward_fold": ("hip", "left_foot", "right_foot"),
    "boat": ("hip",),
    "seated_hamstring_left": ("hip", "left_foot", "right_foot"),
    "seated_hamstring_right": ("hip", "left_foot", "right_foot"),
    # Cross-legged feet rest against the opposite calf/ankle rather than both
    # being forced through the mat. Butterfly soles meet one another; their
    # ankle joints need not coincide with the floor plane.
    "easy_seat": ("hip", "left_knee", "right_knee"),
    "easy_seat_breath": ("hip", "left_knee", "right_knee"),
    "butterfly": ("hip", "left_knee", "right_knee"),
    "high_lunge_left": ("left_foot", "right_foot"),
    "high_lunge_right": ("left_foot", "right_foot"),
    "pyramid_left": ("left_foot", "right_foot"),
    "pyramid_right": ("left_foot", "right_foot"),
    "warrior_three_left": ("left_foot",),
    "warrior_three_right": ("right_foot",),
}

#: The tallest a pose of each kind has any business being, in metres. A
#: Tabletop that measures a metre tall is not a Tabletop.
CEILING = {"low": 1.05, "kneel": 1.70, "seated": 1.25, "standing": 2.25}
POSE_CEILING = {"seated_side_stretch_left": 1.45,
                "seated_side_stretch_right": 1.45}


#: How far past straight a joint may go before it reads as broken. A few
#: degrees is ordinary soft tissue and ordinary rounding; twelve is a leg bent
#: the wrong way, and once it is on a 1.7-metre character nobody looks at
#: anything else.
HYPEREXTENSION = 8.0

# Conservative active ranges for a healthy adult.  The flat pose tables can
# observe hinge flexion and neck/spine direction directly; axial twist and
# wrist/ankle orientation are checked from `bones3d` and `terminals`.  Keeping
# the complete profile here makes the contract explicit even where a 2D table
# cannot exercise every degree of freedom.
JOINT_LIMITS = {
    "shoulder": {"flexion": (-170, 180), "abduction": (-170, 180),
                 "rotation": (-90, 90)},
    "elbow": {"flexion": (0, 155), "hyperextension": (-8, 0)},
    "wrist": {"flexion": (-80, 80), "deviation": (-30, 30)},
    "fingers": {"flexion": (-20, 90)},
    "hip": {"flexion": (-30, 135), "abduction": (-50, 50),
            "rotation": (-45, 45)},
    "knee": {"flexion": (0, 155), "hyperextension": (-8, 0)},
    "ankle": {"flexion": (-50, 35), "inversion": (-25, 20)},
    "spine": {"flexion": (-55, 75), "side_bend": (-45, 45),
              "twist": (-55, 55)},
    "neck": {"flexion": (-65, 65), "side_bend": (-45, 45),
             "twist": (-70, 70)},
}

# The arithmetic pass represents a joint as a zero-radius point. The VRM knee
# has visible volume around that point, and the owner's approved symmetrical
# Child's Pose visibly rests that volume on the mat in all four camera views.
# Keep the ordinary 16 cm tolerance everywhere else; this is mesh-calibrated
# contact allowance, not permission for the limb to penetrate the floor.
CONTACT_ALLOWANCE = {
    "child_pose": {"left_knee": 0.23, "right_knee": 0.23},
}

# Passive Yoga shapes can close a knee farther than ordinary active range;
# 170° is the hard stop, with thigh/calf contact handled separately below.
MAX_HINGE_FLEXION = 170.0
MAX_NECK_FROM_SPINE = 70.0


def _wrap(angle: float) -> float:
    return (angle + 180) % 360 - 180


def joints_bend_the_right_way(pose: dict) -> list[str]:
    """Knees fold backwards and elbows fold forwards. Always.

    Only a side-on pose can be checked this way, because only a side-on pose
    puts the hinge in the plane the table is drawn in. That is not much of a
    limitation: a frontal pose cannot express a bent knee in the first place,
    which is why the sagittal shapes were moved to a side view at all.
    """
    if not pose.get("root") or pose.get("plane"):
        return []
    table, faults = pose["bones"], []
    for side in ("left", "right"):
        knee = _wrap(table[f"{side}_shin"] - table[f"{side}_thigh"])
        if knee < -HYPEREXTENSION:
            faults.append(f"{side} knee bends {-knee:.0f} the wrong way")
        # The elbow is only checked while the upper arm points broadly *down*.
        # Once it swings overhead the fold reverses sense -- reach behind you
        # and bend, and the hand comes forward over your head, which by the
        # arithmetic below is a backwards elbow and by anatomy is not. Rather
        # than model a shoulder properly, the rule declines to answer where it
        # cannot answer honestly, which still covers every standing, seated and
        # quadruped pose in the library.
        upper = table[f"{side}_upper_arm"]
        if math.sin(math.radians(upper)) > 0.5:
            elbow = _wrap(upper - table[f"{side}_forearm"])
            if elbow < -HYPEREXTENSION:
                faults.append(f"{side} elbow bends {-elbow:.0f} the wrong way")
    return faults


def joints_stay_in_range(pose: dict) -> list[str]:
    """Ranges visible in a direction table, plus all authored 3D rotations."""
    table, faults = pose["bones"], []
    neck = abs(_wrap(table["neck"] - table["spine"]))
    if neck > MAX_NECK_FROM_SPINE:
        faults.append(f"neck bends {neck:.0f}° from spine (limit 70°)")
    for side in ("left", "right"):
        elbow = abs(_wrap(table[f"{side}_forearm"]
                          - table[f"{side}_upper_arm"]))
        knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
        if elbow > MAX_HINGE_FLEXION:
            faults.append(f"{side} elbow flexes {elbow:.0f}° "
                          f"(limit {MAX_HINGE_FLEXION:.0f}°)")
        if knee > MAX_HINGE_FLEXION:
            faults.append(f"{side} knee flexes {knee:.0f}° "
                          f"(limit {MAX_HINGE_FLEXION:.0f}°)")
    for bone, turns in (pose.get("bones3d") or {}).items():
        if bone in ("spine", "chest") and abs(turns[1] if len(turns) > 1 else 0) > 55:
            faults.append(f"{bone} twists beyond 55°")
        if bone == "neck" and abs(turns[1] if len(turns) > 1 else 0) > 70:
            faults.append("neck twists beyond 70°")
    return faults


def _direction3d(angle: float, depth: float) -> tuple[float, float, float]:
    """Unit direction for a table angle tilted out of its authored plane."""
    a, d = math.radians(angle), math.radians(depth)
    return (math.cos(a) * math.cos(d), math.sin(a) * math.cos(d),
            math.sin(d))


def _angle3d(a: tuple[float, float, float],
             b: tuple[float, float, float]) -> float:
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def joints3d(pose: dict) -> dict[str, tuple[float, float, float]]:
    """Forward kinematics with the pose's authored lateral depth included."""
    table, depth = pose["bones"], pose.get("depth3d") or {}

    def add(a, direction, length):
        return tuple(a[i] + direction[i] * length for i in range(3))

    def direction(bone):
        return _direction3d(table[bone], depth.get(bone, 0))

    hip = (0.0, 0.0, 0.0)
    out = {"hip": hip}
    out["neck_base"] = add(hip, direction("spine"), RIG["spine"])
    out["head"] = add(out["neck_base"], direction("neck"), RIG["neck"])
    side_on = bool(pose.get("root") and not pose.get("plane"))
    if side_on:
        # Rig sides are player sides and therefore mirror the avatar's own
        # left/right.  This sign is calibrated against the rendered VRM.
        shoulder_offsets = {"left": (0, 0, RIG["shoulder_half"]),
                            "right": (0, 0, -RIG["shoulder_half"])}
        hip_offsets = {"left": (0, 0, RIG["hip_half"]),
                       "right": (0, 0, -RIG["hip_half"])}
    else:
        across = math.radians(table["spine"] + 90)
        shoulder_offsets = {
            "left": (-math.cos(across) * RIG["shoulder_half"],
                     -math.sin(across) * RIG["shoulder_half"], 0),
            "right": (math.cos(across) * RIG["shoulder_half"],
                      math.sin(across) * RIG["shoulder_half"], 0),
        }
        hip_offsets = {
            "left": (-math.cos(across) * RIG["hip_half"],
                     -math.sin(across) * RIG["hip_half"], 0),
            "right": (math.cos(across) * RIG["hip_half"],
                      math.sin(across) * RIG["hip_half"], 0),
        }
    for side in ("left", "right"):
        out[f"{side}_shoulder"] = tuple(
            out["neck_base"][i] + shoulder_offsets[side][i] for i in range(3))
        out[f"{side}_hip"] = tuple(
            hip[i] + hip_offsets[side][i] for i in range(3))
        out[f"{side}_elbow"] = add(out[f"{side}_shoulder"],
                                    direction(f"{side}_upper_arm"),
                                    RIG["upper_arm"])
        out[f"{side}_hand"] = add(out[f"{side}_elbow"],
                                   direction(f"{side}_forearm"),
                                   RIG["forearm"])
        out[f"{side}_knee"] = add(out[f"{side}_hip"],
                                   direction(f"{side}_thigh"), RIG["thigh"])
        out[f"{side}_foot"] = add(out[f"{side}_knee"],
                                   direction(f"{side}_shin"), RIG["shin"])
    return out


def _point_segment_distance3d(point, start, end) -> float:
    v = tuple(end[i] - start[i] for i in range(3))
    w = tuple(point[i] - start[i] for i in range(3))
    length2 = sum(x * x for x in v)
    t = 0.0 if length2 == 0 else max(0.0, min(1.0,
        sum(w[i] * v[i] for i in range(3)) / length2))
    nearest = tuple(start[i] + t * v[i] for i in range(3))
    return math.dist(point, nearest) * METRE


def _segment_segment_distance3d(p1, q1, p2, q2) -> float:
    """Shortest distance between two finite 3D centrelines, in metres."""
    u = tuple(q1[i] - p1[i] for i in range(3))
    v = tuple(q2[i] - p2[i] for i in range(3))
    w = tuple(p1[i] - p2[i] for i in range(3))
    dot = lambda a, b: sum(a[i] * b[i] for i in range(3))
    a, b, c, d, e = dot(u, u), dot(u, v), dot(v, v), dot(u, w), dot(v, w)
    den, eps = a * c - b * b, 1e-9
    s_num, s_den = den, den
    t_num, t_den = den, den
    if den < eps:
        s_num, s_den, t_num, t_den = 0.0, 1.0, e, c
    else:
        s_num, t_num = b * e - c * d, a * e - b * d
        if s_num < 0:
            s_num, t_num, t_den = 0.0, e, c
        elif s_num > s_den:
            s_num, t_num, t_den = s_den, e + b, c
    if t_num < 0:
        t_num = 0.0
        if -d < 0:
            s_num = 0.0
        elif -d > a:
            s_num = s_den
        else:
            s_num, s_den = -d, a
    elif t_num > t_den:
        t_num = t_den
        if -d + b < 0:
            s_num = 0.0
        elif -d + b > a:
            s_num = s_den
        else:
            s_num, s_den = -d + b, a
    s = 0.0 if abs(s_num) < eps else s_num / s_den
    t = 0.0 if abs(t_num) < eps else t_num / t_den
    delta = tuple(w[i] + s * u[i] - t * v[i] for i in range(3))
    return math.sqrt(dot(delta, delta)) * METRE


def researched_pose_faults(name: str, pose: dict) -> list[str]:
    """Biomechanical contracts for the poses in the current approval batch.

    These are deliberately narrower than a person's absolute passive ROM.
    They describe the ordinary, teachable version of each exercise researched
    in `anatomy_sources.md`, not the most flexible human who could perform it.
    """
    table, depth, faults = pose["bones"], pose.get("depth3d") or {}, []
    for bone, turn in depth.items():
        if abs(turn) > 90:
            faults.append(f"{bone} leaves its hinge plane by {abs(turn):.0f}°")

    if name in ("bird_dog_left", "bird_dog_right"):
        reach = "left" if name.endswith("left") else "right"
        support = "right" if reach == "left" else "left"
        reach_knee = abs(_wrap(table[f"{reach}_shin"] -
                               table[f"{reach}_thigh"]))
        support_knee = abs(_wrap(table[f"{support}_shin"] -
                                 table[f"{support}_thigh"]))
        if reach_knee > 10:
            faults.append(f"reaching knee flexes {reach_knee:.0f}° (limit 10°)")
        if not 80 <= support_knee <= 100:
            faults.append(f"supporting knee is {support_knee:.0f}°, expected 80–100°")
        if abs(_wrap(table["neck"] - table["spine"])) > 30:
            faults.append("Bird Dog loses its neutral head/spine line")
        offsets = pose.get("terminal3d") or {}
        for side in ("left_foot", "right_foot"):
            if abs(_wrap((offsets.get(side) or [0, 0])[1])) < 170:
                faults.append(f"{side} is not flipped 180° as owner approved")

    if name == "bridge":
        for side in ("left", "right"):
            knee = abs(_wrap(table[f"{side}_shin"] -
                             table[f"{side}_thigh"]))
            if not 90 <= knee <= 125:
                faults.append(f"{side} bridge knee is {knee:.0f}°, expected 90–125°")
        if any((pose.get("terminals") or {}).get(f"{s}_foot") != "floor_away"
               for s in ("left", "right")):
            faults.append("Bridge feet are not planted flat and facing away")

    if name == "butterfly":
        # Bound Angle is a combined hip motion with knee flexion.  Its feet
        # must be central but anterior to the pelvis; central in XY alone is
        # exactly the old mesh-inside-body bug.
        feet = []
        for side, hip_x in (("left", -RIG["hip_half"]),
                            ("right", RIG["hip_half"])):
            thigh = _direction3d(table[f"{side}_thigh"],
                                  depth.get(f"{side}_thigh", 0))
            shin = _direction3d(table[f"{side}_shin"],
                                 depth.get(f"{side}_shin", 0))
            knee_flex = _angle3d(thigh, shin)
            if not 125 <= knee_flex <= 165:
                faults.append(f"{side} Butterfly knee flexes {knee_flex:.0f}°, expected 125–165°")
            foot = (hip_x + thigh[0] * RIG["thigh"] + shin[0] * RIG["shin"],
                    thigh[1] * RIG["thigh"] + shin[1] * RIG["shin"],
                    thigh[2] * RIG["thigh"] + shin[2] * RIG["shin"])
            feet.append(foot)
            if abs(foot[0]) > 0.20 or foot[2] < 0.20:
                faults.append(f"{side} Butterfly foot is not joined in front of pelvis")
        if feet[0][0] * feet[1][0] > 0:
            faults.append("Butterfly feet cross through one another")

    terminals = pose.get("terminals") or {}
    if name == "cow":
        for side in ("left", "right"):
            knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
            if not 80 <= knee <= 100:
                faults.append(f"{side} Cow knee is {knee:.0f}°, expected 80–100°")
            if terminals.get(f"{side}_foot") != "top_down":
                faults.append(f"{side} Cow foot does not rest on its dorsum")

    if name in ("crescent_moon_left", "crescent_moon_right"):
        bend = abs(_wrap(table["spine"] + 90))
        if not 15 <= bend <= 35:
            faults.append(f"Standing Crescent side bend is {bend:.0f}°, expected 15–35°")
        if any(terminals.get(f"{s}_foot") != "floor" for s in ("left", "right")):
            faults.append("Standing Crescent feet are not both planted")

    if name == "downward_dog":
        for side in ("left", "right"):
            if abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"])) > 12:
                faults.append(f"{side} Down Dog knee is excessively bent")
            if abs(_wrap(table[f"{side}_forearm"] - table[f"{side}_upper_arm"])) > 12:
                faults.append(f"{side} Down Dog elbow is excessively bent")
            if terminals.get(f"{side}_hand") != "floor" or terminals.get(f"{side}_foot") != "floor":
                faults.append(f"{side} Down Dog hand/foot support is incomplete")

    if name in ("easy_seat", "easy_seat_breath"):
        expected = "palm_down" if name == "easy_seat" else "palm_up"
        if abs(_wrap(table["spine"] + 90)) > 8:
            faults.append("Easy Seat spine is not upright")
        if any(terminals.get(f"{s}_hand") != expected for s in ("left", "right")):
            faults.append(f"Easy Seat hands do not use {expected}")

    if name in ("figure_four_left", "figure_four_right"):
        crossed = "left" if name.endswith("left") else "right"
        support = "right" if crossed == "left" else "left"
        crossed_knee = abs(_wrap(table[f"{crossed}_shin"] - table[f"{crossed}_thigh"]))
        support_knee = abs(_wrap(table[f"{support}_shin"] - table[f"{support}_thigh"]))
        if not 75 <= crossed_knee <= 100:
            faults.append(f"crossed Figure Four knee is {crossed_knee:.0f}°, expected 75–100°")
        if not 105 <= support_knee <= 130:
            faults.append(f"supporting Figure Four knee is {support_knee:.0f}°, expected 105–130°")
        if terminals.get(f"{crossed}_foot") != "flex":
            faults.append("crossed Figure Four ankle is not flexed")

    if name == "forward_fold":
        if abs(_wrap(table["spine"] - 90)) > 12:
            faults.append("Forward Fold torso does not hinge down from the hips")
        if any(terminals.get(f"{s}_foot") != "floor" for s in ("left", "right")):
            faults.append("Forward Fold feet are not planted")

    if name == "gentle_back_reach":
        backbend = abs(_wrap(table["spine"] + 90))
        if not 8 <= backbend <= 20:
            faults.append(f"Gentle Back Reach bends {backbend:.0f}°, expected 8–20°")
        if pose.get("root", {}).get("yaw") != 90:
            faults.append("Gentle Back Reach is not authored in the sagittal plane")
        if any(terminals.get(f"{s}_hand") != "palm_to_back" for s in ("left", "right")):
            faults.append("Gentle Back Reach hands do not face the lower back")

    if name == "goddess":
        if abs(_wrap(table["spine"] + 90)) > 8:
            faults.append("Goddess spine is not upright")
        if any(terminals.get(f"{s}_foot") != "floor" for s in ("left", "right")):
            faults.append("Goddess feet are not both planted")
        turns = pose.get("terminal3d") or {}
        if not turns.get("left_foot") or not turns.get("right_foot"):
            faults.append("Goddess feet do not have explicit toe turnout")

    if name == "half_lift":
        if pose.get("root", {}).get("yaw") != 90:
            faults.append("Half Lift is not authored as a sagittal hip hinge")
        if abs(_wrap(table["spine"])) > 15:
            faults.append("Half Lift back is not approximately horizontal")
        for side in ("left", "right"):
            knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
            if knee > 12:
                faults.append(f"{side} Half Lift knee bends {knee:.0f}°")

    if name in ("half_moon_left", "half_moon_right"):
        support = "left" if name.endswith("left") else "right"
        raised = "right" if support == "left" else "left"
        for side in (support, raised):
            knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
            if knee > 12:
                faults.append(f"{side} Half Moon knee bends {knee:.0f}°")
        if terminals.get(f"{support}_foot") != "floor":
            faults.append("Half Moon standing foot is not planted")
        if terminals.get(f"{raised}_foot") != "flex":
            faults.append("Half Moon lifted ankle is not flexed")

    if name in ("half_split_left", "half_split_right"):
        front = "left" if name.endswith("left") else "right"
        back = "right" if front == "left" else "left"
        front_knee = abs(_wrap(table[f"{front}_shin"] - table[f"{front}_thigh"]))
        back_knee = abs(_wrap(table[f"{back}_shin"] - table[f"{back}_thigh"]))
        if front_knee > 12:
            faults.append(f"Half Split front knee bends {front_knee:.0f}°")
        if not 75 <= back_knee <= 105:
            faults.append(f"Half Split back knee is {back_knee:.0f}°, expected 75–105°")
        if terminals.get(f"{front}_foot") != "flex":
            faults.append("Half Split front foot is not flexed")
        if terminals.get(f"{back}_foot") != "top_down":
            faults.append("Half Split back foot does not rest on its dorsum")

    if name in ("hand_to_toe_left", "hand_to_toe_right"):
        support = "left" if name.endswith("left") else "right"
        raised = "right" if support == "left" else "left"
        if abs(_wrap(table["spine"] + 90)) > 8:
            faults.append("Hand-to-Toe torso is not upright")
        for side in (support, raised):
            knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
            if knee > 12:
                faults.append(f"{side} Hand-to-Toe knee bends {knee:.0f}°")
        p = joints(table)
        reach = math.dist(p[f"{raised}_hand"], p[f"{raised}_foot"]) * METRE
        if reach > 0.12:
            faults.append(f"Hand-to-Toe hand misses raised foot by {reach * 100:.0f}cm")
        if terminals.get(f"{support}_foot") != "floor":
            faults.append("Hand-to-Toe standing foot is not planted")
        if terminals.get(f"{raised}_foot") != "flex":
            faults.append("Hand-to-Toe raised foot is not flexed")

    if name == "happy_baby":
        depth = pose.get("depth3d") or {}
        if depth.get("left_thigh", 0) * depth.get("right_thigh", 0) >= 0:
            faults.append("Happy Baby knees do not open to opposite sides")
        for side in ("left", "right"):
            knee = abs(_wrap(table[f"{side}_shin"] - table[f"{side}_thigh"]))
            if not 40 <= knee <= 75:
                faults.append(f"{side} Happy Baby knee direction is {knee:.0f}°, expected 40–75°")
            if terminals.get(f"{side}_foot") != "flex":
                faults.append(f"{side} Happy Baby foot is not flexed")

    if name in ("high_lunge_left", "high_lunge_right"):
        front = "left" if name.endswith("left") else "right"
        back = "right" if front == "left" else "left"
        front_knee = abs(_wrap(table[f"{front}_shin"] - table[f"{front}_thigh"]))
        back_knee = abs(_wrap(table[f"{back}_shin"] - table[f"{back}_thigh"]))
        if not 75 <= front_knee <= 100:
            faults.append(f"High Lunge front knee is {front_knee:.0f}°, expected 75–100°")
        if back_knee > 15:
            faults.append(f"High Lunge back knee bends {back_knee:.0f}°")
        if terminals.get(f"{front}_foot") != "floor":
            faults.append("High Lunge front foot is not planted")
        if terminals.get(f"{back}_foot") != "point":
            faults.append("High Lunge back heel is not lifted onto the toe ball")
        turns = (pose.get("terminal3d") or {}).get(f"{back}_foot")
        if not turns or abs(_wrap(turns[0])) < 140:
            faults.append("High Lunge rear shoe lacks the calibrated toe-ball roll")

    if name == "knees_to_chest":
        p = joints3d(pose)
        for side in ("left", "right"):
            gap = math.dist(p[f"{side}_hand"], p[f"{side}_knee"]) * METRE
            if gap > 0.13:
                faults.append(f"{side} hand misses hugged knee by {gap * 100:.0f}cm")
            if terminals.get(f"{side}_hand") != "palm_in":
                faults.append(f"{side} palm does not face inward around the knees")
        if depth.get("left_thigh", 0) * depth.get("right_thigh", 0) > 0:
            faults.append("Knees-to-Chest legs move to the same lateral side")

    if name in ("lizard_left", "lizard_right"):
        front = "left" if name.endswith("left") else "right"
        back = "right" if front == "left" else "left"
        if terminals.get(f"{front}_foot") != "floor":
            faults.append("Lizard front sole is not planted")
        if terminals.get(f"{back}_foot") != "top_down":
            faults.append("Lizard back foot does not rest dorsum-down")
        for side in ("left", "right"):
            if terminals.get(f"{side}_hand") != "floor":
                faults.append(f"Lizard {side} palm is not weight-bearing")
        if depth.get("left_forearm", 0) * depth.get("right_forearm", 0) >= 0:
            faults.append("Lizard arms occupy one plane and can intersect")

    if name in ("low_lunge_left", "low_lunge_right"):
        front = "left" if name.endswith("left") else "right"
        back = "right" if front == "left" else "left"
        front_knee = abs(_wrap(table[f"{front}_shin"] - table[f"{front}_thigh"]))
        back_knee = abs(_wrap(table[f"{back}_shin"] - table[f"{back}_thigh"]))
        if not 75 <= front_knee <= 100 or not 75 <= back_knee <= 105:
            faults.append("Low Lunge knees do not form the researched support angles")
        if terminals.get(f"{front}_foot") != "floor" or terminals.get(f"{back}_foot") != "top_down":
            faults.append("Low Lunge front sole/back dorsum contact is incomplete")
        p = joints3d(pose)
        for side in ("left", "right"):
            gap = _point_segment_distance3d(
                p[f"{side}_hand"], p[f"{front}_hip"], p[f"{front}_knee"])
            if gap > (0.24 if name == "low_lunge_right" else 0.18):
                faults.append(f"{side} hand misses front thigh by {gap * 100:.0f}cm")
            if terminals.get(f"{side}_hand") != "palm_down":
                faults.append(f"{side} palm does not rest on the front thigh")
    return faults


def terminal_faults(name: str, pose: dict) -> list[str]:
    """Every weight-bearing wrist/ankle must state an orientation."""
    specs = pose.get("terminals") or {}
    faults = []
    for joint in GROUNDED.get(name, ()):
        terminal = joint.replace("hand", "hand").replace("foot", "foot")
        if (joint.endswith("hand") or joint.endswith("foot")) and terminal not in specs:
            faults.append(f"{joint} bears weight without palm/sole orientation")
    for terminal, mode in specs.items():
        if mode not in ("floor", "point", "flex", "relax", "sole_in",
                        "sole_to_leg",
                        "sole_down", "floor_away", "top_down",
                        "palm_forward", "palm_down", "palm_up",
                        "palm_to_back", "palm_in"):
            faults.append(f"{terminal} has unknown orientation {mode!r}")
    return faults


def _point_segment_distance(point, start, end) -> float:
    vx, vy = end[0] - start[0], end[1] - start[1]
    wx, wy = point[0] - start[0], point[1] - start[1]
    length2 = vx * vx + vy * vy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0,
        (wx * vx + wy * vy) / length2))
    return math.hypot(point[0] - (start[0] + t * vx),
                      point[1] - (start[1] + t * vy)) * METRE


# Natural contact is pose semantics, not a collision-check exemption for an
# entire limb.  These lists only suppress the exact pair which is meant to
# meet; every other body-volume check still runs.
HANDS_TO_BODY = {
    "mountain_breath", "standing_twist_left",
    "standing_twist_right", "seated_twist_left", "seated_twist_right",
    "knees_to_chest", "happy_baby", "figure_four_left", "figure_four_right",
    "tree_heart_left", "tree_heart_right", "gentle_back_reach",
}
KNEES_TO_BODY = {
    "knees_to_chest", "happy_baby", "figure_four_left", "figure_four_right",
    "child_pose", "pigeon_prep_left", "pigeon_prep_right",
    "lizard_left", "lizard_right",
}
CROSSED_LEGS = {
    "easy_seat", "easy_seat_breath", "butterfly", "seated_twist_left",
    "seated_twist_right", "seated_side_stretch_left",
    "seated_side_stretch_right", "figure_four_left", "figure_four_right",
}

# Exact forearm-to-front-body contact used while wrapping a raised knee.  The
# opposite arm and both hands remain checked, so this cannot hide an arm going
# through the back or the other side of the torso.
FOREARM_TO_BODY: dict[str, set[str]] = {}


def body_volume_faults(name: str, pose: dict) -> list[str]:
    """Conservative capsule-core checks: contact may occur, penetration may not.

    The thresholds are smaller than the visible skin radii.  Reaching one
    therefore means a joint centre has entered the body's core, not merely
    that two surfaces touch.  That distinction is what permits Yoga's normal
    hand-to-chest, folded-knee and crossed-leg contacts.
    """
    p = joints(pose["bones"], side_on=bool(pose.get("root")
                                           and not pose.get("plane")))
    torso = (p["hip"], p["neck_base"])
    faults = []
    foreshortened = float((pose.get("scales") or {}).get("spine", 1)) < 0.7
    depth = pose.get("depth3d") or {}
    if name not in HANDS_TO_BODY and not foreshortened:
        for side in ("left", "right"):
            # A flat projection cannot judge a chain explicitly moved out of
            # its plane. The 3D pass below remains mandatory for that chain.
            if (depth.get(f"{side}_upper_arm", 0)
                    or depth.get(f"{side}_forearm", 0)):
                continue
            if _point_segment_distance(p[f"{side}_hand"], *torso) < 0.105:
                faults.append(f"{side} hand penetrates torso core")
    if name not in KNEES_TO_BODY and not foreshortened:
        for side in ("left", "right"):
            if depth.get(f"{side}_thigh", 0):
                continue
            if _point_segment_distance(p[f"{side}_knee"], *torso) < 0.13:
                faults.append(f"{side} knee penetrates torso/pelvis core")
    if name not in CROSSED_LEGS and name != "wide_leg_fold":
        # A calf centre entering the opposite thigh's core is unambiguously a
        # pass-through.  Checking endpoints keeps this robust and intentionally
        # conservative for the planar pose representation.
        if _point_segment_distance(p["left_foot"], p["right_hip"],
                                   p["right_knee"]) < 0.065:
            faults.append("left lower leg penetrates right thigh")
        if _point_segment_distance(p["right_foot"], p["left_hip"],
                                   p["left_knee"]) < 0.065:
            faults.append("right lower leg penetrates left thigh")

    grounded = GROUNDED.get(name, ())
    if grounded:
        ground = sum(p[j][1] for j in grounded) / len(grounded)
        allowance = TOUCH / METRE
        for joint, (_, y) in p.items():
            if joint not in grounded and y > ground + allowance:
                faults.append(f"{joint} penetrates floor")
                break
    return faults


def body_volume_faults3d(name: str, pose: dict) -> list[str]:
    """Depth-aware capsule checks for the actual authored limb planes.

    The legacy projection can miss an arm entering the ribs from in front or
    a far leg passing through the near thigh.  This pass uses the same
    `depth3d` directions as the browser solver.  Radii are deliberately core
    radii: ordinary skin contact remains valid, while a centreline entering
    another body's core is rejected.
    """
    p, faults = joints3d(pose), []
    torso = (p["hip"], p["neck_base"])

    def midpoint(a, b):
        return tuple((a[i] + b[i]) / 2 for i in range(3))

    for side in ("left", "right"):
        hand = p[f"{side}_hand"]
        forearm_core = midpoint(p[f"{side}_elbow"], hand)
        if name not in HANDS_TO_BODY:
            if _point_segment_distance3d(hand, *torso) < 0.105:
                faults.append(f"{side} hand penetrates torso core in 3D")
        if (side not in FOREARM_TO_BODY.get(name, set())
                and _point_segment_distance3d(forearm_core, *torso) < 0.09):
            faults.append(f"{side} forearm penetrates torso core in 3D")
    if name not in KNEES_TO_BODY:
        for side in ("left", "right"):
            if _point_segment_distance3d(p[f"{side}_knee"], *torso) < 0.13:
                faults.append(f"{side} knee penetrates torso/pelvis core in 3D")
    if name not in CROSSED_LEGS and name != "wide_leg_fold":
        thigh_gap = _segment_segment_distance3d(
            p["left_hip"], p["left_knee"],
            p["right_hip"], p["right_knee"])
        if thigh_gap < 0.115:
            faults.append(f"thigh cores overlap in 3D ({thigh_gap * 100:.0f}cm)")
        if _point_segment_distance3d(p["left_foot"], p["right_hip"],
                                     p["right_knee"]) < 0.065:
            faults.append("left lower leg penetrates right thigh in 3D")
        if _point_segment_distance3d(p["right_foot"], p["left_hip"],
                                     p["left_knee"]) < 0.065:
            faults.append("right lower leg penetrates left thigh in 3D")
    if name in {"standing_knee_left", "standing_knee_right"}:
        raised = "left" if name.endswith("left") else "right"
        for side in ("left", "right"):
            forearm_core = midpoint(p[f"{side}_elbow"], p[f"{side}_hand"])
            gap = min(
                _point_segment_distance3d(forearm_core, p[f"{raised}_hip"],
                                          p[f"{raised}_knee"]),
                _point_segment_distance3d(forearm_core, p[f"{raised}_knee"],
                                          p[f"{raised}_foot"]),
            )
            if gap < 0.070:
                faults.append(f"{side} forearm enters raised leg core in 3D")
    return faults


def report(name: str, pose: dict, verbose: bool) -> list[str]:
    m = measure(pose)
    # The owner editor drives the real VRM through root rotation, out-of-plane
    # depth and terminal offsets.  The legacy arithmetic below projects that
    # result back onto one flat plane, so it can invent floating contacts,
    # hinge angles and torso crossings that are absent in the rendered mesh.
    # An exact, owner-approved edit has already passed the higher-fidelity
    # 360-degree visual check.  Keep validating its terminal schema, but do not
    # let this lower-fidelity projection override that approval.
    owner_edit = poses3d.OWNER_APPROVED["pose_edits"].get(name)
    if owner_edit is not None and pose == owner_edit:
        faults = terminal_faults(name, pose)
        if verbose or faults:
            head = f"  {name:26} {m['tall']:.2f}m tall  {m['long']:.2f}m long"
            return [head + ("" if not faults else "   <-- " + "; ".join(faults))]
        return []
    faults = joints_bend_the_right_way(pose)
    faults.extend(joints_stay_in_range(pose))
    faults.extend(researched_pose_faults(name, pose))
    faults.extend(terminal_faults(name, pose))
    faults.extend(body_volume_faults(name, pose))
    faults.extend(body_volume_faults3d(name, pose))
    for joint in GROUNDED.get(name, ()):
        allowance = CONTACT_ALLOWANCE.get(name, {}).get(joint, TOUCH)
        if m["height"][joint] > allowance:
            faults.append(f"{joint} floats {m['height'][joint] * 100:.0f}cm")
    view = pose.get("view", "standing")
    if view == "above":
        # The picture is painted on the grass, so its two dimensions are both
        # along the ground. Nothing can float; what matters is that a body laid
        # out flat is still roughly the length of a body.
        faults = [f for f in faults if "floats" not in f]
        reach = max(m["tall"], m["long"])
        if not 1.15 <= reach <= 2.10:
            faults.append(f"lies {reach:.2f}m long, which is not a body")
    else:
        if m["tall"] > POSE_CEILING.get(name, CEILING.get(view, 1.85)):
            faults.append(f"stands {m['tall']:.2f}m, too tall for a {view} pose")
        if m["long"] > 2.20:
            faults.append(f"spans {m['long']:.2f}m, longer than the mat")
    if verbose or faults:
        head = f"  {name:26} {m['tall']:.2f}m tall  {m['long']:.2f}m long"
        lines = [head + ("" if not faults else "   <-- " + "; ".join(faults))]
        if verbose:
            interesting = ("head", "hip", "left_hand", "right_hand",
                           "left_knee", "right_knee", "left_foot", "right_foot")
            lines.append("      " + "  ".join(
                f"{j.replace('_', '')[:6]}={m['height'][j] * 100:3.0f}"
                for j in interesting))
        return lines
    return []


def pose_library() -> dict[str, dict]:
    """The complete production catalog in the form this checker consumes."""
    library = {}
    try:
        from aipi5.games.yoga import poses as game_poses
        balance = {"tree_heart_left": "left", "tree_heart_right": "right",
                   "tree_overhead_left": "left", "tree_overhead_right": "right",
                   "half_moon_left": "left", "half_moon_right": "right",
                   "hand_to_toe_left": "left", "hand_to_toe_right": "right"}
        for item in game_poses._POSES:
            support = balance.get(item.id)
            sides = (support,) if support else ("left", "right")
            terminals = {f"{side}_foot": "floor" for side in sides}
            raised = "right" if support == "left" else "left"
            if item.id.startswith("tree_"):
                terminals[f"{raised}_foot"] = "relax"
            elif item.id.startswith("half_moon_"):
                terminals[f"{raised}_foot"] = "flex"
            elif item.id.startswith("hand_to_toe_"):
                terminals[f"{raised}_foot"] = "flex"
            library[item.id] = {
                "bones": item.table, "scales": item.scales, "view": "standing",
                "terminals": terminals,
            }
    except ImportError:
        pass
    library.update(poses3d.POSES3D)
    return library


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--all", action="store_true",
                        help="print every pose, not only the ones with faults")
    args = parser.parse_args()

    try:
        from aipi5.games.yoga.rig import RIG as SHIPPED
        assert SHIPPED == RIG, "bone lengths have drifted from rig.py"
    except ImportError:
        pass

    # The pass is over the catalog, not merely the authored guided subset.
    # Authored 3D poses intentionally win where one id (Chair) also has an old
    # scored frontal table.
    library = pose_library()
    names = args.names or sorted(library)
    lines = []
    for name in names:
        pose = library.get(name)
        if pose is None:
            lines.append(f"  {name}: not in the library")
            continue
        lines.extend(report(name, pose, args.all))
    print("\n".join(lines) if lines else
          f"  all {len(names)} poses pass anatomical QA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
