/* The Yoga Coach as a posed 3D character.
 *
 * The game already describes every pose as a table of **absolute bone
 * directions** -- `aipi5/games/yoga/rig.py`, degrees, image convention where 0
 * is right, 90 is down and -90 is up. That table is the single source of truth
 * for what the player is scored against, so it must stay the single source of
 * truth for what the coach shows. This module puts those same numbers onto a
 * rigged VRM instead of onto a drawing.
 *
 * ── the two conventions, and how they meet ─────────────────────────────────
 *
 * **Screen to world.** A rig angle t maps to the world direction
 * `(cos t, -sin t, 0)`: the sign on y flips because the rig counts y downward
 * and three.js counts it upward. Spine at -90 becomes world up, a thigh at 90
 * becomes world down.
 *
 * **Left and right swap, deliberately.** The rig's invariant is that the limb
 * at the *smaller screen x* is the one the player copies with their own left --
 * see `design/yoga_v2/mirroring.md`. A VRM faces +Z, toward the camera, so the
 * limb appearing at smaller screen x is the character's own **right**. The
 * coach therefore plays rig-left on VRM-right, which is exactly what "she
 * demonstrates mirrored, the way a teacher faces a class" has always meant.
 * Nothing else in the project has to change for this.
 *
 * ── why retargeting is done this way ───────────────────────────────────────
 *
 * The naive version -- set `bone.rotation.z` to the angle -- looks nearly right
 * and is wrong, because a bone's rest pose already points somewhere and its
 * parent has already been rotated. What is computed here instead, per bone and
 * parents first:
 *
 *   1. the bone's **rest world direction**, measured once from the bind pose as
 *      the direction from this joint to its child joint;
 *   2. the **wanted world direction** from the rig angle;
 *   3. the world rotation that carries one to the other;
 *   4. that composed with the bone's rest world rotation, then expressed in the
 *      parent's current space.
 *
 * This is ordinary retargeting and it means a pose looks the same on any rig
 * whose bind pose is sane. There are no per-pose corrections anywhere in this
 * file, and there should never be: a pose that comes out wrong is a bug in the
 * bone table or in the mapping, not something to paper over.
 */
import * as THREE from "./lib/three.module.js";

const DEG = Math.PI / 180;

/** Shared transition contract. Endpoint poses retain the owner's approved
 * deep-Yoga shapes; generated frames may not exceed these ordinary limits or
 * create more excursion than an approved endpoint already contains. */
export const JOINT_SAFETY = Object.freeze({
  shoulder: { practical: 180, backward: 45, speed: 100 },
  elbow: { flexion: 145, hyperextension: 5, speed: 135 },
  wrist: { flexion: 80, extension: 70,
           supportFlexion: 90, supportExtension: 90,
           radialDeviation: 20, ulnarDeviation: 35, speed: 150 },
  hip: { flexion: 130, extension: 30, abduction: 60, speed: 100 },
  knee: { flexion: 150, hyperextension: 5, speed: 135 },
  ankle: { dorsiflexion: 20, plantarFlexion: 50,
           inversion: 30, eversion: 20,
           toeIn: 15, toeOut: 20, speed: 150 },
  spine: { forward: 90, backward: 35, twist: 45, speed: 60 },
  neck: { flexion: 50, extension: 60, twist: 80, speed: 60 },
});

/* Rig bone -> [VRM bone posed, VRM bone whose position gives the direction].
 * Left and right are swapped on purpose; see the header. */
export const RIG_TO_VRM = {
  spine:            ["spine", "neck"],
  neck:             ["neck", "head"],
  left_upper_arm:   ["rightUpperArm", "rightLowerArm"],
  left_forearm:     ["rightLowerArm", "rightHand"],
  right_upper_arm:  ["leftUpperArm", "leftLowerArm"],
  right_forearm:    ["leftLowerArm", "leftHand"],
  left_thigh:       ["rightUpperLeg", "rightLowerLeg"],
  left_shin:        ["rightLowerLeg", "rightFoot"],
  right_thigh:      ["leftUpperLeg", "leftLowerLeg"],
  right_shin:       ["leftLowerLeg", "leftFoot"],
};

/* Parents before children, so a parent's world rotation is already current
 * when its child is solved. */
export const SOLVE_ORDER = [
  "spine", "neck",
  "left_upper_arm", "left_forearm", "right_upper_arm", "right_forearm",
  "left_thigh", "left_shin", "right_thigh", "right_shin",
];

/** Read the VRM humanoid map straight out of the glTF, rather than guessing
 *  bone names. VRM guarantees this table exists and is correct. */
export function humanoidBones(gltf) {
  const vrm = gltf.parser.json.extensions?.VRMC_vrm
            || gltf.parser.json.extensions?.VRM;
  const out = {};
  if (!vrm) return out;
  const human = vrm.humanoid?.humanBones;
  if (!human) return out;
  // VRM 1.0: { leftUpperArm: { node: 12 } }.  VRM 0.x: [ {bone, node} ].
  const pairs = Array.isArray(human)
    ? human.map((h) => [h.bone, h.node])
    : Object.entries(human).map(([k, v]) => [k, v.node]);
  for (const [name, node] of pairs) {
    const obj = gltf.parser.json.nodes[node];
    if (obj?.name) out[name] = obj.name;
  }
  return out;
}

export class Coach {
  constructor(root, boneNames) {
    this.root = root;
    this.bones = {};
    const byName = new Map();
    root.traverse((o) => byName.set(o.name, o));
    for (const [human, nodeName] of Object.entries(boneNames)) {
      const found = byName.get(nodeName);
      if (found) this.bones[human] = found;
    }
    this.rest = new Map();
    this.solved = new Set(Object.values(RIG_TO_VRM).map((pair) => pair[0]));
    this.rootQuat = new THREE.Quaternion();
    this.planeQuat = new THREE.Quaternion();
    this.euler = new THREE.Euler(0, 0, 0, "YXZ");
    this.captureRest();
    this.captureTerminals();
    this.sampleSilhouette();
    this.height = this.bounds().max.y;
  }

  /** A few hundred vertices, kept so the body can be measured while it is
   *  *posed*.
   *
   *  `Box3.setFromObject` looks like the tool for this and is a trap: for a
   *  skinned mesh it returns the geometry's bind-pose box transformed by the
   *  object matrix, so it reports the same numbers for Savasana as for
   *  Mountain. Ground contact computed from it floats the coach above the
   *  grass -- which is exactly what it did.
   *
   *  Skinning every vertex each frame is the correct answer and costs too
   *  much (~20k vertices in JS). Skinning a stride sample costs nothing and is
   *  wrong by at most the spacing between sampled vertices, which on this mesh
   *  is millimetres. The two ends of the stride are nudged so the sample
   *  always includes the first and last vertex of every mesh. */
  sampleSilhouette(budget = 900) {
    const meshes = [];
    this.root.traverse((o) => { if (o.isSkinnedMesh) meshes.push(o); });
    const total = meshes.reduce((n, m) => n + m.geometry.attributes.position.count, 0);
    const stride = Math.max(1, Math.floor(total / budget));
    this.sample = [];
    for (const mesh of meshes) {
      const count = mesh.geometry.attributes.position.count;
      for (let i = 0; i < count; i += stride) this.sample.push([mesh, i]);
      if (count) this.sample.push([mesh, count - 1]);
    }
    this._v = new THREE.Vector3();
    this._box = new THREE.Box3();
  }

  /** The world-space box of the body as it is posed right now. */
  bounds() {
    this.root.updateMatrixWorld(true);
    this._box.makeEmpty();
    for (const [mesh, index] of this.sample) {
      mesh.getVertexPosition(index, this._v);
      this._box.expandByPoint(this._v.applyMatrix4(mesh.matrixWorld));
    }
    return this._box;
  }

