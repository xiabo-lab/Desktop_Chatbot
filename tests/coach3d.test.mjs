/* The 3D coach, checked without a graphics card.
 *
 * `coach3d.js` is arithmetic wearing a renderer: quaternions in, quaternions
 * out. None of it needs a canvas, so all of it can be tested here -- and it
 * needs to be, because the bugs it had were all silent ones. A bone left
 * facing the way the bind pose faced, a hips rotation quietly accumulating a
 * degree per frame, a mirrored pose that was not a mirror: every one of those
 * rendered a picture, and the picture merely looked a little wrong.
 *
 * The skeleton here is built by hand rather than loaded from the VRM. That is
 * deliberate. The claim in the header of `coach3d.js` is that the retargeting
 * works on any rig with a sane bind pose, and a test that only ever sees the
 * one shipped model cannot tell whether that is true or whether the numbers
 * happen to suit VRoid.
 *
 *     node --test tests/coach3d.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as THREE from "../aipi5/ui/web/assets/yoga/v3/lib/three.module.js";
import { Coach, RIG_TO_VRM, SOLVE_ORDER, blendTables, blendVectorTables,
         blendPose, blendTransition, transitionEffort, breathing,
         resolveView, placeCamera, blendView }
  from "../aipi5/ui/web/assets/yoga/v3/coach3d.js";

const DEG = Math.PI / 180;
const RIGDATA = JSON.parse(readFileSync(new URL(
  "../aipi5/ui/web/assets/yoga/v3/rigdata.json", import.meta.url), "utf8"));

function coursePose(id) {
  return RIGDATA.poses3d[id] || {
    bones: RIGDATA.poses[id].table,
    camera: RIGDATA.poses[id].camera,
    terminals: RIGDATA.poses[id].terminals,
    terminal3d: RIGDATA.poses[id].terminal3d,
  };
}

/** A humanoid in the VRM bind pose: T-pose, facing +Z, arms out along X.
 *  Deliberately not the shipped proportions -- if a pose only solves on one
 *  set of bone lengths, the solve is wrong. */
function skeleton() {
  const made = new Map();
  const make = (name, parent, offset) => {
    const bone = new THREE.Bone();
    bone.name = "J_" + name;
    bone.position.set(...offset);
    (parent || null) && parent.add(bone);
    made.set(name, bone);
    return bone;
  };
  const root = new THREE.Object3D();
  const hips = make("hips", null, [0, 0.93, 0]);
  root.add(hips);
  const spine = make("spine", hips, [0, 0.11, 0]);
  const chest = make("chest", spine, [0, 0.16, 0]);
  const upper = make("upperChest", chest, [0, 0.17, 0]);
  const neck = make("neck", upper, [0, 0.19, 0]);
  make("head", neck, [0, 0.11, 0]);
  for (const [side, sign] of [["left", 1], ["right", -1]]) {
    const shoulder = make(side + "Shoulder", upper, [sign * 0.04, 0.1, 0]);
    const arm = make(side + "UpperArm", shoulder, [sign * 0.08, 0, 0]);
    const fore = make(side + "LowerArm", arm, [sign * 0.27, 0, 0]);
    const hand = make(side + "Hand", fore, [sign * 0.23, 0, 0]);
    make(side + "MiddleProximal", hand, [sign * 0.07, 0, 0]);
    const leg = make(side + "UpperLeg", hips, [sign * 0.09, 0, 0]);
    const shin = make(side + "LowerLeg", leg, [0, -0.42, 0]);
    const foot = make(side + "Foot", shin, [0, -0.41, 0]);
    make(side + "Toes", foot, [0, -0.04, 0.12]);
  }
  const names = {};
  for (const key of made.keys()) names[key] = "J_" + key;
  root.updateMatrixWorld(true);
  return { root, names };
}

const coach = () => {
  const { root, names } = skeleton();
  return new Coach(root, names);
};

const STAND = {
  spine: -90, neck: -90,
  left_upper_arm: 96, left_forearm: 96, right_upper_arm: 84, right_forearm: 84,
  left_thigh: 90, left_shin: 90, right_thigh: 90, right_shin: 90,
};

/** Which way a solved bone actually points, in the world. */
function direction(c, rigName) {
  const [boneName, childName] = RIG_TO_VRM[rigName];
  const here = c.bones[boneName].getWorldPosition(new THREE.Vector3());
  const there = c.bones[childName].getWorldPosition(new THREE.Vector3());
  return there.sub(here).normalize();
}

/** The direction a table angle asks for, in the plane it is read in. */
function wanted(angle, plane) {
  const v = new THREE.Vector3(Math.cos(angle * DEG), -Math.sin(angle * DEG), 0);
  if (!plane) return v;
  const e = new THREE.Euler((plane.pitch || 0) * DEG, (plane.yaw || 0) * DEG,
                            (plane.roll || 0) * DEG, "YXZ");
  return v.applyQuaternion(new THREE.Quaternion().setFromEuler(e));
}

const degreesApart = (a, b) =>
  Math.acos(Math.min(1, Math.max(-1, a.dot(b)))) / DEG;

function pointSegmentDistance(point, start, end) {
  const along = end.clone().sub(start);
  const length2 = along.lengthSq();
  const t = length2 ? THREE.MathUtils.clamp(
    point.clone().sub(start).dot(along) / length2, 0, 1) : 0;
  return point.distanceTo(start.clone().addScaledVector(along, t));
}

function segmentDistance(a0, a1, b0, b1) {
  // Dense enough for the conservative core radii below, and far clearer than
  // hiding an analytic closest-segment solver inside an animation test.
  let closest = Infinity;
  for (let i = 0; i <= 20; i++) {
    const a = a0.clone().lerp(a1, i / 20);
    const b = b0.clone().lerp(b1, i / 20);
    closest = Math.min(closest, pointSegmentDistance(a, b0, b1),
                       pointSegmentDistance(b, a0, a1));
  }
  return closest;
}

