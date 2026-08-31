# The Yoga Coach as a runtime 3D character

The coach used to be a picture. Ninety-one poses meant ninety-one generated
drawings, plus a transition clip for every pair of poses a lesson could put
next to each other, plus a breathing loop for every hold. This document
describes what replaced that: **one rigged model, posed from the same bone
tables the player is scored against, sixty times a second on the Pi.**

Nothing about the curriculum changed. The 21 courses, the holds and the
segments are exactly as approved. One pose moved from scored to guided — Chair,
deliberately, so that the coach could show a real squat instead of a woman
standing up; see below.

## Why the change

The 2D route had five defects, and they were not five problems:

| Defect | Cause |
|---|---|
| The coach was a different person from pose to pose | Each image is an independent generation |
| A magenta rim around her on the grass | Chroma keying a soft edge |
| The backdrop drifted to pink or cyan mid-clip | The model holds the two endpoint frames and wanders between them |
| Floor poses read as standing on a lawn | A painted floor has no perspective to be right about |
| **She changed size between poses** | Every drawing had its own scale and its own ground line |

All five are consequences of the coach being *drawn*. A posed model has one
identity, no edge to key, no backdrop to drift, a real ground plane, and one
size — by construction, not by care.

The measured costs also moved the right way. The clip library was **97 MB** of
WebP and needed a GPU night per batch to regenerate. The data behind the 3D
coach is **103 kB of JSON** plus a **15 MB** model, and regenerating it is
`python scripts/build_yoga_v3_data.py`, which takes under a second.

## The pieces

```
design/yoga_v2/poses3d.py        60 guided poses, as bone angles
design/yoga_v2/check3d.py        checks them by arithmetic
scripts/build_yoga_v3_data.py    exports rigdata.json
aipi5/ui/web/assets/yoga/v3/
  coach3d.js                     the retargeting
  rigdata.json                   103 kB: rig, poses, catalog, courses, views
  models/coach.vrm               15 MB, 33 736 triangles, 54 humanoid bones
  stage.html                     the coach on the mat
  contact.html                   the review tool
tests/coach3d.test.mjs           fourteen tests, no graphics card required
```

## The one idea

A rig table is **a flat picture of a body**: twelve bone directions in a plane.
That is what `rig.py` has always been, and it is what the scorer measures the
player against. Putting it onto a model needs only two more numbers:

- **`root`** — which way the coach is turned to face.
- **`plane`** — which plane the angles of the table are read in.

Standing poses set neither: she faces the player and the angles are screen
directions, unchanged from the shipped game. A side pose sets `root` and leaves
`plane` alone, so the same twelve numbers describe her sagittal silhouette —
Cat, Downward Dog, Cobra. A supine pose sets both to the same thing, laying the
picture onto the grass with the body, and the camera looks down at it.

Keeping the two apart is the whole design. Collapsing them into one frame is a
change that looks like a simplification and quietly makes every side-on pose
face the wrong way; it was written that way first, and every quadruped pose
came out lying on its side in mid-air.

## Retargeting, and the three ways it went wrong

Per bone, parents first: take the bone's **rest world direction** (measured once
from the bind pose as the direction toward its child), take the **wanted
direction** from the rig angle, build the rotation that carries one to the
other, compose it with the bone's rest world rotation, and express the result in
the parent's current space.

Three things about that were wrong at first, and all three rendered a picture
rather than an error:

1. **The wanted direction was taken in the world instead of the plane.** Floor
   poses tipped the body while the limbs stayed in the screen plane.
2. **The rest *rotation* was not carried into the body's frame** even though
   the rest *direction* was. A coach asked to turn side-on turned her pelvis
   and left everything above the waist facing the camera.
3. **`Box3.setFromObject` reports the bind-pose box for a skinned mesh.**
   Ground contact computed from it floated the coach above the grass, and it
   reported the same measurements for Savasana as for Mountain. The fix is a
   stride sample of ~900 vertices, skinned per frame — accurate to millimetres
   and cheap enough to leave the frame time at 60 fps.