  /** The bind pose, remembered: each posed bone's world rotation and the world
   *  direction toward its child. Everything else is derived from these. */
  captureRest() {
    this.root.updateMatrixWorld(true);
    // Every humanoid bone, not only the ten that get solved: `bones3d` turns
    // the hips and the chest, and a rotation that is never undone accumulates
    // a little more every frame until the coach is wound into a knot.
    this.bind = new Map();
    for (const bone of Object.values(this.bones)) {
      this.bind.set(bone, bone.quaternion.clone());
    }
    for (const rigName of SOLVE_ORDER) {
      const [boneName, childName] = RIG_TO_VRM[rigName];
      const bone = this.bones[boneName];
      const child = this.bones[childName];
      if (!bone || !child) continue;
      const here = bone.getWorldPosition(new THREE.Vector3());
      const there = child.getWorldPosition(new THREE.Vector3());
      const dir = there.sub(here);
      if (dir.lengthSq() < 1e-12) continue;
      this.rest.set(rigName, {
        name: boneName,
        bone,
        dir: dir.normalize(),
        quat: bone.getWorldQuaternion(new THREE.Quaternion()),
        local: bone.quaternion.clone(),
      });
    }
  }

  /** Remember the two axes which the ten-chain solve used to omit.
   *
   * A wrist or ankle position does not say which way its palm or sole faces.
   * The shipped VRM provides middle-finger and toe bones, so we measure the
   * terminal's forward axis from the model itself and keep a second, downward
   * axis in terminal-local space.  This stays rig-independent and avoids
   * guessing whether a particular avatar uses X, Y or Z as its hand axis. */
  captureTerminals() {
    this.terminals = {};
    const entries = {
      left_hand: ["rightHand", "rightMiddleProximal", "rightThumbProximal"],
      right_hand: ["leftHand", "leftMiddleProximal", "leftThumbProximal"],
      left_foot: ["rightFoot", "rightToes"],
      right_foot: ["leftFoot", "leftToes"],
    };
    this.root.updateMatrixWorld(true);
    for (const [rigName, [boneName, tipName, thumbName]] of Object.entries(entries)) {
      const bone = this.bones[boneName], tip = this.bones[tipName];
      if (!bone || !tip) continue;
      const q = bone.getWorldQuaternion(new THREE.Quaternion());
      const inv = q.clone().invert();
      const here = bone.getWorldPosition(new THREE.Vector3());
      const forward = tip.getWorldPosition(new THREE.Vector3()).sub(here)
                         .normalize().applyQuaternion(inv);
      const down = new THREE.Vector3(0, -1, 0).applyQuaternion(inv);
      let radial = null;
      const thumb = thumbName ? this.bones[thumbName] : null;
      if (thumb) {
        radial = thumb.getWorldPosition(new THREE.Vector3()).sub(here);
        const forwardWorld = forward.clone().applyQuaternion(q);
        radial.addScaledVector(forwardWorld, -radial.dot(forwardWorld));
        if (radial.lengthSq() > 1e-10) radial.normalize().applyQuaternion(inv);
        else radial = null;
      }
      this.terminals[rigName] = {
        bone, forward, down, radial, neutralLocal: bone.quaternion.clone(),
      };
    }
  }

  /** Read a {pitch, yaw, roll} triple into a YXZ euler.
   *
   *  YXZ rather than the default XYZ so that yaw is applied *after* pitch, in
   *  world terms: a supine pose is "lay her down, then turn her across the
   *  view", and in XYZ order the turn would happen first and lay her out
   *  head-away instead of head-left. */
  orient(euler, spec) {
    euler.order = "YXZ";
    euler.set((spec.pitch || 0) * DEG, (spec.yaw || 0) * DEG,
              (spec.roll || 0) * DEG, "YXZ");
    return euler;
  }

  /** Back to the bind pose. Called before every solve so a pose is absolute
   *  rather than an accumulation of the ones before it. */
  reset() {
    for (const [bone, quat] of this.bind) bone.quaternion.copy(quat);
    this.root.rotation.set(0, 0, 0);
    this.root.position.set(0, 0, 0);
  }

  /**
   * Put the coach into a pose.
   *
   * `pose.bones` are rig angles. `pose.root` optionally tips the whole body --
   * which is how a floor pose is expressed: lying on her back is the standing
   * bone table with the root pitched back ninety degrees, not a separate and
   * disagreeing description of the same body.
   */
  apply(pose, extra = null) {
    this.reset();
    const table = pose.bones || {};
    const root = pose.root || {};
    this.orient(this.root.rotation, root);
    this.root.rotation.order = "YXZ";
    this.rootQuat.setFromEuler(this.root.rotation);
    this.orient(this.euler, pose.plane || {});
    this.planeQuat.setFromEuler(this.euler);
    this.applyExtras(pose.bones3d);
    this.root.updateMatrixWorld(true);

    const want = new THREE.Vector3();
    const planeNormal = new THREE.Vector3(0, 0, 1)
      .applyQuaternion(this.planeQuat);
    const swing = new THREE.Quaternion();
    const parentWorld = new THREE.Quaternion();
    const restQuat = new THREE.Quaternion();
    const target = new THREE.Quaternion();

    for (const rigName of SOLVE_ORDER) {
      const entry = this.rest.get(rigName);
      if (!entry) continue;
      let angle = table[rigName];
      if (angle === undefined) continue;
      if (extra && extra[rigName] !== undefined) angle += extra[rigName];

      // The rig counts y downward; three.js counts it upward.
      //
      // Two frames meet here and they are deliberately different. The wanted
      // direction lives in the **table's plane** -- the flat picture the
      // angles describe. The rest direction lives in the **body's** frame --
      // wherever the coach is currently turned to face. Keeping them apart is
      // what lets one mechanism draw a frontal Warrior II, a side-on Cat, and
      // a Savasana lying on the grass, out of tables that all read the same
      // way. Collapsing them into one frame is a change that looks harmless
      // and quietly makes every sagittal pose face the wrong way.
      want.set(Math.cos(angle * DEG), -Math.sin(angle * DEG), 0)
          .applyQuaternion(this.planeQuat);
      // Some human shapes are not planar.  A Butterfly's knees travel out
      // and forward, for example; keeping every chain in XY puts its joined
      // feet inside the pelvis.  `depth3d` tilts the authored direction out
      // of the table plane while retaining the same measured planar angle.
      const depth = pose.depth3d?.[rigName] || 0;
      if (depth) want.multiplyScalar(Math.cos(depth * DEG))
        .addScaledVector(planeNormal, Math.sin(depth * DEG)).normalize();
      const restDir = entry.dir.clone().applyQuaternion(this.rootQuat);
      swing.setFromUnitVectors(restDir, want);

      // The bind rotation has to be carried into the body's frame too, and
      // for the same reason. Swinging a *direction* into place while composing
      // against the untouched bind *rotation* leaves every solved bone facing
      // the way it faced in the bind pose -- so a coach asked to turn side-on
      // turned her pelvis and nothing else, and the pose came apart at the
      // waist.

      // Wanted world rotation, then expressed in the parent's space.
      restQuat.copy(this.rootQuat).multiply(entry.quat);
      target.copy(swing).multiply(restQuat);
      const parent = entry.bone.parent;
      parent.getWorldQuaternion(parentWorld);
      entry.bone.quaternion.copy(parentWorld.invert().multiply(target));
      // A twist belonging to *this* bone goes on now: after its direction is
      // fixed and before its children are solved. That is the only moment at
      // which it survives -- earlier and the solve overwrites it, later and
      // the children have already been placed against the untwisted parent.
      this.applyExtras(pose.bones3d, entry.name);
      entry.bone.updateMatrixWorld(true);
    }


    if (pose.terminalTransition) {
      this.applyTerminalBlend(pose.terminalTransition.from,
                              pose.terminalTransition.to,
                              pose.terminalTransition.ease);
    } else this.applyTerminals(pose.terminals);
    this.applyTerminalExtras(pose.terminal3d);
    // Semantic terminal modes and reviewer offsets may orient a foot only
    // inside a human ankle's parent-relative range. This must also run on held
    // poses: the first/last frame of a move is an endpoint, and exempting it
    // creates the one-frame backward snap seen in course review. Run it last
    // so no later foot operation can reintroduce the defect.
    this.applyWristLimits(pose);
    this.applyAnkleLimits();

    this.settleOnGround();
  }