function collisionFaults(c) {
  const at = name => c.bones[name].getWorldPosition(new THREE.Vector3());
  const midpoint = (a, b) => a.clone().lerp(b, 0.5);
  const hip = midpoint(at("leftUpperLeg"), at("rightUpperLeg"));
  const torso = [hip, at("neck")];
  const limbs = {
    left: { shoulder: at("rightUpperArm"), elbow: at("rightLowerArm"),
            hand: at("rightHand"), hip: at("rightUpperLeg"),
            knee: at("rightLowerLeg"), foot: at("rightFoot") },
    right: { shoulder: at("leftUpperArm"), elbow: at("leftLowerArm"),
             hand: at("leftHand"), hip: at("leftUpperLeg"),
             knee: at("leftLowerLeg"), foot: at("leftFoot") },
  };
  const faults = new Set();
  for (const side of ["left", "right"]) {
    const p = limbs[side];
    if (pointSegmentDistance(p.hand, ...torso) < 0.045)
      faults.add(`${side} hand/torso`);
    if (segmentDistance(midpoint(p.elbow, p.hand), p.hand, ...torso) < 0.035)
      faults.add(`${side} forearm/torso`);
    if (segmentDistance(midpoint(p.shoulder, p.elbow), p.elbow, ...torso) < 0.03)
      faults.add(`${side} upper-arm/torso`);
    if (pointSegmentDistance(p.knee, ...torso) < 0.045)
      faults.add(`${side} knee/torso`);
    if (segmentDistance(midpoint(p.hip, p.knee), p.knee, ...torso) < 0.035)
      faults.add(`${side} thigh/torso`);
  }
  const l = limbs.left, r = limbs.right;
  if (segmentDistance(l.hip, l.knee, r.hip, r.knee) < 0.04)
    faults.add("thigh/thigh");
  if (pointSegmentDistance(l.foot, r.hip, r.knee) < 0.03)
    faults.add("left foot/right thigh");
  if (pointSegmentDistance(r.foot, l.hip, l.knee) < 0.03)
    faults.add("right foot/left thigh");
  return faults;
}

function poseDirectionInBody(pose, name) {
  const angle = pose.bones[name] * DEG;
  const depth = (pose.depth3d?.[name] || 0) * DEG;
  const direction = new THREE.Vector3(
    Math.cos(angle) * Math.cos(depth),
    -Math.sin(angle) * Math.cos(depth), Math.sin(depth));
  const rotate = spec => new THREE.Quaternion().setFromEuler(new THREE.Euler(
    (spec?.pitch || 0) * DEG, (spec?.yaw || 0) * DEG,
    (spec?.roll || 0) * DEG, "YXZ"));
  return direction.applyQuaternion(rotate(pose.plane))
    .applyQuaternion(rotate(pose.root).invert()).normalize();
}

test("every solved bone ends up pointing where the table says", () => {
  const c = coach();
  const cases = [
    { name: "standing", pose: { bones: STAND } },
    { name: "a wide, asymmetric standing pose", pose: { bones: {
        ...STAND, spine: -104, neck: -120, left_upper_arm: 180,
        left_forearm: 180, right_upper_arm: 0, right_forearm: 0,
        left_thigh: 150, left_shin: 92, right_thigh: 55, right_shin: 55 } } },
    { name: "side-on", pose: { bones: { ...STAND, spine: -19, neck: 4,
        left_thigh: 92, left_shin: 178, right_thigh: 88, right_shin: 182,
        left_upper_arm: 92, left_forearm: 92 }, root: { yaw: 90 } } },
    { name: "side-on the other way", pose: { bones: STAND, root: { yaw: -90 } } },
    { name: "supine", pose: { bones: { ...STAND, left_thigh: 98,
        right_thigh: 82 }, root: { pitch: -90, yaw: 90 },
        plane: { pitch: -90, yaw: 90 } } },
  ];
  for (const { name, pose } of cases) {
    c.apply(pose);
    for (const rigName of SOLVE_ORDER) {
      const off = degreesApart(direction(c, rigName),
                               wanted(pose.bones[rigName], pose.plane));
      assert.ok(off < 0.5,
        `${name}: ${rigName} is ${off.toFixed(1)} degrees off the table`);
    }
  }
});

test("turning the body does not leave the bones behind", () => {
  // The bug this pins down: the bind rotations were composed un-turned, so a
  // side-on coach turned her pelvis and kept everything above the waist
  // facing the camera. The give-away is that the *shoulders* stop being
  // square to the way she faces.
  const c = coach();
  c.apply({ bones: STAND, root: { yaw: 90 } });
  const left = c.bones.leftUpperArm.getWorldPosition(new THREE.Vector3());
  const right = c.bones.rightUpperArm.getWorldPosition(new THREE.Vector3());
  const across = left.sub(right).normalize();
  assert.ok(Math.abs(across.z) > 0.9,
    `turned side-on, the shoulder line should run in depth, not across the ` +
    `screen -- it came out ${across.toArray().map(n => n.toFixed(2))}`);
});

test("a pose is absolute, not an accumulation", () => {
  const c = coach();
  const pose = { bones: { ...STAND, spine: -19 }, root: { yaw: 90 },
                 bones3d: { hips: [88, 0, 0], chest: [0, 0, 20] } };
  c.apply(pose);
  const first = c.bones.hips.quaternion.clone();
  for (let i = 0; i < 40; i++) c.apply(pose);
  assert.ok(c.bones.hips.quaternion.angleTo(first) < 1e-6,
    "forty frames of the same pose drifted the hips");
  const other = { bones: STAND };
  c.apply(other);
  c.apply(pose);
  assert.ok(c.bones.hips.quaternion.angleTo(first) < 1e-6,
    "the hips remember a pose they were asked to leave");
});

