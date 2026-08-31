"""The 3D pose library: every shape the 21 courses ask for, as bone angles.

The scored poses need nothing authored here. They already have bone tables in
`aipi5/games/yoga/poses.py`, those tables are what the player is scored
against, and `coach3d.js` puts them straight onto the model -- so the coach and
the scorer cannot disagree, which was the original point of the rig.

What is authored here is the **guided** tier: the floor, seated, kneeling,
prone and supine poses that never had a bone table because they were never
going to be scored, plus the new standing shapes the curriculum adds.

-- the one idea in this file --------------------------------------------

A rig table is a **flat picture of a body**: twelve directions in a plane. The
plane is the only thing that ever changes.

    root    which way the coach is turned to face
    plane   which plane the angles of the table are read in

For a standing pose both are identity: she faces the camera and the angles are
screen directions, exactly as `rig.py` has always meant them.

For a **side** pose she is turned to face screen-right and the plane is left
alone, so the same twelve numbers now describe her sagittal silhouette. Cat,
Downward Dog and Cobra are all just side-on pictures.

For a **supine** pose the body and the plane are laid down *together*, so the
picture ends up painted on the grass and the camera looks down at it. Savasana
is not a new description of a body; it is a body standing, laid flat.

That is the whole mechanism. There are no per-pose corrections in here and
there should never be: a pose that comes out wrong is wrong in its numbers, and
`check3d.py` will usually say which ones.

-- reading the angles ---------------------------------------------------

Every angle is the direction the bone points, in degrees, in the plane of the
table. What never changes, in any plane, is that an angle near 180 belongs to
the limb the player copies with their **left** -- that is the whole of the
mirroring convention, and it survives the change of plane because the rig-left
angles drive the model's right-side bones (`RIG_TO_VRM`).

    frontal    0 screen-right   90 down         180 screen-left  -90 up
    side       0 forward        90 down         180 behind       -90 up
    lying      0 toward feet    90 into floor   180 toward head  -90 off floor
    flat     -90 toward head    90 toward feet  180 player-right   0 left

The last two both describe a body on its back, and which one to use depends on
one question: **does anything come off the floor?** Savasana and a supine
twist stay down, so they are `flat` -- the picture is painted on the grass and
the camera looks down at it. Knees-to-Chest and Happy Baby lift the legs into
the air, which a picture painted on the grass simply cannot say, so they are
`lying`: turned on their side, seen from the side, with up on the page meaning
up off the mat.

Sides are named for the *player*, never for the model: the player-left side is
the model's own right, because she demonstrates facing them. The `flat`
directions were measured rather than reasoned about -- a probe pose with one
limb at each of the four cardinal angles, rendered and looked at -- because
reasoning about them from the euler order got it backwards twice.

The frontal row is `rig.py` unchanged: the coach faces the player and
demonstrates mirrored, so her left limb is drawn at the *smaller* screen x.

`view` picks the camera height and distance. A floor pose seen from standing
eye level is the mistake the 2D route made twice -- an overhead Savasana read
as somebody standing on a lawn. In 3D the camera simply moves. `check3d.py`
holds each view to a ceiling, so a pose filed under the wrong one is caught
rather than discovered on the mat.

In a side pose the left limb of the coach is the one nearest the camera. Near
limbs are therefore given a degree or two of their own so the far limb is not
perfectly hidden behind them.

`bones3d` is the escape hatch for what a flat picture genuinely cannot say: a
twist, or a pelvis tipped out of the plane. `applyExtras` in `coach3d.js`
explains the timing; what matters while authoring is which bone to put it on.
On the hips and the chest -- bones the solve never touches -- anything works,
and it moves joints rather than directions. On a solved bone (the spine, the
neck, a limb) use the **Y** entry, which in a VRM is the long axis of the bone:
that turns the body without arguing with the direction the table just asked
for. A side-bend therefore belongs on the chest, never on the spine.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Camera presets: five framings, sized so the tallest pose filed under each
#: fills about four fifths of the frame. `check3d.CEILING` holds the library to
#: that, which is what keeps the coach the same size from pose to pose instead
#: of drifting the way the generated clips did.
#:
#: A pose may also carry a `camera` of its own, merged over its preset. Mostly
#: that is an `azimuth` -- degrees round the coach, zero straight in front,
#: positive walking toward her own left. It exists because some poses simply
#: cannot be taught from the front: Thread the Needle seen side-on is
#: Tabletop, and a Chair seen from the front is a person standing up. The rule
#: for choosing one is the only rule that matters here -- **point the camera
#: wherever the pose is easiest to copy** -- and the cost is nothing, because
#: the camera move happens during the transition and is over before the hold
#: begins.
VIEWS = {
    "standing": {"distance": 4.2, "height": 1.15, "target": 0.95},
    "kneel":    {"distance": 3.8, "height": 1.00, "target": 0.78},
    "seated":   {"distance": 3.0, "height": 0.85, "target": 0.50},
    "low":      {"distance": 3.2, "height": 0.70, "target": 0.35},
    "above":    {"distance": 2.0, "height": 3.00, "target": 0.05},
}

#: Standing, for reference: the resting table of the rig itself.
STAND = {"spine": -90, "neck": -90,
         "left_upper_arm": 96, "left_forearm": 96,
         "right_upper_arm": 84, "right_forearm": 84,
         "left_thigh": 90, "left_shin": 90, "right_thigh": 90, "right_shin": 90}

#: Turned to face screen-right, table read as a side view.
SIDE = {"yaw": 90}
#: Laid on her back across the view, head to screen-left, table read flat on
#: the grass. For poses where nothing leaves the floor.
SUPINE = {"pitch": -90, "yaw": 90}
#: The same, turned end for end: head to screen-right, and the player-left side
#: of her toward the camera instead of away from it. A twist needs this. Seen
#: from above, a knee dropped to the far side and a knee lifted into the air
#: project to the same picture, so the knees have to fall toward the viewer --
#: which for a left twist means turning her round.
SUPINE_FAR = {"pitch": -90, "yaw": -90}
#: On her back, but seen from the side, so a lifted leg is a lifted leg.
LYING = {"pitch": -90, "roll": 90}


def _p(bones=None, *, root=None, plane=None, view="standing", bones3d=None,
       depth3d=None, camera=None):
    pose = {"bones": dict(STAND, **(bones or {})), "view": view}
    if root:
        pose["root"] = root
    if plane:
        pose["plane"] = plane
    if bones3d:
        pose["bones3d"] = bones3d
    if depth3d:
        pose["depth3d"] = depth3d
    if camera:
        pose["camera"] = camera
    return pose


def _front(bones, view="standing", bones3d=None, depth3d=None, camera=None):
    return _p(bones, view=view, bones3d=bones3d, depth3d=depth3d,
              camera=camera)


def _side(bones, view="low", bones3d=None, depth3d=None, camera=None):
    return _p(bones, root=SIDE, view=view, bones3d=bones3d,
              depth3d=depth3d, camera=camera)


def _supine(bones, view="above", bones3d=None, depth3d=None, camera=None):
    return _p(bones, root=SUPINE, plane=SUPINE, view=view, bones3d=bones3d,
              depth3d=depth3d, camera=camera)


def _lying(bones, view="low", bones3d=None, depth3d=None, camera=None):
    return _p(bones, root=LYING, view=view, bones3d=bones3d,
              depth3d=depth3d, camera=camera)


# -- supine: lying on the grass, seen from above and in front ---------------
# -90 is toward her head, 90 toward her feet, 180 out to her left.
SUPINE_POSES = {
    "savasana": _supine({"spine": -90, "neck": -90,
                         "left_upper_arm": 114, "right_upper_arm": 66,
                         "left_forearm": 110, "right_forearm": 70,
                         "left_thigh": 98, "right_thigh": 82,
                         "left_shin": 96, "right_shin": 84}, camera={"azimuth": 14}),
    # Knees drawn up to the chest and held there -- which lifts them off the
    # grass, so this one is seen from the side.
    "knees_to_chest": _lying({"spine": 180, "neck": 180,
                              "left_thigh": -150, "right_thigh": -146,
                              "left_shin": -32, "right_shin": -28,
                              # Two-link solutions place each hand on its
                              # own knee instead of reaching past the feet.
                              # Use the inward IK branch: elbows stay beside
                              # the ribs and the forearms wrap back over the
                              # shins.  The old outward branch touched the
                              # knees numerically but looked like two arms
                              # thrown beside the head, not an embrace.
                              "left_upper_arm": -4, "right_upper_arm": -8,
                              "left_forearm": -131, "right_forearm": -127},
                             depth3d={"left_thigh": -7, "left_shin": -7,
                                      "right_thigh": 7, "right_shin": 7,
                                      "left_upper_arm": -9, "left_forearm": -9,
                                      "right_upper_arm": 9, "right_forearm": 9},
                             camera={"azimuth": 24}),
    # Knees wide beside the ribs, shins straight up, hands holding the feet.
    "happy_baby": _lying({"spine": 180, "neck": 176,
                          "left_thigh": -142, "right_thigh": -136,
                          "left_shin": -88, "right_shin": -82,
                          "left_upper_arm": -108, "right_upper_arm": -102,
                          "left_forearm": -84, "right_forearm": -78},
                         depth3d={"left_thigh": -32, "left_shin": -32,
                                  "left_upper_arm": -28, "left_forearm": -28,
                                  "right_thigh": 32, "right_shin": 32,
                                  "right_upper_arm": 28, "right_forearm": 28},
                         camera={"azimuth": 24}),
    # Feet flat near the hips, pelvis lifted. Seen side-on, because the whole
    # of this pose is the arch and an arch viewed from above is a rectangle.
    "bridge": _lying({"spine": 150, "neck": 180,
                       "left_thigh": -25, "right_thigh": -29,
                       "left_shin": 90, "right_shin": 94,
                       "left_upper_arm": 0, "right_upper_arm": 0,
                       "left_forearm": 0, "right_forearm": 0},
                      camera={"azimuth": 18}),
    # Arms in a T, knees drawn up and dropped over to one side, gaze the other
    # way. The knees have to actually go somewhere: pointed at the head they
    # are Knees-to-Chest, and the pose stops being a twist at all.
    # Both twists keep the body the same way round -- head to screen-left, as
    # in Savasana, so a lesson does not appear to flip her over between sides.
    # What changes is where the camera stands: it comes round to the side the
    # knees fall toward, which is the only way an overhead view can tell a
    # knee dropped to the floor from a knee lifted into the air.
    "supine_twist_left": _supine({"spine": -90, "neck": -90,
                                  "left_thigh": 0, "right_thigh": 4,
                                  "left_shin": 90, "right_shin": 94,
                                  "left_upper_arm": 178, "right_upper_arm": 2,
                                  "left_forearm": 176, "right_forearm": 4},
                                 bones3d={"hips": [0, 0, -24],
                                          "chest": [0, 0, 14],
                                          "neck": [0, -35, 0]},
                                 # Stack the upper leg above the lower one;
                                 # two thighs cannot occupy one flat tube.
                                 depth3d={"left_thigh": 22,
                                          "right_thigh": 22},
                                 camera={"azimuth": 152, "height": 2.30,
                                         "target": 0.12, "distance": 2.8}),
    "supine_twist_right": _supine({"spine": -90, "neck": -90,
                                   "left_thigh": 180, "right_thigh": 176,
                                   "left_shin": 90, "right_shin": 86,
                                   "left_upper_arm": 178, "right_upper_arm": 2,
                                   "left_forearm": 176, "right_forearm": 4},
                                  bones3d={"hips": [0, 0, 24],
                                           "chest": [0, 0, -14],
                                           "neck": [0, 35, 0]},
                                  depth3d={"left_thigh": 22,
                                           "right_thigh": 22},
                                  camera={"azimuth": 28, "height": 2.30,
                                          "target": 0.12, "distance": 2.8}),
    # One ankle across the opposite knee, both drawn in toward the chest and
    # both off the grass.
    "figure_four_left": _lying({"spine": 180, "neck": 180,
                                "right_thigh": -146, "right_shin": -30,
                                "left_thigh": -78, "left_shin": 8,
                                "left_upper_arm": 30, "right_upper_arm": 34,
                                "left_forearm": -70, "right_forearm": -66}, camera={"azimuth": 24}),
    "figure_four_right": _lying({"spine": 180, "neck": 180,
                                 "left_thigh": -150, "left_shin": -34,
                                 "right_thigh": -74, "right_shin": 12,
                                 "left_upper_arm": 30, "right_upper_arm": 34,
                                 "left_forearm": -70, "right_forearm": -66}, camera={"azimuth": -24}),
}

# -- seated, facing the camera, so the shape of the legs reads --------------
# Sitting on the floor puts the hip joints at ground level, which is why the
# thighs here are close to horizontal and the elbows are bent: an arm hanging
# straight down from a seated shoulder would end below the grass.
SEATED_FRONT = {
    "easy_seat": _front({"left_thigh": 168, "right_thigh": 12,
                         "left_shin": -22, "right_shin": 182,
                         "left_upper_arm": 118, "right_upper_arm": 62,
                         "left_forearm": 140, "right_forearm": 40}, "seated"),
    "easy_seat_breath": _front({"left_thigh": 168, "right_thigh": 12,
                                "left_shin": -22, "right_shin": 182,
                                "left_upper_arm": 122, "right_upper_arm": 58,
                                "left_forearm": 152, "right_forearm": 28},
                               "seated"),
    # Soles together, knees out; the shins run back toward the midline.
    "butterfly": _front({"left_thigh": 178, "right_thigh": 2,
                         "left_shin": 10, "right_shin": 170,
                         "left_upper_arm": 115, "right_upper_arm": 65,
                         "left_forearm": 50, "right_forearm": 130}, "seated",
                        depth3d={"left_thigh": 60, "right_thigh": 60,
                                 "left_shin": -25, "right_shin": -25,
                                 "left_upper_arm": 25, "right_upper_arm": 25,
                                 "left_forearm": 15, "right_forearm": 15}),
    "seated_side_stretch_left": _front({"spine": -114, "neck": -110,
                                        "left_thigh": 130, "right_thigh": 29,
                                        "left_shin": -20, "right_shin": -169,
                                        "left_upper_arm": 146,
                                        "right_upper_arm": -34,
                                        "left_forearm": 160,
                                        "right_forearm": -30}, "seated",
                                       depth3d={"left_thigh": 60, "left_shin": -9,
                                                "right_thigh": 48, "right_shin": -3},
                                       camera={"azimuth": 16}),
    "seated_side_stretch_right": _front({"spine": -66, "neck": -70,
                                         "left_thigh": 130, "right_thigh": 29,
                                         "left_shin": -20, "right_shin": -169,
                                         "left_upper_arm": 214,
                                         "right_upper_arm": 34,
                                         "left_forearm": 210,
                                         "right_forearm": 20}, "seated",
                                        depth3d={"left_thigh": 60, "left_shin": -9,
                                                 "right_thigh": 48, "right_shin": -3},
                                        camera={"azimuth": -16}),
    "seated_twist_left": _front({"left_thigh": 130, "right_thigh": 29,
                                 "left_shin": -20, "right_shin": -169,
                                 "left_upper_arm": 100, "right_upper_arm": 44,
                                 "left_forearm": 40, "right_forearm": 10},
                                "seated",
                                {"spine": [0, 30, 0], "chest": [0, 18, 0],
                                 "neck": [0, 22, 0]},
                                depth3d={"left_thigh": 60, "left_shin": -9,
                                         "right_thigh": 48, "right_shin": -3},
                                camera={"azimuth": 34}),
    "seated_twist_right": _front({"left_thigh": 130, "right_thigh": 29,
                                  "left_shin": -20, "right_shin": -169,
                                  "left_upper_arm": 136, "right_upper_arm": 80,
                                  "left_forearm": 170, "right_forearm": 140},
                                 "seated",
                                 {"spine": [0, -30, 0], "chest": [0, -18, 0],
                                  "neck": [0, -22, 0]},
                                 depth3d={"left_thigh": 60, "left_shin": -9,
                                          "right_thigh": 48, "right_shin": -3},
                                 camera={"azimuth": -34}),
}

# -- seated, side-on: shapes that are a fold or a lift, not a leg pattern ---
SEATED_SIDE = {
    "staff": _side({"spine": -90, "neck": -86,
                    "left_thigh": 2, "right_thigh": -2,
                    "left_shin": 2, "right_shin": -2,
                    "left_upper_arm": 92, "right_upper_arm": 88,
                    "left_forearm": 50, "right_forearm": 46}, "seated"),
    # Folding over straight legs: the chest comes down onto the thighs, so the
    # shoulders end just above the grass rather than below it.
    "seated_forward_fold": _side({"spine": -40, "neck": 6,
                                  "left_thigh": 2, "right_thigh": -2,
                                  "left_shin": 2, "right_shin": -2,
                                  "left_upper_arm": 36, "right_upper_arm": 32,
                                  "left_forearm": 30, "right_forearm": 26},
                                 "seated"),
    # Preparation for the V: sitting on the sit bones, chest lifted back,
    # knees bent and shins level.  Straight legs belong to full Boat, which is
    # not the shape named by the course UI.
    "boat": _side({"spine": -126, "neck": -118,
                   "left_thigh": -62, "right_thigh": -66,
                   "left_shin": 8, "right_shin": 4,
                   "left_upper_arm": 4, "right_upper_arm": 0,
                   "left_forearm": 2, "right_forearm": -2}, "seated", camera={"azimuth": 18}),
    "seated_hamstring_left": _side({"spine": -40, "neck": 6,
                                    "left_thigh": 2, "left_shin": 2,
                                    "right_thigh": 6, "right_shin": 176,
                                    "left_upper_arm": 36, "right_upper_arm": 32,
                                    "left_forearm": 30, "right_forearm": 26},
                                   "seated", depth3d={"right_thigh": -45,
                                                      "right_shin": 45}),
    "seated_hamstring_right": _side({"spine": -40, "neck": 6,
                                     "right_thigh": -2, "right_shin": -2,
                                     "left_thigh": 10, "left_shin": 180,
                                     "left_upper_arm": 36, "right_upper_arm": 32,
                                     "left_forearm": 30, "right_forearm": 26},
                                    "seated", depth3d={"left_thigh": 45,
                                                       "left_shin": -45}),
}

# -- quadruped and kneeling: side-on, because that is how a spine reads -----
#
# A horizontal torso is a *direction*, not a rotation: the spine bone runs from
# the waist to the neck, so pointing it forward is all a tabletop back needs.
# The pelvis is tipped separately, and only because the pelvis really does tip
# -- it moves the hip sockets, and the legs then take their own directions from
# the table regardless (see `applyExtras` in coach3d.js).
#
# The spine is at -19 and not level, and the number is arithmetic rather than
# taste: the arm is 1.20 spines long and the thigh 0.88, so if the knees and
# the hands are to reach the same grass the shoulders have to sit 0.32 above
# the hips, and asin(0.32) is nineteen degrees.
_TABLE = {"spine": -19, "neck": 4,
          "left_thigh": 92, "right_thigh": 88,
          "left_shin": 178, "right_shin": 182,
          "left_upper_arm": 92, "right_upper_arm": 88,
          "left_forearm": 92, "right_forearm": 88}
_ON_ALL_FOURS = {"hips": [88, 0, 0]}

KNEELING = {
    "table": _side(dict(_TABLE), bones3d=dict(_ON_ALL_FOURS), camera={"azimuth": 16}),
    # Cat rounds: the tail tucks under and the head drops, so the middle of the
    # back is the highest thing in the pose. The dome itself is the chest, not
    # the spine -- the spine bone only says where the shoulders are.
    "cat": _side(dict(_TABLE, neck=51),
                 bones3d={"hips": [104, 0, 0], "chest": [24, 0, 0]}, camera={"azimuth": 16}),
    # Cow is the opposite at both ends: tail up, chest and chin lifted.
    "cow": _side(dict(_TABLE, neck=-48),
                 bones3d={"hips": [72, 0, 0], "chest": [-18, 0, 0]}, camera={"azimuth": 16}),
    "bird_dog_left": _side(dict(_TABLE, left_thigh=174, left_shin=178,
                                right_upper_arm=-6, right_forearm=-2),
                           bones3d=dict(_ON_ALL_FOURS), camera={"azimuth": -34}),
    "bird_dog_right": _side(dict(_TABLE, right_thigh=174, right_shin=178,
                                 left_upper_arm=-6, left_forearm=-2),
                            bones3d=dict(_ON_ALL_FOURS), camera={"azimuth": -34}),
    # Thread the Needle is a rotation, and a rotation seen edge-on is nothing
    # at all -- side-on it was indistinguishable from Tabletop. The chest both
    # twists (Y, about its own long axis) and drops a shoulder to the mat (Z),
    # and the camera comes round to three-quarters and lifts, which is the
    # angle from which an arm sliding under a chest looks like an arm sliding
    # under a chest.
    "thread_needle_left": _side({"spine": -14, "neck": 56,
                                 "left_thigh": 92, "right_thigh": 88,
                                 "left_shin": 178, "right_shin": 182,
                                 "left_upper_arm": 112, "right_upper_arm": 88,
                                 "left_forearm": 116, "right_forearm": 88},
                                bones3d={"hips": [88, 0, 0],
                                         "chest": [0, 40, 62]},
                                camera={"azimuth": -30, "height": 1.30,
                                        "target": 0.34, "distance": 2.9}),
    "thread_needle_right": _side({"spine": -14, "neck": 56,
                                  "left_thigh": 92, "right_thigh": 88,
                                  "left_shin": 178, "right_shin": 182,
                                  "left_upper_arm": 88, "right_upper_arm": 112,
                                  "left_forearm": 88, "right_forearm": 116},
                                 bones3d={"hips": [88, 0, 0],
                                          "chest": [0, -40, -62]},
                                 camera={"azimuth": 30, "height": 1.30,
                                         "target": 0.34, "distance": 2.9}),
    # Hips back on the heels. The thigh is barely tilted because the hip only
    # sits a hand above the knee once she is folded down onto her own calves.
    # Seen level from the side this reads as lying face-down: the whole story
    # is that the hips have gone *back* onto the heels, and travel toward the
    # camera is exactly what a level side view cannot show. Lifted and brought
    # round to three-quarters, the fold is obvious.
    "child_pose": _side({"spine": 18, "neck": 28,
                         "left_thigh": 26, "right_thigh": 22,
                         "left_shin": 172, "right_shin": 176,
                         "left_upper_arm": 20, "right_upper_arm": 16,
                         "left_forearm": 6, "right_forearm": 2},
                        bones3d={"hips": [46, 0, 0]},
                        camera={"azimuth": 40, "height": 1.15, "target": 0.24,
                                "distance": 2.9}),
    # Back knee down, front knee stacked over the ankle, chest lifted.
    "low_lunge_left": _side({"spine": -88, "neck": -84,
                             "left_thigh": 2, "left_shin": 92,
                             "right_thigh": 90, "right_shin": 178,
                             "left_upper_arm": 64, "right_upper_arm": 64,
                             "left_forearm": 64, "right_forearm": 64},
                            "kneel", {"hips": [16, 0, 0]},
                            depth3d={"left_upper_arm": -4, "left_forearm": -42,
                                     "right_upper_arm": 4, "right_forearm": 42}),
    "low_lunge_right": _side({"spine": -88, "neck": -84,
                              "right_thigh": -2, "right_shin": 88,
                              "left_thigh": 90, "left_shin": 182,
                              "left_upper_arm": 64, "right_upper_arm": 64,
                              "left_forearm": 64, "right_forearm": 64},
                             "kneel", {"hips": [16, 0, 0]},
                             depth3d={"left_upper_arm": -4, "left_forearm": -28,
                                      "right_upper_arm": 4, "right_forearm": 28}),
    "low_lunge_reach_left": _side({"spine": -102, "neck": -114,
                                   "left_thigh": 2, "left_shin": 92,
                                   "right_thigh": 90, "right_shin": 178,
                                   "left_upper_arm": -78, "right_upper_arm": -82,
                                   "left_forearm": -80, "right_forearm": -84},
                                  "kneel", {"hips": [16, 0, 0]}),
    "low_lunge_reach_right": _side({"spine": -102, "neck": -114,
                                    "right_thigh": -2, "right_shin": 88,
                                    "left_thigh": 90, "left_shin": 182,
                                    "left_upper_arm": -78, "right_upper_arm": -82,
                                    "left_forearm": -80, "right_forearm": -84},
                                   "kneel", {"hips": [16, 0, 0]}),
    # Front leg straight along the grass, hips back over the kneeling foot.
    # Thirty degrees is where a straight leg of 1.74 reaches the floor from a
    # hip 0.88 up, which is where the kneeling knee puts it.
    "half_split_left": _side({"spine": -6, "neck": 26,
                              "left_thigh": 30, "left_shin": 30,
                              "right_thigh": 90, "right_shin": 178,
                              "left_upper_arm": 60, "right_upper_arm": 56,
                              "left_forearm": 50, "right_forearm": 46},
                             bones3d={"hips": [40, 0, 0]}, camera={"azimuth": -16}),
    "half_split_right": _side({"spine": -6, "neck": 26,
                               "right_thigh": 30, "right_shin": 30,
                               "left_thigh": 90, "left_shin": 182,
                               "left_upper_arm": 60, "right_upper_arm": 56,
                               "left_forearm": 50, "right_forearm": 46},
                              bones3d={"hips": [40, 0, 0]}, camera={"azimuth": 16}),
    # Low lunge again, but folded down onto the hands.
    "lizard_left": _side({"spine": -16, "neck": 10,
                          "left_thigh": 2, "left_shin": 92,
                          "right_thigh": 90, "right_shin": 178,
                          "left_upper_arm": 62, "right_upper_arm": 58,
                          "left_forearm": 58, "right_forearm": 54},
                         bones3d={"hips": [30, 0, 0]},
                         depth3d={"left_upper_arm": -10, "left_forearm": -10,
                                  "right_upper_arm": 10, "right_forearm": 10},
                         camera={"azimuth": -20}),
    "lizard_right": _side({"spine": -16, "neck": 10,
                           "right_thigh": -2, "right_shin": 88,
                           "left_thigh": 90, "left_shin": 182,
                           "left_upper_arm": 62, "right_upper_arm": 58,
                           "left_forearm": 58, "right_forearm": 54},
                          bones3d={"hips": [30, 0, 0]},
                          depth3d={"left_upper_arm": -10, "left_forearm": -10,
                                   "right_upper_arm": 10, "right_forearm": 10},
                          camera={"azimuth": 20}),
    # Front shin folded across the mat, back leg long behind.
    "pigeon_prep_left": _side({"spine": -66, "neck": -58,
                               "left_thigh": 20, "left_shin": 178,
                               "right_thigh": 176, "right_shin": 182,
                               "left_upper_arm": 86, "right_upper_arm": 82,
                               "left_forearm": 60, "right_forearm": 56},
                              "kneel", {"hips": [26, 0, 0]}, camera={"azimuth": -32}),
    "pigeon_prep_right": _side({"spine": -66, "neck": -58,
                                "right_thigh": 16, "right_shin": 182,
                                "left_thigh": 180, "left_shin": 178,
                                "left_upper_arm": 86, "right_upper_arm": 82,
                                "left_forearm": 60, "right_forearm": 56},
                               "kneel", {"hips": [26, 0, 0]}, camera={"azimuth": 32}),
}

# -- prone, plank and the inversions: all sagittal shapes -------------------
PRONE = {
    # The inverted V: hips are the high point, hands and heels the two feet of
    # it, and the spine runs down-forward from the hips to the shoulders.
    "downward_dog": _side({"spine": 42, "neck": 56,
                           "left_thigh": 122, "right_thigh": 118,
                           "left_shin": 124, "right_shin": 120,
                           "left_upper_arm": 60, "right_upper_arm": 56,
                           "left_forearm": 62, "right_forearm": 58},
                          "kneel", {"hips": [46, 0, 0]}, camera={"azimuth": 12}),
    # One straight line from the heels to the crown, tilted by the twenty-six
    # degrees that put the shoulders an arm's length above the hands.
    "plank": _side({"spine": -26, "neck": -10,
                    "left_thigh": 154, "right_thigh": 158,
                    "left_shin": 154, "right_shin": 158,
                    "left_upper_arm": 92, "right_upper_arm": 88,
                    "left_forearm": 92, "right_forearm": 88},
                   bones3d={"hips": [64, 0, 0]}),
    "side_plank_left": _side({"spine": -26, "neck": -10,
                              "left_thigh": 154, "right_thigh": 156,
                              "left_shin": 154, "right_shin": 156,
                              "left_upper_arm": 92, "right_upper_arm": -88,
                              "left_forearm": 92, "right_forearm": -88},
                             "kneel", {"hips": [64, 0, 0]}, camera={"azimuth": -34}),
    "side_plank_right": _side({"spine": -26, "neck": -10,
                               "left_thigh": 156, "right_thigh": 154,
                               "left_shin": 156, "right_shin": 154,
                               "right_upper_arm": 88, "left_upper_arm": -92,
                               "right_forearm": 88, "left_forearm": -92},
                              "kneel", {"hips": [64, 0, 0]}, camera={"azimuth": 34}),
    # Legs and pelvis stay on the grass; only the chest comes up. The spine
    # cannot lift past forty degrees or the elbow would have to reach through
    # the ground to put the forearm on it.
    "sphinx": _side({"spine": -38, "neck": -18,
                     "left_thigh": 178, "right_thigh": 182,
                     "left_shin": 178, "right_shin": 182,
                     "left_upper_arm": 104, "right_upper_arm": 100,
                     "left_forearm": 2, "right_forearm": -2},
                    bones3d={"hips": [88, 0, 0]}),
    # Higher chest, straighter arms, elbows drawn back beside the ribs.
    "cobra": _side({"spine": -62, "neck": -38,
                    "left_thigh": 178, "right_thigh": 182,
                    "left_shin": 178, "right_shin": 182,
                    "left_upper_arm": 130, "right_upper_arm": 126,
                    "left_forearm": 50, "right_forearm": 46},
                   bones3d={"hips": [88, 0, 0]}),
}

# -- new standing shapes the curriculum adds --------------------------------
# Guided rather than scored (the camera cannot tell several of them apart --
# see `assets.md`), but the coach still has to demonstrate them, so they live
# here rather than in `poses.py`, which is only for shapes that get scored.
STANDING_NEW = {
    # Wide externally-rotated squat.  The scoring table already gives the
    # knee-over-ankle geometry; this 3D copy adds the toe turnout that a flat
    # landmark table cannot express.
    "goddess": {
        "bones": {"spine": -90, "neck": -90,
                  "left_upper_arm": 175, "left_forearm": -88,
                  "right_upper_arm": 5, "right_forearm": -92,
                  "left_thigh": 135, "left_shin": 90,
                  "right_thigh": 45, "right_shin": 90},
        "view": "standing",
        "terminal3d": {"left_foot": [0, -45, 0],
                       "right_foot": [0, 45, 0]},
    },
    # A front-facing shoulder-opening shape: upper arms level with the
    # shoulders, elbows at right angles and palms forward.  This used to fall
    # through to the scoring table, which could place the bones but could not
    # state the wrist orientation.
    "cactus_arms": {
        "bones": {"spine": -90, "neck": -90,
                  "left_thigh": 90, "right_thigh": 90,
                  "left_shin": 90, "right_shin": 90,
                  "left_upper_arm": 175, "right_upper_arm": 5,
                  "left_forearm": -88, "right_forearm": -92},
        "view": "standing",
    },
    # Chair, and the reason it is here rather than in `poses.py` with the
    # other standing poses: sitting back into a chair is a bend in the
    # sagittal plane, and a frontal table cannot say it -- the shipped table
    # drew a woman standing upright with her arms in the air, because from the
    # front that is very nearly what deep Chair looks like. Scored off
    # deliberately (`guided_only` in the catalog) and drawn properly instead.
    #
    # The numbers are arithmetic, not taste: a thigh at 25 degrees and a shin
    # near vertical put the hips at 71% of standing height and well behind the
    # heels, which is what sitting back into a chair actually is.
    "chair": _side({"spine": -52, "neck": -66,
                    "left_thigh": 25, "right_thigh": 21,
                    "left_shin": 95, "right_shin": 91,
                    "left_upper_arm": -58, "right_upper_arm": -62,
                    "left_forearm": -58, "right_forearm": -62},
                   "standing", {"hips": [26, 0, 0]},
                   camera={"azimuth": 28, "height": 1.05, "target": 0.85,
                           "distance": 3.9}),
    # Ardha Uttanasana is a sagittal hip hinge, not a side bend.  Keep the
    # back long and nearly horizontal, with both hands reaching to the shins.
    "half_lift": _side({"spine": -8, "neck": -8,
                         "left_upper_arm": 142, "right_upper_arm": 138,
                         "left_forearm": 142, "right_forearm": 138},
                        "standing", camera={"azimuth": 22}),
    # Same-side hand meets the raised flexed foot.  The scored frontal tables
    # left both arms in a T and therefore did not depict the named toe hold.
    # Raising the straight leg to an ordinary reachable height keeps the
    # torso upright; the free hand folds naturally toward the hip.
    "hand_to_toe_left": _front({"spine": -90, "neck": -90,
                                 "left_thigh": 92, "left_shin": 90,
                                 "right_thigh": -35, "right_shin": -35,
                                 "left_upper_arm": 145, "left_forearm": 35,
                                 "right_upper_arm": 0, "right_forearm": 0}),
    "hand_to_toe_right": _front({"spine": -90, "neck": -90,
                                  "right_thigh": 88, "right_shin": 90,
                                  "left_thigh": 215, "left_shin": 215,
                                  "right_upper_arm": 35, "right_forearm": 145,
                                  "left_upper_arm": 180, "left_forearm": 180}),
    "gentle_back_reach": _side({"spine": -104, "neck": -118,
                                 "left_upper_arm": 100, "right_upper_arm": 100,
                                 "left_forearm": 20, "right_forearm": 20},
                                "standing", camera={"azimuth": 18}),
    "weight_shift": _front({"spine": -86, "neck": -88,
                            "left_thigh": 98, "right_thigh": 86,
                            "left_shin": 96, "right_shin": 88}),
    "standing_twist_left": _front({"left_upper_arm": 130, "right_upper_arm": 58,
                                   "left_forearm": 152, "right_forearm": 26},
                                  bones3d={"spine": [0, 38, 0],
                                           "chest": [0, 26, 0],
                                           "neck": [0, 22, 0]}, camera={"azimuth": 30}),
    "standing_twist_right": _front({"left_upper_arm": 122, "right_upper_arm": 50,
                                    "left_forearm": 154, "right_forearm": 28},
                                   bones3d={"spine": [0, -38, 0],
                                            "chest": [0, -26, 0],
                                            "neck": [0, -22, 0]}, camera={"azimuth": -30}),
    "standing_knee_left": _front({"left_thigh": -70, "left_shin": 90,
                                  "right_thigh": 88, "right_shin": 88,
                                  "left_upper_arm": 100, "right_upper_arm": 80,
                                  "left_forearm": -19, "right_forearm": -161},
                                 depth3d={"left_thigh": 42, "left_shin": 0,
                                          "left_upper_arm": 32, "left_forearm": 32,
                                          "right_upper_arm": 32, "right_forearm": 32}),
    "standing_knee_right": _front({"right_thigh": -110, "right_shin": 90,
                                   "left_thigh": 92, "left_shin": 92,
                                   "left_upper_arm": 100, "right_upper_arm": 80,
                                   "left_forearm": -19, "right_forearm": -161},
                                  depth3d={"right_thigh": 42, "right_shin": 0,
                                           "left_upper_arm": 32, "left_forearm": 32,
                                           "right_upper_arm": 32, "right_forearm": 32}),
    # A body folded level at the hip is a pure side view; from the front it is
    # a foreshortened smudge, which is why these six turn.
    "high_lunge_left": _side({"spine": -94, "neck": -92,
                              "left_thigh": 8, "left_shin": 92,
                              "right_thigh": 144, "right_shin": 152,
                              "left_upper_arm": -86, "right_upper_arm": -90,
                              "left_forearm": -88, "right_forearm": -92},
                             "standing", {"hips": [14, 0, 0]}, camera={"azimuth": 16}),
    "high_lunge_right": _side({"spine": -94, "neck": -92,
                               "right_thigh": 4, "right_shin": 88,
                               "left_thigh": 148, "left_shin": 156,
                               "left_upper_arm": -86, "right_upper_arm": -90,
                               "left_forearm": -88, "right_forearm": -92},
                              "standing", {"hips": [14, 0, 0]}, camera={"azimuth": -16}),
    "warrior_three_left": _side({"spine": -4, "neck": -12,
                                 "left_thigh": 92, "left_shin": 90,
                                 "right_thigh": 178, "right_shin": 180,
                                 "left_upper_arm": -4, "right_upper_arm": -8,
                                 "left_forearm": -4, "right_forearm": -8},
                                "kneel", {"hips": [88, 0, 0]}, camera={"azimuth": 18}),
    "warrior_three_right": _side({"spine": -4, "neck": -12,
                                  "right_thigh": 88, "right_shin": 86,
                                  "left_thigh": 180, "left_shin": 182,
                                  "left_upper_arm": -4, "right_upper_arm": -8,
                                  "left_forearm": -4, "right_forearm": -8},
                                 "kneel", {"hips": [88, 0, 0]}, camera={"azimuth": -18}),
    "pyramid_left": _side({"spine": 34, "neck": 48,
                           "left_thigh": 68, "left_shin": 72,
                           "right_thigh": 116, "right_shin": 118,
                           "left_upper_arm": 48, "right_upper_arm": 44,
                           "left_forearm": 34, "right_forearm": 30},
                          "standing", {"hips": [44, 0, 0]}, camera={"azimuth": 16}),
    "pyramid_right": _side({"spine": 34, "neck": 48,
                            "right_thigh": 64, "right_shin": 68,
                            "left_thigh": 120, "left_shin": 122,
                            "left_upper_arm": 48, "right_upper_arm": 44,
                            "left_forearm": 34, "right_forearm": 30},
                           "standing", {"hips": [44, 0, 0]}, camera={"azimuth": -16}),
    "reverse_warrior_left": _front({"left_thigh": 150, "left_shin": 92,
                                    "right_thigh": 55, "right_shin": 55,
                                    "left_upper_arm": -112, "right_upper_arm": 102,
                                    "left_forearm": -96, "right_forearm": 88,
                                    "spine": -72, "neck": -78}),
    "reverse_warrior_right": _front({"right_thigh": 30, "right_shin": 88,
                                     "left_thigh": 125, "left_shin": 125,
                                     "right_upper_arm": -68, "left_upper_arm": 78,
                                     "right_forearm": -84, "left_forearm": 92,
                                     "spine": -108, "neck": -102}),
    "shoulder_opener": _side({"spine": -90, "neck": -90,
                               "left_thigh": 90, "left_shin": 90,
                               "right_thigh": 90, "right_shin": 90,
                               "left_upper_arm": 105, "right_upper_arm": 105,
                               "left_forearm": 105, "right_forearm": 105},
                              "standing",
                              depth3d={"left_upper_arm": -20, "left_forearm": -20,
                                       "right_upper_arm": 20, "right_forearm": 20},
                              camera={"azimuth": 28}),
    # Scored frontal tables cannot express "in front of the body".  These
    # overlays keep every scored direction but give contact limbs physical
    # depth so palms and bent legs reach surfaces instead of entering them.
    "mountain_breath": _front({"spine": -90, "neck": -90,
                                "left_upper_arm": 100, "left_forearm": -30,
                                "right_upper_arm": 80, "right_forearm": -150,
                                "left_thigh": 90, "left_shin": 90,
                                "right_thigh": 90, "right_shin": 90},
                               depth3d={"left_upper_arm": 18, "left_forearm": 18,
                                        "right_upper_arm": 18, "right_forearm": 18}),
    "tree_heart_left": _front({"spine": -90, "neck": -90,
                                "left_upper_arm": 100, "left_forearm": -30,
                                "right_upper_arm": 80, "right_forearm": -150,
                                "left_thigh": 90, "left_shin": 90,
                                "right_thigh": 65, "right_shin": 200},
                               depth3d={"right_thigh": 25, "right_shin": -25,
                                        "left_upper_arm": 18, "left_forearm": 18,
                                        "right_upper_arm": 18, "right_forearm": 18}),
    "tree_heart_right": _front({"spine": -90, "neck": -90,
                                 "left_upper_arm": 100, "left_forearm": -30,
                                 "right_upper_arm": 80, "right_forearm": -150,
                                 "left_thigh": 115, "left_shin": -20,
                                 "right_thigh": 90, "right_shin": 90},
                                depth3d={"left_thigh": 25, "left_shin": -25,
                                         "left_upper_arm": 18, "left_forearm": 18,
                                         "right_upper_arm": 18, "right_forearm": 18}),
    "tree_overhead_left": _front({"spine": -90, "neck": -90,
                                   "left_upper_arm": -98, "left_forearm": -94,
                                   "right_upper_arm": -82, "right_forearm": -86,
                                   "left_thigh": 90, "left_shin": 90,
                                   "right_thigh": 65, "right_shin": 200},
                                  depth3d={"right_thigh": 25, "right_shin": -25}),
    "tree_overhead_right": _front({"spine": -90, "neck": -90,
                                    "left_upper_arm": -98, "left_forearm": -94,
                                    "right_upper_arm": -82, "right_forearm": -86,
                                    "left_thigh": 115, "left_shin": -20,
                                    "right_thigh": 90, "right_shin": 90},
                                   depth3d={"left_thigh": 25, "left_shin": -25}),
    # A real forward fold is out of the frontal scoring plane.  Tilt only the
    # torso into depth, leaving both wide legs grounded and visible.
    "wide_leg_fold": _front({"spine": 30, "neck": 30,
                              "left_upper_arm": 90, "left_forearm": 90,
                              "right_upper_arm": 90, "right_forearm": 90,
                              "left_thigh": 115, "left_shin": 115,
                              "right_thigh": 65, "right_shin": 65},
                             depth3d={"spine": 70, "neck": 70},
                             camera={"azimuth": 18, "height": 0.78,
                                     "target": 0.62, "distance": 3.4}),
}

#: Cameras for poses that keep their *scored* bone table.
#:
#: A scored pose cannot be re-authored -- its table is what the player is
#: measured against -- but nothing says it has to be looked at square on, and
#: a body folded at the hip is very nearly invisible from the front.
#:
#: The angles here are deliberately small, and the reason is a real constraint
#: rather than timidity. The player has to face the camera to be measured at
#: all, and they copy what they see; a coach shown at forty degrees is an
#: instruction to turn forty degrees, and the scorer would mark them down for
#: obeying it. Fifteen degrees buys the depth that makes a fold read as a fold
#: without ever looking like a pose that is facing away.
SCORED_CAMERAS: dict[str, dict] = {
    # Lowered rather than raised. Looking *down* on a fold shows the top of a
    # head; looking slightly up along the line of the body shows the hinge at
    # the hip, which is the thing being taught.
    "forward_fold":   {"azimuth": 15, "height": 0.72, "target": 0.62,
                       "distance": 3.4},
    "wide_leg_fold":  {"azimuth": 15, "height": 0.72, "target": 0.62,
                       "distance": 3.4},
    "half_moon_left": {"azimuth": 12}, "half_moon_right": {"azimuth": -12},
    "triangle_left":  {"azimuth": 12}, "triangle_right": {"azimuth": -12},
    "side_angle_left": {"azimuth": 12}, "side_angle_right": {"azimuth": -12},
}

POSES3D: dict[str, dict] = {}
for _group in (SUPINE_POSES, SEATED_FRONT, SEATED_SIDE, KNEELING, PRONE,
               STANDING_NEW):
    POSES3D.update(_group)

# Terminal joints need an orientation as well as a position.  The original
# ten-chain solve stopped at the wrist and ankle, which made palms and soles
# rigidly inherit the forearm and shin.  These declarations are deliberately
# semantic (supporting palm, planted sole, or pointed/relaxed foot); the shared
# runtime turns each semantic into an orientation using the actual VRM hand,
# finger, foot and toe bones.
PALMS_ON_FLOOR = {
    "table", "cat", "cow", "bird_dog_left", "bird_dog_right",
    "thread_needle_left", "thread_needle_right", "child_pose",
    "downward_dog", "plank", "side_plank_left", "side_plank_right",
    "cobra", "half_split_left", "half_split_right", "lizard_left",
    "lizard_right", "pigeon_prep_left", "pigeon_prep_right",
}

PLANTED_FEET: dict[str, tuple[str, ...]] = {
    "downward_dog": ("left", "right"), "plank": ("left", "right"),
    "side_plank_left": ("left",), "side_plank_right": ("right",),
    "chair": ("left", "right"),
    "low_lunge_left": ("left",), "low_lunge_right": ("right",),
    "low_lunge_reach_left": ("left",), "low_lunge_reach_right": ("right",),
    "half_split_left": ("left",), "half_split_right": ("right",),
    "lizard_left": ("left",), "lizard_right": ("right",),
    "high_lunge_left": ("left", "right"),
    "high_lunge_right": ("left", "right"),
    "pyramid_left": ("left", "right"), "pyramid_right": ("left", "right"),
    "warrior_three_left": ("left",), "warrior_three_right": ("right",),
    "standing_knee_left": ("right",), "standing_knee_right": ("left",),
}

POINTED_FEET = {
    "sphinx", "cobra",
    "pigeon_prep_left", "pigeon_prep_right",
    "warrior_three_left", "warrior_three_right",
}

FLEXED_FEET = {
    "staff", "seated_forward_fold", "seated_hamstring_left",
    "seated_hamstring_right", "half_split_left", "half_split_right",
}

RELAXED_FEET = {
    "savasana", "knees_to_chest", "figure_four_left",
    "figure_four_right", "easy_seat", "easy_seat_breath", "butterfly",
    "seated_side_stretch_left", "seated_side_stretch_right",
    "seated_twist_left", "seated_twist_right", "child_pose", "bridge",
}

# In quadruped, Child's Pose and prone backbends, the ankle is plantar-flexed
# and the dorsum (not the sole) rests on the mat.  `point` alone controls toe
# direction but leaves foot roll inherited from the shin, which is why these
# poses previously displayed upright shoe soles from the rear.
TOPS_OF_FEET_ON_FLOOR = {
    "cat", "cow", "child_pose", "cobra", "sphinx", "table",
    "thread_needle_left", "thread_needle_right",
}

PALMS_FORWARD = {"cactus_arms"}
PALMS_DOWN = {"easy_seat", "low_lunge_left", "low_lunge_right"}
PALMS_UP = {"easy_seat_breath", "savasana", "supine_twist_left",
            "supine_twist_right"}


def terminal_orientations(name: str) -> dict[str, str]:
    """Return semantic hand/foot constraints for one authored pose."""
    out: dict[str, str] = {}
    if name in PALMS_ON_FLOOR:
        # Side plank and Thread the Needle have only one supporting palm.
        sides = ("left", "right")
        if name == "side_plank_left": sides = ("left",)
        elif name == "side_plank_right": sides = ("right",)
        elif name == "thread_needle_left": sides = ("right",)
        elif name == "thread_needle_right": sides = ("left",)
        elif name == "bird_dog_left": sides = ("left",)
        elif name == "bird_dog_right": sides = ("right",)
        for side in sides:
            out[f"{side}_hand"] = "floor"
    if name in PALMS_FORWARD:
        out["left_hand"] = "palm_forward"
        out["right_hand"] = "palm_forward"
    if name in PALMS_DOWN:
        out["left_hand"] = "palm_down"
        out["right_hand"] = "palm_down"
    if name in PALMS_UP:
        out["left_hand"] = "palm_up"
        out["right_hand"] = "palm_up"
    if name == "gentle_back_reach":
        out["left_hand"] = "palm_to_back"
        out["right_hand"] = "palm_to_back"
    if name == "knees_to_chest":
        out["left_hand"] = "palm_in"
        out["right_hand"] = "palm_in"
    if name in {"mountain_breath", "shoulder_opener",
                "tree_heart_left", "tree_heart_right"}:
        out["left_hand"] = "palm_in"
        out["right_hand"] = "palm_in"
    if name in {"standing_knee_left", "standing_knee_right"}:
        out["left_hand"] = "palm_in"
        out["right_hand"] = "palm_in"
    if name == "reverse_warrior_left":
        out["left_hand"] = "palm_forward"
        out["right_hand"] = "palm_to_back"
    if name == "reverse_warrior_right":
        out["right_hand"] = "palm_forward"
        out["left_hand"] = "palm_to_back"
    for side in PLANTED_FEET.get(name, ()):
        out[f"{side}_foot"] = "floor"
    if name in POINTED_FEET:
        for side in ("left", "right"):
            out.setdefault(f"{side}_foot", "point")
    if name == "bird_dog_left":
        out["left_foot"] = "sole_down"
        out["right_foot"] = "sole_down"
    if name == "bird_dog_right":
        out["right_foot"] = "sole_down"
        out["left_foot"] = "sole_down"
    if name == "boat":
        out["left_foot"] = "flex"
        out["right_foot"] = "flex"
    if name == "happy_baby":
        out["left_foot"] = "flex"
        out["right_foot"] = "flex"
    if name == "butterfly":
        out["left_foot"] = "sole_in"
        out["right_foot"] = "sole_in"
    if name == "standing_knee_left": out["left_foot"] = "flex"
    if name == "standing_knee_right": out["right_foot"] = "flex"
    if name in {"tree_heart_left", "tree_overhead_left"}:
        out["left_foot"] = "floor"
        out["right_foot"] = "sole_to_leg"
    if name in {"tree_heart_right", "tree_overhead_right"}:
        out["right_foot"] = "floor"
        out["left_foot"] = "sole_to_leg"
    if name == "hand_to_toe_left":
        out["left_foot"] = "floor"
        out["right_foot"] = "flex"
    if name == "hand_to_toe_right":
        out["right_foot"] = "floor"
        out["left_foot"] = "flex"
    if name in FLEXED_FEET:
        for side in ("left", "right"):
            out.setdefault(f"{side}_foot", "flex")
    if name == "half_split_left":
        out["left_foot"] = "flex"
        out["right_foot"] = "top_down"
    if name == "half_split_right":
        out["right_foot"] = "flex"
        out["left_foot"] = "top_down"
    if name in RELAXED_FEET:
        for side in ("left", "right"):
            out.setdefault(f"{side}_foot", "relax")
    if name == "figure_four_left": out["left_foot"] = "flex"
    if name == "figure_four_right": out["right_foot"] = "flex"
    if name == "high_lunge_left": out["right_foot"] = "point"
    if name == "high_lunge_right": out["left_foot"] = "point"
    # A knee-down lunge has two different ankle jobs: the front sole bears
    # weight while the trailing ankle plantar-flexes and its dorsum meets the
    # mat.  Treating both as generic planted feet produced the upright soles
    # visible in every old Low Lunge and Lizard render.
    if name in {"low_lunge_left", "low_lunge_reach_left", "lizard_left",
                "pigeon_prep_left"}:
        out["right_foot"] = "top_down"
    if name in {"low_lunge_right", "low_lunge_reach_right", "lizard_right",
                "pigeon_prep_right"}:
        out["left_foot"] = "top_down"
    if name in TOPS_OF_FEET_ON_FLOOR:
        out["left_foot"] = "top_down"
        out["right_foot"] = "top_down"
    if name in {"bridge", "sphinx"}:
        out["left_hand"] = "floor"
        out["right_hand"] = "floor"
    if name == "bridge":
        out["left_foot"] = "floor_away"
        out["right_foot"] = "floor_away"
    return out


for _name, _pose in POSES3D.items():
    _terminals = terminal_orientations(_name)
    if _pose.get("view", "standing") == "standing":
        _terminals.setdefault("left_foot", "floor")
        _terminals.setdefault("right_foot", "floor")
    if _terminals:
        _pose["terminals"] = _terminals
    # Owner calibration established the actual VRM's neutral wrist/ankle
    # frames.  Apply those frames to every still-unapproved weight-bearing
    # terminal; semantic `floor` alone controls the surface normal but not the
    # shoe/palm roll around it.
    _terminal3d = dict(_pose.get("terminal3d") or {})
    for _side in ("left", "right"):
        if _terminals.get(f"{_side}_hand") == "floor":
            _terminal3d.setdefault(f"{_side}_hand",
                                   [0, 0, 30 if _side == "left" else -30])
        if (_pose.get("view", "standing") == "standing"
                and _terminals.get(f"{_side}_foot") == "floor"):
            _terminal3d.setdefault(f"{_side}_foot", [30, 0, 0])
    if _name == "high_lunge_right":
        # Exact mirror of the owner-approved High Lunge Left shoe frame.
        _terminal3d["left_foot"] = [160, -5, 180]
        _terminal3d["right_foot"] = [30, 0, 0]
    if _terminal3d:
        _pose["terminal3d"] = _terminal3d
    if _name in {"bird_dog_left", "bird_dog_right"}:
        _pose.setdefault("terminal3d", {}).update({
            "left_foot": [180, 0, 0], "right_foot": [180, 0, 0]})

# Owner approval exports are production data, not hints to be reconstructed.
# Full reviewed pose objects deliberately replace the authored drafts
# verbatim. Poses approved without edits keep their authored definitions.
_approval_files = (
    "approved_pose_edits.json",
    "approved_pose_edits_06_10.json",
    "approved_pose_edits_11_20.json",
    "approved_pose_edits_21_25.json",
    "approved_pose_edits_26_30.json",
    "approved_pose_edits_31_91.json",
    "approved_pose_edits_fixed_17.json",
    "approved_pose_edits_fixed_6.json",
    "approved_pose_edits_head_91.json",
)
OWNER_APPROVED_BATCHES = [json.loads(Path(__file__).with_name(filename)
    .read_text(encoding="utf-8")) for filename in _approval_files]
OWNER_APPROVED = {"approved": [], "pose_edits": {}}
for _batch in OWNER_APPROVED_BATCHES:
    for _name in _batch["approved"]:
        if _name not in OWNER_APPROVED["approved"]:
            OWNER_APPROVED["approved"].append(_name)
    OWNER_APPROVED["pose_edits"].update(_batch["pose_edits"])
POSES3D.update(OWNER_APPROVED["pose_edits"])