  /** Apply reviewer-authored wrist/ankle offsets after semantic alignment.
   * Exported edits still pass anatomical QA before becoming production data.
   */
  applyTerminalExtras(extras) {
    if (!extras) return;
    for (const [rigName, rot] of Object.entries(extras)) {
      const bone = this.terminals[rigName]?.bone;
      if (!bone) continue;
      bone.rotateX((rot[0] || 0) * DEG);
      bone.rotateY((rot[1] || 0) * DEG);
      bone.rotateZ((rot[2] || 0) * DEG);
      bone.updateMatrixWorld(true);
    }
  }

  /** Blend semantic palm/sole modes as rotations rather than switching the
   * whole terminal frame at the midpoint. This removes the single-frame palm
   * flip between, for example, a floor-supporting hand and an overhead hand. */
  applyTerminalBlend(fromSpecs, toSpecs, ease) {
    const names = new Set([...Object.keys(fromSpecs || {}),
                           ...Object.keys(toSpecs || {})]);
    for (const rigName of names) {
      const bone = this.terminals[rigName]?.bone;
      if (!bone) continue;
      const base = bone.quaternion.clone();
      if (fromSpecs?.[rigName]) this.applyTerminals(
        { [rigName]: fromSpecs[rigName] });
      const from = bone.quaternion.clone();
      bone.quaternion.copy(base);
      bone.updateMatrixWorld(true);
      if (toSpecs?.[rigName]) this.applyTerminals(
        { [rigName]: toSpecs[rigName] });
      const to = bone.quaternion.clone();
      bone.quaternion.copy(from).slerp(to, ease);
      bone.updateMatrixWorld(true);
    }
  }

  /** Apply palm/sole semantics after the limb endpoints have been solved.
   *
   * `floor` makes the measured palm or sole normal point down while keeping
   * fingers/toes in the body's forward direction. `top_down` lays the top of
   * a pointed foot on the mat instead of leaving its ankle roll accidental.
   * `palm_forward` makes a Cactus hand face forward without changing its
   * elbow. `point` carries the toes
   * into the shin direction, for relaxed prone and extended-leg poses.  The
   * operation is a full frame-to-frame rotation, so it controls wrist/ankle
   * roll as well as pitch and works on avatars with different local axes. */
  applyTerminals(specs) {
    if (!specs) return;
    const bodyForward = new THREE.Vector3(0, 0, 1)
      .applyQuaternion(this.rootQuat).setY(0);
    if (bodyForward.lengthSq() < 1e-8) bodyForward.set(0, 0, 1);
    bodyForward.normalize();
    for (const [rigName, mode] of Object.entries(specs)) {
      const entry = this.terminals[rigName];
      if (!entry) continue;
      const bone = entry.bone;
      const world = bone.getWorldQuaternion(new THREE.Quaternion());
      const currentForward = entry.forward.clone().applyQuaternion(world);
      const currentDown = entry.down.clone().applyQuaternion(world);
      let targetForward, targetDown;
      if (mode === "floor") {
        targetForward = bodyForward.clone();
        targetDown = new THREE.Vector3(0, -1, 0);
      } else if (mode === "palm_forward") {
        targetForward = new THREE.Vector3(0, 1, 0);
        targetDown = bodyForward.clone();
      } else if (mode === "palm_down" || mode === "palm_up") {
        targetForward = currentForward.clone().setY(0);
        if (targetForward.lengthSq() < 1e-8) targetForward.copy(bodyForward);
        targetForward.normalize();
        targetDown = new THREE.Vector3(0, mode === "palm_down" ? -1 : 1, 0);
      } else if (mode === "palm_to_back") {
        targetForward = new THREE.Vector3(0, -1, 0);
        targetDown = bodyForward.clone();
      } else if (mode === "palm_in") {
        // A grasp around a knee/shin needs the two palms facing one another.
        // This is intentionally side-aware; palm_down can touch the same
        // landmark while still looking like two flat, open hands beside it.
        targetForward = currentForward.clone();
        const inward = rigName.startsWith("left") ? 1 : -1;
        targetDown = new THREE.Vector3(inward, 0, 0)
          .applyQuaternion(this.rootQuat).normalize();
      } else if (mode === "sole_down") {
        const legName = rigName.startsWith("left") ? "rightLowerLeg"
                                                    : "leftLowerLeg";
        const footName = rigName.startsWith("left") ? "rightFoot" : "leftFoot";
        const leg = this.bones[legName], foot = this.bones[footName];
        if (!leg || !foot) continue;
        targetForward = foot.getWorldPosition(new THREE.Vector3()).sub(
          leg.getWorldPosition(new THREE.Vector3())).normalize();
        targetDown = new THREE.Vector3(0, -1, 0);
      } else if (mode === "floor_away") {
        const footName = rigName.startsWith("left") ? "rightFoot" : "leftFoot";
        const foot = this.bones[footName], head = this.bones.head;
        if (!foot || !head) continue;
        targetForward = foot.getWorldPosition(new THREE.Vector3()).sub(
          head.getWorldPosition(new THREE.Vector3())).setY(0).normalize();
        targetDown = new THREE.Vector3(0, -1, 0);
      } else if (mode === "sole_in") {
        targetForward = bodyForward.clone();
        // Butterfly and similar bound-foot shapes need the soles to meet,
        // rather than two feet occupying the same plane with standing ankle
        // roll.  `left`/`right` are player sides, so their inward directions
        // are positive/negative body X respectively.
        const inward = rigName.startsWith("left") ? 1 : -1;
        targetDown = new THREE.Vector3(inward, 0, 0)
          .applyQuaternion(this.rootQuat).normalize();
      } else if (mode === "sole_to_leg") {
        // Tree Pose: sole presses inward, but unlike Butterfly the toes hang
        // down the standing thigh rather than pointing forward.
        targetForward = new THREE.Vector3(0, -1, 0);
        const inward = rigName.startsWith("left") ? 1 : -1;
        targetDown = new THREE.Vector3(inward, 0, 0)
          .applyQuaternion(this.rootQuat).normalize();
      } else if (mode === "top_down" || mode === "point" ||
                 mode === "flex" || mode === "relax") {
        const legName = rigName.startsWith("left") ? "rightLowerLeg"
                                                    : "leftLowerLeg";
        const footName = rigName.startsWith("left") ? "rightFoot" : "leftFoot";
        const leg = this.bones[legName], foot = this.bones[footName];
        if (!leg || !foot) continue;
        const shinForward = foot.getWorldPosition(new THREE.Vector3()).sub(
          leg.getWorldPosition(new THREE.Vector3())).normalize();
        if (mode === "top_down" || mode === "point") targetForward = shinForward;
        else if (mode === "flex") targetForward = new THREE.Vector3(0, 1, 0);
        else targetForward = shinForward.clone().multiplyScalar(0.82)
          .add(new THREE.Vector3(0, 0.18, 0)).normalize();
        targetDown = mode === "top_down" ? new THREE.Vector3(0, 1, 0)
          : currentDown.clone().addScaledVector(
              targetForward, -currentDown.dot(targetForward));
        if (targetDown.lengthSq() < 1e-8) targetDown.copy(bodyForward);
        targetDown.normalize();
      } else continue;
      this.alignTerminalFrame(bone, currentForward, currentDown,
                              targetForward, targetDown);
      bone.updateMatrixWorld(true);
    }
  }