test("bones3d survives the solve where it should, and only there", () => {
  const c = coach();
  const plain = { bones: STAND };
  const twisted = { bones: STAND, bones3d: { spine: [0, 30, 0] } };
  c.apply(plain);
  const before = c.bones.leftUpperArm.getWorldPosition(new THREE.Vector3());
  c.apply(twisted);
  const after = c.bones.leftUpperArm.getWorldPosition(new THREE.Vector3());
  assert.ok(before.distanceTo(after) > 0.02,
    "a turn about the spine should carry the shoulders round with it");
  // ...while the arm itself still points exactly where the table asked.
  assert.ok(degreesApart(direction(c, "left_upper_arm"),
                         wanted(STAND.left_upper_arm)) < 0.5,
    "the twist moved the arm off its table direction");
});

test("neck extras turn the face and bend the head independently", () => {
  const c = coach();
  const face = () => new THREE.Vector3(0, 0, 1)
    .applyQuaternion(c.bones.head.getWorldQuaternion(new THREE.Quaternion()));
  c.apply({ bones: STAND });
  const forward = face();
  const headAtRest = c.bones.head.getWorldPosition(new THREE.Vector3());

  c.apply({ bones: STAND, bones3d: { neck: [0, 45, 0] } });
  assert.ok(degreesApart(forward, face()) > 40,
    "neck Y should turn the face left/right");
  assert.ok(c.bones.head.getWorldPosition(new THREE.Vector3())
      .distanceTo(headAtRest) < 1e-6,
    "turning the face should not move the head off the neck axis");

  c.apply({ bones: STAND, bones3d: { neck: [25, 0, 0] } });
  assert.ok(degreesApart(direction(c, "neck"), new THREE.Vector3(0, 1, 0)) > 20,
    "neck X should bend the head up/down");
});

test("depth3d moves a chain out of the flat pose plane", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, left_thigh: 180 },
            depth3d: { left_thigh: 60 } });
  const thigh = direction(c, "left_thigh");
  assert.ok(thigh.x < -0.49 && thigh.z > 0.86,
    "a forward-opening hip must move the knee in depth, not through the pelvis");
});

test("the coach demonstrates mirrored", () => {
  // The rule the whole game rests on: an angle near 180 is the limb the
  // player copies with their *left*, and it is drawn at the smaller screen x.
  const c = coach();
  c.apply({ bones: { ...STAND, left_upper_arm: 170, left_forearm: 170,
                     right_upper_arm: 10, right_forearm: 10 } });
  const leftHand = c.bones.rightHand.getWorldPosition(new THREE.Vector3());
  const rightHand = c.bones.leftHand.getWorldPosition(new THREE.Vector3());
  assert.ok(leftHand.x < rightHand.x,
    "the limb driven by the rig's left angles must sit at the smaller x");
});

test("a mirrored table gives a mirrored body", () => {
  const c = coach();
  const wrap = (a) => ((((a + 180) % 360) + 360) % 360) - 180;
  const swap = (t) => {
    const out = {};
    for (const [k, v] of Object.entries(t)) {
      const other = k.startsWith("left_") ? "right_" + k.slice(5)
                  : k.startsWith("right_") ? "left_" + k.slice(6) : k;
      out[other] = wrap(180 - v);
    }
    return out;
  };
  const table = { ...STAND, spine: -104, left_upper_arm: 180,
                  left_forearm: 150, left_thigh: 150, left_shin: 92,
                  right_thigh: 55, right_shin: 55 };
  c.apply({ bones: table });
  const seen = SOLVE_ORDER.map((n) => direction(c, n).clone());
  c.apply({ bones: swap(table) });
  for (let i = 0; i < SOLVE_ORDER.length; i++) {
    const name = SOLVE_ORDER[i];
    const other = name.startsWith("left_") ? "right_" + name.slice(5)
                : name.startsWith("right_") ? "left_" + name.slice(6) : name;
    const now = direction(c, other);
    const flipped = seen[i].clone().setX(-seen[i].x);
    assert.ok(degreesApart(now, flipped) < 0.5,
      `${name} is not the mirror of ${other}`);
  }
});

test("a blend takes the short way round", () => {
  // -170 to 170 is twenty degrees the short way and three hundred and forty
  // the long way; halfway is the back of the circle, named either way round.
  const out = blendTables({ spine: -170 }, { spine: 170 }, 0.5);
  assert.equal(Math.abs(out.spine), 180, "the arm swung through the body");
  assert.equal(blendTables({ a: 10 }, { a: 50 }, 0.25).a, 20);
  assert.equal(blendTables({}, { a: 7 }, 0.5).a, 7, "a missing start is held");
});

test("a blend carries the plane, so a pose does not stand up mid-move", () => {
  const flat = { bones: STAND, root: { pitch: -90, yaw: 90 },
                 plane: { pitch: -90, yaw: 90 }, view: "above" };
  const up = { bones: STAND, view: "standing" };
  const half = blendPose(flat, up, 0.5);
  const turn = spec => new THREE.Quaternion().setFromEuler(new THREE.Euler(
    (spec.pitch || 0) * DEG, (spec.yaw || 0) * DEG,
    (spec.roll || 0) * DEG, "YXZ"));
  assert.ok(Math.abs(turn(half.plane).angleTo(turn(flat.plane)) -
                     turn(half.plane).angleTo(turn(up.plane || {}))) < 1e-6,
    "the plane has to SLERP continuously with the body or the limbs tear off it");
  assert.ok(Math.abs(turn(half.root).angleTo(turn(flat.root)) -
                     turn(half.root).angleTo(turn(up.root || {}))) < 1e-6);
  const depthHalf = blendPose({ bones: STAND, depth3d: { left_thigh: 0 } },
                              { bones: STAND, depth3d: { left_thigh: 60 } }, 0.5);
  assert.ok(depthHalf.depth3d.left_thigh > 0 && depthHalf.depth3d.left_thigh < 60,
    "limb depth must travel continuously instead of popping mid-transition");
});

test("large arm sweeps travel through the coach's front, never behind", () => {
  const down = { bones: { ...STAND, left_upper_arm: 96, left_forearm: 96,
                          right_upper_arm: 84, right_forearm: 84 } };
  const overhead = { bones: { ...STAND, left_upper_arm: -96, left_forearm: -93,
                              right_upper_arm: -84, right_forearm: -87 } };
  const half = blendPose(down, overhead, 0.5);
  for (const name of ["left_upper_arm", "left_forearm",
                      "right_upper_arm", "right_forearm"]) {
    assert.ok(half.depth3d[name] > 60,
      `${name} did not pass through the body's front hemisphere`);
  }
});

