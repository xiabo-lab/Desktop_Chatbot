/* AIPI5 Yoga Coach renderer.
 *
 * The coach is not a picture. She is the same fourteen bone directions the Pi
 * scores the player against, run through the same forward kinematics and drawn
 * — so the shape being demonstrated and the shape being marked are one number
 * per joint, and cannot drift apart. It is also what makes the transitions
 * free: interpolating two angles gives a limb that swings, where interpolating
 * two drawings gives a cross-fade and interpolating two sets of joint
 * positions gives a limb that stretches.
 *
 * Rules, timing and scoring all stay on the Pi beside the pose stream, exactly
 * as they do for Boxing. This file owns no camera, no pose model and no clock
 * that matters.
 *
 * The drawing primitives below are yoga's own rather than Boxing's, and
 * deliberately: a different body, a different outfit and a different silhouette
 * are art, not infrastructure. What is shared is everything that would be a bug
 * to duplicate — the camera lease, the pose service, the crossed-arms gesture,
 * the game manager, the state stream and the start/over sheets.
 */
(function () {
  "use strict";

  const W = 1280, H = 800;

  /* Must match `RIG` in aipi5/games/yoga/rig.py. A test asserts it does: if
   * the two drift, the coach demonstrates one pose and the player is marked
   * against another, and nothing on screen would say so. */
  const RIG = {
    spine: 1.00, neck: 0.40, upper_arm: 0.64, forearm: 0.56,
    thigh: 0.88, shin: 0.86, shoulder_half: 0.42, hip_half: 0.30,
  };

  /* Where the coach stands. `UNIT` is pixels per spine length and `FLOOR_Y` is
   * where her lowest foot lands; `COACH_HIP_Y` is only the origin the rig is
   * laid out around, since `drawCoach` re-anchors her to the floor.
   *
   * The three numbers are a fit rather than a taste. She has to clear the pose
   * card above her (Mountain Pose puts her head highest at rest), keep
   * Triangle's raised hand below the top bar, and keep Extended Hand to Toe's
   * lifted foot clear of the live camera panel at x=656 — which is the widest
   * anything in the pose library reaches. */
  const COACH_X = 376, COACH_HIP_Y = 424, UNIT = 110, FLOOR_Y = 712;

  /* Limb thickness in pixels, scaled with the figure. Written against a
   * 150-pixel spine because that is the size these numbers were drawn at; the
   * coach shrank afterwards to fit around the pose card, and a body whose bones
   * shrink while its limbs do not is a body that gets steadily stouter every
   * time the layout moves. */
  const K = UNIT / 150;

  const INK = "rgba(22,14,34,.62)";
  const SKIN = "#f0c6a0", SKIN_DARK = "#cf9670", SKIN_LIGHT = "#ffe4ca";
  const TOP = "#33c6ae", TOP_DARK = "#1c8e7c", TOP_LIGHT = "#68e6d1";
  const LEG = "#5f4d92", LEG_DARK = "#3a2e60", LEG_LIGHT = "#8a75c6";
  const HAIR = "#2a1f36", HAIR_LIGHT = "#4d3b63";
  const MAT = "#c9603d", MAT_DARK = "#8f3f25";
  const GOOD = "#5fe3b4", WARN = "#ffcc5c", BAD = "#ff7a6b";

  let active = false;
  let latest = null;
  let audio = null;
  let lastPoseId = "";
  let lastPhase = "";
  let lastCountdown = "";
  let breathPhase = 0;
  let smoothedAccuracy = 0;
  let smoothedHold = 0;
  let ripples = [];

  const el = (id) => gameEl(id);

  /* ── geometry ───────────────────────────────────────────────────── */

  function wrap(degrees) {
    let value = (degrees + 180) % 360;
    if (value < 0) value += 360;
    return value - 180;
  }

  /* The short way round. Interpolating -170 to 170 the long way makes an arm
   * sweep a full circle through the coach's body to travel twenty degrees. */
  function lerpAngle(from, to, t) {
    return from + wrap(to - from) * t;
  }

  function step(origin, degrees, length) {
    const radians = degrees * Math.PI / 180;
    return [origin[0] + length * Math.cos(radians),
            origin[1] + length * Math.sin(radians)];
  }

  /* The same forward kinematics as `rig.forward_kinematics`, including the
   * shoulder and hip lines defaulting to square across the spine. */
  function kinematics(bones, scales) {
    const len = (name) => RIG[name] * (scales && scales[name] !== undefined
                                       ? scales[name] : 1);
    const spine = bones.spine;
    const shoulderLine = bones.shoulder_line !== undefined
      ? bones.shoulder_line : spine - 90;
    const hipLine = bones.hip_line !== undefined ? bones.hip_line : spine - 90;

    const hipMid = [0, 0];
    const shoulderMid = step(hipMid, spine, len("spine"));
    const j = {
      hip_mid: hipMid,
      shoulder_mid: shoulderMid,
      head: step(shoulderMid, bones.neck, len("neck")),
      left_shoulder: step(shoulderMid, shoulderLine, len("shoulder_half")),
      right_shoulder: step(shoulderMid, shoulderLine + 180, len("shoulder_half")),
      left_hip: step(hipMid, hipLine, len("hip_half")),
      right_hip: step(hipMid, hipLine + 180, len("hip_half")),
    };
    for (const side of ["left", "right"]) {
      j[side + "_elbow"] = step(j[side + "_shoulder"], bones[side + "_upper_arm"],
                                len("upper_arm"));
      j[side + "_wrist"] = step(j[side + "_elbow"], bones[side + "_forearm"],
                                len("forearm"));
      j[side + "_knee"] = step(j[side + "_hip"], bones[side + "_thigh"],
                               len("thigh"));
      j[side + "_ankle"] = step(j[side + "_knee"], bones[side + "_shin"],
                                len("shin"));
    }
    return j;
  }

  const BONE_KEYS = ["spine", "neck", "shoulder_line", "hip_line",
    "left_upper_arm", "left_forearm", "right_upper_arm", "right_forearm",
    "left_thigh", "left_shin", "right_thigh", "right_shin"];
  const SCALE_KEYS = ["spine", "neck", "upper_arm", "forearm", "thigh", "shin",
    "shoulder_half", "hip_half"];

  function blendBones(from, to, t) {
    const out = {};
    for (const key of BONE_KEYS) {
      const a = from[key], b = to[key];
      if (a === undefined && b === undefined) continue;
      // A pose that names `shoulder_line` and one that does not are still
      // interpolable: the unnamed one means "square across the spine", which
      // is a real angle rather than a missing one.
      const fa = a === undefined ? from.spine - 90 : a;
      const fb = b === undefined ? to.spine - 90 : b;
      out[key] = lerpAngle(fa, fb, t);
    }
    return out;
  }

  function blendScales(from, to, t) {
    const out = {};
    for (const key of SCALE_KEYS) {
      const a = (from && from[key] !== undefined) ? from[key] : 1;
      const b = (to && to[key] !== undefined) ? to[key] : 1;
      if (a === 1 && b === 1) continue;
      out[key] = a + (b - a) * t;
    }
    return out;
  }

  /* Slow at both ends. A coach that snapped into the pose and stopped dead
   * would be demonstrating the shape without demonstrating the movement, and
   * the movement is half of what somebody following along is copying. */
  function ease(t) {
    const x = Math.max(0, Math.min(1, t));
    return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
  }

  function toScreen(point) {
    return [COACH_X + point[0] * UNIT, COACH_HIP_Y + point[1] * UNIT];
  }

  /* ── drawing the body ───────────────────────────────────────────── */

  /* A tapered limb with a lit edge: two arcs joined by their tangents, which
   * gives a rounded joint at each end with no visible seam where a plain
   * stroked line would show one. */
  function limb(ctx, a, b, wideA, wideB, base, shade, light) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const length = Math.hypot(dx, dy) || 0.0001;
    const nx = -dy / length, ny = dx / length;
    const angle = Math.atan2(dy, dx);

    ctx.beginPath();
    ctx.arc(a[0], a[1], wideA, angle + Math.PI / 2, angle - Math.PI / 2);
    ctx.arc(b[0], b[1], wideB, angle - Math.PI / 2, angle + Math.PI / 2);
    ctx.closePath();

    // Across the limb rather than along it, so the light falls on one side of
    // a leg the way it would on a cylinder.
    const gradient = ctx.createLinearGradient(
      a[0] + nx * wideA * 1.1, a[1] + ny * wideA * 1.1,
      a[0] - nx * wideA * 1.1, a[1] - ny * wideA * 1.1);
    gradient.addColorStop(0, light);
    gradient.addColorStop(0.44, base);
    gradient.addColorStop(1, shade);
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2.2;
    ctx.stroke();
  }

  function tip(ctx, from, at, long, wide) {
    const angle = Math.atan2(at[1] - from[1], at[0] - from[0]);
    ctx.save();
    ctx.translate(at[0], at[1]);
    ctx.rotate(angle);
    ctx.beginPath();
    ctx.ellipse(long * 0.45, 0, long, wide, 0, 0, Math.PI * 2);
    ctx.fillStyle = SKIN;
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function arm(ctx, joints, side) {
    const shoulder = toScreen(joints[side + "_shoulder"]);
    const elbow = toScreen(joints[side + "_elbow"]);
    const wrist = toScreen(joints[side + "_wrist"]);
    limb(ctx, shoulder, elbow, 15 * K, 11.5 * K, SKIN, SKIN_DARK, SKIN_LIGHT);
    limb(ctx, elbow, wrist, 11.5 * K, 8.5 * K, SKIN, SKIN_DARK, SKIN_LIGHT);
    tip(ctx, elbow, wrist, 10 * K, 7.5 * K);
  }

  function leg(ctx, joints, side) {
    const hip = toScreen(joints[side + "_hip"]);
    const knee = toScreen(joints[side + "_knee"]);
    const ankle = toScreen(joints[side + "_ankle"]);
    limb(ctx, hip, knee, 22 * K, 14.5 * K, LEG, LEG_DARK, LEG_LIGHT);
    limb(ctx, knee, ankle, 14.5 * K, 9.5 * K, LEG, LEG_DARK, LEG_LIGHT);
    tip(ctx, knee, ankle, 13 * K, 8 * K);
  }

  function torso(ctx, joints) {
    const ls = toScreen(joints.left_shoulder), rs = toScreen(joints.right_shoulder);
    const lh = toScreen(joints.left_hip), rh = toScreen(joints.right_hip);
    const top = toScreen(joints.shoulder_mid), base = toScreen(joints.hip_mid);

    // Deltoids sit outside the shoulder joint and the pelvis outside the hip
    // joint; a quad drawn through the four landmarks alone is a body with no
    // shoulders, which is what a rigged figure looks like when nobody widens
    // them.
    const out = (point, mid, factor) =>
      [mid[0] + (point[0] - mid[0]) * factor, mid[1] + (point[1] - mid[1]) * factor];
    const sL = out(ls, top, 1.16), sR = out(rs, top, 1.16);
    const hL = out(lh, base, 1.10), hR = out(rh, base, 1.10);
    const waist = (a, b) => [a[0] + (b[0] - a[0]) * 0.44, a[1] + (b[1] - a[1]) * 0.44];
    const wL = out(waist(hL, sL), waist(base, top), 0.80);
    const wR = out(waist(hR, sR), waist(base, top), 0.80);

    ctx.beginPath();
    ctx.moveTo(sL[0], sL[1]);
    ctx.quadraticCurveTo(wL[0], wL[1], hL[0], hL[1]);
    ctx.quadraticCurveTo(base[0], base[1] + 0, hR[0], hR[1]);
    ctx.quadraticCurveTo(wR[0], wR[1], sR[0], sR[1]);
    ctx.quadraticCurveTo(top[0], top[1], sL[0], sL[1]);
    ctx.closePath();

    // Along the spine, not corner to corner: the top is the vest and the
    // bottom is the leggings, and a gradient run from a shoulder to the
    // opposite hip splits them diagonally instead.
    const gradient = ctx.createLinearGradient(top[0], top[1], base[0], base[1]);
    gradient.addColorStop(0, TOP_LIGHT);
    gradient.addColorStop(0.30, TOP);
    gradient.addColorStop(0.52, TOP_DARK);
    gradient.addColorStop(0.56, LEG);
    gradient.addColorStop(1, LEG_DARK);
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2.4 * K;
    ctx.stroke();

    // The crossed straps across the back. Small, but they are the only thing
    // on a figure with no face that says which way she is turned, and she is
    // drawn from behind so that her left is the player's left.
    const mid = waist(base, top);
    ctx.beginPath();
    ctx.moveTo(ls[0] + (rs[0] - ls[0]) * 0.20, ls[1] + (rs[1] - ls[1]) * 0.20);
    ctx.lineTo(mid[0], mid[1]);
    ctx.moveTo(ls[0] + (rs[0] - ls[0]) * 0.80, ls[1] + (rs[1] - ls[1]) * 0.80);
    ctx.lineTo(mid[0], mid[1]);
    ctx.strokeStyle = "rgba(12,44,40,.42)";
    ctx.lineWidth = 6 * K;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineCap = "butt";
  }

  function head(ctx, joints) {
    const centre = toScreen(joints.head);
    const shoulders = toScreen(joints.shoulder_mid);
    const angle = Math.atan2(centre[1] - shoulders[1], centre[0] - shoulders[0]);
    const radius = 33 * K;

    limb(ctx, shoulders, centre, 12 * K, 10.5 * K, SKIN, SKIN_DARK,
         SKIN_LIGHT);

    ctx.save();
    ctx.translate(centre[0], centre[1]);
    // The head's own up is along the neck, so a forward fold takes the hair
    // with it rather than leaving a tidy bun pointing at the ceiling.
    ctx.rotate(angle + Math.PI / 2);

    // A low bun at the base of the skull, behind the head.
    ctx.beginPath();
    ctx.arc(0, radius * 0.74, radius * 0.42, 0, Math.PI * 2);
    ctx.fillStyle = HAIR;
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();

    ctx.beginPath();
    ctx.ellipse(0, 0, radius * 0.93, radius, 0, 0, Math.PI * 2);
    const hair = ctx.createLinearGradient(-radius, -radius, radius * 0.7, radius);
    hair.addColorStop(0, HAIR_LIGHT);
    hair.addColorStop(0.5, HAIR);
    hair.addColorStop(1, "#1b1425");
    ctx.fillStyle = hair;
    ctx.strokeStyle = INK;
    ctx.lineWidth = 2.2;
    ctx.fill();
    ctx.stroke();

    // A parting, so the head reads as the back of a head rather than a ball.
    ctx.beginPath();
    ctx.moveTo(0, -radius * 0.86);
    ctx.quadraticCurveTo(radius * 0.16, 0, 0, radius * 0.42);
    ctx.strokeStyle = "rgba(120,96,150,.42)";
    ctx.lineWidth = 2.4;
    ctx.stroke();
    ctx.restore();
  }

  function drawCoach(ctx, bones, scales) {
    const joints = kinematics(bones, scales);

    // Stand her on the mat. Anchoring the hips instead — which is what the rig
    // does, since the hips are its origin — floats a wide Warrior II clear of
    // the floor, because widening a stance lowers the hips without moving the
    // feet. The lowest foot is the one on the ground, which also puts Half
    // Moon's standing leg on the mat and leaves the lifted one in the air.
    const lowest = Math.max(toScreen(joints.left_ankle)[1],
                            toScreen(joints.right_ankle)[1]);
    ctx.save();
    ctx.translate(0, FLOOR_Y - lowest);

    // The shadow spans whatever is actually on the floor. Centred on one foot
    // it looked like a wide stance with only one leg in the light; centred on
    // the pair and widened with them, it reads as a stance.
    const lx = toScreen(joints.left_ankle)[0], rx = toScreen(joints.right_ankle)[0];
    const grounded = Math.abs(toScreen(joints.left_ankle)[1]
                              - toScreen(joints.right_ankle)[1]) < 40;
    const shadowX = grounded ? (lx + rx) / 2
      : (toScreen(joints.left_ankle)[1] > toScreen(joints.right_ankle)[1] ? lx : rx);
    ctx.save();
    ctx.globalAlpha = 0.36;
    ctx.beginPath();
    ctx.ellipse(shadowX, lowest + 8 * K,
                44 * K + (grounded ? Math.abs(lx - rx) * 0.5 : 0), 13 * K, 0, 0,
                Math.PI * 2);
    ctx.fillStyle = "#0d0718";
    ctx.fill();
    ctx.restore();

    // Her right side first, so a crossed arm or a lifted knee passes in front
    // of the body rather than through it.
    leg(ctx, joints, "right");
    arm(ctx, joints, "right");
    torso(ctx, joints);
    leg(ctx, joints, "left");
    head(ctx, joints);
    arm(ctx, joints, "left");
    ctx.restore();
  }

  /* ── the studio ─────────────────────────────────────────────────── */

  function drawStudio(ctx, now) {
    const sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, "#14102a");
    sky.addColorStop(0.55, "#241a41");
    sky.addColorStop(1, "#160f28");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, W, H);

    // A warm light behind the coach. Slowly breathing, which is the only thing
    // moving on screen while somebody holds a pose for forty seconds.
    const pulse = 1 + 0.03 * Math.sin(now / 2600);
    const glow = ctx.createRadialGradient(
      COACH_X, 430, 40, COACH_X, 430, 430 * pulse);
    glow.addColorStop(0, "rgba(255,196,140,.20)");
    glow.addColorStop(0.55, "rgba(160,110,200,.10)");
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 120, 672, 640);

    ctx.fillStyle = "rgba(255,255,255,.035)";
    ctx.fillRect(0, FLOOR_Y + 6, 672, 800 - FLOOR_Y - 6);

    // The mat, in perspective, hung off the same floor line the coach stands
    // on rather than off a second number that would have to be kept in step.
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(COACH_X - 210, FLOOR_Y - 12);
    ctx.lineTo(COACH_X + 210, FLOOR_Y - 12);
    ctx.lineTo(COACH_X + 268, FLOOR_Y + 46);
    ctx.lineTo(COACH_X - 268, FLOOR_Y + 46);
    ctx.closePath();
    const mat = ctx.createLinearGradient(0, FLOOR_Y - 12, 0, FLOOR_Y + 46);
    mat.addColorStop(0, MAT_DARK);
    mat.addColorStop(1, MAT);
    ctx.fillStyle = mat;
    ctx.fill();
    ctx.strokeStyle = "rgba(255,220,190,.24)";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.restore();
  }

  /* ── the rings ──────────────────────────────────────────────────── */

  function ring(ctx, x, y, radius, fraction, colour, track) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.strokeStyle = track || "rgba(255,255,255,.13)";
    ctx.lineWidth = 13;
    ctx.stroke();
    if (fraction <= 0) return;
    ctx.beginPath();
    ctx.arc(x, y, radius, -Math.PI / 2,
            -Math.PI / 2 + Math.PI * 2 * Math.min(1, fraction));
    ctx.strokeStyle = colour;
    ctx.lineWidth = 13;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineCap = "butt";
  }

  function accuracyColour(value) {
    if (value >= 0.82) return GOOD;
    if (value >= 0.58) return WARN;
    return BAD;
  }

  function drawGauges(ctx, game, now) {
    const pose = game.pose || {};
    const holding = !!game.holding;

    // Hold. Under the live feed, because that is where the player is already
    // looking — and never over it, because a number across somebody's chest is
    // a number covering the thing they are trying to correct.
    const holdFraction = pose.hold ? 1 - (pose.hold_left || 0) / pose.hold : 0;
    smoothedHold += (holdFraction - smoothedHold) * 0.2;
    ring(ctx, 964, 620, 62, smoothedHold, holding ? GOOD : "rgba(255,255,255,.34)");
    ctx.textAlign = "center";
    ctx.fillStyle = "#fff";
    ctx.font = "700 40px Inter, system-ui, sans-serif";
    ctx.fillText(Math.ceil(pose.hold_left || 0), 964, 634);
    ctx.font = "600 13px Inter, system-ui, sans-serif";
    ctx.fillStyle = holding ? GOOD : "rgba(255,255,255,.5)";
    ctx.fillText(holding ? "HOLDING" : "FIND THE POSE", 964, 706);

    // Accuracy.
    const accuracy = Math.max(0, Math.min(1, (game.accuracy || 0) / 100));
    smoothedAccuracy += (accuracy - smoothedAccuracy) * 0.18;
    ring(ctx, 782, 620, 62, smoothedAccuracy, accuracyColour(smoothedAccuracy));
    ctx.fillStyle = "#fff";
    ctx.font = "700 40px Inter, system-ui, sans-serif";
    ctx.fillText(Math.round(game.accuracy || 0), 782, 634);
    ctx.font = "600 13px Inter, system-ui, sans-serif";
    ctx.fillStyle = "rgba(255,255,255,.5)";
    ctx.fillText("ACCURACY", 782, 706);

    // Lesson progress, as a thin bar the whole width of the coach's half.
    const total = Number(game.duration) || 1200;
    const done = Math.max(0, Math.min(1, 1 - (game.time_left || 0) / total));
    ctx.fillStyle = "rgba(255,255,255,.12)";
    ctx.fillRect(1146, 580, 10, 168);
    ctx.fillStyle = "rgba(140,220,255,.85)";
    ctx.fillRect(1146, 580 + 168 * (1 - done), 10, 168 * done);
    ctx.textAlign = "left";
  }

  function drawCountdown(ctx, text) {
    if (!text) return;
    ctx.save();
    ctx.fillStyle = "rgba(10,7,20,.62)";
    ctx.fillRect(0, 0, W, H);
    ctx.textAlign = "center";
    ctx.fillStyle = "#fff";
    ctx.font = "800 168px Inter, system-ui, sans-serif";
    ctx.fillText(text, W / 2, H / 2 + 56);
    ctx.restore();
    ctx.textAlign = "left";
  }

  function stepRipples(ctx, now) {
    ripples = ripples.filter((r) => now - r.at < 900);
    for (const r of ripples) {
      const t = (now - r.at) / 900;
      ctx.beginPath();
      ctx.arc(r.x, r.y, 60 + t * 120, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(95,227,180,${(1 - t) * 0.5})`;
      ctx.lineWidth = 4 * (1 - t);
      ctx.stroke();
    }
  }

  /* ── the frame ──────────────────────────────────────────────────── */

  function render(ctx, now, payload, fps) {
    if (!active) return;
    ctx.clearRect(0, 0, W, H);
    drawStudio(ctx, now);

    const game = (payload && payload.game) || null;
    if (!game || game.kind !== "yoga") { drawCoach(ctx, restingBones(), {}); return; }

    const rig = game.rig || {};
    const to = rig.bones || restingBones();
    const from = rig.from_bones || to;
    const blend = ease(rig.blend === undefined ? 1 : rig.blend);
    let bones = blendBones(from, to, blend);
    const scales = blendScales(rig.from_scales, rig.scales, blend);

    // Breathing, once she has arrived. Two degrees on the spine and a little
    // on the arms — under a hold that lasts forty seconds a perfectly still
    // figure stops reading as a person.
    if (blend >= 0.999 && game.state === "playing") {
      breathPhase = now / 1000;
      const breath = Math.sin(breathPhase * 0.9);
      bones = Object.assign({}, bones);
      bones.spine += breath * 0.9;
      bones.neck += breath * 1.2;
      bones.left_upper_arm += breath * 1.1;
      bones.right_upper_arm -= breath * 1.1;
    }

    drawCoach(ctx, bones, scales);
    stepRipples(ctx, now);
    if (game.state === "playing") drawGauges(ctx, game, now);
    drawCountdown(ctx, game.countdown === "READY" ? "" : game.countdown);
  }

  function restingBones() {
    return {
      spine: -90, neck: -90,
      left_upper_arm: 96, left_forearm: 96,
      right_upper_arm: 84, right_forearm: 84,
      left_thigh: 90, left_shin: 90, right_thigh: 90, right_shin: 90,
    };
  }

  /* ── the HUD ────────────────────────────────────────────────────── */

  function clock(seconds) {
    const total = Math.max(0, Math.round(seconds || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function cameraOn(on) {
    const camera = el("yoga-camera");
    if (on && !camera.getAttribute("src")) {
      // Twelve frames a second and a wider frame than the start screen's
      // thumbnail: this is the mirror the player corrects themselves in, and
      // the server clamps both numbers so a stale tab cannot ask for more.
      camera.src = "/api/game/preview?fps=12&w=640&t=" + Date.now();
    } else if (!on && camera.getAttribute("src")) {
      camera.removeAttribute("src");
    }
  }

  function applyState(state) {
    latest = state;
    const game = state.game;
    if (!game || game.kind !== "yoga") return;
    const playing = game.state === "playing";
    const stage = el("game-stage");
    stage.classList.toggle("yoga-active", active);
    stage.classList.toggle("yoga-playing", active && playing);
    // While the coach is moving into the pose there is nothing for the player
    // to do but read; once the hold starts the card folds down to the pose
    // name, which gives the whole left half back to the coach. A Standing Side
    // Bend puts her raised hand exactly where the third line of the
    // instruction was.
    stage.classList.toggle("yoga-reading",
                           active && playing && game.phase === "transition");
    cameraOn(active && playing);

    const pose = game.pose || {};
    el("yoga-segment").textContent = pose.segment || game.lesson || "";
    el("yoga-count").textContent = pose.total
      ? `POSE ${pose.index} / ${pose.total}` : "";
    el("yoga-clock").textContent = clock(game.time_left);
    el("yoga-score").textContent = game.score ? `SCORE ${game.score}` : "SCORE —";

    el("yoga-name").textContent = pose.name || "";
    el("yoga-sanskrit").textContent = pose.sanskrit || "";
    el("yoga-instruction").textContent = pose.instruction || "";
    el("yoga-cue").textContent = pose.cue || "";
    el("yoga-hold").textContent = pose.hold ? `HOLD ${pose.hold} SECONDS` : "";

    const feedback = el("yoga-feedback");
    const message = playing ? (game.feedback || "") : (game.framing || "");
    feedback.textContent = message;
    feedback.className = !message ? "" :
      game.wrong_side ? "urgent" :
      !game.tracked ? "urgent" :
      game.holding && (game.accuracy || 0) >= 86 ? "praise" : "";

    const card = el("yoga-result");
    if (game.last) {
      card.innerHTML =
        `<span class="band">${game.last.band}</span>` +
        `<span class="value">${game.last.score}</span>` +
        `<span class="which">${game.last.name}</span>`;
      card.classList.add("on");
    } else {
      card.classList.remove("on");
    }

    if (game.state === "over" && game.summary) fillSummary(game.summary);
    // Nobody loses a yoga class. The shared sheet is the right sheet — same
    // buttons, same crossed-arms Play Again — but "GAME OVER" is the wrong
    // three words at the end of twenty minutes of breathing.
    overHeading("YOGA COMPLETE");

    // One chime per pose, driven off the snapshot rather than off an event —
    // `status()` drains the event queue as a side effect, so anything a poll
    // could steal must not be the only way a sound gets played.
    if (pose.id && pose.id !== lastPoseId && playing) {
      lastPoseId = pose.id;
      chime(392, 0.5, 0.05);
      ripples.push({ x: COACH_X, y: 430, at: performance.now() });
    }
    if (!playing) lastPoseId = "";
    if (game.countdown && game.countdown !== lastCountdown) {
      lastCountdown = game.countdown;
      if (/^[123]$/.test(game.countdown)) chime(523, 0.16, 0.06);
      else if (game.countdown === "BEGIN") chime(784, 0.6, 0.07);
    }
    if (game.phase !== lastPhase) lastPhase = game.phase;
  }

  function fillSummary(summary) {
    const rows = [
      ["Overall score", `${summary.overall}`],
      ["Average accuracy", `${summary.average_accuracy}%`],
      ["Poses completed", `${summary.completed} of ${summary.total}`],
      ["Best pose", summary.best
        ? `${summary.best.name} · ${summary.best.score}` : "—"],
      ["Lowest pose", summary.lowest
        ? `${summary.lowest.name} · ${summary.lowest.score}` : "—"],
      ["Correct hold time", `${clock(summary.hold_seconds)} of ` +
        `${clock(summary.required_seconds)}`],
    ];
    el("yoga-results").innerHTML =
      `<div class="verdict">${summary.band}</div>` +
      rows.map(([label, value]) =>
        `<div class="row"><span>${label}</span><b>${value}</b></div>`).join("");
  }

  /* ── sound ──────────────────────────────────────────────────────── */

  function context() {
    if (audio === null) {
      try { audio = new (window.AudioContext || window.webkitAudioContext)(); }
      catch (err) { audio = false; }
    }
    if (audio && audio.state === "suspended") audio.resume().catch(() => {});
    return audio || null;
  }

  /* Deliberately soft and deliberately short. This game is twenty minutes long
   * and the arcade sounds that suit a sixty-second Fruit Ninja round would be
   * unbearable by minute four. */
  function chime(frequency, seconds, gainPeak) {
    const ctx = context();
    if (!ctx) return;
    const now = ctx.currentTime;
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, now);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(gainPeak, now + 0.04);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + seconds);
    oscillator.connect(gain).connect(ctx.destination);
    oscillator.start(now);
    oscillator.stop(now + seconds + 0.05);
  }

  function handleEvent(event) {
    switch (event.name) {
      case "yoga-in-pose":
        chime(659, 0.35, 0.05);
        ripples.push({ x: 964, y: 620, at: performance.now() });
        break;
      case "yoga-pose-complete":
        chime(523, 0.3, 0.055);
        window.setTimeout(() => chime(784, 0.55, 0.05), 130);
        break;
      case "yoga-over":
        chime(523, 0.9, 0.06);
        window.setTimeout(() => chime(659, 0.9, 0.06), 220);
        window.setTimeout(() => chime(784, 1.4, 0.06), 440);
        break;
      default:
        break;
    }
  }

  /* ── lifecycle ──────────────────────────────────────────────────── */

  /* The shared start screen asks for the upper body, which is what Fruit
   * Ninja and Boxing are played with. A yoga pose is scored on knees and
   * ankles, so this game asks the player to stand further back — and it is
   * worth saying on the screen where they are deciding where to stand rather
   * than only in a correction once the class has begun. */
  const SHARED_COPY = "Stand where the camera can see your full upper body.";
  const SHARED_OVER = "GAME OVER";
  const YOGA_COPY = "Stand back until the camera can see your whole body, "
    + "including your feet.";

  function overHeading(text) {
    const heading = el("sheet-over").querySelector("h2");
    if (heading) heading.textContent = text;
  }

  function setStartCopy(copy) {
    const instruction = el("start-instruction");
    if (instruction && instruction.firstChild) {
      instruction.firstChild.nodeValue = copy;
    }
  }

  function onOpen(id) {
    active = id === "yoga";
    const stage = el("game-stage");
    stage.classList.toggle("yoga-active", active);
    if (!active) {
      stage.classList.remove("yoga-playing");
      cameraOn(false);
      return;
    }
    resetRound();
    el("start-title").textContent = "Yoga Coach";
    setStartCopy(YOGA_COPY);
    for (const peer of document.querySelectorAll("[data-yoga-difficulty]")) {
      peer.classList.remove("selected");
    }
    // The level sheet comes before the shared start screen, exactly as
    // Boxing's mode sheet does, and for the same reason: the crossed-arms
    // gesture must not start a lesson nobody has chosen.
    showSheet("yoga");
  }

  function resetRound() {
    latest = null;
    lastPoseId = "";
    lastPhase = "";
    lastCountdown = "";
    smoothedAccuracy = 0;
    smoothedHold = 0;
    ripples = [];
  }

  function stop() {
    const was = active;
    active = false;
    cameraOn(false);
    const stage = el("game-stage");
    stage.classList.remove("yoga-active", "yoga-playing");
    // Only put the shared wording back if it was ours to change. Leaving a
    // game and immediately opening another one calls stop() after the new
    // game's onOpen has already set its own copy.
    if (was) { setStartCopy(SHARED_COPY); overHeading(SHARED_OVER); }
    resetRound();
  }

  for (const button of document.querySelectorAll("[data-yoga-difficulty]")) {
    button.addEventListener("click", () => {
      for (const peer of document.querySelectorAll("[data-yoga-difficulty]")) {
        peer.classList.remove("selected");
      }
      button.classList.add("selected");
      gameCommand(`difficulty-${button.dataset.yogaDifficulty}`)
        .then(() => showSheet("start"));
    });
  }
  el("yoga-back").addEventListener("click", () => show("games"));

  window.YogaUI = {
    onOpen, stop, resetRound, applyState, handleEvent, render,
    // Exposed so the rig can be checked against the Pi's copy of it from
    // outside the browser. The coach being drawn in a pose the scorer is not
    // asking for is the one bug in this game that nothing on screen would
    // report, so it is worth a seam to test through.
    rig: { RIG, kinematics, blendBones, lerpAngle },
  };
})();