  alignTerminalFrame(bone, fromForward, fromDown, toForward, toDown) {
    const frame = (forward, down) => {
      const x = forward.clone().normalize();
      const y = down.clone().addScaledVector(x, -down.dot(x)).normalize();
      const z = new THREE.Vector3().crossVectors(x, y).normalize();
      return new THREE.Matrix4().makeBasis(x, y, z);
    };
    const fromQ = new THREE.Quaternion().setFromRotationMatrix(
      frame(fromForward, fromDown));
    const toQ = new THREE.Quaternion().setFromRotationMatrix(
      frame(toForward, toDown));
    const delta = toQ.multiply(fromQ.invert());
    const world = bone.getWorldQuaternion(new THREE.Quaternion());
    const targetWorld = delta.multiply(world);
    const parentWorld = bone.parent.getWorldQuaternion(new THREE.Quaternion());
    bone.quaternion.copy(parentWorld.invert().multiply(targetWorld));
  }

  /** Measure wrist bend relative to the solved forearm, not to the room.
   * Flexion is toward the palm, extension away from it. Positive deviation is
   * toward the thumb (radial); negative is toward the little finger (ulnar).
   * Axial palm rotation is deliberately excluded: humans turn a palm through
   * forearm pronation/supination rather than by twisting the wrist. */
  wristAngles(rigName) {
    const entry = this.terminals[rigName];
    if (!entry || !rigName.endsWith("_hand")) return null;
    this.root.updateMatrixWorld(true);
    const parentWorld = entry.bone.parent.getWorldQuaternion(
      new THREE.Quaternion());
    const neutralWorld = parentWorld.clone().multiply(entry.neutralLocal);
    const neutralForward = entry.forward.clone().applyQuaternion(neutralWorld)
      .normalize();
    const neutralDown = entry.down.clone().applyQuaternion(neutralWorld);
    neutralDown.addScaledVector(neutralForward,
      -neutralDown.dot(neutralForward)).normalize();
    const radial = new THREE.Vector3().crossVectors(neutralForward, neutralDown)
      .normalize();
    if (entry.radial) {
      const thumbward = entry.radial.clone().applyQuaternion(neutralWorld);
      if (radial.dot(thumbward) < 0) radial.negate();
    } else if (rigName.startsWith("right")) {
      // Synthetic/minimal rigs may omit thumb bones. Mirroring the fallback
      // keeps positive deviation thumbward on both hands.
      radial.negate();
    }
    const world = entry.bone.getWorldQuaternion(new THREE.Quaternion());
    const forward = entry.forward.clone().applyQuaternion(world).normalize();
    const down = entry.down.clone().applyQuaternion(world);
    const deviation = Math.asin(THREE.MathUtils.clamp(
      forward.dot(radial), -1, 1)) / DEG;
    const flexion = Math.atan2(forward.dot(neutralDown),
                               forward.dot(neutralForward)) / DEG;
    return { flexion, deviation, forward, down,
             neutralForward, neutralDown, radial };
  }

  /** Keep hand direction inside a normal wrist envelope after every semantic
   * palm placement and reviewer offset. Palm-facing roll is preserved because
   * it represents forearm rotation; only impossible hand-to-forearm bending is
   * removed, without moving the hand, elbow or shoulder. */
  wristExtensionLimit(pose, rigName) {
    const limits = JOINT_SAFETY.wrist;
    const forMode = mode => mode === "floor"
      ? limits.supportExtension : limits.extension;
    if (pose?.terminalTransition) {
      const a = forMode(pose.terminalTransition.from?.[rigName]);
      const b = forMode(pose.terminalTransition.to?.[rigName]);
      return THREE.MathUtils.lerp(a, b, pose.terminalTransition.ease);
    }
    return forMode(pose?.terminals?.[rigName]);
  }

  wristFlexionLimit(pose, rigName) {
    const limits = JOINT_SAFETY.wrist;
    const forMode = mode => mode === "floor"
      ? limits.supportFlexion : limits.flexion;
    if (pose?.terminalTransition) {
      const a = forMode(pose.terminalTransition.from?.[rigName]);
      const b = forMode(pose.terminalTransition.to?.[rigName]);
      return THREE.MathUtils.lerp(a, b, pose.terminalTransition.ease);
    }
    return forMode(pose?.terminals?.[rigName]);
  }

  palmSupportWeight(pose, rigName) {
    const forMode = mode => mode === "floor" ? 1 : 0;
    if (pose?.terminalTransition) {
      return THREE.MathUtils.lerp(
        forMode(pose.terminalTransition.from?.[rigName]),
        forMode(pose.terminalTransition.to?.[rigName]),
        pose.terminalTransition.ease);
    }
    return forMode(pose?.terminals?.[rigName]);
  }

  applyPalmContact(pose, rigName) {
    const weight = this.palmSupportWeight(pose, rigName);
    if (weight <= 1e-6) return;
    const entry = this.terminals[rigName];
    const world = entry.bone.getWorldQuaternion(new THREE.Quaternion());
    const forward = entry.forward.clone().applyQuaternion(world).normalize();
    const down = entry.down.clone().applyQuaternion(world).normalize();
    let floorForward = forward.clone().setY(0);
    if (floorForward.lengthSq() < 1e-10) {
      floorForward.set(0, 0, 1).applyQuaternion(this.rootQuat).setY(0);
    }
    floorForward.normalize();
    const targetForward = forward.clone().lerp(floorForward, weight).normalize();
    const floorDown = new THREE.Vector3(0, -1, 0);
    const targetDown = down.clone().lerp(floorDown, weight);
    if (targetDown.lengthSq() < 1e-10) targetDown.copy(floorDown);
    targetDown.normalize();
    this.alignTerminalFrame(entry.bone, forward, down,
                            targetForward, targetDown);
    entry.bone.updateMatrixWorld(true);
  }

  /** The ten-chain rig has no separate forearm-roll channel. Without one, a
   * palm turning toward the mat is falsely charged to radial wrist deviation,
   * producing the sideways hand hinge seen in Tabletop. Rotate the lower arm
   * about its own long axis (pronation/supination), then restore the requested
   * hand frame. Joint positions do not move because the axis passes through
   * the elbow and wrist. */
  routePalmThroughForearm(rigName, measured) {
    const entry = this.terminals[rigName];
    const parent = entry?.bone.parent;
    if (!entry || !parent) return;
    const allowed = measured.deviation >= 0
      ? JOINT_SAFETY.wrist.radialDeviation
      : JOINT_SAFETY.wrist.ulnarDeviation;
    const excursion = Math.abs(measured.deviation);
    if (excursion <= allowed) return;
    const desiredForward = measured.forward.clone();
    const desiredDown = measured.down.clone();
    const axis = entry.bone.getWorldPosition(new THREE.Vector3()).sub(
      parent.getWorldPosition(new THREE.Vector3()));
    if (axis.lengthSq() < 1e-10) return;
    axis.normalize();
    let bendPlane = desiredForward.clone().addScaledVector(
      axis, -desiredForward.dot(axis));
    if (bendPlane.lengthSq() < 1e-10) return;
    bendPlane.normalize();
    const signed = target => Math.atan2(
      axis.dot(new THREE.Vector3().crossVectors(measured.neutralDown, target)),
      THREE.MathUtils.clamp(measured.neutralDown.dot(target), -1, 1));
    const a = signed(bendPlane), b = signed(bendPlane.clone().negate());
    // Route only the excess beyond the wrist's own available deviation. The
    // proportional onset makes the forearm roll continuous as the hand enters
    // or leaves the allowed cone; switching the whole correction on at 20°
    // would merely replace the old palm flip with a smaller one.
    const share = (excursion - allowed) / excursion;
    const twist = THREE.MathUtils.clamp(
      (Math.abs(a) <= Math.abs(b) ? a : b) * share,
      -90 * DEG, 90 * DEG);
    if (Math.abs(twist) < 1e-6) return;

    const parentWorld = parent.getWorldQuaternion(new THREE.Quaternion());
    const targetWorld = new THREE.Quaternion().setFromAxisAngle(axis, twist)
      .multiply(parentWorld);
    const grandWorld = parent.parent.getWorldQuaternion(new THREE.Quaternion());
    parent.quaternion.copy(grandWorld.invert().multiply(targetWorld));
    parent.updateMatrixWorld(true);

    const handWorld = entry.bone.getWorldQuaternion(new THREE.Quaternion());
    const movedForward = entry.forward.clone().applyQuaternion(handWorld);
    const movedDown = entry.down.clone().applyQuaternion(handWorld);
    this.alignTerminalFrame(entry.bone, movedForward, movedDown,
                            desiredForward, desiredDown);
    entry.bone.updateMatrixWorld(true);
  }