test("transition knees preserve parent-relative flexion", () => {
  // This is the formerly broken Savasana -> Knees-to-Chest transition.  The
  // old independent angle blend put the left shin about 120 degrees behind
  // straight at its midpoint even though the endpoints are -2 and +121.
  const rest = { bones: { ...STAND, left_thigh: 98, left_shin: 96,
                          right_thigh: 82, right_shin: 84 } };
  const hug = { bones: { ...STAND, left_thigh: -146, left_shin: -25,
                         right_thigh: -146, right_shin: -25 } };
  const wrap = (angle) => ((angle + 180) % 360 + 360) % 360 - 180;
  for (let t = 0.01; t < 1; t += 0.01) {
    const pose = blendPose(rest, hug, t);
    for (const side of ["left", "right"]) {
      const flex = wrap(pose.bones[`${side}_shin`] - pose.bones[`${side}_thigh`]);
      assert.ok(flex >= -2.001 && flex <= 121.001,
        `${side} knee left its reviewed flexion interval at t=${t}: ${flex}`);
    }
  }
});

test("large leg changes release, move from the hip, then bend", () => {
  const from = { bones: { ...STAND, left_thigh: 90, left_shin: -20 } };
  const to = { bones: { ...STAND, left_thigh: -90, left_shin: 30 } };
  const half = blendPose(from, to, 0.5);
  const bend = pose => degreesApart(poseDirectionInBody(pose, "left_thigh"),
                                    poseDirectionInBody(pose, "left_shin"));
  assert.ok(bend(half) < 1,
    "the knee must be released while the femur is repositioned");
  assert.ok(degreesApart(poseDirectionInBody(from, "left_thigh"),
                         poseDirectionInBody(half, "left_thigh")) > 20,
    "the orientation change must come from the hip");
});

test("every course transition keeps reviewed knee-hinge bounds", () => {
  const wrap = (angle) => ((angle + 180) % 360 + 360) % 360 - 180;
  let segments = 0, samples = 0;
  for (const course of Object.values(RIGDATA.courses)) {
    let previous = "mountain";
    for (const step of course.steps) {
      if (step.pose === previous) continue;
      const key = `${previous}>${step.pose}`;
      const ids = [previous, ...(RIGDATA.transition_paths[key] || []), step.pose];
      for (let index = 0; index < ids.length - 1; index++) {
        const from = coursePose(ids[index]), to = coursePose(ids[index + 1]);
        segments++;
        for (const side of ["left", "right"]) {
          const thigh = `${side}_thigh`, shin = `${side}_shin`;
          const a = wrap(from.bones[shin] - from.bones[thigh]);
          const b = wrap(to.bones[shin] - to.bones[thigh]);
          for (let t = 0.025; t < 1; t += 0.025) {
            const pose = blendPose(from, to, t);
            const flex = wrap(pose.bones[shin] - pose.bones[thigh]);
            // A large reposition is deliberately allowed to release toward
            // straight before the hip moves, then reapply the target bend.
            const low = Math.min(0, a, b), high = Math.max(0, a, b);
            assert.ok(flex >= low - 1e-6 && flex <= high + 1e-6,
              `${key} ${ids[index]} -> ${ids[index + 1]} ${side} knee ` +
              `left [${low}, ${high}] at t=${t}: ${flex}`);
            if (a >= -8 && b >= -8) assert.ok(flex >= Math.min(0, a, b) - 1e-6,
              `${key} invented ${side} knee hyperextension: ${flex}`);
            samples++;
          }
        }
      }
      previous = step.pose;
    }
  }
  assert.ok(segments > 500 && samples > 40000,
    `expected the complete 21-course pass, got ${segments} segments/${samples} samples`);
});

test("every generated foot stays inside the reviewed human ankle range", () => {
  const c = coach();
  const checked = new Set();
  let samples = 0;
  for (const course of Object.values(RIGDATA.courses)) {
    let previous = "mountain";
    for (const step of course.steps) {
      if (step.pose === previous) continue;
      const key = `${previous}>${step.pose}`;
      const ids = [previous, ...(RIGDATA.transition_paths[key] || []), step.pose];
      for (let index = 0; index < ids.length - 1; index++) {
        const segmentKey = `${ids[index]}>${ids[index + 1]}`;
        if (checked.has(segmentKey)) continue;
        checked.add(segmentKey);
        const from = coursePose(ids[index]), to = coursePose(ids[index + 1]);
        c.apply(from);
        const previousFoot = Object.fromEntries(["left", "right"].map(side => [
          side, c.terminals[`${side}_foot`].bone.quaternion.clone(),
        ]));
        const previousAnkle = Object.fromEntries(["left", "right"].map(side => [
          side, c.ankleAngles(`${side}_foot`),
        ]));
        for (let frame = 1; frame <= 40; frame++) {
          c.apply(frame === 40 ? to : blendPose(from, to, frame / 40));
          for (const side of ["left", "right"]) {
            const ankle = c.ankleAngles(`${side}_foot`);
            assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01,
              `${segmentKey} ${side} ankle flexion ${ankle.flexion.toFixed(2)}° ` +
              `at frame ${frame}`);
            assert.ok(ankle.deviation >= -15.01 && ankle.deviation <= 20.01,
              `${segmentKey} ${side} ankle toe deviation ` +
              `${ankle.deviation.toFixed(2)}° at frame ${frame}`);
            assert.ok(ankle.forward.dot(ankle.neutralForward) > 0,
              `${segmentKey} ${side} toes crossed behind the shin at frame ${frame}`);
            assert.ok(ankle.roll >= -20.01 && ankle.roll <= 30.01,
              `${segmentKey} ${side} ankle roll ${ankle.roll.toFixed(2)}° ` +
              `at frame ${frame}`);
            const current = c.terminals[`${side}_foot`].bone.quaternion.clone();
            const turn = previousFoot[side].angleTo(current) / DEG;
            // This is a hard flip detector, not a playback-speed assertion:
            // accelerated review deliberately compresses a transition. A
            // terminal may move quickly, but it must never reverse in one
            // sample as the former 180-degree defect did.
            assert.ok(turn < 90,
              `${segmentKey} ${side} foot jumped ${turn.toFixed(1)}° ` +
              `at frame ${frame}; flex=${ankle.flexion.toFixed(1)}, ` +
              `dev=${ankle.deviation.toFixed(1)}, roll=${ankle.roll.toFixed(1)}; ` +
              `prior=${previousAnkle[side].flexion.toFixed(1)}/` +
              `${previousAnkle[side].deviation.toFixed(1)}/` +
              `${previousAnkle[side].roll.toFixed(1)}`);
            previousFoot[side] = current;
            previousAnkle[side] = ankle;
            samples++;
          }
        }
      }
      previous = step.pose;
    }
  }
  assert.ok(checked.size > 100 && samples > 8000,
    `expected all 21 courses, got ${checked.size} segments/${samples} feet`);
});