There are no per-pose corrections anywhere in the retargeting, and there should
never be. A pose that comes out wrong is wrong in its numbers.

### `bones3d`, and when each kind runs

Some shapes a flat picture cannot state: a twist, a pelvis tipped out of the
plane. `bones3d` says them, and **when** each entry is applied is the whole of
it.

- A bone the solve never touches — the **hips**, the **chest** — is turned
  *before* the solve, so it moves joints rather than directions. The pelvis
  tips for Tabletop and the legs stay on the floor.
- A bone the solve does touch — the **spine**, the **neck** — is turned from
  inside the solve loop, in the instant between its own direction being fixed
  and its children being placed. Any earlier and the solve overwrites it; any
  later and the children have already been placed against an untwisted parent.

The second case was found by the test suite, not by looking: every seated and
standing twist was silently losing its spine and neck rotation and keeping only
the chest.

## Mirroring

Unchanged, and now provable. The convention is that **an angle near 180 belongs
to the limb the player copies with their left**, drawn at the smaller screen x.
A VRM faces the camera, so the limb at the smaller screen x is the character's
own *right* — which is why `RIG_TO_VRM` maps rig-left onto the model's
right-side bones. That is what "she demonstrates mirrored, the way a teacher
faces a class" has always meant, and no other part of the project changes.

The rule survives the change of plane for free: it is stated about which *bone*
receives the angle, not about which way the screen happens to be pointing.

Two tests hold it: one that the rig-left limb ends up at the smaller x, and one
that a mirrored table produces a body which is the mirror image of the original
in every one of the ten solved bones.

## Camera

Five presets, and any pose may carry a `camera` of its own merged over its
preset.

| view | for | distance | height | target |
|---|---|---|---|---|
| `standing` | standing poses | 4.2 | 1.15 | 0.95 |
| `kneel` | lunges, Downward Dog, Warrior III | 3.8 | 1.00 | 0.78 |
| `seated` | sitting on the floor | 3.0 | 0.85 | 0.50 |
| `low` | quadruped, prone, Bridge | 3.2 | 0.70 | 0.35 |
| `above` | supine | 2.0 | 3.00 | 0.05 |

The field that earns its keep is **`azimuth`** — degrees around the coach, zero
straight in front, positive walking toward her own left. Without it the camera
could only ever be in front and the *body* had to turn instead, which is fine
for a silhouette and hopeless for anything whose content is a rotation.

The rule for choosing one is the only rule here: **point the camera wherever
the pose is easiest to copy.** A silhouette teaches itself from the side and is
left alone — Downward Dog, Plank, Cobra, the lunges, the seated folds. What
gets moved is anything a single flat viewpoint destroys:

| pose | azimuth | why |
|---|---|---|
| Chair | 28 | A squat is a sagittal bend. From the front, deep Chair and standing upright are almost the same picture. |
| Thread the Needle | ∓30 | Seen side-on it is Tabletop. Off-axis it is obviously an arm sliding under a chest. |
| Child's Pose | 40 | The whole story is the hips travelling *back*, which is the one direction a level side view cannot show. |
| Supine Twist | 152 / 28 | From overhead, a knee dropped to the floor and a knee lifted into the air are the same picture; the camera goes round to the side the knees fall toward. |
| Bird Dog | −34 | It is a diagonal. Edge-on, a diagonal is a straight line. |
| Pigeon, Side Plank, the twists | ±16 to ±34 | Rotation and stacking, neither of which survives a flat view. |
| Cat, Cow, Tabletop, Dog | 12–16 | Enough off square that a body stops looking like a diagram of one. |

Thirty-nine of the sixty authored poses now carry their own camera.