  applyWristLimits(pose) {
    const limits = JOINT_SAFETY.wrist;
    for (const rigName of ["left_hand", "right_hand"]) {
      const entry = this.terminals[rigName];
      // Restore the requested weight-bearing contact before deciding how the
      // human arm can reach it. Forearm roll then absorbs the palm turn and the
      // wrist clamp handles only the remaining bend.
      if (entry) this.applyPalmContact(pose, rigName);
      let measured = this.wristAngles(rigName);
      if (!entry || !measured) continue;
      this.routePalmThroughForearm(rigName, measured);
      measured = this.wristAngles(rigName);
      const extension = this.wristExtensionLimit(pose, rigName);
      const maxFlexion = this.wristFlexionLimit(pose, rigName);
      const flexion = THREE.MathUtils.clamp(
        measured.flexion, -extension, maxFlexion);
      const deviation = THREE.MathUtils.clamp(
        measured.deviation, -limits.ulnarDeviation, limits.radialDeviation);
      if (Math.abs(flexion - measured.flexion) >= 1e-5
          || Math.abs(deviation - measured.deviation) >= 1e-5) {
        const flex = flexion * DEG, dev = deviation * DEG;
        const targetForward = measured.neutralForward.clone().multiplyScalar(
          Math.cos(flex) * Math.cos(dev))
          .addScaledVector(measured.neutralDown,
                           Math.sin(flex) * Math.cos(dev))
          .addScaledVector(measured.radial, Math.sin(dev)).normalize();
        // Carry the existing palm normal through the smallest correction. This
        // retains down/up/inward facing while removing only excess bend.
        const targetDown = measured.down.clone().applyQuaternion(
          new THREE.Quaternion().setFromUnitVectors(measured.forward,
                                                    targetForward));
        this.alignTerminalFrame(entry.bone, measured.forward, measured.down,
                                targetForward, targetDown);
        entry.bone.updateMatrixWorld(true);
      }
    }
  }

  /** Parent-relative ankle angles for a solved foot.
   *
   * The bind-relative toe axis is the important part of this measurement. An
   * unsigned foot-to-shin angle gives the same answer on opposite sides of the
   * shin, which allowed a foot to flip 180 degrees yet still pass the old
   * flexion check. Positive flexion is dorsiflexion, negative is plantar
   * flexion, deviation is toe-out/toe-in, and roll is inversion/eversion. */
  ankleAngles(rigName) {
    const entry = this.terminals[rigName];
    if (!entry || !rigName.endsWith("_foot")) return null;
    this.root.updateMatrixWorld(true);
    const parentWorld = entry.bone.parent.getWorldQuaternion(
      new THREE.Quaternion());
    const neutralWorld = parentWorld.clone().multiply(entry.neutralLocal);
    const neutralForward = entry.forward.clone().applyQuaternion(neutralWorld)
      .normalize();
    const neutralDown = entry.down.clone().applyQuaternion(neutralWorld);
    neutralDown.addScaledVector(neutralForward,
      -neutralDown.dot(neutralForward)).normalize();
    const neutralSide = new THREE.Vector3().crossVectors(
      neutralForward, neutralDown).normalize();
    const world = entry.bone.getWorldQuaternion(new THREE.Quaternion());
    const forward = entry.forward.clone().applyQuaternion(world).normalize();
    const down = entry.down.clone().applyQuaternion(world);
    const deviation = Math.asin(THREE.MathUtils.clamp(
      forward.dot(neutralSide), -1, 1)) / DEG;
    const forwardDot = forward.dot(neutralForward);
    const reversed = forwardDot < 0;
    // The minus sign makes toes moving toward the sole/down axis plantar
    // flexion. asin deliberately folds the reversed hemisphere back onto the
    // anatomical one. That makes correction continuous while a raw semantic
    // blend crosses 180 degrees; atan2 would jump from +180 to -180 and send
    // consecutive corrected frames to opposite ankle limits.
    const planarScale = Math.max(1e-8,
      Math.sqrt(Math.max(0, 1 - Math.sin(deviation * DEG) ** 2)));
    const flexion = -Math.asin(THREE.MathUtils.clamp(
      forward.dot(neutralDown) / planarScale, -1, 1)) / DEG;

    // Compare sole roll with the neutral frame carried through the shortest
    // toe-direction swing. The exact 180-degree case has no unique swing axis;
    // it is corrected by flexion/deviation first, so zero is the safe roll.
    const referenceDown = neutralDown.clone();
    if (neutralForward.dot(forward) > -0.99999) {
      referenceDown.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(
        neutralForward, forward));
    }
    referenceDown.addScaledVector(forward,
      -referenceDown.dot(forward));
    const actualDown = down.clone().addScaledVector(
      forward, -down.dot(forward));
    let roll = 0;
    if (referenceDown.lengthSq() > 1e-10 && actualDown.lengthSq() > 1e-10) {
      referenceDown.normalize();
      actualDown.normalize();
      roll = Math.atan2(
        forward.dot(new THREE.Vector3().crossVectors(referenceDown, actualDown)),
        THREE.MathUtils.clamp(referenceDown.dot(actualDown), -1, 1)) / DEG;
    }
    return { flexion, deviation, roll, reversed, forwardDot, forward, down,
             neutralForward, neutralDown, neutralSide };
  }

  /** Clamp every generated transition frame to the ankle ranges supplied by
   * the anatomy review: dorsiflexion <= 20 degrees, plantar flexion <= 50,
   * inversion <= 30 and eversion <= 20. The ankle position and the leg are
   * never moved; only the terminal foot frame is corrected. */
  applyAnkleLimits() {
    const limits = JOINT_SAFETY.ankle;
    for (const rigName of ["left_foot", "right_foot"]) {
      const entry = this.terminals[rigName];
      const measured = this.ankleAngles(rigName);
      if (!entry || !measured) continue;
      const wantedFlex = THREE.MathUtils.clamp(
        measured.flexion, -limits.plantarFlexion, limits.dorsiflexion);
      // Once the requested toe axis leaves the anatomical cone, progressively
      // discard its lateral twist and sole roll as well. At 90 degrees these
      // rotations have no stable anatomical meaning; retaining them caused a
      // one-frame 40-degree snap when a raw blend crossed the rear hemisphere.
      const directionTrust = THREE.MathUtils.clamp(
        measured.forwardDot / Math.cos(limits.plantarFlexion * DEG), 0, 1) ** 3;
      const wantedDeviation = THREE.MathUtils.clamp(
        measured.deviation, -limits.toeIn, limits.toeOut) * directionTrust;
      const flex = wantedFlex * DEG, deviation = wantedDeviation * DEG;
      // Always rebuild from the bind-relative forward hemisphere. Never use
      // the current foot as the tangent: that was the source of the 180-degree
      // reviewer flips in kneeling and lunge transitions.
      const targetForward = measured.neutralForward.clone().multiplyScalar(
        Math.cos(flex) * Math.cos(deviation))
        .addScaledVector(measured.neutralDown,
                         -Math.sin(flex) * Math.cos(deviation))
        .addScaledVector(measured.neutralSide, Math.sin(deviation)).normalize();
      const targetReferenceDown = measured.neutralDown.clone().applyQuaternion(
        new THREE.Quaternion().setFromUnitVectors(measured.neutralForward,
                                                  targetForward));
      const wantedRoll = THREE.MathUtils.clamp(
        measured.roll, -limits.eversion, limits.inversion) * directionTrust;
      const targetDown = targetReferenceDown.applyQuaternion(
        new THREE.Quaternion().setFromAxisAngle(targetForward,
                                                wantedRoll * DEG));

      if (Math.abs(wantedFlex - measured.flexion) > 1e-5
          || Math.abs(wantedDeviation - measured.deviation) > 1e-5
          || measured.reversed
          || Math.abs(wantedRoll - measured.roll) > 1e-5) {
        this.alignTerminalFrame(entry.bone, measured.forward, measured.down,
                                targetForward, targetDown);
        entry.bone.updateMatrixWorld(true);
      }
    }
  }


  /**
   * The escape hatch for what a flat picture cannot say: a twist, or a pelvis
   * tipped out of the plane.
   *
   * **When each entry runs is the whole trick**, and the two kinds run at
   * different moments.
   *
   * A bone the solve never touches -- the hips, the chest -- is turned
   * *before* the solve, and so it only moves **joints**: where the hip sockets
   * and the base of the spine end up. Every solved bone then takes its
   * absolute direction from the table regardless, so the pelvis tips for
   * Tabletop and the legs stay on the floor. Turned afterwards it would carry
   * the legs and the spine with it and pull them off the grass, which is
   * exactly what it did.
   *
   * A bone the solve *does* touch -- the spine, the neck -- is turned from
   * inside the solve loop, in the one instant between its own direction being
   * fixed and its children being placed. Before that instant the solve
   * overwrites it; after it, the children have already been placed against an
   * untwisted parent. This is what makes a seated twist a twist rather than a
   * shrug: the shoulders and the head come round, and the arms still point
   * where the table put them.
   *
   * On a solved bone, use the Y entry: in a VRM that is the long axis of the
   * bone, so it turns the body without moving the bone off the direction the
   * solve just gave it. An X or Z entry there fights the pose it is part of,
   * so side-bends belong on the chest.
   */
  applyExtras(extras, only = null) {
    if (!extras) return;
    for (const [human, rot] of Object.entries(extras)) {
      if (only === null ? this.solved.has(human) : human !== only) continue;
      const bone = this.bones[human];
      if (!bone) continue;
      bone.rotateX((rot[0] || 0) * DEG);
      bone.rotateY((rot[1] || 0) * DEG);
      bone.rotateZ((rot[2] || 0) * DEG);
    }
  }

  /** Put whatever is lowest onto the ground.
   *
   *  This is the whole of "floor contact" and it is also the whole of "she must
   *  not change size between poses" -- both were real defects in the 2D route,
   *  where every drawing had to have its scale and its ground line
   *  reconstructed. Here the body is one object with one size, and the only
   *  question is where the bottom of it is. */
  settleOnGround() {
    const box = this.bounds();
    if (!isFinite(box.min.y)) return;
    this.root.position.y -= box.min.y;
    this.root.position.x -= (box.min.x + box.max.x) / 2;
    this.root.position.z -= (box.min.z + box.max.z) / 2;
    this.root.updateMatrixWorld(true);
  }
}