test("all 91 held poses also stay inside the reviewed human ankle range", () => {
  const c = coach();
  const ids = Object.keys(RIGDATA.catalog);
  for (const id of ids) {
    c.apply(coursePose(id));
    for (const side of ["left", "right"]) {
      const ankle = c.ankleAngles(`${side}_foot`);
      assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01,
        `${id} ${side} ankle flexion ${ankle.flexion.toFixed(2)}°`);
      assert.ok(ankle.deviation >= -15.01 && ankle.deviation <= 20.01,
        `${id} ${side} ankle toe deviation ${ankle.deviation.toFixed(2)}°`);
      assert.ok(ankle.forward.dot(ankle.neutralForward) > 0,
        `${id} ${side} toes crossed behind the shin`);
      assert.ok(ankle.roll >= -20.01 && ankle.roll <= 30.01,
        `${id} ${side} ankle roll ${ankle.roll.toFixed(2)}°`);
    }
  }
  assert.equal(ids.length, 91);
});

test("every generated palm stays inside the reviewed human wrist range", () => {
  const c = coach();
  const checked = new Set();
  let samples = 0;
  for (const course of Object.values(RIGDATA.courses)) {
    let previous = "mountain";
    for (const step of course.steps) {
      if (step.pose === previous) continue;
      const key = `${previous}>${step.pose}`;
      const ids = [previous, ...(RIGDATA.transition_paths[key] || []), step.pose];
      for (let index = 0; index < ids.length - 1; index++) {
        const segmentKey = `${ids[index]}>${ids[index + 1]}`;
        if (checked.has(segmentKey)) continue;
        checked.add(segmentKey);
        const from = coursePose(ids[index]), to = coursePose(ids[index + 1]);
        for (let frame = 1; frame < 40; frame++) {
          c.apply(blendPose(from, to, frame / 40));
          for (const side of ["left", "right"]) {
            const wrist = c.wristAngles(`${side}_hand`);
            const extension = c.wristExtensionLimit(
              blendPose(from, to, frame / 40), `${side}_hand`);
            const maxFlexion = c.wristFlexionLimit(
              blendPose(from, to, frame / 40), `${side}_hand`);
            assert.ok(wrist.flexion >= -extension - 0.01
                      && wrist.flexion <= maxFlexion + 0.01,
              `${segmentKey} ${side} wrist flexion ${wrist.flexion.toFixed(2)}° ` +
              `at frame ${frame}`);
            assert.ok(wrist.deviation >= -35.01 && wrist.deviation <= 20.01,
              `${segmentKey} ${side} wrist deviation ` +
              `${wrist.deviation.toFixed(2)}° at frame ${frame}`);
            samples++;
          }
        }
      }
      previous = step.pose;
    }
  }
  assert.ok(checked.size > 100 && samples > 8000,
    `expected all 21 courses, got ${checked.size} segments/${samples} hands`);
});

test("all 91 held poses also stay inside the reviewed human wrist range", () => {
  const c = coach();
  const ids = Object.keys(RIGDATA.catalog);
  for (const id of ids) {
    c.apply(coursePose(id));
    for (const side of ["left", "right"]) {
      const wrist = c.wristAngles(`${side}_hand`);
      const extension = c.wristExtensionLimit(coursePose(id), `${side}_hand`);
      const maxFlexion = c.wristFlexionLimit(coursePose(id), `${side}_hand`);
      assert.ok(wrist.flexion >= -extension - 0.01
                && wrist.flexion <= maxFlexion + 0.01,
        `${id} ${side} wrist flexion ${wrist.flexion.toFixed(2)}°`);
      assert.ok(wrist.deviation >= -35.01 && wrist.deviation <= 20.01,
        `${id} ${side} wrist deviation ${wrist.deviation.toFixed(2)}°`);
    }
  }
  assert.equal(ids.length, 91);
});

test("semantic palm modes turn continuously instead of flipping halfway", () => {
  const c = coach();
  const from = { bones: STAND, terminals: { left_hand: "floor" } };
  const to = { bones: STAND, terminals: { left_hand: "palm_up" } };
  let previous = null;
  for (let frame = 0; frame <= 40; frame++) {
    c.apply(frame === 0 ? from : frame === 40 ? to
      : blendPose(from, to, frame / 40));
    const current = c.terminals.left_hand.bone.getWorldQuaternion(
      new THREE.Quaternion());
    // Forty frames cover the 2.6-second review move. This is the combined
    // forearm-roll plus wrist result; 20° still catches the former 90–180°
    // midpoint mode flip while allowing both joints to move together.
    if (previous) assert.ok(previous.angleTo(current) / DEG < 20,
      `palm jumped ${(previous.angleTo(current) / DEG).toFixed(1)}° at frame ${frame}`);
    previous = current;
  }
});