**The move happens during the transition and is over before the hold begins.**
That is a property, not a hope: the camera is a pure function of the pose blend
rather than a spring chasing a target, so at the instant the move ends the
blend is exactly 1 and the camera is exactly where the pose asked, however far
it had to travel. Azimuth blends the short way round, so it never sails round
the back of the coach. Four tests hold this.

Scale still does not wander. The *character* never changes size at all;
`check3d.py` holds every pose to the ceiling of the view it is filed under, and
the camera only ever moves between framings a pose asked for.

## Chair, and scoring as a choice

Chair used to be scored, and the coach drew it with straight legs — because
sitting back into a chair is a bend in the sagittal plane and the frontal table
that scores it cannot say so. From the front, deep Chair and standing upright
are very nearly the same seventeen numbers.

That is now settled the other way round. `PoseSpec.guided_only` turns scoring
off for a pose deliberately, and Chair is the case: the pose the coach has to
show in order to teach it is not the pose the frontal table can describe, and
**teaching wins**. It is authored in `poses3d.py` as a side-on squat with the
hips at 71% of standing height and well behind the heels, and shown from
three-quarters.

The split is now **36 scored / 55 guided**. Nothing else moved: the other three
poses reworked at the same time — Thread the Needle, Child's Pose and the
Supine Twist — were already guided by family.

## Anatomy and terminal joints

The solve now continues past the ten direction chains into the hands, fingers,
feet and toes. A pose declares a small semantic constraint (`floor`, `point`,
`flex`, or `relax`) rather than a model-specific Euler angle. At load time the
coach measures the avatar's actual wrist-to-middle-finger and ankle-to-toe axes;
at pose time it aligns that measured frame. Weight-bearing palms and planted
soles therefore meet the mat independently of the forearm or shin, while prone
and balance poses can point, flex, or relax the foot without a per-avatar hack.

`check3d.py` is also the anatomical gate for the complete 91-pose catalog. It
checks hinge direction, passive flexion limits, neck/spine separation, authored
3D twist limits, required terminal semantics, conservative torso/pelvis and
opposite-leg capsule cores, intentional-contact exceptions, and floor
penetration. Surface contact is allowed; a joint centre entering a body core is
not. The explicit limit profile covers shoulders, elbows, wrists, fingers,
hips, knees, ankles, spine and neck, including axes that the flat scoring table
cannot itself express.

The visual companion remains `contact.html`: eight 12-cell pages cover the
whole catalog and use the real VRM, skinning, camera and ground-settle path.

## Checking it

Three layers, in increasing cost:

**`python -m yoga_v2.check3d`** runs each pose through the same forward
kinematics the rig uses and asks whether the body it describes is the body it
claims to be: are the hands on the floor, is the knee under the hip, does the
knee bend the way a knee bends, is the pose the right size for its camera. It
found Tabletop's hips eleven centimetres too low — invisible in a thumbnail and
obvious at full size — and three poses with a leg bent backwards.

**`node --test tests/coach3d.test.mjs`** builds a humanoid by hand, with
deliberately different proportions from the shipped model, and checks the
retargeting itself: every solved bone points where the table says, in every
plane; a pose is absolute rather than an accumulation; twists survive; blends
take the short way round; breathing stays an order of magnitude below the
tightest scoring tolerance. `pytest tests/test_yoga_coach3d.py` runs it too, so
`pytest` still means "run the tests".

**`scripts/v3_shot.sh` with `contact.html`** renders twelve real poses to a
screen on the Pi and measures where each body lands in its own frame. `?tween=`
samples a transition across its whole travel, which is the only practical way to
review a two-second move.

## What this does not do

- It does not make guided poses scored. Scoring coverage was never the goal,
  and one pose went the other way to teach better.
- It does not change what a *scored* pose is measured against. The 36 scored
  poses still use their shipped tables, unmodified, so the coach and the scorer
  cannot disagree.
- The 2D artwork and clips are still on disk. Nothing has been deleted.