/**
 * Where the camera stands for a pose, as plain numbers.
 *
 * `azimuth` is the one that earns its keep. Until it existed the camera could
 * only ever be in front, and the body had to turn instead -- which is fine for
 * a silhouette (Downward Dog, Plank) and hopeless for anything whose content
 * is a rotation. Thread the Needle seen from the side is Tabletop; seen from
 * three-quarters it is obviously an arm sliding under a chest.
 *
 * Zero is straight in front, and positive walks the camera round toward the
 * coach's own left.
 */
export function resolveView(pose, views) {
  const preset = views[(pose && pose.view) || "standing"] || views.standing;
  return { azimuth: 0, ...preset, ...((pose && pose.camera) || {}) };
}

/** Point a camera at the coach from a resolved view. */
export function placeCamera(camera, view) {
  const a = (view.azimuth || 0) * DEG;
  camera.position.set(Math.sin(a) * view.distance, view.height,
                      Math.cos(a) * view.distance);
  camera.lookAt(0, view.target, 0);
}

/** Interpolate two rig tables the short way round, so an arm travelling from
 *  -170 to 170 moves twenty degrees rather than sweeping through the body. */
export function blendTables(from, to, t) {
  const out = {};
  const names = new Set([...Object.keys(from || {}), ...Object.keys(to || {})]);
  for (const name of names) {
    const a = from?.[name], b = to?.[name];
    if (a === undefined) { out[name] = b; continue; }
    if (b === undefined) { out[name] = a; continue; }
    let d = (b - a) % 360;
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    out[name] = a + d * t;
  }
  return out;
}

/** Interpolate reviewer-authored rotations as orientations, not as three
 * unrelated Euler numbers. Quaternion SLERP preserves rotation continuity at
 * +/-180 and cannot manufacture the wrist/head flips caused by independent
 * XYZ interpolation. The returned triples remain compatible with applyExtras.
 */
export function blendVectorTables(from, to, t) {
  const out = {};
  const names = new Set([...Object.keys(from || {}), ...Object.keys(to || {})]);
  for (const name of names) {
    const a = from?.[name] || [0, 0, 0];
    const b = to?.[name] || [0, 0, 0];
    const qa = new THREE.Quaternion().setFromEuler(new THREE.Euler(
      (a[0] || 0) * DEG, (a[1] || 0) * DEG, (a[2] || 0) * DEG, "XYZ"));
    const qb = new THREE.Quaternion().setFromEuler(new THREE.Euler(
      (b[0] || 0) * DEG, (b[1] || 0) * DEG, (b[2] || 0) * DEG, "XYZ"));
    const e = new THREE.Euler().setFromQuaternion(qa.slerp(qb, t), "XYZ");
    out[name] = [e.x / DEG, e.y / DEG, e.z / DEG];
  }
  return out;
}

const wrapDegrees = (angle) => ((angle + 180) % 360 + 360) % 360 - 180;
const smooth = (t) => t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t);

function blendAngle(a, b, t) {
  let delta = (b - a) % 360;
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;
  return a + delta * t;
}

function blendOptionalAngleTables(from, to, t) {
  const out = {};
  const names = new Set([...Object.keys(from || {}), ...Object.keys(to || {})]);
  for (const name of names) out[name] = blendAngle(
    from?.[name] || 0, to?.[name] || 0, t);
  return out;
}

/** A knee is a hinge, not a second free-spinning compass needle.
 *
 * Interpolating thigh and shin as two unrelated absolute angles can send the
 * shin round the opposite side of the circle.  The endpoint poses may both be
 * sound while the generated midpoint briefly bends the knee backwards.  Move
 * the thigh first, then interpolate the shin's flexion *relative to it*.
 * Flexion is a bounded interval, so it deliberately does not wrap at 180.
 */
function preserveKneeHinges(from, to, t, bones) {
  for (const side of ["left", "right"]) {
    const thigh = `${side}_thigh`, shin = `${side}_shin`;
    const aThigh = from.bones?.[thigh], aShin = from.bones?.[shin];
    const bThigh = to.bones?.[thigh], bShin = to.bones?.[shin];
    if ([aThigh, aShin, bThigh, bShin].some((v) => v === undefined)) continue;
    const aFlex = wrapDegrees(aShin - aThigh);
    const bFlex = wrapDegrees(bShin - bThigh);
    let flex = aFlex + (bFlex - aFlex) * t;
    // Ordinary straight knees can carry a few degrees of authored softness,
    // but a transition must never invent more hyperextension than either
    // reviewed endpoint contains.
    if (aFlex >= -JOINT_SAFETY.knee.hyperextension
        && bFlex >= -JOINT_SAFETY.knee.hyperextension) {
      flex = Math.max(Math.min(0, aFlex, bFlex), flex);
    }
    bones[shin] = bones[thigh] + flex;
  }
}