test("Tabletop keeps both weight-bearing palm normals on the floor", () => {
  const c = coach();
  c.apply(coursePose("table"));
  for (const side of ["left", "right"]) {
    const entry = c.terminals[`${side}_hand`];
    const world = entry.bone.getWorldQuaternion(new THREE.Quaternion());
    const palm = entry.down.clone().applyQuaternion(world).normalize();
    const wrist = c.wristAngles(`${side}_hand`);
    assert.ok(palm.y < -0.9,
      `${side} supporting palm points away from the floor: y=${palm.y.toFixed(3)}`);
    assert.ok(wrist.flexion >= -90.01 && wrist.flexion <= 90.01
              && wrist.deviation >= -35.01 && wrist.deviation <= 20.01);
  }
});

test("every course transition rejects newly-created body-core penetration", () => {
  const c = coach(), endpointFaults = new Map(), failures = new Set();
  const faultsAt = (id) => {
    if (!endpointFaults.has(id)) {
      c.apply(coursePose(id));
      endpointFaults.set(id, collisionFaults(c));
    }
    return endpointFaults.get(id);
  };
  let samples = 0;
  for (const course of Object.values(RIGDATA.courses)) {
    let previous = "mountain";
    for (const step of course.steps) {
      if (step.pose === previous) continue;
      const key = `${previous}>${step.pose}`;
      const ids = [previous, ...(RIGDATA.transition_paths[key] || []), step.pose];
      for (let index = 0; index < ids.length - 1; index++) {
        const fromId = ids[index], toId = ids[index + 1];
        const allowed = new Set([...faultsAt(fromId), ...faultsAt(toId)]);
        for (let t = 0.05; t < 1; t += 0.05) {
          c.apply(blendPose(coursePose(fromId), coursePose(toId), t));
          for (const fault of collisionFaults(c)) {
            if (!allowed.has(fault)) failures.add(
              `${fromId} -> ${toId}: ${fault}`);
          }
          samples++;
        }
      }
      previous = step.pose;
    }
  }
  assert.deepEqual([...failures].slice(0, 100), [],
    `transition-created core collisions (${failures.size}; ${samples} samples)`);
});

test("every course transition preserves hinge limits and frame continuity", () => {
  const pairs = [
    ["left_thigh", "left_shin", 150], ["right_thigh", "right_shin", 150],
    ["left_upper_arm", "left_forearm", 145],
    ["right_upper_arm", "right_forearm", 145],
    ["spine", "neck", 80],
  ];
  const checked = new Set();
  for (const course of Object.values(RIGDATA.courses)) {
    let previous = "mountain";
    for (const step of course.steps) {
      if (step.pose === previous) continue;
      const key = `${previous}>${step.pose}`;
      const ids = [previous, ...(RIGDATA.transition_paths[key] || []), step.pose];
      for (let index = 0; index < ids.length - 1; index++) {
        const segmentKey = `${ids[index]}>${ids[index + 1]}`;
        if (checked.has(segmentKey)) continue;
        checked.add(segmentKey);
        const from = coursePose(ids[index]), to = coursePose(ids[index + 1]);
        let prior = from;
        for (let frame = 1; frame <= 40; frame++) {
          const pose = frame === 40 ? to : blendPose(from, to, frame / 40);
          for (const [parent, child, ordinaryLimit] of pairs) {
            const angle = degreesApart(poseDirectionInBody(pose, parent),
                                       poseDirectionInBody(pose, child));
            const endpointLimit = Math.max(
              degreesApart(poseDirectionInBody(from, parent),
                           poseDirectionInBody(from, child)),
              degreesApart(poseDirectionInBody(to, parent),
                           poseDirectionInBody(to, child)));
            assert.ok(angle <= Math.max(ordinaryLimit, endpointLimit) + 0.05,
              `${segmentKey} invented ${parent}/${child} flexion ${angle.toFixed(1)}°`);
            const frameTurn = degreesApart(poseDirectionInBody(prior, child),
                                           poseDirectionInBody(pose, child));
            assert.ok(frameTurn < 30,
              `${segmentKey} ${child} jumped ${frameTurn.toFixed(1)}° in one frame`);
          }
          prior = pose;
        }
      }
      previous = step.pose;
    }
  }
  assert.ok(checked.size > 100, `only checked ${checked.size} unique segments`);
});

test("3D head, limb, hand and foot corrections move continuously", () => {
  const half = blendVectorTables({ neck: [0, 170, 0], left_foot: [10, 0, 0] },
                                 { neck: [0, -170, 0], left_foot: [30, 20, 0] },
                                 0.5);
  const rotation = turns => new THREE.Quaternion().setFromEuler(new THREE.Euler(
    turns[0] * DEG, turns[1] * DEG, turns[2] * DEG, "XYZ"));
  assert.ok(Math.abs(rotation(half.neck).angleTo(rotation([0, 170, 0])) / DEG - 10) < 0.01,
    "the face must SLERP twenty degrees instead of snapping through front");
  assert.ok(Math.abs(rotation(half.left_foot).angleTo(rotation([10, 0, 0])) -
                     rotation(half.left_foot).angleTo(rotation([30, 20, 0]))) < 1e-6,
    "the midpoint must be equally far from both ankle corrections");
  const poseHalf = blendPose(
    { bones: STAND, bones3d: { neck: [0, 0, 0] } },
    { bones: STAND, bones3d: { neck: [20, 40, 0] } }, 0.5);
  const neckHalf = rotation(poseHalf.bones3d.neck);
  assert.ok(Math.abs(neckHalf.angleTo(rotation([0, 0, 0])) -
                     neckHalf.angleTo(rotation([20, 40, 0]))) < 1e-6,
    "reviewed 3D corrections must not pop at the midpoint");
});

