/* AIPI5 Yoga Coach renderer.
 *
 * The coach is a person now, standing in a field, and the pictures of her were
 * made *from* the fourteen bone directions the Pi scores the player against --
 * `scripts/build_yoga_coach.py` renders each pose's skeleton with the scorer's
 * own `forward_kinematics` and the artwork is drawn on top of that. So the
 * shape demonstrated and the shape marked still come from one table, one joint
 * at a time; what changed is who draws the last step.
 *
 * The rig that used to draw her is still here and still exact, and it is the
 * fallback: until the images have loaded -- or if one is missing -- the coach
 * is drawn from the bones rather than not drawn at all.
 *
 * **There is no live camera on this screen.** The camera runs the whole time,
 * because every number in the HUD comes from it, but the player watches the
 * coach and their own score rather than themselves.
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
   * laid out around, since both the drawn and the photographed coach re-anchor
   * her to the floor.
   *
   * Centre of the screen and half again as big as she was: the live camera
   * panel used to own the right half, and with it gone the constraint that set
   * these numbers is gone too. 160 is the largest that keeps Tree Pose with
   * arms high -- the tallest shape in the library at 3.9 spines -- under the
   * top bar. `UNIT` must match `SERVED_UNIT` in `scripts/build_yoga_coach.py`;
   * a test checks it, because a coach drawn at one scale and photographed at
   * another swaps size every time the images finish loading. */
  const COACH_X = 640, COACH_HIP_Y = 424, UNIT = 160, FLOOR_Y = 726;

  /* The accuracy dial. Right of the widest pose in the library -- Half Moon
   * stops around x=970 -- and high enough that its two labels finish above the
   * correction line, which owns the full width from y=708 down. */
  const ACCURACY_X = 1150, ACCURACY_Y = 548;

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
  const GOOD = "#5fe3b4", WARN = "#ffcc5c", BAD = "#ff7a6b";

  let active = false;
  let latest = null;
  let audio = null;
  let lastPoseId = "";
  let lastPhase = "";
  let lastCountdown = "";
  let breathPhase = 0;
  let smoothedAccuracy = 0;
  let ripples = [];

  /* == the artwork ================================================== */

  /* The field she stands in, and the pictures of her standing in it. Both are
   * loaded once, at file scope, because this page is opened and closed all
   * evening and re-fetching a background every time somebody starts a class is
   * work the browser cache should not have to be trusted with.
   *
   * `coach` is keyed by pose id. Left and right versions of a pose share one
   * picture and the right one is drawn mirrored -- see the manifest's
   * `mirror_of` -- which is not a saving so much as a guarantee: a pair built
   * from one drawing cannot disagree about anything except which way round it
   * is. */
  const field = new Image();
  let fieldReady = false;
  field.decoding = "async";
  field.onload = () => { fieldReady = true; };
  field.src = "/assets/yoga/field-forest.webp";

  let coachManifest = null;
  const coachImages = {};

  /* Fetched when they are about to be needed, not all at once.
   *
   * There are 78 drawings now -- 21 poses and three transition frames for
   * almost every one of them -- and a decoded 500x600 bitmap is about a
   * megabyte whatever the WebP on disk cost. Asking Chromium on a Pi to hold
   * all of them at once is asking for ninety megabytes of decoded pixels to
   * demonstrate a pose that lasts forty seconds. So a drawing is loaded when
   * the class first reaches for it, and the frames of the pose *after* this
   * one are asked for while the current one is being held -- which is forty
   * seconds of warning for a 30 kB file. */
  fetch("/assets/yoga/coach/manifest.json")
    .then((response) => response.json())
    .then((manifest) => {
      coachManifest = manifest;
      // Standing is the shape every transition passes near, and the fallback
      // when a frame is missing. It is the one drawing worth having early.
      load("mountain");
    })
    .catch(() => { coachManifest = null; });

  function load(id) {
    if (!coachManifest || !id) return null;
    const entry = coachManifest.poses && coachManifest.poses[id];
    if (!entry) return null;
    const file = entry.mirror_of || id;
    if (!coachImages[file]) {
      const image = new Image();
      image.decoding = "async";
      image.src = `/assets/yoga/coach/${file}.webp`;
      coachImages[file] = image;
    }
    return { image: coachImages[file], entry, mirror: !!entry.mirror_of };
  }

  /* Whether there is a picture of this shape that has finished loading. Every
   * caller has a rig to fall back on, so this is a question and not an error;
   * asking also starts the download. */
  function coachPicture(id) {
    const found = load(id);
    if (!found || !found.image.complete || !found.image.naturalWidth) return null;
    return found;
  }

  /* Everything the next transition will ask for, requested early. */
  function preload(poseId) {
    if (!poseId) return;
    load(poseId);
    load(stepId(poseId));
    loadClip(poseId);
  }

  /* == the coach, moving ============================================ */

  /* Twenty short films of her standing up out of one shape and down into
   * another, and they are what a transition actually plays.
   *
   * The drawings above can only dissolve one shape into the next, and a
   * dissolve is not a movement: two bodies overlap, and neither of them is
   * doing anything. These are the same coach really moving, generated *from*
   * two of those drawings as the first and last frame of one continuous shot
   * -- so the ends of a clip are the drawings it was made from, measured at
   * one to three pixels of placement across all twenty, and the handover from
   * clip to drawing when she arrives has nothing in it to see.
   *
   * **One clip per pose, played both ways.** Every clip runs from standing
   * into its pose. Entering a pose plays it forwards, leaving one plays it
   * backwards, and standing is the hinge they meet on. That is why twenty
   * clips cover the seventy-four transitions the three lessons contain, and
   * why the coach is recognisably one person for the whole of each: a single
   * shot cannot change her face halfway through, and a second visit to an
   * image model always did.
   */
  let clipManifest = null;
  const clipFrames = {};
  const clipUsed = {};
  let clipClock = 0;

  fetch("/assets/yoga/clips/manifest.json")
    .then((response) => response.json())
    .then((manifest) => { clipManifest = manifest; })
    .catch(() => { clipManifest = null; });

  /* Which clip plays this pose, and whether it plays mirrored.
   *
   * The right-hand poses are the left-hand clip flipped, exactly as the
   * drawings are. She stands on the centre line of the stage, so flipping the
   * whole 1280-wide frame about its middle is the same flip `drawCoachPicture`
   * does about her hips -- and a pair built from one clip cannot disagree
   * about anything except which way round it is.
   *
   * Standing has no clip of its own, deliberately: it is the first frame of
   * every one of these, so a clip of her standing still would be twenty copies
   * of a frame that already ships. Callers read the `null` as "nothing to play
   * here", which for standing is the truth. */
  function clipSource(poseId) {
    if (!clipManifest || !poseId) return null;
    const poses = clipManifest.poses || {};
    if (poses[poseId]) return { id: poseId, entry: poses[poseId], mirror: false };
    if (poseId.endsWith("_right")) {
      const twin = poseId.slice(0, -"_right".length) + "_left";
      if (poses[twin]) return { id: twin, entry: poses[twin], mirror: true };
    }
    return null;
  }

  /* How many poses' clips stay decoded.
   *
   * Seven cut-out frames is four or five megabytes once Chromium has unpacked
   * them, whatever the 140 kB on disk cost, and a lesson visits ten poses:
   * keeping every clip a class touches is fifty megabytes of bitmaps held to
   * demonstrate the one transition happening now. Three is exactly what is
   * ever wanted at once -- the half being played, the half it hands over to,
   * and the pose the class moves to next, asked for a hold in advance. */
  const CLIPS_KEPT = 3;

  function loadClip(poseId) {
    const source = clipSource(poseId);
    if (!source) return null;
    clipUsed[source.id] = ++clipClock;
    if (!clipFrames[source.id]) {
      const frames = [];
      for (let index = 0; index < source.entry.frames; index += 1) {
        const image = new Image();
        image.decoding = "async";
        image.src = "/assets/yoga/clips/" + source.id + "/"
                    + String(index).padStart(2, "0") + ".webp";
        frames.push(image);
      }
      clipFrames[source.id] = frames;
      forgetOldClips();
    }
    return source;
  }

  /* Drop the clips nobody has reached for lately. Letting go of the last
   * reference to an Image is the whole of what this can do -- when the bitmap
   * is actually freed is the browser's business -- but keeping the reference
   * is a guarantee that it is not. */
  function forgetOldClips() {
    const ids = Object.keys(clipFrames);
    if (ids.length <= CLIPS_KEPT) return;
    ids.sort((a, b) => (clipUsed[b] || 0) - (clipUsed[a] || 0));
    for (const id of ids.slice(CLIPS_KEPT)) {
      delete clipFrames[id];
      delete clipUsed[id];
    }
  }

  /* One frame of one pose's clip, if it has arrived. Every caller has the
   * drawings to fall back on, so a frame that has not decoded yet is a
   * question and not an error -- and asking for it starts the download. */
  function clipFrame(poseId, index) {
    const source = loadClip(poseId);
    if (!source) return null;
    const frames = clipFrames[source.id];
    const at = (source.entry.at || [])[index];
    const image = frames && frames[index];
    if (!at || !image || !image.complete || !image.naturalWidth) return null;
    return { image, at, mirror: source.mirror };
  }

  function clipIndex(source, along) {
    const count = Math.max(1, source.entry.frames);
    return Math.min(count - 1, Math.max(0, Math.round(along * (count - 1))));
  }

  /* Which frame of which clip a transition is on, at this blend.
   *
   * A transition leaves the shape she is in and enters the next one, and each
   * half is one clip: `from`'s run backwards to stand her up, then `to`'s run
   * forwards. A pose with no clip contributes no half -- standing is the one
   * that matters, being every clip's first frame rather than a clip of its own
   * -- so moving out of Mountain is the second half alone and spans the whole
   * blend, and moving into it is the first half alone.
   *
   * `blend` is read raw here where the rig reads it eased. The easing is there
   * to make interpolated bones start and stop softly; a clip already carries
   * how its movement is paced, and easing it again would drag out both ends of
   * a movement that was generated with ends of its own.
   *
   * Returns null the moment there is nothing to play -- she has arrived, or
   * neither end has a clip -- and null is what sends `drawTheCoach` back to
   * the drawings. Arriving is a handover rather than a fallback: the last
   * frame of a clip is a frame of video *of* the drawing it was made from, and
   * the drawing is the better picture to hold still for forty seconds. */
  function clipAt(rig, blend) {
    const from = rig.from, to = rig.pose;
    if (!from || !to || from === to || !(blend < 0.999)) return null;
    const leaving = clipSource(from) ? from : null;
    const entering = clipSource(to) ? to : null;
    if (!leaving && !entering) return null;

    // Both halves are asked for the instant the transition starts. The second
    // is not drawn until the midpoint, but seven files requested *at* the
    // midpoint are seven files that arrive after it, and she would finish the
    // move as a cross-fade having spent the first half loading nothing.
    if (leaving) loadClip(leaving);
    if (entering) loadClip(entering);

    const t = Math.min(1, Math.max(0, blend));
    const play = (poseId, along) => {
      const source = clipSource(poseId);
      const frame = source && clipFrame(poseId, clipIndex(source, along));
      return frame ? { poseId, frame } : null;
    };
    if (leaving && entering) {
      return t < 0.5 ? play(leaving, 1 - t / 0.5)
                     : play(entering, (t - 0.5) / 0.5);
    }
    return leaving ? play(leaving, 1 - t) : play(entering, t);
  }

  /* A clip frame goes where the packer found her, in the stage's own pixels.
   * These were generated at the size and in the place the game draws, then
   * cropped to her with the offset written down, so there is nothing here to
   * anchor the way a trimmed drawing has to be: the picture's own corner is
   * already the answer. A mirrored pose flips the whole stage, which is why
   * the offset flips with it rather than being recomputed. */
  function drawClipFrame(ctx, frame, alpha) {
    const { image, at, mirror } = frame;
    ctx.save();
    ctx.globalAlpha = alpha === undefined ? 1 : alpha;
    if (mirror) {
      ctx.translate(W, 0);
      ctx.scale(-1, 1);
    }
    ctx.drawImage(image, at.x, at.y, at.w, at.h);
    ctx.restore();
  }

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

  /* == the photographed coach ======================================= */

  /* Where a picture of the coach goes. Her hips land on `COACH_X` and her floor
   * line on `FLOOR_Y`, both read from the manifest in the picture's own pixels,
   * because a trimmed drawing is only as big as the ink in it: Standing Forward
   * Fold and Tree Pose have their hips at completely different fractions of
   * their own heights, and stacking the two by their centres makes the coach
   * hop every time the pose changes. */
  function drawCoachPicture(ctx, found, alpha) {
    const { image, entry, mirror } = found;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(COACH_X, FLOOR_Y);
    if (mirror) ctx.scale(-1, 1);
    ctx.drawImage(image, -entry.hip_x, -entry.floor_y, entry.w, entry.h);
    ctx.restore();
  }

  /* The soft ground shadow. The pictures are cut out with no shadow of their
   * own -- deliberately, since one baked into the artwork would be lit for a
   * room she is not in -- so the grass gets one here, and it widens with her
   * stance the same way the drawn coach's did. */
  function drawGroundShadow(ctx, figureWidth, alpha) {
    const width = (figureWidth || 220) * 0.42;
    ctx.save();
    ctx.globalAlpha = 0.3 * alpha;
    ctx.beginPath();
    ctx.ellipse(COACH_X, FLOOR_Y + 4, width, 15, 0, 0, Math.PI * 2);
    ctx.fillStyle = "#20361c";
    ctx.fill();
    ctx.restore();
  }

  /* How far apart two poses are, in degrees of bone rotation summed over the
   * body. Used for one decision only: whether a transition needs a step in the
   * middle. */
  function boneDistance(from, to) {
    let total = 0;
    for (const name of Object.keys(Object.assign({}, from, to))) {
      const a = from[name], b = to[name];
      if (a === undefined || b === undefined) continue;
      total += Math.abs(wrap(b - a));
    }
    return total;
  }

  /* Above this, in summed degrees, a pose is "far from standing". Both ends of
   * a transition being far from standing is the case that needs a frame in the
   * middle -- Warrior II straight into Triangle is two deep shapes with nothing
   * between them, and cross-fading one into the other is a dissolve, not a
   * movement. */
  const FAR_FROM_STANDING = 300;

  /* The chain of pictures a transition plays through.
   *
   * **A class must never cut from one shape to another.** Between any two
   * poses the coach comes *out* of the one she is in, passes through standing,
   * and moves *into* the next, and the artwork for that is one picture per
   * pose rather than one per pair: every pose has a frame of its own halfway
   * between standing and itself. That matters because the three lessons
   * contain 74 consecutive pairs and would otherwise need a drawing for each.
   *
   *     Warrior II -> halfway out -> Mountain -> halfway in -> Triangle
   *
   * It was three frames per pose (a quarter, a half, three quarters) first.
   * Three frames are three separate visits to the image model and it does not
   * draw the same face twice, so the drift was at its most visible exactly
   * where the frames play back to back. One frame per pose is one thing to
   * keep consistent, and the movement still reads.
   */
  const STEP = 50;

  function stepId(pose) { return pose + "__" + STEP; }

  function chain(rig) {
    const from = rig.from || rig.pose, to = rig.pose;
    if (!from || from === to) return [to];
    const keys = [from];
    if (coachPicture(stepId(from))) keys.push(stepId(from));
    // Standing between the two halves: it is where a class actually passes
    // between two deep poses, and it is the one picture always present.
    if (from !== "mountain" && to !== "mountain" && coachPicture("mountain")) {
      keys.push("mountain");
    }
    if (coachPicture(stepId(to))) keys.push(stepId(to));
    keys.push(to);
    return keys;
  }

  /* The bones of any key in that chain, without asking the Pi for them.
   *
   * A step frame is defined as an interpolation between standing and its pose,
   * and that is a calculation this file already does sixty times a second. The
   * payload carries the two endpoint tables; everything between them is
   * derived, so the chain costs nothing on the wire. */
  function keyBones(id, rig) {
    const standing = restingBones();
    const to = rig.bones || standing, from = rig.from_bones || to;
    const [base, step] = id.split("__");
    const table = base === rig.pose ? to
      : base === (rig.from || rig.pose) ? from
      : standing;
    if (!step) return table;
    return blendBones(standing, table, Number(step) / 100);
  }

  function keyScales(id, rig) {
    const [base, step] = id.split("__");
    const scales = base === rig.pose ? rig.scales
      : base === (rig.from || rig.pose) ? rig.from_scales : {};
    const t = step ? Number(step) / 100 : 1;
    return blendScales({}, scales, t);
  }

  /* Which two pictures are on screen, how far between them, and which pair of
   * shapes the arrows are being drawn for. Each key holds still for the first
   * and last quarter of its slice and dissolves across the middle half, so the
   * coach arrives in each shape long enough to be copied. */
  function transitionKeys(rig, blend) {
    const keys = chain(rig);
    if (keys.length === 1) return { keys: [{ id: keys[0], alpha: 1 }] };
    const segments = keys.length - 1;
    const along = Math.min(0.999999, Math.max(0, blend)) * segments;
    const index = Math.floor(along);
    const t = ease(Math.min(1, Math.max(0, (along - index - 0.25) / 0.5)));
    const here = keys[index], next = keys[index + 1];
    const drawn = t >= 0.999 ? [{ id: next, alpha: 1 }]
      : t <= 0.001 ? [{ id: here, alpha: 1 }]
      : [{ id: here, alpha: 1 - t }, { id: next, alpha: t }];
    return { keys: drawn, here, next, t };
  }

  /* == the arrows =================================================== */

  /* Where each joint of a shape lands on the screen.
   *
   * The same anchoring the coach herself gets -- hips on `COACH_X`, lowest
   * foot on `FLOOR_Y` -- because the arrows have to start on her hands and
   * feet, not near them. It works for the photographed coach as well as the
   * drawn one because both are laid out at `UNIT` pixels per spine from the
   * same bone table; that is the whole reason those two numbers are required
   * to match. */
  function screenJoints(bones, scales) {
    const joints = kinematics(bones, scales);
    const points = {};
    let lowest = -Infinity;
    for (const [name, point] of Object.entries(joints)) {
      const at = toScreen(point);
      points[name] = { x: at[0], y: at[1] };
      if (name === "left_ankle" || name === "right_ankle") {
        lowest = Math.max(lowest, at[1]);
      }
    }
    const lift = FLOOR_Y - lowest;
    for (const point of Object.values(points)) point.y += lift;
    return points;
  }

  /* Which joints get an arrow, and how far a joint has to travel to earn one.
   *
   * Hands and feet only. A body moving into Warrior II moves every joint it
   * has, and an arrow on each of them is a diagram of a skeleton rather than
   * an instruction -- what a player needs to know is where to put the ends of
   * their limbs, and the rest follows. The threshold is in pixels at the size
   * she is drawn: a quarter of a spine length, which is roughly a hand's
   * travel that somebody would notice. */
  const ARROW_JOINTS = ["left_wrist", "right_wrist", "left_ankle", "right_ankle"];
  const ARROW_MIN = UNIT * 0.25;

  /* One curved arrow, drawn dark-then-light so it survives sunlit grass. */
  function arrow(ctx, from, to, phase) {
    const dx = to.x - from.x, dy = to.y - from.y;
    const length = Math.hypot(dx, dy);
    if (length < 1) return;
    // Bowed to one side, always the same side relative to the direction of
    // travel, so a pair of arrows on two limbs reads as one movement rather
    // than as two unrelated hooks.
    const bow = Math.min(58, length * 0.28);
    const mid = { x: (from.x + to.x) / 2 - (dy / length) * bow,
                  y: (from.y + to.y) / 2 + (dx / length) * bow };
    // A breath of travel along the arrow, so it reads as a direction and not
    // as a bracket. Slow: this sits under a five-second movement.
    const grow = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(phase * 2.2));
    const head = {
      x: from.x + (mid.x - from.x) * 2 * grow * (1 - grow)
         + (to.x - from.x) * grow * grow,
      y: from.y + (mid.y - from.y) * 2 * grow * (1 - grow)
         + (to.y - from.y) * grow * grow,
    };
    const before = {
      x: from.x + (mid.x - from.x) * 2 * (grow - 0.03) * (1 - grow + 0.03)
         + (to.x - from.x) * (grow - 0.03) * (grow - 0.03),
      y: from.y + (mid.y - from.y) * 2 * (grow - 0.03) * (1 - grow + 0.03)
         + (to.y - from.y) * (grow - 0.03) * (grow - 0.03),
    };

    // A dark rim under a solid cream stroke. The rim is not decoration: this
    // is drawn over sunlit grass and over the coach herself, and a single
    // pale line disappears against both. It has to be *opaque* cream, too —
    // a translucent one takes the rim's colour and the arrow comes out olive.
    for (const pass of [{ w: 15, colour: "rgba(14,30,12,.55)" },
                        { w: 8, colour: "#fff2a8" }]) {
      ctx.save();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = pass.colour;
      ctx.fillStyle = pass.colour;
      ctx.lineWidth = pass.w;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.quadraticCurveTo(mid.x, mid.y, to.x, to.y);
      ctx.stroke();

      // The head rides along the curve at `grow`, pointing the way the curve
      // is going at that instant.
      const angle = Math.atan2(head.y - before.y, head.x - before.x);
      const size = 13 + pass.w;
      ctx.translate(head.x, head.y);
      ctx.rotate(angle);
      ctx.beginPath();
      ctx.moveTo(size, 0);
      ctx.lineTo(-size * 0.65, size * 0.55);
      ctx.lineTo(-size * 0.35, 0);
      ctx.lineTo(-size * 0.65, -size * 0.55);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  }

  /* The arrows for the step of the transition currently on screen: from where
   * each hand and foot is now to where the next frame puts it.
   *
   * Computed rather than drawn into the artwork, for the reason the coach's
   * shapes are computed: there are 74 transitions in the three lessons and
   * one bone table for all of them. They fade in and out with the frames they
   * belong to, so nothing snaps. */
  function drawMoveArrows(ctx, rig, step, now) {
    const here = screenJoints(keyBones(step.here, rig), keyScales(step.here, rig));
    const next = screenJoints(keyBones(step.next, rig), keyScales(step.next, rig));
    // Brightest mid-dissolve, but never off: the moment the coach is *holding*
    // a transition frame is exactly the moment a player looks up to see where
    // the next one is, and an arrow that has faded out by then is an arrow
    // that is only ever visible while the thing it describes is happening.
    const strength = Math.sin(Math.PI * Math.min(1, Math.max(0, step.t || 0)));
    ctx.save();
    ctx.globalAlpha = 0.8 + 0.2 * strength;
    for (const joint of ARROW_JOINTS) {
      const a = here[joint], b = next[joint];
      if (!a || !b) continue;
      if (Math.hypot(b.x - a.x, b.y - a.y) < ARROW_MIN) continue;
      arrow(ctx, a, b, now / 1000);
    }
    ctx.restore();
  }

  /* The coach, however she can be drawn: photographed if her pictures are
   * here, and from the rig if they are not. The fallback is not a placeholder
   * -- it is the coach this game shipped with, exact to the same bone table --
   * so a slow first load or a missing file costs the picture and nothing else. */
  function drawTheCoach(ctx, game, bones, scales, now) {
    const rig = game && game.rig;
    const blend = rig && rig.blend !== undefined ? rig.blend : 1;

    /* Her clips first, because a film of the movement beats a dissolve between
     * two drawings of its ends. `clipAt` returns null the moment there is
     * nothing to play -- she has arrived, or this pair has no clip -- and the
     * chain of drawings below is what happens then.
     *
     * No arrows over a clip. They exist to say where a limb is going while two
     * still shapes cross-fade through each other, and a picture of her limb
     * going there says it better than an arrow drawn on top of it does. */
    const moving = rig ? clipAt(rig, blend) : null;
    if (moving) {
      drawGroundShadow(ctx, moving.frame.at.w, 1);
      drawClipFrame(ctx, moving.frame, 1);
      return;
    }

    const step = rig ? transitionKeys(rig, blend)
                     : { keys: [{ id: "", alpha: 1 }] };
    const found = step.keys.map((key) => ({ key, found: coachPicture(key.id) }))
                           .filter((item) => item.found);
    if (!found.length) {
      // No picture for this pose yet. The rig still knows the shape exactly,
      // so she is drawn rather than missing.
      drawCoach(ctx, bones, scales);
      return;
    }
    const heaviest = found.reduce((a, b) => (a.key.alpha >= b.key.alpha ? a : b));
    drawGroundShadow(ctx, heaviest.found.entry.w, 1);
    for (const item of found) drawCoachPicture(ctx, item.found, item.key.alpha);
    // The arrows go on top of her, and only while she is actually moving.
    if (rig && step.next && game.state === "playing"
        && game.phase === "transition") {
      drawMoveArrows(ctx, rig, step, now);
    }
  }

  /* == the field =================================================== */

  /* A mown clearing ringed by forest, and nothing else: no mat, no props, no
   * path, and the middle of the frame deliberately empty grass. The coach is
   * the only thing on this screen the player has to read, and every mark
   * behind her is something the eye has to reject first.
   *
   * The gradient below is what is drawn until the picture arrives, and is the
   * same dawn-to-grass it fades into, so a slow load reads as a plain sky
   * rather than as a broken screen. */
  function drawField(ctx, now) {
    if (fieldReady) {
      ctx.drawImage(field, 0, 0, W, H);
    } else {
      const sky = ctx.createLinearGradient(0, 0, 0, H);
      sky.addColorStop(0, "#b9dcea");
      sky.addColorStop(0.52, "#cfe6d9");
      sky.addColorStop(0.56, "#87ae63");
      sky.addColorStop(1, "#6f9a4e");
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, W, H);
    }

    // Warm light on the clearing, breathing slowly. Under a forty-second hold
    // this is the only thing on screen that moves at all.
    const pulse = 1 + 0.03 * Math.sin(now / 2600);
    const glow = ctx.createRadialGradient(
      COACH_X, 430, 60, COACH_X, 430, 520 * pulse);
    glow.addColorStop(0, "rgba(255,244,206,.22)");
    glow.addColorStop(0.6, "rgba(255,236,190,.07)");
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 80, W, 700);
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

  /* == the hold clock =============================================== */

  /* One number, read two ways.
   *
   * The bar across the bottom and the figure in the top right are the same
   * `hold_left` -- there is no second timer anywhere in this file, and the
   * text is written from the value the bar is drawn from, so the two cannot
   * disagree even by a frame. What they share is *interpolated*, because the
   * Pi sends state thirty times a second rounded to a tenth of a second and a
   * bar stepping in tenths is a bar that visibly ticks. So the page runs the
   * clock down itself between snapshots and is pulled back to the server's
   * number gently rather than snapped to it.
   *
   * It only runs while the player is actually in the pose, which is the rule
   * the Pi scores by: `hold_left` freezes when they come out of it. */
  let holdShown = 0, holdTotal = 0, holdPose = "", holdAt = 0;

  function holdClock(game, now) {
    const pose = game.pose || {};
    const total = Number(pose.hold) || 0;
    const target = Math.max(0, Number(pose.hold_left) || 0);
    const dt = holdAt ? Math.min(0.5, (now - holdAt) / 1000) : 0;
    holdAt = now;

    if (pose.id !== holdPose || !total) {
      // A new pose starts full. Easing into it from the last one's remainder
      // would run the bar backwards up the screen for a third of a second.
      holdPose = pose.id || "";
      holdTotal = total;
      holdShown = target;
    } else {
      if (game.holding && game.state === "playing") holdShown -= dt;
      holdShown += (target - holdShown) * Math.min(1, dt * 6);
      holdShown = Math.max(0, Math.min(total, holdShown));
    }
    return { left: holdShown, total: holdTotal || total };
  }

  /* The bar. Full width, at the very bottom, falling from 100% to 0 over the
   * same seconds the number counts down -- a shape anybody can read from the
   * back of the room without finding a two-digit number first. */
  function drawHoldBar(ctx, hold, holding) {
    const height = 18, top = H - height;
    ctx.fillStyle = "rgba(12,20,10,.42)";
    ctx.fillRect(0, top, W, height);
    const fraction = hold.total ? Math.max(0, Math.min(1, hold.left / hold.total)) : 0;
    if (fraction > 0) {
      const fill = ctx.createLinearGradient(0, top, 0, H);
      const warm = hold.left <= 5 && holding;
      fill.addColorStop(0, holding ? (warm ? "#ffd166" : "#7cf0c0") : "#9fb6c8");
      fill.addColorStop(1, holding ? (warm ? "#f5a623" : "#3fbf90") : "#6d8296");
      ctx.fillStyle = fill;
      ctx.fillRect(0, top, W * fraction, height);
    }
    ctx.fillStyle = "rgba(255,255,255,.22)";
    ctx.fillRect(0, top, W, 2);
  }

  /* == the gauges =================================================== */

  function drawGauges(ctx, game, now, hold) {
    const holding = !!game.holding;

    // Accuracy, out on the right where the coach never reaches: the widest
    // pose in the library, Half Moon, stops around x=970.
    if (game.pose?.scored !== false) {
      const accuracy = Math.max(0, Math.min(1, (game.accuracy || 0) / 100));
      smoothedAccuracy += (accuracy - smoothedAccuracy) * 0.18;
      ring(ctx, ACCURACY_X, ACCURACY_Y, 58, smoothedAccuracy,
           accuracyColour(smoothedAccuracy));
      ctx.textAlign = "center";
      ctx.fillStyle = "#fff";
      ctx.font = "700 40px Inter, system-ui, sans-serif";
      ctx.fillText(Math.round(game.accuracy || 0), ACCURACY_X, ACCURACY_Y + 14);
      ctx.font = "600 13px Inter, system-ui, sans-serif";
      ctx.fillStyle = "rgba(255,255,255,.62)";
      ctx.fillText("ACCURACY", ACCURACY_X, ACCURACY_Y + 84);
      ctx.fillStyle = holding ? GOOD : "#ffe9a8";
      ctx.font = "700 15px Inter, system-ui, sans-serif";
      ctx.fillText(holding ? "HOLDING" : "FIND THE POSE", ACCURACY_X,
                   ACCURACY_Y + 110);
    }

    // The lesson, as a hairline along the very top edge. Twenty minutes is a
    // fact the player checks twice a class; it does not need a gauge.
    const total = Number(game.duration) || 1200;
    const done = Math.max(0, Math.min(1, 1 - (game.time_left || 0) / total));
    ctx.fillStyle = "rgba(255,255,255,.2)";
    ctx.fillRect(0, 0, W, 4);
    ctx.fillStyle = "rgba(150,225,255,.9)";
    ctx.fillRect(0, 0, W * done, 4);
    ctx.textAlign = "left";

    drawHoldBar(ctx, hold, holding);
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

    const game = (payload && payload.game) || null;
    const using3d = !!window.Yoga3D?.ready;
    if (using3d) {
      if (game && game.kind === "yoga" && game.state === "playing") {
        const hold = holdClock(game, now);
        drawGauges(ctx, game, now, hold);
        const seconds = String(Math.ceil(hold.left));
        const box = el("yoga-hold-clock");
        if (box && box.textContent !== seconds) box.textContent = seconds;
        drawCountdown(ctx, game.countdown === "READY" ? "" : game.countdown);
      }
      return;
    }
    drawField(ctx, now);
    if (!game || game.kind !== "yoga") {
      drawTheCoach(ctx, null, restingBones(), {}, now);
      return;
    }

    const rig = game.rig || {};
    const to = rig.bones || restingBones();
    const from = rig.from_bones || to;
    const blend = ease(rig.blend === undefined ? 1 : rig.blend);
    let bones = blendBones(from, to, blend);
    const scales = blendScales(rig.from_scales, rig.scales, blend);

    // Breathing, once she has arrived. Two degrees on the spine and a little
    // on the arms — under a hold that lasts forty seconds a perfectly still
    // figure stops reading as a person. It moves the drawn coach only; her
    // pictures breathe by being scaled, below, which is the same idea done to
    // a photograph rather than to a skeleton.
    if (blend >= 0.999 && game.state === "playing") {
      breathPhase = now / 1000;
      const breath = Math.sin(breathPhase * 0.9);
      bones = Object.assign({}, bones);
      bones.spine += breath * 0.9;
      bones.neck += breath * 1.2;
      bones.left_upper_arm += breath * 1.1;
      bones.right_upper_arm -= breath * 1.1;
    }

    drawTheCoach(ctx, game, bones, scales, now);
    stepRipples(ctx, now);
    const hold = holdClock(game, now);
    if (game.state === "playing") {
      drawGauges(ctx, game, now, hold);
      // The figure in the top right is HTML, and it is written here rather
      // than in `applyState` so that it comes off the same interpolated clock
      // the bar is drawn from. Only when the second changes: this runs sixty
      // times a second and layout is not free.
      const seconds = String(Math.ceil(hold.left));
      const box = el("yoga-hold-clock");
      if (box && box.textContent !== seconds) box.textContent = seconds;
    }
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

  /* There is no `cameraOn` here any more, and its absence is the point.
   *
   * This screen used to show the player a live 12 fps feed of themselves,
   * because somebody two metres away being told to straighten their back
   * cannot see their own back. It was the only game on the device that showed
   * the room during play. It is gone: the class is the coach, the correction
   * and the time left, and a video of yourself in the corner is a thing to
   * watch instead of the coach. The camera itself never stops -- every number
   * on this screen is computed from it on the Pi -- it is only that none of
   * its pixels reach the screen. `/api/game/preview` is still what the start
   * sheet uses to prove somebody is standing there. */

  function applyState(state) {
    latest = state;
    const game = state.game;
    if (!game || game.kind !== "yoga") return;
    const playing = game.state === "playing";
    const pose = game.pose || {};
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
    // The preview: the finished pose, standing still, before anybody is asked
    // to move into it. The card shows its name and holds the instruction back
    // until the movement starts, which is the point of the phase.
    stage.classList.toggle("yoga-previewing",
                           active && playing && game.phase === "preview");
    const guided = pose.scored === false;
    stage.classList.toggle("yoga-guided", active && playing && guided);
    if (window.Yoga3D) window.Yoga3D.applyState(game);

    // The music runs for as long as the class does and is never restarted in
    // between - a pose change must not be audible as one.
    if (active && playing) startMusic(); else stopMusic();
    duckMusic(!!game.speaking);
    el("yoga-course").textContent = game.lesson || "";
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
    const message = playing
      ? (guided ? (pose.cue || game.feedback || "FOLLOW THE COACH")
                : (game.feedback || ""))
      : (game.framing || "");
    feedback.textContent = message;
    feedback.className = !message ? "" :
      game.wrong_side ? "urgent" :
      !game.tracked ? "urgent" :
      game.holding && (game.accuracy || 0) >= 86 ? "praise" : "";

    const card = el("yoga-result");
    if (game.last && !guided) {
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
      // The frames this pose will need on the way out, and the ones the next
      // pose will need on the way in. Asked for a whole hold in advance.
      preload(pose.id);
      preload(game.next_pose);
      chime(392, 0.5, 0.05);
      ripples.push({ x: COACH_X, y: 420, at: performance.now() });
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

  /* == the music ==================================================== */

  /* Twenty minutes of calm, synthesised here rather than played from a file.
   *
   * Not for cleverness: a twenty-minute recording is a twenty-minute download
   * on a device fed by `scp`, it loops audibly however carefully it is cut,
   * and it would be the first sound in this repository that somebody else
   * owns. What plays instead is a slow pad on an A minor pentatonic
   * progression with an occasional bell over it — endless by construction, so
   * it never restarts and never has a seam, which is exactly what "do not stop
   * and start the music between poses" asks for.
   *
   * It starts when the class starts and stops when the class stops, and
   * nothing in between touches it. */
  const CHORDS = [
    [110.00, 164.81, 220.00],   // A2  E3  A3
    [ 87.31, 130.81, 174.61],   // F2  C3  F3
    [ 98.00, 146.83, 196.00],   // G2  D3  G3
    [130.81, 196.00, 261.63],   // C3  G3  C4
  ];
  const BELLS = [440.00, 523.25, 587.33, 659.25, 783.99];

  //: Chord length, and how long one fades into the next. Long, and overlapping,
  //: because the point is that nobody can hear where one ends.
  const CHORD_S = 15, CHORD_FADE = 6;

  //: The ceiling. Low enough that a spoken instruction sits on top of it
  //: without either of them being turned up.
  const MUSIC_GAIN = 0.055;
  //: What it drops to while the coach is speaking, and how long the duck and
  //: the recovery take. Down fast enough not to talk over her first word, back
  //: slowly enough that the return is not itself an event.
  const DUCK_GAIN = 0.22, DUCK_S = 0.25, UNDUCK_S = 1.1;

  let music = null;

  function startMusic() {
    const ctx = context();
    if (!ctx || music) return;
    const out = ctx.createGain();
    out.connect(ctx.destination);

    /* Four seconds up from silence, scheduled from *now* — and again from
     * whenever "now" turns out to be.
     *
     * A browser that has not seen a real touch yet keeps its audio clock at
     * zero, so a fade scheduled against `currentTime` is a fade scheduled in
     * the past: the moment the context resumes it has already finished, and
     * the class opens with the pad at full volume instead of arriving. The
     * kiosk normally has been touched — the level sheet is a button — but a
     * class started by voice has not. */
    const fadeIn = () => {
      const at = ctx.currentTime;
      out.gain.cancelScheduledValues(at);
      out.gain.setValueAtTime(0.0001, at);
      out.gain.linearRampToValueAtTime(MUSIC_GAIN, at + 4);
    };
    fadeIn();
    if (ctx.state !== "running") ctx.resume().then(fadeIn).catch(() => {});
    music = { out, voices: [], timer: null, bell: null, ducked: false,
              level: 1, chord: -1 };

    // Three voices, retuned rather than restarted. A note that stops and
    // starts is a note somebody hears begin.
    for (let i = 0; i < 3; i += 1) {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      const filter = ctx.createBiquadFilter();
      oscillator.type = "sine";
      oscillator.detune.value = (i - 1) * 6;
      filter.type = "lowpass";
      filter.frequency.value = 900;
      gain.gain.value = [0.5, 0.34, 0.22][i];
      oscillator.connect(filter).connect(gain).connect(out);
      oscillator.start();
      music.voices.push({ oscillator, gain });
    }

    const nextChord = () => {
      if (!music) return;
      music.chord = (music.chord + 1) % CHORDS.length;
      const chord = CHORDS[music.chord];
      const at = ctx.currentTime;
      music.voices.forEach((voice, i) => {
        // Glided, not jumped: the pad slides between chords over six seconds.
        voice.oscillator.frequency.cancelScheduledValues(at);
        voice.oscillator.frequency.setValueAtTime(
          voice.oscillator.frequency.value || chord[i], at);
        voice.oscillator.frequency.linearRampToValueAtTime(
          chord[i], at + CHORD_FADE);
      });
      music.timer = window.setTimeout(nextChord, CHORD_S * 1000);
    };
    nextChord();

    const nextBell = () => {
      if (!music) return;
      const note = BELLS[Math.floor(Math.random() * BELLS.length)];
      const at = ctx.currentTime;
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(note, at);
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.13, at + 0.08);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 3.2);
      oscillator.connect(gain).connect(music.out);
      oscillator.start(at);
      oscillator.stop(at + 3.4);
      music.bell = window.setTimeout(nextBell, (5 + Math.random() * 7) * 1000);
    };
    music.bell = window.setTimeout(nextBell, 6000);
  }

  function stopMusic() {
    if (!music) return;
    const ctx = context();
    const dying = music;
    music = null;
    window.clearTimeout(dying.timer);
    window.clearTimeout(dying.bell);
    if (!ctx) return;
    // Faded, not cut. Two seconds, which is under the shortest gap there is
    // between a class ending and anything else being started.
    const at = ctx.currentTime;
    dying.out.gain.cancelScheduledValues(at);
    dying.out.gain.setValueAtTime(dying.out.gain.value, at);
    dying.out.gain.linearRampToValueAtTime(0.0001, at + 2);
    window.setTimeout(() => {
      for (const voice of dying.voices) {
        try { voice.oscillator.stop(); } catch (err) { /* already stopped */ }
      }
      try { dying.out.disconnect(); } catch (err) { /* already gone */ }
    }, 2200);
  }

  /* Under her voice and back up again. Called with what the Pi believes about
   * whether the coach is talking, which is an estimate from the length of the
   * line — a duck does not need the truth to the millisecond, and asking piper
   * for it would mean asking on every frame. */
  function duckMusic(under) {
    if (!music || under === music.ducked) return;
    const ctx = context();
    if (!ctx) return;
    music.ducked = under;
    const at = ctx.currentTime;
    music.out.gain.cancelScheduledValues(at);
    music.out.gain.setValueAtTime(music.out.gain.value, at);
    music.out.gain.linearRampToValueAtTime(
      under ? MUSIC_GAIN * DUCK_GAIN : MUSIC_GAIN,
      at + (under ? DUCK_S : UNDUCK_S));
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
        ripples.push({ x: ACCURACY_X, y: ACCURACY_Y, at: performance.now() });
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
    + "including your feet. Follow the coach — she faces you, so copy her "
    + "as you see her.";

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
    if (window.Yoga3D) window.Yoga3D.setActive(active);
    if (!active) {
      stage.classList.remove("yoga-playing");
      stopMusic();
      return;
    }
    resetRound();
    el("start-title").textContent = "Yoga Coach";
    setStartCopy(YOGA_COPY);
    loadCourses();
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
    holdShown = 0;
    holdTotal = 0;
    holdPose = "";
    holdAt = 0;
    ripples = [];
  }

  function stop() {
    const was = active;
    active = false;
    stopMusic();
    const stage = el("game-stage");
    stage.classList.remove("yoga-active", "yoga-playing", "yoga-guided");
    if (window.Yoga3D) window.Yoga3D.setActive(false);
    // Only put the shared wording back if it was ours to change. Leaving a
    // game and immediately opening another one calls stop() after the new
    // game's onOpen has already set its own copy.
    if (was) { setStartCopy(SHARED_COPY); overHeading(SHARED_OVER); }
    resetRound();
  }

  /* Choosing one of the twenty-one courses. `gameChoice` rather than
   * `gameCommand` because this
   * sheet is on screen for the ~1.8 s the Pi takes to open the game, and a tap
   * in that window has nothing to talk to yet -- see the queue in index.html.
   * The level is kept and sent when there is a session.
   *
   * The sheet only advances when the Pi has actually taken the choice.
   * Advancing on the click and letting the next state message flip it back is
   * what made this look like it needed choosing twice. */
  let courseData = null;
  let shownLevel = "beginner";
  async function loadCourses() {
    if (!courseData) {
      const response = await fetch("/assets/yoga/v3/rigdata.json");
      courseData = (await response.json()).courses || {};
    }
    renderCourses(shownLevel);
  }
  function renderCourses(level) {
    shownLevel = level;
    for (const tab of document.querySelectorAll("[data-yoga-level]")) {
      tab.classList.toggle("selected", tab.dataset.yogaLevel === level);
    }
    const courses = Object.entries(courseData || {})
      .filter(([, course]) => course.level === level)
      .sort(([a], [b]) => a.localeCompare(b));
    el("yoga-course-grid").innerHTML = courses.map(([id, course]) => {
      const seconds = course.steps.reduce((sum, step) =>
        sum + Number(step.hold || 0) + Number(step.transition || 0), 4);
      const guided = course.steps.filter((step) => step.scored === false).length;
      return `<button class="yoga-course" data-yoga-course="${id}">` +
        `<strong>${course.name}</strong>` +
        `<small>${Math.round(seconds / 60)} min · ${course.steps.length} poses` +
        `${guided ? ` · ${guided} guided` : ""}</small></button>`;
    }).join("");
    for (const button of document.querySelectorAll("[data-yoga-course]")) {
      button.addEventListener("click", () => {
        for (const peer of document.querySelectorAll("[data-yoga-course]")) {
          peer.classList.remove("selected");
        }
        button.classList.add("selected");
        gameChoice(`course-${button.dataset.yogaCourse}`).then((answer) => {
          if (answer.ok) showSheet("start");
        });
      });
    }
  }
  for (const tab of document.querySelectorAll("[data-yoga-level]")) {
    tab.addEventListener("click", () => renderCourses(tab.dataset.yogaLevel));
  }
  el("yoga-back").addEventListener("click", () => show("games"));
  el("yoga-review").addEventListener("click", () => {
    window.location.href = "/assets/yoga/v3/stage.html?review=1";
  });

  window.YogaUI = {
    onOpen, stop, resetRound, applyState, handleEvent, render,
    // Exposed so the rig can be checked against the Pi's copy of it from
    // outside the browser. The coach being drawn in a pose the scorer is not
    // asking for is the one bug in this game that nothing on screen would
    // report, so it is worth a seam to test through.
    rig: { RIG, kinematics, blendBones, lerpAngle },
    // The same seam for the clips. Which frame of which clip a blend lands on
    // is arithmetic with no picture in it, and getting it wrong is a coach who
    // plays a movement backwards or stops halfway -- visible on the Pi and
    // nowhere else, unless it can be asked the question directly.
    clips: { source: clipSource, at: clipAt, index: clipIndex,
             manifest: () => clipManifest },
    // The music is a graph of oscillators with no visible output, so the only
    // way to check that it is playing, that it ducks under the coach and that
    // it is never restarted between poses is to be able to ask it.
    audio: () => (music ? { playing: true, gain: music.out.gain.value,
                            ducked: music.ducked, chord: music.chord } : null),
  };
})();