/** Elbows get the same parent-relative treatment as knees. Their apparent
 * bend sign changes when an arm crosses overhead, so this does not impose one
 * global sign; it prevents the transition from leaving the reviewed endpoint
 * interval or taking a circular shortcut through a backwards elbow. */
function preserveElbowHinges(from, to, t, bones) {
  for (const side of ["left", "right"]) {
    const upper = `${side}_upper_arm`, forearm = `${side}_forearm`;
    const aUpper = from.bones?.[upper], aForearm = from.bones?.[forearm];
    const bUpper = to.bones?.[upper], bForearm = to.bones?.[forearm];
    if ([aUpper, aForearm, bUpper, bForearm].some((v) => v === undefined)) continue;
    const aFlex = wrapDegrees(aForearm - aUpper);
    const bFlex = wrapDegrees(bForearm - bUpper);
    bones[forearm] = bones[upper] + aFlex + (bFlex - aFlex) * t;
  }
}

function preserveNeckFromSpine(from, to, t, bones) {
  const aSpine = from.bones?.spine, aNeck = from.bones?.neck;
  const bSpine = to.bones?.spine, bNeck = to.bones?.neck;
  if ([aSpine, aNeck, bSpine, bNeck].some((v) => v === undefined)) return;
  const a = wrapDegrees(aNeck - aSpine), b = wrapDegrees(bNeck - bSpine);
  bones.neck = bones.spine + a + (b - a) * t;
}

function preserveBodyRelativeTorso(from, to, t, root, plane, bones, depth3d) {
  for (const name of ["spine", "neck"]) {
    if (from.bones?.[name] === undefined || to.bones?.[name] === undefined) continue;
    const direction = slerpDirection(rigDirectionInBody(from, name),
                                     rigDirectionInBody(to, name), t);
    writeRigDirection(direction, root, plane, name, bones, depth3d);
  }
}

function orientation(spec) {
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(
    (spec?.pitch || 0) * DEG, (spec?.yaw || 0) * DEG,
    (spec?.roll || 0) * DEG, "YXZ"));
}

function rigDirectionInBody(pose, name) {
  const angle = (pose.bones?.[name] || 0) * DEG;
  const depth = (pose.depth3d?.[name] || 0) * DEG;
  const direction = new THREE.Vector3(
    Math.cos(angle) * Math.cos(depth),
    -Math.sin(angle) * Math.cos(depth), Math.sin(depth));
  direction.applyQuaternion(orientation(pose.plane));
  return direction.applyQuaternion(orientation(pose.root).invert()).normalize();
}

function slerpDirection(from, to, t) {
  const dot = THREE.MathUtils.clamp(from.dot(to), -1, 1);
  if (dot > 0.9995) return from.clone().lerp(to, t).normalize();
  const angle = Math.acos(dot), sine = Math.sin(angle);
  if (sine < 1e-6) return from.clone();
  return from.clone().multiplyScalar(Math.sin((1 - t) * angle) / sine)
    .addScaledVector(to, Math.sin(t * angle) / sine).normalize();
}

function writeRigDirection(direction, root, plane, name, bones, depth3d) {
  direction.applyQuaternion(orientation(root))
    .applyQuaternion(orientation(plane).invert()).normalize();
  bones[name] = Math.atan2(-direction.y, direction.x) / DEG;
  depth3d[name] = Math.asin(THREE.MathUtils.clamp(direction.z, -1, 1)) / DEG;
}

function legNeedsRelease(from, to, side) {
  const thigh = `${side}_thigh`, shin = `${side}_shin`;
  const a = rigDirectionInBody(from, thigh), b = rigDirectionInBody(to, thigh);
  const turn = Math.acos(THREE.MathUtils.clamp(a.dot(b), -1, 1)) / DEG;
  return turn >= 45;
}

function hipArc(from, to, side, t) {
  const outward = new THREE.Vector3(side === "left" ? -0.55 : 0.55, 0, 0);
  const middle = from.clone().add(to);
  if (middle.lengthSq() < 1e-5) middle.set(0, -0.65, 0.35);
  middle.normalize().add(outward).normalize();
  return t < 0.5
    ? slerpDirection(from, middle, smooth(t * 2))
    : slerpDirection(middle, to, smooth((t - 0.5) * 2));
}

/** Large leg changes are three human actions, not four free angles morphing
 * simultaneously: release the knee, reposition the femur from the hip, then
 * apply the destination bend. During the hip phase thigh and shin share one
 * direction, so the knee cannot twist sideways to drag the foot to its target.
 */
function preserveLegReposition(from, to, t, root, plane, bones, depth3d) {
  for (const side of ["left", "right"]) {
    if (!legNeedsRelease(from, to, side)) continue;
    const thigh = `${side}_thigh`, shin = `${side}_shin`;
    const aFlex = wrapDegrees(from.bones[shin] - from.bones[thigh]);
    const bFlex = wrapDegrees(to.bones[shin] - to.bones[thigh]);
    const aThigh = rigDirectionInBody(from, thigh);
    const bThigh = rigDirectionInBody(to, thigh);
    let hipProgress, flex, shinDepthMode, depthProgress;
    if (t < 0.25) {
      const release = smooth(t / 0.25);
      hipProgress = 0;
      flex = aFlex * (1 - release);
      shinDepthMode = "release";
      depthProgress = release;
    } else if (t <= 0.75) {
      hipProgress = smooth((t - 0.25) / 0.5);
      flex = 0;
      shinDepthMode = "straight";
    } else {
      const bend = smooth((t - 0.75) / 0.25);
      hipProgress = 1;
      flex = bFlex * bend;
      shinDepthMode = "bend";
      depthProgress = bend;
    }
    const direction = hipArc(aThigh, bThigh, side, hipProgress);
    writeRigDirection(direction, root, plane, thigh, bones, depth3d);
    bones[shin] = bones[thigh] + flex;
    if (shinDepthMode === "release") {
      depth3d[shin] = blendAngle(from.depth3d?.[shin] || 0,
                                 depth3d[thigh], depthProgress);
    } else if (shinDepthMode === "bend") {
      depth3d[shin] = blendAngle(depth3d[thigh], to.depth3d?.[shin] || 0,
                                 depthProgress);
    } else depth3d[shin] = depth3d[thigh];
  }
}

function armNeedsSafeRoute(from, to, side) {
  const a = rigDirectionInBody(from, `${side}_upper_arm`);
  const b = rigDirectionInBody(to, `${side}_upper_arm`);
  return Math.acos(THREE.MathUtils.clamp(a.dot(b), -1, 1)) / DEG >= 20;
}

/** Route large shoulder sweeps through the coach's own front hemisphere.
 * The calculation is body-relative, so it remains correct for side-facing and
 * floor poses instead of assuming that canvas +Z is always the chest front.
 */