test("a multi-action transition visits every reviewed station", () => {
  const pose = n => ({ bones: { ...STAND, spine: n } });
  const start = pose(-90), fold = pose(-45), table = pose(0), end = pose(45);
  const points = [start, fold, table, end];
  const weights = points.slice(0, -1).map((p, i) => transitionEffort(p, points[i + 1]));
  const total = weights.reduce((sum, value) => sum + value, 0);
  assert.equal(blendTransition(start, end, [fold, table], 0), start);
  assert.equal(blendTransition(start, end, [fold, table], 1), end);
  assert.equal(blendTransition(start, end, [fold, table], weights[0] / total).bones.spine,
               -45, "the first station must be reached exactly");
  assert.ok(Math.abs(blendTransition(start, end, [fold, table],
    (weights[0] + weights[1]) / total).bones.spine) < 1e-9,
    "the second station must be reached exactly");
});

test("breathing is visible and far below the scoring tolerance", () => {
  let biggest = 0;
  for (let t = 0; t < 60; t += 0.05) {
    for (const v of Object.values(breathing(t))) {
      biggest = Math.max(biggest, Math.abs(v));
    }
  }
  assert.ok(biggest > 0.5, "a coach who does not move is not breathing");
  // The tightest tolerance in scoring.py is the torso, at 13 degrees.
  assert.ok(biggest < 4, `breathing swings ${biggest.toFixed(1)} degrees, ` +
    "which is close enough to a scoring tolerance to change the pose");
});

test("a coach with no meshes settles instead of crashing", () => {
  const c = coach();
  c.apply({ bones: STAND });
  assert.ok(Number.isFinite(c.root.position.y));
});

test("supporting palms and soles orient independently of forearms and shins", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, left_forearm: 45, left_shin: 135 },
            terminals: { left_hand: "floor", left_foot: "floor" } });
  const axis = (a, b) => c.bones[b].getWorldPosition(new THREE.Vector3())
    .sub(c.bones[a].getWorldPosition(new THREE.Vector3())).normalize();
  const fingers = axis("rightHand", "rightMiddleProximal");
  const toes = axis("rightFoot", "rightToes");
  const wrist = c.wristAngles("left_hand");
  assert.ok(wrist.flexion >= -90.01 && wrist.flexion <= 90.01
            && wrist.deviation >= -35.01 && wrist.deviation <= 20.01,
    "a weight-bearing palm must stop before its wrist leaves human range");
  assert.ok(degreesApart(direction(c, "left_forearm"), fingers) > 20,
    "the palm terminal must still orient independently from the forearm");
  assert.ok(Math.abs(toes.y) < 1e-6 && toes.z > 0.99,
    "a planted foot must stay flat instead of following its bent shin");
});

test("a pointed foot extends without exceeding plantar-flexion range", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, left_thigh: 150, left_shin: 160 },
            terminals: { left_foot: "point" } });
  const ankle = c.ankleAngles("left_foot");
  assert.ok(Math.abs(ankle.flexion + 50) < 0.05,
    `pointing must stop at 50° plantar flexion, got ${ankle.flexion.toFixed(2)}°`);
});

test("joined soles face inward without changing the leg shape", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, left_thigh: 178, left_shin: 10,
                     right_thigh: 2, right_shin: 170 },
            terminals: { left_foot: "sole_in", right_foot: "sole_in" } });
  const soleNormal = name => {
    const entry = c.terminals[name];
    return entry.down.clone().applyQuaternion(
      entry.bone.getWorldQuaternion(new THREE.Quaternion())).normalize();
  };
  const left = soleNormal("left_foot"), right = soleNormal("right_foot");
  assert.ok(Math.abs(left.x) > 0.9 && Math.abs(right.x) > 0.9,
    "both sole normals should run across the body");
  assert.ok(left.dot(right) < -0.75,
    "the two soles should face one another, not overlap in one direction");
});

test("a Tree foot inclines inward without leaving the ankle envelope", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, right_thigh: 65, right_shin: 200 },
            terminals: { right_foot: "sole_to_leg" } });
  const entry = c.terminals.right_foot;
  const world = entry.bone.getWorldQuaternion(new THREE.Quaternion());
  const toes = entry.forward.clone().applyQuaternion(world).normalize();
  const sole = entry.down.clone().applyQuaternion(world).normalize();
  const ankle = c.ankleAngles("right_foot");
  assert.ok(ankle.forward.dot(ankle.neutralForward) > 0,
    "Tree toes must not reverse behind the lower leg");
  assert.ok(Math.abs(sole.x) > 0.25,
    "Tree sole must visibly incline across toward the standing leg");
  assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01
            && ankle.deviation >= -15.01 && ankle.deviation <= 20.01
            && ankle.roll >= -20.01 && ankle.roll <= 30.01);
});

test("Bird Dog and Bridge terminal overrides cannot twist an ankle past range", () => {
  const c = coach();
  c.apply({ bones: { ...STAND, left_thigh: 178, left_shin: 180 },
            terminals: { left_foot: "sole_down" },
            terminal3d: { left_foot: [-88, 180, 0] } });
  let ankle = c.ankleAngles("left_foot");
  assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01
            && ankle.roll >= -20.01 && ankle.roll <= 30.01);

  c.apply({ bones: { ...STAND, spine: 150, left_thigh: -25, left_shin: 90 },
            root: { pitch: -90, roll: 90 },
            terminals: { left_foot: "floor_away" } });
  ankle = c.ankleAngles("left_foot");
  assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01
            && ankle.roll >= -20.01 && ankle.roll <= 30.01);
});

test("a 180-degree reviewer foot offset cannot reverse either toe axis", () => {
  const c = coach();
  for (const side of ["left", "right"]) {
    c.apply({ bones: STAND, terminal3d: { [`${side}_foot`]: [0, 180, 0] } });
    const ankle = c.ankleAngles(`${side}_foot`);
    assert.ok(ankle.forward.dot(ankle.neutralForward) > 0.5,
      `${side} foot remained in the reversed hemisphere`);
    assert.ok(ankle.flexion >= -50.01 && ankle.flexion <= 20.01);
    assert.ok(ankle.deviation >= -15.01 && ankle.deviation <= 20.01);
  }
});

test("kneeling feet stop at human plantar range and Cactus palms face forward", () => {
  const c = coach();
  const normal = name => {
    const entry = c.terminals[name];
    return entry.down.clone().applyQuaternion(
      entry.bone.getWorldQuaternion(new THREE.Quaternion())).normalize();
  };
  c.apply({ bones: { ...STAND, left_thigh: 90, left_shin: 180 },
            terminals: { left_foot: "top_down" } });
  const ankle = c.ankleAngles("left_foot");
  assert.ok(Math.abs(ankle.flexion + 50) < 0.05,
    `kneeling plantar flexion must stop at 50°, got ${ankle.flexion.toFixed(2)}°`);

  c.apply({ bones: STAND, terminals: { left_hand: "palm_forward" } });
  const bodyForward = new THREE.Vector3(0, 0, 1)
    .applyQuaternion(c.rootQuat).setY(0).normalize();
  assert.ok(normal("left_hand").dot(bodyForward) > 0.9,
    "a Cactus palm must face forward rather than inherit forearm roll");
});

test("seated palms and back-supporting palms use anatomical normals", () => {
  const c = coach();
  const normal = name => {
    const entry = c.terminals[name];
    return entry.down.clone().applyQuaternion(
      entry.bone.getWorldQuaternion(new THREE.Quaternion())).normalize();
  };
  c.apply({ bones: STAND, terminals: { left_hand: "palm_down" } });
  assert.ok(normal("left_hand").y < -0.9);
  c.apply({ bones: STAND, terminals: { left_hand: "palm_up" } });
  assert.ok(normal("left_hand").y > 0.9);
  c.apply({ bones: STAND, terminals: { left_hand: "palm_to_back" } });
  const bodyForward = new THREE.Vector3(0, 0, 1)
    .applyQuaternion(c.rootQuat).setY(0).normalize();
  assert.ok(normal("left_hand").dot(bodyForward) > 0.9,
    "a hand behind the torso must place its palm against the lower back");
});

test("reviewer terminal edits cannot flip a foot or move its ankle", () => {
  const c = coach();
  const pose = { bones: STAND, terminals: { left_foot: "floor" } };
  c.apply(pose);
  const ankle = c.bones.rightFoot.getWorldPosition(new THREE.Vector3());
  const plain = c.bones.rightFoot.getWorldQuaternion(new THREE.Quaternion());
  c.apply({ ...pose, terminal3d: { left_foot: [180, 0, 0] } });
  const editedAnkle = c.bones.rightFoot.getWorldPosition(new THREE.Vector3());
  const edited = c.bones.rightFoot.getWorldQuaternion(new THREE.Quaternion());
  assert.ok(ankle.distanceTo(editedAnkle) < 1e-9,
    "editing the foot must not tear it away from the leg");
  assert.ok(plain.angleTo(edited) < 1.25,
    "a 180 degree reviewer edit must be reduced to an anatomical correction");
  const measured = c.ankleAngles("left_foot");
  assert.ok(measured.forward.dot(measured.neutralForward) > 0,
    "a reviewer edit must not reverse the toe axis");
});

test("a two-hand knee grasp turns both palms inward", () => {
  const c = coach();
  const normal = name => {
    const entry = c.terminals[name];
    return entry.down.clone().applyQuaternion(
      entry.bone.getWorldQuaternion(new THREE.Quaternion())).normalize();
  };
  c.apply({ bones: STAND,
            terminals: { left_hand: "palm_in", right_hand: "palm_in" } });
  assert.ok(normal("left_hand").dot(normal("right_hand")) < -0.9,
    "the two grasping palms must face one another");
});

test("a view is its preset with the pose's own camera merged over it", () => {
  const views = { standing: { distance: 4.2, height: 1.15, target: 0.95 },
                  low: { distance: 3.2, height: 0.7, target: 0.35 } };
  assert.deepEqual(resolveView({ view: "low" }, views),
                   { azimuth: 0, distance: 3.2, height: 0.7, target: 0.35 });
  const own = resolveView({ view: "low", camera: { azimuth: -30, height: 1.3 } },
                          views);
  assert.equal(own.azimuth, -30);
  assert.equal(own.height, 1.3, "the pose's own value has to win");
  assert.equal(own.distance, 3.2, "and the rest of the preset has to survive");
  assert.equal(resolveView({}, views).azimuth, 0, "the default is straight on");
});

test("the camera is settled by the time the hold begins", () => {
  // Not "it usually catches up": at the end of the move the blend is exactly
  // 1, so the camera is exactly where the pose asked, however far it came.
  // A spring chasing a target cannot promise that, and a camera still
  // travelling while the player is trying to copy a pose is the whole reason
  // this is a pure function of the blend instead.
  const a = { distance: 2.9, height: 1.3, target: 0.34, azimuth: -30 };
  const b = { distance: 3.4, height: 0.95, target: 0.7, azimuth: 40 };
  const end = blendView(a, b, 1);
  for (const k of Object.keys(b)) {
    assert.ok(Math.abs(end[k] - b[k]) < 1e-9, `${k} had not arrived`);
  }
  assert.deepEqual(blendView(a, b, 0), a, "and it starts where it started");
  const half = blendView(a, b, 0.5);
  assert.equal(half.azimuth, 5, "which is halfway, the short way round");
});

test("the camera never sails round the back of the coach", () => {
  const near = blendView({ distance: 3, height: 1, target: .5, azimuth: 170 },
                         { distance: 3, height: 1, target: .5, azimuth: -170 },
                         0.5);
  assert.equal(Math.abs(near.azimuth), 180,
    "170 to -170 is twenty degrees, not three hundred and forty");
});

test("placing the camera puts it where the azimuth says", () => {
  const camera = new THREE.PerspectiveCamera(30, 1.6, 0.1, 60);
  placeCamera(camera, { distance: 4, height: 1.2, target: 0.9, azimuth: 0 });
  assert.ok(Math.abs(camera.position.x) < 1e-9 && camera.position.z > 3.9,
    "zero azimuth is straight in front");
  placeCamera(camera, { distance: 4, height: 1.2, target: 0.9, azimuth: 90 });
  assert.ok(camera.position.x > 3.9 && Math.abs(camera.position.z) < 1e-9,
    "ninety degrees is square onto the coach's own left");
});