function preserveFrontArmSweeps(from, to, t, root, plane, bones, depth3d) {
  if (t <= 0 || t >= 1) return;
  for (const side of ["left", "right"]) {
    const safeRoute = armNeedsSafeRoute(from, to, side);
    const outward = side === "left" ? -0.38 : 0.38;
    const fromUpper = rigDirectionInBody(from, `${side}_upper_arm`);
    const toUpper = rigDirectionInBody(to, `${side}_upper_arm`);
    // Behind-the-back endpoint poses are valid only when explicitly authored.
    // Reach them around the outside of the ribs; every other large move uses
    // the guideline's front -> outward/up -> destination path.
    const routeZ = Math.min(fromUpper.z, toUpper.z) < -0.15 ? 0.25 : 0.925;
    const frontArc = new THREE.Vector3(outward, 0, routeZ).normalize();
    for (const part of ["upper_arm", "forearm"]) {
      const name = `${side}_${part}`;
      if (from.bones?.[name] === undefined || to.bones?.[name] === undefined) continue;
      const a = rigDirectionInBody(from, name), b = rigDirectionInBody(to, name);
      const direction = safeRoute
        ? t < 0.5
          ? slerpDirection(a, frontArc, smooth(t * 2))
          : slerpDirection(frontArc, b, smooth((t - 0.5) * 2))
        : slerpDirection(a, b, t);
      // Convert the body-relative result back into the current authored plane,
      // which is the representation consumed by Coach.apply().
      writeRigDirection(direction, root, plane, name, bones, depth3d);
    }
  }
}

function blendOrientation(from, to, t) {
  const q = orientation(from).slerp(orientation(to), t);
  const e = new THREE.Euler().setFromQuaternion(q, "YXZ");
  return { pitch: e.x / DEG, yaw: e.y / DEG, roll: e.z / DEG };
}

/** Blend two resolved views. Azimuth goes the short way round for the same
 *  reason a shoulder does: 170 to -170 is twenty degrees, not three hundred
 *  and forty, and taking the long way sends the camera sailing round the back
 *  of the coach in the middle of a lesson. */
export function blendView(from, to, ease) {
  let turn = ((to.azimuth || 0) - (from.azimuth || 0)) % 360;
  if (turn > 180) turn -= 360;
  if (turn < -180) turn += 360;
  const mix = (k) => from[k] + (to[k] - from[k]) * ease;
  return { distance: mix("distance"), height: mix("height"),
           target: mix("target"), azimuth: (from.azimuth || 0) + turn * ease };
}

export function blendPose(from, to, t) {
  const ease = smooth(t);
  const root = blendOrientation(from.root, to.root, ease);
  const plane = blendOrientation(from.plane, to.plane, ease);
  const bones = blendTables(from.bones, to.bones, ease);
  const depth3d = blendOptionalAngleTables(from.depth3d, to.depth3d, ease);
  preserveBodyRelativeTorso(from, to, ease, root, plane, bones, depth3d);
  preserveKneeHinges(from, to, ease, bones);
  preserveElbowHinges(from, to, ease, bones);
  // For poses authored in one plane, the parent-relative neck rule is more
  // precise. Across changing body frames, the body-relative SLERP above is
  // the safe representation and must not be projected back into 2D.
  if (orientation(from.root).angleTo(orientation(to.root)) < 1e-5
      && orientation(from.plane).angleTo(orientation(to.plane)) < 1e-5) {
    preserveNeckFromSpine(from, to, ease, bones);
  }
  preserveLegReposition(from, to, ease, root, plane, bones, depth3d);
  preserveFrontArmSweeps(from, to, ease, root, plane, bones, depth3d);
  return {
    bones,
    depth3d,
    transitionSafety: true,
    terminalTransition: {
      from: from.terminals || {}, to: to.terminals || {}, ease,
    },
    terminal3d: blendVectorTables(from.terminal3d, to.terminal3d, ease),
    bones3d: blendVectorTables(from.bones3d, to.bones3d, ease),
    terminals: ease < 0.5 ? from.terminals : to.terminals,
    root,
    plane,
    view: ease < 0.5 ? (from.view || "standing") : (to.view || "standing"),
    camera: ease < 0.5 ? from.camera : to.camera,
  };
}

function degreesBetweenDirections(from, to, name) {
  return Math.acos(THREE.MathUtils.clamp(
    rigDirectionInBody(from, name).dot(rigDirectionInBody(to, name)), -1, 1)) / DEG;
}

/** Nominal human movement time for one station-to-station action.
 * It is used to distribute the course's existing transition clock: a 100° hip
 * change receives more of that clock than a five-degree wrist correction.
 */
export function transitionEffort(from, to) {
  let seconds = 0.35;
  const rate = (name) => name === "spine" ? JOINT_SAFETY.spine.speed
    : name === "neck" ? JOINT_SAFETY.neck.speed
    : name.includes("upper_arm") ? JOINT_SAFETY.shoulder.speed
    : name.includes("thigh") ? JOINT_SAFETY.hip.speed
    : name.includes("forearm") ? JOINT_SAFETY.elbow.speed
    : JOINT_SAFETY.knee.speed;
  for (const name of SOLVE_ORDER) {
    if (from.bones?.[name] === undefined || to.bones?.[name] === undefined) continue;
    seconds = Math.max(seconds, degreesBetweenDirections(from, to, name) / rate(name));
  }
  seconds = Math.max(seconds,
    orientation(from.root).angleTo(orientation(to.root)) / DEG / 60,
    orientation(from.plane).angleTo(orientation(to.plane)) / DEG / 60);
  for (const tableName of ["bones3d", "terminal3d"]) {
    const names = new Set([...Object.keys(from[tableName] || {}),
                           ...Object.keys(to[tableName] || {})]);
    for (const name of names) {
      const a = from[tableName]?.[name] || [0, 0, 0];
      const b = to[tableName]?.[name] || [0, 0, 0];
      const qa = new THREE.Quaternion().setFromEuler(new THREE.Euler(
        (a[0] || 0) * DEG, (a[1] || 0) * DEG, (a[2] || 0) * DEG, "XYZ"));
      const qb = new THREE.Quaternion().setFromEuler(new THREE.Euler(
        (b[0] || 0) * DEG, (b[1] || 0) * DEG, (b[2] || 0) * DEG, "XYZ"));
      seconds = Math.max(seconds, qa.angleTo(qb) / DEG / 150);
    }
  }
  return seconds;
}

/** Move along a sequence of reviewed anatomical stations.  `t` still covers
 * the complete transition, so callers keep their existing course clocks.
 * Time is divided by anatomical effort rather than equally: a large hip turn
 * is not rushed because a tiny wrist adjustment happened to be another path
 * segment. */
export function blendTransition(from, to, waypoints, t) {
  if (t <= 0) return from;
  if (t >= 1) return to;
  const poses = [from, ...(waypoints || []), to];
  const weights = poses.slice(0, -1).map((pose, index) =>
    transitionEffort(pose, poses[index + 1]));
  const total = weights.reduce((sum, value) => sum + value, 0);
  let elapsed = t * total, segment = 0;
  while (segment < weights.length - 1 && elapsed > weights[segment]) {
    elapsed -= weights[segment++];
  }
  return blendPose(poses[segment], poses[segment + 1], elapsed / weights[segment]);
}

/**
 * Breathing, as a small additive offset on the rig angles rather than as a
 * separate animation.
 *
 * §15 asks for motion that is visible but never changes the demonstrated pose,
 * and the numbers here are chosen against that: a degree and a half on the
 * spine is a chest that rises, and it is an order of magnitude below the
 * tightest scoring tolerance in `scoring.py` (torso, 13 degrees), so a breathing
 * coach cannot drift into demonstrating a different shape. In the 2D route this
 * needed 59 generated clips; here it is six lines and costs nothing.
 */
export function breathing(seconds, depth = 1) {
  const cycle = Math.sin(seconds * 2 * Math.PI / 5.5);   // ~5.5 s per breath
  const sway = Math.sin(seconds * 2 * Math.PI / 11);      // slower, so it never
  return {                                               // looks metronomic
    spine: cycle * 1.5 * depth,
    neck: cycle * 1.1 * depth + sway * 0.4 * depth,
    left_upper_arm: cycle * 0.9 * depth,
    right_upper_arm: -cycle * 0.9 * depth,
    left_forearm: cycle * 0.5 * depth,
    right_forearm: -cycle * 0.5 * depth,
  };
}
