/* AIPI5 Boxing renderer.
 *
 * Rules stay on the Pi beside the pose stream.  This file renders snapshots at
 * display rate, synthesises bounded one-shot audio, and owns no camera or pose
 * model.  Classified motion selects registered full-body anime poses while
 * pose capture remains live even when world simulation is slowed.
 *
 * **The view is the player's own eyes.**  It was over their shoulder until the
 * first-person change, and the difference is not a camera position — it is
 * which things exist.  Three of them:
 *
 *   The player has no body.  Nothing of them is drawn but two gloves coming up
 *   from the bottom of the frame, so the rear-view fighter, his head, his
 *   shadow and the whole 21-pose player matrix are simply not used any more.
 *   The files stay on disk; nothing asks for them.
 *
 *   Their head is the camera.  Leaning, ducking and dodging used to move a
 *   figure across a still arena; now they move the arena, because that is what
 *   moving your head does.  `headCamera` is the whole of it and it is applied
 *   to everything except the gloves — which are attached to the same head and
 *   so must not move relative to it.
 *
 *   The opponent is close enough to hit.  Drawn about half again the size it
 *   was and low in the frame, because a boxer you are actually facing fills
 *   your view rather than standing in the middle distance.
 */
(function () {
  "use strict";

  const W = 1280, H = 800;
  const stage = () => gameEl("game-stage");
  let active = false;
  let latest = null;
  let effects = [];
  let messageUntil = 0;
  let messagePriority = 0;
  let heartbeat = null;
  let previousSlow = false;
  let previousCountdown = "";
  let lastMode = "";
  let lastRenderFps = 0;
  let opponentReaction = null;
  let playerReaction = null;
  let crowdEnergy = 0;
  let shakeUntil = 0;
  let shakePower = 0;
  //: When the red wash over the whole view fades out. First person's only way
  //: of saying "that one landed on *you*" — there is no body on screen to show
  //: recoiling, so the view itself has to take the punch.
  let hitFlashUntil = 0;
  let hitFlashPower = 0;
  let laneMotion = {
    player: { value: 0, at: 0 },
    opponent: { value: 0, at: 0 },
  };
  let opponentScreenX = 640;

  const arenaImage = new Image();
  let arenaReady = false;
  arenaImage.decoding = "async";
  arenaImage.onload = () => { arenaReady = true; };
  arenaImage.src = "/assets/boxing/arena-anime-v2.png";

  // The player's own glove and forearm, cut out of the approved pose sheet by
  // `scripts/build_boxing_first_person.py` — see that file for why the arm is
  // two pieces rather than one. The geometry it measured comes with them; the
  // fallback below is only for the first frames before the manifest lands.
  const fpImages = { forearm: new Image(), glove: new Image() };
  const fpReady = { forearm: false, glove: false };
  let fpRig = {
    forearm: { width: 360, height: 214, elbow: [0, 127], wrist: [360, 89] },
    glove: { width: 196, height: 166, wrist: [0, 88] },
  };
  for (const part of ["forearm", "glove"]) {
    fpImages[part].decoding = "async";
    fpImages[part].onload = () => { fpReady[part] = true; };
    fpImages[part].src = `/assets/boxing/fp/${part}.webp`;
  }
  fetch("/assets/boxing/fp/manifest.json")
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => { if (data && data.glove) fpRig = data; })
    .catch(() => {});

  // Only the opponent has a body on screen now. The red rear-view torso, the
  // sixteen baked player damage bodies and the twenty-one-pose player matrix
  // are all still on disk and not one of them is requested: the player is
  // behind the camera. Kept rather than deleted because between them they are
  // the whole of the third-person view, and nothing needs them gone.
  const opponentBodyImage = new Image();
  let opponentBodyReady = false;
  opponentBodyImage.decoding = "async";
  opponentBodyImage.onload = () => { opponentBodyReady = true; };
  opponentBodyImage.src = "/assets/boxing/opponent-blue-torso.png";

  const DAMAGE_ASSET_VERSION = "20260816-1";
  const DAMAGE_CACHE_LIMIT = 12;
  const DAMAGE_ORDERS = {
    opponent: ["left_eye", "right_eye", "left_shoulder", "right_shoulder"],
  };
  const damageCache = { opponent: new Map() };
  const damageBodies = {
    opponent: { key: "", current: null, previous: null, changedAt: 0 },
  };

  function resetDamageBodies() {
    for (const kind of Object.keys(damageBodies)) {
      damageBodies[kind] = { key: "", current: null, previous: null, changedAt: 0 };
    }
  }

  function damageKey(kind, fighter) {
    const matrix = Object.assign({}, fighter && fighter.damage_matrix || {});
    for (const injury of fighter && fighter.injuries || []) {
      matrix[injury.zone] = injury.level;
    }
    return DAMAGE_ORDERS[kind].map((zone) =>
      Math.max(0, Math.min(3, Number(matrix[zone]) || 0))).join("");
  }

  function requestDamageBody(kind, key, now) {
    const cache = damageCache[kind];
    let entry = cache.get(key);
    if (entry) {
      entry.usedAt = now;
      return entry;
    }
    const image = new Image();
    entry = { image, ready: false, usedAt: now };
    image.decoding = "async";
    image.onload = () => { entry.ready = true; };
    image.src = `/assets/boxing/damage/${kind}/${key}.webp?v=${DAMAGE_ASSET_VERSION}`;
    cache.set(key, entry);

    if (cache.size > DAMAGE_CACHE_LIMIT) {
      const protectedImages = new Set([
        damageBodies[kind].current, damageBodies[kind].previous,
      ]);
      const removable = [...cache.entries()]
        .filter(([, candidate]) => !protectedImages.has(candidate.image))
        .sort((a, b) => a[1].usedAt - b[1].usedAt);
      if (removable.length) cache.delete(removable[0][0]);
    }
    return entry;
  }

  function selectDamageBody(kind, fighter, fallback, now) {
    const key = damageKey(kind, fighter || {});
    const entry = requestDamageBody(kind, key, now);
    const state = damageBodies[kind];
    if (!state.current) {
      state.current = fallback;
      state.key = "fallback";
    }
    if (entry.ready && state.key !== key) {
      state.previous = state.current;
      state.current = entry.image;
      state.key = key;
      state.changedAt = now;
    }
    const blend = state.previous ? clamp01((now - state.changedAt) / 180) : 1;
    if (blend >= 1) state.previous = null;
    return { current: state.current, previous: state.previous, blend };
  }

  function drawDamageBody(ctx, kind, fighter, fallback, now, destination, alpha) {
    const selected = selectDamageBody(kind, fighter, fallback, now);
    const drawOne = (image, opacity) => {
      if (!image || !image.naturalWidth || opacity <= 0) return;
      ctx.save();
      ctx.globalAlpha *= alpha * opacity;
      if (image === fallback) {
        const crop = kind === "opponent"
          ? [240, 18, 650, 1384] : [280, 55, 570, 1290];
        ctx.drawImage(image, ...crop, ...destination);
      } else {
        ctx.drawImage(image, 0, 0, image.naturalWidth, image.naturalHeight,
                      ...destination);
      }
      ctx.restore();
    };
    drawOne(selected.previous, 1 - selected.blend);
    drawOne(selected.current, selected.blend);
  }

  // Every pose/damage combination is a complete generated fighter image.
  // Runtime drawing only chooses a matrix cell and crossfades it; it never
  // constructs arms or paints an injury over a different base pose.
  const POSE_ASSET_VERSION = "20260816-1";
  const POSE_CACHE_LIMIT = 36;
  const poseCache = { opponent: new Map() };
  const poseBodies = {
    opponent: { id: "", current: null, previous: null, changedAt: 0 },
  };

  function resetPoseBodies() {
    for (const kind of Object.keys(poseBodies)) {
      poseBodies[kind] = { id: "", current: null, previous: null, changedAt: 0 };
    }
  }

  function requestPoseBody(kind, pose, key, now) {
    const id = `${pose}/${key}`;
    const cache = poseCache[kind];
    let entry = cache.get(id);
    if (entry) {
      entry.usedAt = now;
      return entry;
    }
    const image = new Image();
    entry = { image, ready: false, usedAt: now };
    image.decoding = "async";
    image.onload = () => { entry.ready = true; };
    image.src = `/assets/boxing/poses/matrix/${kind}/${pose}/${key}.webp?v=${POSE_ASSET_VERSION}`;
    cache.set(id, entry);
    if (cache.size > POSE_CACHE_LIMIT) {
      const protectedImages = new Set([
        poseBodies[kind].current, poseBodies[kind].previous,
      ]);
      const removable = [...cache.entries()]
        .filter(([, candidate]) => !protectedImages.has(candidate.image))
        .sort((a, b) => a[1].usedAt - b[1].usedAt);
      if (removable.length) cache.delete(removable[0][0]);
    }
    return entry;
  }

  function drawPoseBody(ctx, kind, pose, fighter, now, destination, alpha) {
    const key = damageKey(kind, fighter || {});
    const id = `${pose}/${key}`;
    const entry = requestPoseBody(kind, pose, key, now);
    const state = poseBodies[kind];
    if (entry.ready && state.id !== id) {
      state.previous = state.current;
      state.current = entry.image;
      state.id = id;
      state.changedAt = now;
    }
    if (!state.current) return false;
    const blend = state.previous ? clamp01((now - state.changedAt) / 140) : 1;
    if (blend >= 1) state.previous = null;
    const drawOne = (image, opacity) => {
      if (!image || !image.naturalWidth || opacity <= 0) return;
      ctx.save();
      ctx.globalAlpha *= alpha * opacity;
      ctx.drawImage(image, 0, 0, image.naturalWidth, image.naturalHeight,
                    ...destination);
      ctx.restore();
    };
    drawOne(state.previous, 1 - blend);
    drawOne(state.current, blend);
    return true;
  }

  const crowd = Array.from({ length: 112 }, (_, i) => ({
    x: 18 + ((i * 83) % 1240),
    y: 188 + ((i * 47) % 178),
    r: 3 + (i % 5),
    hue: 190 + (i * 31) % 65,
    phase: (i * 0.73) % 6.28,
  }));

  function onOpen(id) {
    active = id === "boxing";
    stage().classList.toggle("boxing-active", active);
    if (!active) {
      restoreSharedCopy();
      return;
    }
    resetRound();
    gameEl("boxing-hud").style.display = "none";
    gameEl("game-pause").style.display = "none";
    gameEl("start-title").textContent = "Boxing";
    setStartCopy("Choose a mode, then stand where the camera can see your full upper body.");
    showSheet("mode");
  }

  function setStartCopy(copy) {
    const instruction = gameEl("start-instruction");
    if (instruction && instruction.firstChild) instruction.firstChild.nodeValue = copy;
  }

  function restoreSharedCopy() {
    stage().classList.remove("boxing-active");
    setStartCopy("Stand where the camera can see your full upper body.");
    gameEl("over-detail").style.display = "";
    gameEl("over-score").parentElement.parentElement.style.display = "";
    gameEl("game-debug").classList.remove("on");
    gameEl("boxing-hud").style.display = "";
    gameEl("game-pause").style.display = "";
  }

  function stop() {
    clearHeartbeat();
    latest = null;
    effects = [];
    previousSlow = false;
    previousCountdown = "";
    active = false;
    restoreSharedCopy();
  }

  function resetRound() {
    effects = [];
    messageUntil = 0;
    messagePriority = 0;
    previousSlow = false;
    previousCountdown = "";
    opponentReaction = null;
    playerReaction = null;
    crowdEnergy = 0;
    shakeUntil = 0;
    hitFlashUntil = 0;
    resetFirstPersonHands();
    laneMotion = {
      player: { value: 0, at: 0 },
      opponent: { value: 0, at: 0 },
    };
    opponentScreenX = 640;
    resetDamageBodies();
    resetPoseBodies();
    clearHeartbeat();
  }

  function applyState(payload) {
    latest = payload;
    const game = payload.game || {};
    if (game.kind !== "boxing") return;
    lastMode = game.mode || lastMode;
    gameEl("boxing-hud").style.display = game.mode && game.state === "playing" ? "block" : "none";
    gameEl("game-pause").style.display = game.state === "playing" ? "flex" : "none";

    if (!game.mode) {
      showSheet("mode");
    } else {
      const title = game.mode === "training" ? "Boxing Training" : "Fight Opponent";
      gameEl("start-title").textContent = title;
      setStartCopy(game.mode === "training"
        ? "Train for 90 seconds. Cross both arms into an X when you are ready."
        : `Difficulty: ${capitalise(game.difficulty)}. Cross both arms into an X when you are ready.`);
      if (game.state === "ready") showSheet("start");
      else if (game.state === "paused") showSheet("paused");
      else if (game.state === "over") showSheet("over");
      else showSheet("");
    }

    if (game.state === "ready") {
      const pose = payload.pose;
      const ready = pose && pose.ready;
      const detected = gameEl("game-detected");
      detected.textContent = ready ? "Player detected ✓"
        : (pose && pose.advice) || "Looking for a player…";
      detected.className = ready ? "yes" : "no";
      gameEl("start-play").disabled = !ready;
    }

    gameEl("start-round-copy").textContent = game.mode === "training" ? " · 90 SEC" : "";
    gameEl("boxing-score").textContent = `SCORE ${game.score || 0}`;
    gameEl("boxing-combo").textContent =
      (game.combo || 0) > 1 ? `${game.combo}× COMBO` : "";
    gameEl("boxing-timer").textContent = clock(game.time_left || 0);
    updateHealth("boxing-opponent-health", gameEl("boxing-opponent-value"), game.opponent);
    updateHealth("boxing-player-health", gameEl("boxing-player-value"), game.player);

    const training = game.mode === "training";
    gameEl("boxing-opponent-health").style.display = training ? "none" : "block";
    gameEl("boxing-player-health").style.display = training ? "none" : "block";
    gameEl("boxing-timer").style.display = game.mode ? "block" : "none";

    const prompt = game.prompt;
    const promptEl = gameEl("boxing-prompt");
    if (prompt && game.state === "playing") {
      promptEl.textContent = promptLabel(prompt);
      promptEl.classList.add("on");
    } else {
      promptEl.classList.remove("on");
    }

    if (game.countdown && game.countdown !== previousCountdown) {
      previousCountdown = game.countdown;
      showMessage(game.countdown, 1050, game.countdown === "FIGHT!" ? "parry" : "", 4);
    }
    if (!game.countdown) previousCountdown = "";

    const slow = !!game.slow_motion;
    gameEl("boxing-slow").classList.toggle("on", slow);
    if (slow && !previousSlow) startHeartbeat();
    if (!slow && previousSlow) clearHeartbeat();
    previousSlow = slow;

    for (const button of document.querySelectorAll("[data-boxing-difficulty]")) {
      button.classList.toggle("selected", button.dataset.boxingDifficulty === game.difficulty);
    }
    if (game.state === "over") fillResults(game);
    drawDebug(payload);
  }

  function clock(seconds) {
    const safe = Math.max(0, Math.ceil(Number(seconds) || 0));
    return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
  }

  function capitalise(value) {
    return String(value || "").replace(/(^|_)(.)/g, (_, gap, char) =>
      (gap ? " " : "") + char.toUpperCase());
  }

  function promptLabel(prompt) {
    const labels = {
      left_punch: "LEFT PUNCH", right_punch: "RIGHT PUNCH",
      left_hook: "LEFT HOOK", right_hook: "RIGHT HOOK",
      dodge_left: "DODGE LEFT", dodge_right: "DODGE RIGHT",
      duck: "DUCK", lean_back: "LEAN BACK", block: "BLOCK", parry: "PARRY",
    };
    const target = prompt.target ? ` · ${prompt.target.toUpperCase()}` : "";
    return `${labels[prompt.kind] || capitalise(prompt.kind)}${target}`;
  }

  function updateHealth(id, valueEl, fighter) {
    fighter = fighter || { hp: 100, display_hp: 100, delayed_hp: 100 };
    const maxHp = Math.max(1, Number(fighter.max_hp) || 100);
    const holder = gameEl(id);
    const main = holder.querySelector(".boxing-health-main");
    const delay = holder.querySelector(".boxing-health-delay");
    main.style.transform = `scaleX(${Math.max(0, fighter.display_hp || 0) / maxHp})`;
    delay.style.transform = `scaleX(${Math.max(0, fighter.delayed_hp || 0) / maxHp})`;
    valueEl.textContent = `${fighter.hp ?? maxHp} / ${maxHp}`;
  }

  function fillResults(game) {
    const heading = game.mode === "training" ? "TRAINING COMPLETE" :
      game.result === "win" ? "YOU WIN" : game.result === "lose" ? "YOU LOSE" : "DRAW";
    gameEl("sheet-over").querySelector("h2").textContent = heading;
    const stats = game.stats || {};
    const values = game.mode === "training" ? [
      [game.score || 0, "SCORE"], [`${stats.accuracy || 0}%`, "ACCURACY"],
      [stats.successful_hits || 0, "HITS"], [stats.missed_punches || 0, "MISSES"],
      [stats.successful_dodges || 0, "DODGES"], [stats.successful_blocks || 0, "BLOCKS"],
      [stats.successful_parries || 0, "PARRIES"], [stats.best_combo || 0, "BEST COMBO"],
      [stats.average_reaction_ms || 0, "AVG REACTION MS"], [stats.best_reaction_ms || 0, "BEST REACTION MS"],
    ] : [
      [game.score || 0, "SCORE"], [`${stats.accuracy || 0}%`, "ACCURACY"],
      [stats.successful_hits || 0, "HITS"], [stats.successful_dodges || 0, "DODGES"],
      [stats.successful_blocks || 0, "BLOCKS"], [stats.successful_parries || 0, "PARRIES"],
      [stats.best_combo || 0, "BEST COMBO"], [game.opponent ? game.opponent.hp : 0, "OPPONENT HP"],
      [game.player ? game.player.hp : 0, "PLAYER HP"], [capitalise(game.difficulty), "DIFFICULTY"],
    ];
    gameEl("boxing-results").innerHTML = values.map(([value, label]) =>
      `<div class="boxing-stat"><b>${value}</b><span>${label}</span></div>`).join("");
  }

  function showMessage(text, ms = 850, className = "", priority = 1) {
    const now = performance.now();
    if (now < messageUntil && priority < messagePriority) return;
    const node = gameEl("boxing-message");
    node.textContent = text;
    node.className = `on ${className}`.trim();
    messageUntil = now + ms;
    messagePriority = priority;
  }

  function handleEvent(event) {
    const name = event.name;
    if (name === "training-success") {
      showMessage(`+${event.points}`, 650, "", 1);
      burst(640, 330, "#65efff", 14, 1.0);
    } else if (name === "training-miss") {
      showMessage("MISS", 520, "", 1);
    } else if (name === "perfect-parry") {
      showMessage("PERFECT PARRY", 1500, "parry action", 6);
      opponentReaction = reaction("parry", event, 980);
      crowdEnergy = Math.max(crowdEnergy, 1);
      addImpact(opponentScreenX, 335, "#7df7ff", "parry", 1.8);
      burst(opponentScreenX, 335, "#7df7ff", 42, 1.8);
    } else if (name === "player-hit") {
      const y = event.target === "head" ? 305 : 470;
      opponentReaction = reaction(event.target === "head" ? "head" : "body", event,
        event.hook ? 560 : 390);
      crowdEnergy = Math.max(crowdEnergy, event.target === "head" ? .72 : .38);
      burst(opponentScreenX + (event.side === "left" ? 55 : -55), y,
            event.hook ? "#ffd166" : "#ffffff", event.hook ? 24 : 15, 1.1);
      addImpact(opponentScreenX + (event.side === "left" ? 42 : -42), y,
                event.hook ? "#ffd166" : "#ffffff",
                event.target === "head" ? "star" : "ring", event.hook ? 1.35 : 1);
      shake(event.hook || event.target === "head" ? 9 : 4, event.hook ? 220 : 130);
    } else if (name === "opponent-hit") {
      // **Taken on the camera, not on a body.** The player has no figure on
      // screen to flinch, so a punch that lands on them is a wash of red over
      // the whole view and a harder shake — and the sparks happen where the
      // glove arrived, which in this view is in front of the eyes rather than
      // at some position along the bottom of the picture.
      playerReaction = reaction(event.target === "head" ? "head" : "body", event, 430);
      hitFlash(event.target === "head" ? 1 : .62,
               event.target === "head" ? 420 : 320);
      const inbound = 640 + (event.side === "left" ? 120 : -120);
      burst(inbound, event.target === "head" ? 330 : 470, "#ff665f", 22, 1.35);
      addImpact(inbound, event.target === "head" ? 330 : 470, "#ff665f", "ring", 1.3);
      shake(event.target === "head" ? 16 : 10, 260);
    } else if (name === "player-block" || name === "opponent-block") {
      showMessage("BLOCK", 520, "action", 2);
      const blockX = name === "opponent-block" ? opponentScreenX : 640;
      burst(blockX, 500, "#85c7ff", 12, .7);
      addImpact(blockX, 500, "#85c7ff", "shield", .9);
      if (name === "opponent-block") opponentReaction = reaction("block", event, 320);
      else playerReaction = reaction("block", event, 320);
    } else if (name === "player-dodge" || name === "opponent-dodge") {
      showMessage("DODGE!", 480, "action", 1);
      if (name === "opponent-dodge") opponentReaction = reaction("dodge", event, 440);
    } else if (name === "knockout") {
      showMessage("KO", 1900, "ko", 8);
      const koX = event.fighter === "player" ? 640 : opponentScreenX;
      if (event.fighter === "player") hitFlash(1.35, 1400);
      burst(koX, 300, "#ffc34d", 54, 2.0);
      crowdEnergy = 1.5;
      if (event.fighter === "player") playerReaction = reaction("ko", event, 2200);
      else opponentReaction = reaction("ko", event, 2200);
      addImpact(koX, 300, "#ffc34d", "star", 2.1);
      shake(18, 520);
    } else if (name === "boxing-over") {
      const words = event.result === "win" ? "YOU WIN" :
        event.result === "lose" ? "YOU LOSE" : "TIME";
      showMessage(words, 1300, event.result === "win" ? "parry" : "", 7);
    }
    playSound(event);
  }

  function reaction(kind, event, duration) {
    const started = performance.now();
    return { kind, side: event.side || "", started,
             until: started + duration, duration };
  }

  function hitFlash(power, duration) {
    const now = performance.now();
    hitFlashPower = Math.max(hitFlashPower * (now < hitFlashUntil ? 1 : 0), power);
    hitFlashUntil = Math.max(hitFlashUntil, now + duration);
  }

  function shake(power, duration) {
    shakePower = Math.max(shakePower, power);
    shakeUntil = Math.max(shakeUntil, performance.now() + duration);
  }

  function addImpact(x, y, colour, kind, power) {
    effects.push({ impact: true, x, y, colour, kind, power,
                   age: 0, life: kind === "parry" ? .55 : .32 });
  }

  function burst(x, y, colour, count, power) {
    const total = Math.min(count, 60);
    for (let i = 0; i < total; i++) {
      const angle = (i / total) * Math.PI * 2 + (i % 3) * .17;
      const speed = (80 + (i * 47) % 170) * power;
      effects.push({ x, y, vx: Math.cos(angle) * speed,
                     vy: Math.sin(angle) * speed, age: 0,
                     life: .35 + (i % 5) * .055, colour,
                     r: 2 + (i % 4) });
    }
    if (effects.length > 100) effects = effects.slice(-100);
  }

  function playSound(event) {
    try {
      const ctx = gameAudio();
      if (!ctx) return;
      const now = ctx.currentTime;
      const name = event.name;
      if (name === "fight-bell") {
        [0, .13].forEach(offset => tone(ctx, now + offset,
          { freq: 1180, to: 760, ms: 420, type: "triangle", gain: .16 }));
      } else if (name === "player-hit") {
        // The glove's jab/heavy voice and the target's body/head impact are
        // separate layers, so every requested combat event remains distinct.
        sweptNoise(ctx, now, event.hook ? 2200 : 5200,
                   event.hook ? 230 : 1100, event.hook ? .24 : .10,
                   event.hook ? .18 : .11);
        tone(ctx, now, { freq: event.target === "head" ? 210 : 132, to: 48,
                         ms: event.target === "head" ? 250 : 310,
                         type: "sawtooth", gain: event.target === "head" ? .23 : .17 });
      } else if (name === "opponent-attack") {
        const heavy = String(event.kind || "").includes("hook");
        sweptNoise(ctx, now, heavy ? 2800 : 5000, heavy ? 260 : 900,
                   heavy ? .22 : .12, heavy ? .13 : .08);
      } else if (name === "opponent-hit") {
        sweptNoise(ctx, now, 1200, 110, .25, .2);
        tone(ctx, now, { freq: 120, to: 42, ms: 320, type: "sawtooth", gain: .22 });
      } else if (name.includes("block")) {
        sweptNoise(ctx, now, 6500, 900, .09, .12);
        tone(ctx, now, { freq: 720, to: 360, ms: 100, type: "square", gain: .11 });
      } else if (name === "perfect-parry") {
        sweptNoise(ctx, now, 1200, 9000, .18, .18);
        [740, 1110, 1480].forEach((freq, i) => tone(ctx, now + i * .045,
          { freq, to: freq * 1.08, ms: 320, type: "triangle", gain: .12 }));
      } else if (name.includes("dodge")) {
        sweptNoise(ctx, now, 4000, 500, .14, .1);
      } else if (name === "knockout") {
        sweptNoise(ctx, now, 5000, 80, .55, .28);
        tone(ctx, now, { freq: 120, to: 28, ms: 900, type: "sawtooth", gain: .25 });
      } else if (name === "knockdown") {
        sweptNoise(ctx, now, 900, 70, .32, .18);
        tone(ctx, now, { freq: 92, to: 35, ms: 520, type: "sine", gain: .2 });
      } else if (name === "boxing-over" && event.result === "win") {
        [523, 659, 784, 1047].forEach((freq, i) => tone(ctx, now + i * .1,
          { freq, to: freq, ms: 460, type: "triangle", gain: .13 }));
      } else if (name === "boxing-over") {
        [392, 330, 220].forEach((freq, i) => tone(ctx, now + i * .15,
          { freq, to: freq, ms: 280, type: "sine", gain: .13 }));
      } else if (name === "training-success") {
        tone(ctx, now, { freq: 680, to: 1020, ms: 100, type: "square", gain: .08 });
      } else if (name === "crowd-reaction" || name === "crowd-cheer") {
        const cheer = name === "crowd-cheer";
        sweptNoise(ctx, now, cheer ? 900 : 650, cheer ? 3200 : 1800,
                   cheer ? .75 : .34, cheer ? .08 : .045);
        if (cheer) tone(ctx, now, { freq: 330, to: 660, ms: 620,
                                    type: "triangle", gain: .07 });
      }
    } catch (_) { /* no audio output means a quiet game */ }
  }

  function startHeartbeat() {
    clearHeartbeat();
    const beat = () => {
      try {
        const ctx = gameAudio();
        const at = ctx.currentTime;
        tone(ctx, at, { freq: 72, to: 46, ms: 170, type: "sine", gain: .16 });
        tone(ctx, at + .19, { freq: 65, to: 40, ms: 130, type: "sine", gain: .11 });
      } catch (_) {}
    };
    beat();
    heartbeat = setInterval(beat, 760);
  }

  function clearHeartbeat() {
    if (heartbeat !== null) clearInterval(heartbeat);
    heartbeat = null;
  }

  function render(ctx, now, payload, fps) {
    lastRenderFps = fps || 0;
    if (!active || !payload || !payload.game) {
      ctx.clearRect(0, 0, W, H);
      return;
    }
    const game = payload.game;
    const slowZoom = game.slow_motion ? 1.025 : 1;
    const shaking = now < shakeUntil;
    const shakeFade = shaking ? Math.max(0, (shakeUntil - now) / 520) : 0;
    const sx = shaking ? Math.sin(now * .19) * shakePower * shakeFade : 0;
    const sy = shaking ? Math.cos(now * .23) * shakePower * .55 * shakeFade : 0;
    if (!shaking) shakePower = 0;
    const camera = headCamera(game.motion && game.motion.posture);
    ctx.save();
    ctx.translate(W / 2 + sx, H / 2 + sy);
    ctx.scale(slowZoom, slowZoom);
    ctx.translate(-W / 2, -H / 2);

    // Two depths, one head. The arena is across the room and the opponent is
    // an arm's length away, so the same movement of the player's head shifts
    // them by very different amounts — which is the parallax that makes a flat
    // pair of images read as a room you are standing in. The numbers are in
    // `applyCamera`; all that is chosen here is which layer is how far away.
    ctx.save();
    applyCamera(ctx, camera, .34);
    drawArena(ctx, now, fps, game);
    ctx.restore();

    ctx.save();
    applyCamera(ctx, camera, 1);
    if (game.mode === "training") drawTraining(ctx, game, now);
    else drawOpponent(ctx, game, now);
    stepAndDrawEffects(ctx, now);
    ctx.restore();

    // **Outside the camera, deliberately.** These gloves are on the ends of
    // the arms of the head the camera *is*. Moving them with it would be
    // moving them twice, and a duck would drop the player's own hands out of
    // the bottom of their own view.
    drawFirstPersonHands(ctx, payload, now);
    drawForeground(ctx, game, now);
    if (payload.debug) drawDebugCanvas(ctx, payload);
    ctx.restore();

    const message = gameEl("boxing-message");
    if (now >= messageUntil && !game.countdown) {
      message.className = "";
      messagePriority = 0;
    }
  }

  //: How far the world moves for one shoulder-width of head movement, in
  //: pixels, at the near depth. Generous — a first-person view that barely
  //: moves when you slip a punch feels like a photograph of a fight rather
  //: than a fight, and this is the whole of what makes ducking feel like
  //: ducking rather than like pressing a button.
  const CAMERA_SWAY_X = 190;
  const CAMERA_SWAY_Y = 150;

  function headCamera(posture) {
    const p = posture || {};
    const bound = (value, limit) =>
      Math.max(-limit, Math.min(limit, Number(value) || 0));
    // Negated: an eye that moves right sees the room move left. Bounded rather
    // than trusted, because `offset_x` is a measured quantity divided by a
    // measured shoulder span and one badly tracked frame should tilt the room
    // a little, not throw it off the screen.
    return {
      x: -bound(p.offset_x, .62),
      y: -bound(p.offset_y, .48),
      // Leaning back shrinks the shoulder span, which is the only depth cue
      // this camera has and a surprisingly convincing one.
      zoom: 1 + bound(p.scale_change, .3) * .34,
      roll: bound(p.torso_angle, 16) * Math.PI / 180 * .30,
    };
  }

  function applyCamera(ctx, camera, depth) {
    ctx.translate(W / 2, H / 2);
    ctx.rotate(camera.roll * depth);
    const zoom = 1 + (camera.zoom - 1) * depth;
    ctx.scale(zoom, zoom);
    ctx.translate(-W / 2, -H / 2);
    ctx.translate(camera.x * CAMERA_SWAY_X * depth,
                  camera.y * CAMERA_SWAY_Y * depth);
  }

  function drawArena(ctx, now, fps, game) {
    // No parallax of its own any more: the whole layer is moved by
    // `applyCamera` before this is called, which is one mechanism instead of
    // two disagreeing ones.
    const parallaxX = 0;
    const parallaxY = 0;
    if (arenaReady && arenaImage.naturalWidth) {
      const scale = Math.max((W + 54) / arenaImage.naturalWidth,
                             (H + 42) / arenaImage.naturalHeight);
      const dw = arenaImage.naturalWidth * scale;
      const dh = arenaImage.naturalHeight * scale;
      ctx.drawImage(arenaImage, (W - dw) / 2 + parallaxX,
                    (H - dh) / 2 + parallaxY, dw, dh);

      ctx.save();
      ctx.globalCompositeOperation = "screen";
      const spotlight = ctx.createRadialGradient(640 + parallaxX * .25, 35, 18,
        640 + parallaxX * .25, 310, 560);
      spotlight.addColorStop(0, game.slow_motion
        ? "rgba(105,231,255,.25)" : "rgba(255,244,204,.13)");
      spotlight.addColorStop(1, "rgba(15,45,72,0)");
      ctx.fillStyle = spotlight; ctx.fillRect(0, 0, W, 650);
      ctx.restore();

      crowdEnergy *= .965;
      const stride = fps && fps < 42 ? 7 : fps && fps < 52 ? 5 : 4;
      for (let i = 0; i < crowd.length; i += stride) {
        const person = crowd[i];
        const bounce = Math.sin(now * .008 + person.phase) * crowdEnergy * 7;
        if (crowdEnergy > .08 && i % 3 === 0) {
          ctx.fillStyle = `hsla(${person.hue},75%,68%,${.08 + crowdEnergy * .08})`;
          ctx.beginPath(); ctx.arc(person.x + parallaxX * .5, person.y + bounce,
            2 + crowdEnergy, 0, Math.PI * 2); ctx.fill();
        }
      }
      const shade = ctx.createLinearGradient(0, 0, 0, H);
      shade.addColorStop(0, game.slow_motion ? "rgba(3,12,24,.30)" : "rgba(0,0,0,.08)");
      shade.addColorStop(.55, "rgba(0,0,0,0)");
      shade.addColorStop(1, "rgba(0,6,12,.18)");
      ctx.fillStyle = shade; ctx.fillRect(0, 0, W, H);
      return;
    }

    const bg = ctx.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#02050a"); bg.addColorStop(.46, "#101b27");
    bg.addColorStop(1, "#05090e");
    ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);

    ctx.save();
    ctx.globalCompositeOperation = "screen";
    const beam = ctx.createRadialGradient(640, 40, 8, 640, 250, 570);
    beam.addColorStop(0, "rgba(155,225,255,.25)");
    beam.addColorStop(1, "rgba(20,70,100,0)");
    ctx.fillStyle = beam; ctx.fillRect(0, 0, W, 610);
    ctx.restore();

    const stride = fps && fps < 42 ? 3 : fps && fps < 52 ? 2 : 1;
    for (let i = 0; i < crowd.length; i += stride) {
      const person = crowd[i];
      const bounce = Math.sin(now * .002 + person.phase) * (i % 9 === 0 ? 4 : 1.3);
      ctx.fillStyle = `hsla(${person.hue},35%,${20 + (i % 4) * 5}%,.72)`;
      ctx.beginPath(); ctx.arc(person.x, person.y + bounce, person.r, 0, Math.PI * 2); ctx.fill();
      ctx.fillRect(person.x - person.r * .72, person.y + person.r + bounce,
                   person.r * 1.44, person.r * 2.5);
    }

    // Perspective ring: back ropes first, canvas, then front ropes.
    ctx.lineCap = "round";
    for (const y of [315, 350, 386]) {
      ctx.strokeStyle = y === 350 ? "#e9edf2" : "#e44048";
      ctx.lineWidth = 5;
      ctx.beginPath(); ctx.moveTo(120, y); ctx.lineTo(1160, y); ctx.stroke();
    }
    const mat = ctx.createLinearGradient(0, 330, 0, 760);
    mat.addColorStop(0, "#466176"); mat.addColorStop(1, "#162431");
    ctx.fillStyle = mat;
    ctx.beginPath(); ctx.moveTo(130, 380); ctx.lineTo(1150, 380);
    ctx.lineTo(1270, 770); ctx.lineTo(10, 770); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "rgba(210,230,244,.19)"; ctx.lineWidth = 2;
    for (let i = 1; i < 6; i++) {
      ctx.beginPath(); ctx.moveTo(130, 380 + i * 60); ctx.lineTo(1150, 380 + i * 60); ctx.stroke();
    }
    for (const x of [95, 1185]) {
      ctx.fillStyle = "#d7e3eb"; ctx.fillRect(x - 7, 292, 14, 400);
      ctx.fillStyle = "#d43c45"; ctx.fillRect(x - 13, 310, 26, 60);
    }
    for (const y of [492, 545, 598]) {
      ctx.strokeStyle = y === 545 ? "#f4f6f8" : "#d83f47"; ctx.lineWidth = 7;
      ctx.beginPath(); ctx.moveTo(94, y); ctx.quadraticCurveTo(640, y + 38, 1186, y); ctx.stroke();
    }
  }

  function drawForeground(ctx, game, now) {
    ctx.save();
    // No rope across the bottom any more. It sat where the player's own gloves
    // now are, and a rope in front of your own hands puts you outside the ring
    // looking in — which is the one thing this view is not.
    const vignette = ctx.createRadialGradient(640, 390, 230, 640, 390, 730);
    vignette.addColorStop(.58, "rgba(0,0,0,0)");
    vignette.addColorStop(1, game.slow_motion ? "rgba(0,18,34,.48)" : "rgba(0,3,8,.36)");
    ctx.fillStyle = vignette; ctx.fillRect(0, 0, W, H);

    if (now < hitFlashUntil) {
      // Strongest at the edges and thin in the middle, so it reads as being
      // rattled rather than as a red filter over the fight. The opponent stays
      // visible through it, which matters — the punch after the one that hurt
      // is the one you have to see coming.
      const fade = Math.max(0, (hitFlashUntil - now) / 420);
      const power = Math.min(1, hitFlashPower) * Math.min(1, fade);
      const wash = ctx.createRadialGradient(640, 400, 140, 640, 400, 780);
      wash.addColorStop(0, `rgba(190,20,26,${power * .12})`);
      wash.addColorStop(1, `rgba(150,8,14,${power * .72})`);
      ctx.fillStyle = wash; ctx.fillRect(0, 0, W, H);
      if (now >= hitFlashUntil) hitFlashPower = 0;
    }
    ctx.restore();
  }

  function drawTraining(ctx, game, now) {
    // A neutral coaching dummy keeps the target anchored in the same visual
    // coordinate system as the fight opponent — including how close it stands,
    // or the two modes would teach different distances.
    drawBoxer(ctx, 640, OPPONENT_Y, OPPONENT_SCALE,
              { guard: "none", state: "observe" }, {}, now, true);
    const prompt = game.prompt;
    if (!prompt) return;
    const offensive = /punch|hook/.test(prompt.kind);
    if (offensive) {
      const x = prompt.kind.startsWith("left") ? 725 : 555;
      const y = prompt.target === "body" ? 430 : 270;
      const pulse = 1 + .10 * Math.sin(now * .012);
      ctx.save(); ctx.translate(x, y); ctx.scale(pulse, pulse);
      ctx.strokeStyle = prompt.stage === 3 ? "#ffcd58" : "#62efff";
      ctx.lineWidth = 10; ctx.beginPath(); ctx.arc(0, 0, prompt.stage === 1 ? 68 : 52, 0, Math.PI * 2); ctx.stroke();
      ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(0, 0, 24, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-15, 0); ctx.lineTo(15, 0); ctx.moveTo(0, -15); ctx.lineTo(0, 15); ctx.stroke();
      ctx.restore();
    } else {
      ctx.save(); ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = "900 76px system-ui"; ctx.fillStyle = "rgba(101,239,255,.92)";
      const icon = { dodge_left: "←", dodge_right: "→", duck: "↓",
                     lean_back: "↙", block: "🛡", parry: "✦" }[prompt.kind] || "!";
      ctx.fillText(icon, 640, 285); ctx.restore();
    }
  }

  //: How big and how low the other fighter stands.
  //:
  //: Both moved when the view did. Over the player's shoulder he was a figure
  //: in the middle distance with the player's own back in front of him; from
  //: the player's eyes he is a man within arm's reach, and a man within arm's
  //: reach fills most of the frame and is looked slightly *up* at rather than
  //: down on. Half again the size and lower in the picture is what that comes
  //: to. The training dummy uses the same two numbers, so the distance a
  //: player learns in training is the distance they fight at.
  const OPPONENT_SCALE = 1.62;
  const OPPONENT_Y = 372;

  function drawOpponent(ctx, game, now) {
    const ai = game.ai || {};
    const dodge = ai.guard === "dodge" || ai.state === "defend";
    const attack = ai.attack || {};
    const motion = attackMotion(attack);
    const react = liveReaction(opponentReaction, now);
    const idleX = Math.sin(now * .0017) * 10;
    const idleY = Math.sin(now * .0031) * 5;
    const dodgeLean = dodge ? 66 * Math.sin(now * .014) : 0;
    const reactionLean = react && react.kind === "dodge"
      ? (react.side === "left" ? 70 : -70) * reactionPulse(react, now) : 0;
    const laneX = smoothLane("opponent", laneIndex(ai.lane), now, 150);
    const x = 640 + laneX + idleX + dodgeLean + reactionLean;
    opponentScreenX = x;
    const y = OPPONENT_Y + idleY + motion.drive * 30 +
      (react && react.kind === "body" ? reactionPulse(react, now) * 20 : 0);
    const opponent = game.opponent || {};
    const hpRatio = clamp01((opponent.hp ?? 100) / Math.max(1, opponent.max_hp || 100));
    drawAttackCue(ctx, x, y, attack, motion, now);
    drawBoxer(ctx, x, y, OPPONENT_SCALE + motion.drive * .14,
      Object.assign({}, ai, { reaction: react, hpRatio }),
      game.opponent || {}, now, false);
  }

  function liveReaction(value, now) {
    if (!value || now >= value.until) return null;
    return value;
  }

  function reactionPulse(value, now) {
    if (!value) return 0;
    const t = clamp01((now - value.started) / value.duration);
    return Math.sin(t * Math.PI) * (1 - t * .22);
  }

  function clamp01(value) {
    return Math.max(0, Math.min(1, Number(value) || 0));
  }

  function opponentPoseFor(ai, react) {
    if (react) {
      if (react.kind === "ko") return "knockout";
      if (react.kind === "head") {
        return react.side === "right" ? "head_hit_right" : "head_hit_left";
      }
      if (react.kind === "body") return "body_hit";
      if (react.kind === "parry") return "parried";
      if (react.kind === "dodge") {
        return react.side === "right" ? "dodge_left" : "dodge_right";
      }
    }
    const attack = ai.attack || {};
    if (attack.kind) {
      const target = attack.target === "body" ? "body" : "head";
      const kind = String(attack.kind);
      if (kind.includes("hook")) {
        return `${attack.side === "right" ? "right" : "left"}_hook_${target}`;
      }
      return `${attack.side === "right" ? "right_cross" : "left_jab"}_${target}`;
    }
    if (ai.guard === "head") return "high_guard";
    if (ai.guard === "body") return "body_guard";
    if (ai.guard === "dodge" || ai.state === "defend") {
      return laneIndex(ai.lane) > 0 ? "dodge_right" : "dodge_left";
    }
    return "idle";
  }

  function laneIndex(value) {
    if (value === "left") return -1;
    if (value === "right") return 1;
    if (value === "middle") return 0;
    const numeric = Number(value) || 0;
    return numeric < -.5 ? -1 : numeric > .5 ? 1 : 0;
  }

  function smoothLane(key, target, now, spacing) {
    const lane = laneMotion[key];
    if (!lane.at || now - lane.at > 600) lane.value = target;
    const dt = Math.max(0, Math.min(.05, (now - (lane.at || now)) / 1000));
    lane.at = now;
    // Time-based easing keeps lane movement the same at 30 and 60 render FPS.
    lane.value += (target - lane.value) * (1 - Math.exp(-dt * 7.5));
    return lane.value * spacing;
  }

  function smoothStep(value) {
    const p = clamp01(value);
    return p * p * (3 - 2 * p);
  }

  function attackMotion(attack) {
    if (!attack || !attack.kind) return { drive: 0, coil: 0 };
    const extension = clamp01(attack.extension ?? attack.progress);
    if (attack.phase === "recover") {
      return { drive: smoothStep(extension), coil: 0 };
    }
    const timeline = clamp01(attack.progress);
    // The first third is a visible shoulder/glove wind-up.  The remaining
    // wind-up time drives the glove toward the camera, reaching the player at
    // the same instant the simulation resolves impact.
    const drive = smoothStep((timeline - .28) / .72);
    const coil = Math.sin(Math.min(1, timeline / .32) * Math.PI / 2) * (1 - drive);
    return { drive, coil };
  }

  function drawAttackCue(ctx, x, y, attack, motion, now) {
    if (!attack.kind) return;
    const timeline = clamp01(attack.progress);
    const alpha = attack.phase === "recover"
      ? .72 * motion.drive : .34 + .42 * timeline;
    if (alpha <= .03) return;
    const sign = attack.side === "left" ? -1 : 1;
    const hook = String(attack.kind).includes("hook");
    const shoulderX = x + sign * 78;
    const shoulderY = y + 76;
    ctx.save();
    ctx.globalAlpha = alpha;
    const glow = ctx.createRadialGradient(shoulderX, shoulderY, 4,
      shoulderX, shoulderY, 76 + motion.coil * 26);
    glow.addColorStop(0, attack.target === "head" ? "rgba(255,223,117,.74)" : "rgba(255,124,90,.7)");
    glow.addColorStop(1, "rgba(255,65,70,0)");
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(shoulderX, shoulderY, 92, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = attack.target === "head" ? "rgba(255,231,145,.78)" : "rgba(255,139,106,.72)";
    ctx.lineWidth = 5; ctx.lineCap = "round";
    for (let i = 0; i < 3; i++) {
      const bend = (i - 1) * 18;
      ctx.beginPath();
      ctx.moveTo(shoulderX + sign * (48 + i * 10), shoulderY - 42 + bend);
      ctx.quadraticCurveTo(shoulderX + sign * (82 + i * 12), shoulderY + bend,
        shoulderX + sign * (106 + i * 15), shoulderY + 46 + bend);
      ctx.stroke();
    }
    if (hook) {
      ctx.beginPath();
      ctx.arc(x, y + (attack.target === "body" ? 168 : 32), 118,
        sign < 0 ? -.25 : Math.PI + .25, sign < 0 ? 1.2 : Math.PI * 2 - 1.2, sign < 0);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawBoxer(ctx, x, y, scale, ai, fighterDamage, now, training) {
    ctx.save(); ctx.translate(x, y); ctx.scale(scale, scale);
    const attack = ai.attack || {};
    const motion = attackMotion(attack);
    const guardHead = ai.guard === "head";
    const guardBody = ai.guard === "body";
    const react = ai.reaction;
    const reactionAmount = reactionPulse(react, now);
    const sideSign = react && react.side === "left" ? -1 : 1;
    if (react && react.kind === "ko") {
      const fall = smoothStep((now - react.started) / react.duration);
      ctx.translate(sideSign * fall * 108, fall * 118);
      ctx.rotate(sideSign * fall * .82);
    } else if (react && react.kind === "head") {
      ctx.translate(sideSign * reactionAmount * 22, reactionAmount * 7);
      ctx.rotate(sideSign * reactionAmount * .11);
    } else if (react && (react.kind === "body" || react.kind === "parry")) {
      ctx.translate(0, reactionAmount * 16);
      ctx.scale(1 + reactionAmount * .05, 1 - reactionAmount * .08);
    }

    const breath = Math.sin(now * .0032) * 3;
    const ink = training ? "#102a39" : "#221622";
    const skinLight = training ? "#a9d5dc" : "#e6a06d";
    const skinMid = training ? "#6aa4b1" : "#b96548";
    const skinDark = training ? "#315d6c" : "#71362f";
    const useBodyImage = !training && opponentBodyReady && opponentBodyImage.naturalWidth;

    ctx.fillStyle = "rgba(0,0,0,.38)";
    ctx.beginPath(); ctx.ellipse(0, 278, 116, 24, 0, 0, Math.PI * 2); ctx.fill();

    const selectedPose = opponentPoseFor(ai, react);
    if (!training && drawPoseBody(ctx, "opponent", selectedPose,
        fighterDamage, now, [-180, -160, 360, 480], 1)) {
      ctx.restore();
      return;
    }

    if (useBodyImage) {
      // Swap one complete, pre-baked body image for each four-zone damage
      // combination. Arms remain separate so every live action still maps.
      drawDamageBody(ctx, "opponent", fighterDamage, opponentBodyImage, now,
        [-122, -84 + breath, 244, 384], 1);
      if (react && react.kind === "parry") {
        ctx.fillStyle = "rgba(116,238,255,.82)";
        for (let i = 0; i < 3; i++) {
          ctx.beginPath(); ctx.ellipse(46 + i * 8, -49 + i * 13,
            4, 8, -.4, 0, Math.PI * 2); ctx.fill();
        }
      }
    } else {
    // Shorts and legs keep the front-facing training fighter planted.
    ctx.fillStyle = training ? "#183e55" : "#26384f";
    ctx.strokeStyle = ink; ctx.lineWidth = 8;
    ctx.beginPath(); ctx.moveTo(-83, 218); ctx.lineTo(-91, 280); ctx.lineTo(-21, 280);
    ctx.lineTo(0, 246); ctx.lineTo(21, 280); ctx.lineTo(91, 280); ctx.lineTo(83, 218); ctx.closePath();
    ctx.fill(); ctx.stroke();
    ctx.strokeStyle = training ? "#5fd3e6" : "#f2c45f"; ctx.lineWidth = 9;
    ctx.beginPath(); ctx.moveTo(-82, 224); ctx.lineTo(82, 224); ctx.stroke();

    const torso = ctx.createLinearGradient(-85, 55, 95, 236);
    torso.addColorStop(0, skinLight); torso.addColorStop(.58, skinMid); torso.addColorStop(1, skinDark);
    ctx.fillStyle = torso; ctx.strokeStyle = ink; ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.moveTo(-77, 57 + breath); ctx.bezierCurveTo(-122, 72, -117, 142, -89, 185);
    ctx.quadraticCurveTo(-76, 214, -69, 226); ctx.lineTo(69, 226);
    ctx.quadraticCurveTo(76, 214, 89, 185); ctx.bezierCurveTo(117, 142, 122, 72, 77, 57 + breath);
    ctx.quadraticCurveTo(42, 38, 0, 45 + breath); ctx.quadraticCurveTo(-42, 38, -77, 57 + breath);
    ctx.closePath(); ctx.fill(); ctx.stroke();

    // Cel-shaded muscles: broad readable shapes instead of photoreal detail.
    ctx.fillStyle = "rgba(255,226,184,.24)";
    ctx.beginPath(); ctx.ellipse(-43, 94, 42, 29, -.18, Math.PI, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.ellipse(43, 94, 42, 29, .18, Math.PI, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(77,33,31,.44)"; ctx.lineWidth = 4;
    ctx.beginPath(); ctx.moveTo(0, 75); ctx.lineTo(0, 190); ctx.stroke();
    ctx.beginPath(); ctx.arc(-38, 113, 38, .15, 2.7); ctx.stroke();
    ctx.beginPath(); ctx.arc(38, 113, 38, .45, Math.PI - .15, true); ctx.stroke();
    ctx.beginPath(); ctx.arc(0, 177, 46, .18, Math.PI - .18); ctx.stroke();

    // Neck, jaw, ears, hair and face remain readable beneath a high guard.
    ctx.fillStyle = skinDark; ctx.strokeStyle = ink; ctx.lineWidth = 7;
    ctx.beginPath(); ctx.roundRect(-35, 26, 70, 57, 24); ctx.fill(); ctx.stroke();
    ctx.fillStyle = skinMid;
    ctx.beginPath(); ctx.ellipse(-61, 0, 15, 24, 0, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.ellipse(61, 0, 15, 24, 0, 0, Math.PI * 2); ctx.fill();
    const headTilt = react && react.kind === "head" ? sideSign * reactionAmount * .24 : 0;
    ctx.save(); ctx.rotate(headTilt);
    ctx.fillStyle = skinLight; ctx.strokeStyle = ink; ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.moveTo(-54, -53); ctx.quadraticCurveTo(0, -87, 54, -53);
    ctx.lineTo(58, 6); ctx.quadraticCurveTo(48, 58, 0, 70);
    ctx.quadraticCurveTo(-48, 58, -58, 6); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = training ? "#153244" : "#24213b";
    ctx.beginPath(); ctx.moveTo(-59, -45); ctx.quadraticCurveTo(-28, -92, 8, -71);
    ctx.quadraticCurveTo(39, -88, 60, -43); ctx.lineTo(42, -52);
    ctx.lineTo(28, -38); ctx.lineTo(12, -55); ctx.lineTo(-7, -37);
    ctx.lineTo(-25, -55); ctx.lineTo(-43, -35); ctx.closePath(); ctx.fill();

    drawOpponentFace(ctx, ai.hpRatio ?? 1, react, reactionAmount, training);
    ctx.restore();
    }

    for (const side of ["left", "right"]) {
      const sign = side === "left" ? -1 : 1;
      const activeArm = attack.side === side;
      // The reference opponent never hangs his hands beside his waist: idle
      // is a compact cheek guard, head guard closes further, and body guard
      // drops only to the ribs.
      const restX = sign * (guardHead ? 52 : guardBody ? 70 : 68);
      const restY = guardHead ? 8 : guardBody ? 126 : 34;
      let gloveX = restX, gloveY = restY, gloveScale = 1;
      if (activeArm) {
        const hook = String(attack.kind || "").includes("hook");
        const coilX = sign * (hook ? 166 : 148);
        const coilY = hook ? 62 : 102;
        const targetX = hook ? -sign * 48 : sign * 18;
        const targetY = attack.target === "body" ? 310 : 240;
        gloveX += (coilX - restX) * motion.coil + (targetX - restX) * motion.drive;
        gloveY += (coilY - restY) * motion.coil + (targetY - restY) * motion.drive;
        gloveScale = 1 + motion.drive * .62;
        if (motion.drive > .04) {
          ctx.strokeStyle = training
            ? `rgba(77,218,244,${.12 + motion.drive * .38})`
            : `rgba(44,111,230,${.12 + motion.drive * .42})`;
          ctx.lineWidth = 14 + motion.drive * 22; ctx.lineCap = "round";
          ctx.beginPath(); ctx.moveTo(restX, restY); ctx.lineTo(gloveX, gloveY); ctx.stroke();
        }
      }
      const shoulder = [sign * 78, (useBodyImage ? 40 : 82) + breath];
      const wrist = [gloveX, gloveY];
      const elbow = [
        shoulder[0] + (wrist[0] - shoulder[0]) * .48 + sign * (22 - motion.drive * 8),
        shoulder[1] + (wrist[1] - shoulder[1]) * .45 + 38 - motion.drive * 20,
      ];
      drawAnimeArm(ctx, shoulder, elbow, wrist, {
        ink,
        light: training ? "#bce7eb" : skinLight,
        mid: training ? "#6aa4b1" : skinMid,
        dark: training ? "#315d6c" : skinDark,
        wrap: training ? "#49cfe8" : "#164f9f",
        width: 42 + (activeArm ? motion.drive * 7 : 0),
      });
      const gloveAngle = Math.atan2(elbow[1] - wrist[1], elbow[0] - wrist[0]) - Math.PI / 2;
      glove(ctx, gloveX, gloveY, training ? "#49cfe8" : "#164f9f", gloveScale,
        side === "left" ? -1 : 1, gloveAngle);
    }

    ctx.restore();
  }

  function drawOpponentFace(ctx, hpRatio, react, reactionAmount, training) {
    const hurt = 1 - clamp01(hpRatio);
    const stunned = react && (react.kind === "parry" || react.kind === "head");
    const eyeY = -5;
    ctx.strokeStyle = training ? "#173544" : "#3b2026"; ctx.lineCap = "round";
    ctx.lineWidth = 7;
    const browLift = stunned ? -7 : hurt * 5;
    ctx.beginPath(); ctx.moveTo(-41, eyeY - 17 + browLift); ctx.lineTo(-13, eyeY - 12); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(41, eyeY - 17 + browLift); ctx.lineTo(13, eyeY - 12); ctx.stroke();
    ctx.fillStyle = "#f7f5e9";
    ctx.beginPath(); ctx.ellipse(-25, eyeY, 13, stunned ? 12 : 8, 0, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.ellipse(25, eyeY, 13, stunned ? 12 : 8, 0, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#171524";
    const pupil = stunned ? Math.sin(performance.now() * .04) * 3 : 0;
    ctx.beginPath(); ctx.arc(-25 + pupil, eyeY, 5, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(25 - pupil, eyeY, 5, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(70,30,30,.58)"; ctx.lineWidth = 4;
    ctx.beginPath(); ctx.moveTo(1, 1); ctx.lineTo(-7, 22); ctx.lineTo(5, 24); ctx.stroke();
    const openMouth = stunned || hurt > .64;
    ctx.fillStyle = "#351524"; ctx.strokeStyle = "#52242c"; ctx.lineWidth = 4;
    ctx.beginPath();
    if (openMouth) ctx.ellipse(0, 43, 22 + reactionAmount * 7, 12 + reactionAmount * 6, 0, 0, Math.PI * 2);
    else { ctx.moveTo(-21, 42); ctx.quadraticCurveTo(0, 35 + hurt * 12, 21, 42); }
    openMouth ? ctx.fill() : ctx.stroke();
    if (hurt > .35) {
      ctx.fillStyle = `rgba(102,45,113,${.18 + hurt * .24})`;
      ctx.beginPath(); ctx.ellipse(-34, 22, 14, 8, -.2, 0, Math.PI * 2); ctx.fill();
    }
  }

  function muscleSegment(ctx, start, end, startWidth, endWidth, bulge, palette) {
    const dx = end[0] - start[0], dy = end[1] - start[1];
    const length = Math.max(1, Math.hypot(dx, dy));
    const ux = dx / length, uy = dy / length;
    const nx = -uy, ny = ux;
    const sw = startWidth / 2, ew = endWidth / 2;
    const upperBulge = bulge, lowerBulge = bulge * .55;
    const gradient = ctx.createLinearGradient(
      (start[0] + end[0]) / 2 - nx * startWidth,
      (start[1] + end[1]) / 2 - ny * startWidth,
      (start[0] + end[0]) / 2 + nx * startWidth,
      (start[1] + end[1]) / 2 + ny * startWidth);
    gradient.addColorStop(0, palette.dark);
    gradient.addColorStop(.42, palette.mid);
    gradient.addColorStop(.72, palette.light);
    gradient.addColorStop(1, palette.mid);

    ctx.fillStyle = gradient;
    ctx.strokeStyle = palette.ink;
    ctx.lineWidth = Math.max(4, startWidth * .12);
    ctx.beginPath();
    ctx.moveTo(start[0] + nx * sw, start[1] + ny * sw);
    ctx.bezierCurveTo(
      start[0] + ux * length * .34 + nx * (sw + upperBulge),
      start[1] + uy * length * .34 + ny * (sw + upperBulge),
      end[0] - ux * length * .34 + nx * (ew + upperBulge * .45),
      end[1] - uy * length * .34 + ny * (ew + upperBulge * .45),
      end[0] + nx * ew, end[1] + ny * ew);
    ctx.lineTo(end[0] - nx * ew, end[1] - ny * ew);
    ctx.bezierCurveTo(
      end[0] - ux * length * .32 - nx * (ew + lowerBulge * .35),
      end[1] - uy * length * .32 - ny * (ew + lowerBulge * .35),
      start[0] + ux * length * .30 - nx * (sw + lowerBulge),
      start[1] + uy * length * .30 - ny * (sw + lowerBulge),
      start[0] - nx * sw, start[1] - ny * sw);
    ctx.closePath(); ctx.fill(); ctx.stroke();

    // One angular cel highlight and one muscle crease match the generated
    // reference torsos without turning the live limb into a glossy tube.
    ctx.strokeStyle = "rgba(255,224,184,.42)";
    ctx.lineWidth = Math.max(3, startWidth * .10);
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(start[0] + ux * length * .13 + nx * sw * .48,
               start[1] + uy * length * .13 + ny * sw * .48);
    ctx.lineTo(start[0] + ux * length * .48 + nx * (sw + bulge) * .50,
               start[1] + uy * length * .48 + ny * (sw + bulge) * .50);
    ctx.lineTo(end[0] - ux * length * .22 + nx * ew * .40,
               end[1] - uy * length * .22 + ny * ew * .40);
    ctx.stroke();
    ctx.strokeStyle = "rgba(76,31,29,.40)";
    ctx.lineWidth = Math.max(2, startWidth * .055);
    ctx.beginPath();
    ctx.moveTo(start[0] + ux * length * .52 - nx * startWidth * .18,
               start[1] + uy * length * .52 - ny * startWidth * .18);
    ctx.lineTo(start[0] + ux * length * .70 - nx * endWidth * .16,
               start[1] + uy * length * .70 - ny * endWidth * .16);
    ctx.stroke();
  }

  function jointEllipse(ctx, point, angle, rx, ry, palette) {
    ctx.save(); ctx.translate(point[0], point[1]); ctx.rotate(angle);
    const gradient = ctx.createLinearGradient(-rx, -ry, rx, ry);
    gradient.addColorStop(0, palette.dark);
    gradient.addColorStop(.55, palette.mid);
    gradient.addColorStop(1, palette.light);
    ctx.fillStyle = gradient; ctx.strokeStyle = palette.ink;
    ctx.lineWidth = Math.max(4, ry * .19);
    ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, 0, Math.PI * 2);
    ctx.fill(); ctx.stroke(); ctx.restore();
  }

  function drawAnimeArm(ctx, shoulder, elbow, wrist, options) {
    const width = options.width || 46;
    const palette = {
      ink: options.ink || "#26171a",
      light: options.light || "#f0ab77",
      mid: options.mid || "#bd6b4a",
      dark: options.dark || "#71362f",
    };
    const upperAngle = Math.atan2(elbow[1] - shoulder[1], elbow[0] - shoulder[0]);
    const foreAngle = Math.atan2(wrist[1] - elbow[1], wrist[0] - elbow[0]);
    muscleSegment(ctx, shoulder, elbow, width * 1.28, width * .82,
      width * .14, palette);
    muscleSegment(ctx, elbow, wrist, width * .91, width * .61,
      width * .10, palette);
    const deltoid = [shoulder[0] + Math.cos(upperAngle) * width * .12,
                      shoulder[1] + Math.sin(upperAngle) * width * .12];
    jointEllipse(ctx, deltoid, upperAngle, width * .49, width * .61, palette);
    jointEllipse(ctx, elbow, (upperAngle + foreAngle) / 2,
      width * .34, width * .29, palette);

    // Reference-sheet muscle cuts: a dark triceps wedge, bright biceps ridge,
    // and a forearm split. These remain joint-relative as the pose changes.
    const upperMid = [shoulder[0] + (elbow[0] - shoulder[0]) * .46,
                      shoulder[1] + (elbow[1] - shoulder[1]) * .46];
    const foreMid = [elbow[0] + (wrist[0] - elbow[0]) * .48,
                     elbow[1] + (wrist[1] - elbow[1]) * .48];
    ctx.save(); ctx.translate(upperMid[0], upperMid[1]); ctx.rotate(upperAngle);
    ctx.fillStyle = "rgba(86,34,31,.28)";
    ctx.beginPath(); ctx.ellipse(-width * .02, width * .17,
      width * .28, width * .14, -.18, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(255,221,180,.44)"; ctx.lineWidth = Math.max(3, width * .07);
    ctx.beginPath(); ctx.arc(-width * .04, -width * .02,
      width * .25, 3.55, 5.82); ctx.stroke(); ctx.restore();
    ctx.save(); ctx.translate(foreMid[0], foreMid[1]); ctx.rotate(foreAngle);
    ctx.strokeStyle = "rgba(78,31,30,.42)"; ctx.lineWidth = Math.max(2, width * .055);
    ctx.beginPath(); ctx.moveTo(-width * .22, width * .13);
    ctx.quadraticCurveTo(0, -width * .04, width * .22, width * .10); ctx.stroke();
    ctx.restore();

    // Reference-style coloured wrist wrap joins the tapered forearm to the
    // glove and remains aligned with every live wrist angle.
    ctx.save(); ctx.translate(wrist[0], wrist[1]); ctx.rotate(foreAngle);
    ctx.fillStyle = options.wrap || "#c52e43";
    ctx.strokeStyle = palette.ink; ctx.lineWidth = Math.max(3, width * .09);
    ctx.beginPath(); ctx.roundRect(-width * .39, -width * .31,
      width * .55, width * .62, width * .10); ctx.fill(); ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,.24)"; ctx.lineWidth = Math.max(2, width * .05);
    ctx.beginPath(); ctx.moveTo(-width * .25, -width * .20);
    ctx.lineTo(-width * .25, width * .20); ctx.stroke();
    ctx.restore();
  }

  function glove(ctx, x, y, colour, scale = 1, direction = 1, angle = 0) {
    ctx.save(); ctx.translate(x, y); ctx.rotate(angle); ctx.scale(scale, scale);
    ctx.fillStyle = colour; ctx.strokeStyle = "#211521"; ctx.lineWidth = 7;
    ctx.beginPath();
    ctx.moveTo(-27, 28);
    ctx.bezierCurveTo(-36, 15, -39, -8, -29, -27);
    ctx.quadraticCurveTo(-17, -47, 7, -45);
    ctx.quadraticCurveTo(31, -43, 37, -22);
    ctx.quadraticCurveTo(44, 2, 28, 28);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-direction * 19, 18);
    ctx.quadraticCurveTo(-direction * 43, 18, -direction * 41, -3);
    ctx.quadraticCurveTo(-direction * 39, -19, -direction * 25, -15);
    ctx.quadraticCurveTo(-direction * 12, -7, -direction * 9, 7);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "rgba(0,0,0,.22)"; ctx.strokeStyle = "#211521"; ctx.lineWidth = 5;
    ctx.beginPath(); ctx.roundRect(-29, 23, 58, 22, 5); ctx.fill(); ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,.52)"; ctx.lineWidth = 5;
    ctx.beginPath(); ctx.moveTo(-17, -27); ctx.quadraticCurveTo(2, -42, 21, -24); ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,.24)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(-22, 31); ctx.lineTo(22, 31); ctx.stroke();
    ctx.restore();
  }

  //: Where the player's arms come into the frame, off the bottom corners.
  //: Off-screen on purpose — a first-person arm has no visible beginning, and
  //: an elbow appearing at the edge of the picture is the single thing that
  //: most makes a view stop reading as your own eyes.
  const FP_ENTRY_X = 402, FP_ENTRY_Y = 902;
  //: Where a glove rests when the arm is folded, and where it goes when the
  //: arm is straight. The rest position is low and wide — you look over your
  //: own guard, not through it.
  const FP_REST_X = 336, FP_REST_Y = 716;
  const FP_REACH_Y = 500;
  //: Arm extension, as `boxing/motion.py` measures it, mapped to nothing and
  //: everything. Below the first number the arm is folded; above the second it
  //: is as straight as it gets.
  const FP_FOLDED = .42, FP_STRAIGHT = .90;
  //: Glove size at the two ends of that. **Smaller when extended**, which is
  //: the opposite of what it feels like it should be and is simply what
  //: happens: your fist at your chin is half as far from your eyes as your
  //: fist at the end of a straight punch, so it looks twice the size.
  const FP_NEAR = 1.28, FP_FAR = .62;
  //: A forearm is thinner than the glove on the end of it. Without this the
  //: extended arm reads as a length of pipe rather than as an arm going away
  //: from you.
  const FP_ARM_WIDTH = .84;
  //: How long the forearm is drawn, as a multiple of its own natural length
  //: at the glove's scale.
  //:
  //: **Not "however far it is to the edge of the frame", which is what this
  //: did first and what made the arm look like a plank.** Stretching a 360 px
  //: drawing to six hundred smears every muscle in it into a streak, and the
  //: streak is at its worst on a straight punch — the one shot the player
  //: throws most. So the arm keeps its own proportions and its far end fades
  //: out wherever it happens to land, which the sprite carries baked in.
  const FP_ARM_LENGTH = 1.42;
  //: How far a hand held high or low moves the glove up or down the screen,
  //: in pixels per shoulder width. **Much smaller when the arm is out than
  //: when it is folded**, and that is perspective rather than taste: a hand at
  //: the end of a straight arm is twice as far from the eye, so the same
  //: movement of it covers half the angle. The first version used one number
  //: for both and threw the glove off the top of the screen on every jab.
  const FP_REST_LIFT = 84, FP_REACH_LIFT = 132;

  let fpHands = {};

  function resetFirstPersonHands() { fpHands = {}; }

  //: Render smoothing on the glove positions. Heavier than the blade in Fruit
  //: Ninja gets, and it can afford to be: nothing is being aimed with these —
  //: the punch was classified on the Pi from the unsmoothed wrist before this
  //: ever ran — so the only job left is to not shiver.
  const FP_SMOOTH = .34;

  function drawFirstPersonHands(ctx, payload, now) {
    const motion = payload.game.motion || {};
    const posture = motion.posture || {};
    const hands = motion.hands || {};
    const centre = posture.centre;
    const span = Number(posture.shoulder_width) || 0;
    const react = liveReaction(playerReaction, now);
    const hurt = reactionPulse(react, now);

    for (const side of ["left", "right"]) {
      const hand = hands[side];
      // A hand the model has lost keeps its last place and stays drawn. The
      // alternative is a glove that blinks out of the player's own view, which
      // reads as the game breaking rather than as tracking dropping a frame.
      let target = fpHands[side];
      if (hand && centre && span > .02) {
        target = {
          dx: (Number(hand.x) - centre[0]) / span,
          dy: (Number(hand.y) - centre[1]) / span,
          reach: clamp01((Number(hand.extension) - FP_FOLDED) /
                         (FP_STRAIGHT - FP_FOLDED)),
        };
      }
      if (!target) continue;
      const previous = fpHands[side] || target;
      const eased = {
        dx: previous.dx + (target.dx - previous.dx) * FP_SMOOTH,
        dy: previous.dy + (target.dy - previous.dy) * FP_SMOOTH,
        reach: previous.reach + (target.reach - previous.reach) * FP_SMOOTH,
      };
      fpHands[side] = eased;
      drawFirstPersonArm(ctx, side, eased, hurt);
    }
  }

  function drawFirstPersonArm(ctx, side, hand, hurt) {
    if (!fpReady.glove || !fpReady.forearm) return;
    ctx.save();
    // Both arms are drawn as the right one and the left is reflected about the
    // middle of the screen. The sprite is a right arm, the geometry below is
    // written once, and the two hands are guaranteed to be mirror images
    // rather than two sets of numbers that have to be kept agreeing.
    if (side === "left") { ctx.translate(W, 0); ctx.scale(-1, 1); }
    const across = side === "left" ? -hand.dx : hand.dx;
    const t = clamp01(hand.reach);

    const restX = W / 2 + FP_REST_X + across * 96;
    const restY = FP_REST_Y + hand.dy * FP_REST_LIFT + hurt * 34;
    const reachX = W / 2 + across * 300;
    const reachY = FP_REACH_Y + hand.dy * FP_REACH_LIFT + hurt * 34;
    const gx = restX + (reachX - restX) * t;
    const gy = restY + (reachY - restY) * t;
    const scale = FP_NEAR + (FP_FAR - FP_NEAR) * t;

    // The arm points from a fixed anchor off the bottom corner — where the
    // player's shoulder would be — towards wherever the glove is. Only the
    // direction comes from that anchor; the length does not.
    const entryX = W / 2 + FP_ENTRY_X, entryY = FP_ENTRY_Y;
    const theta = Math.atan2(gy - entryY, gx - entryX);

    const forearm = fpRig.forearm, glove = fpRig.glove;
    const axisX = forearm.wrist[0] - forearm.elbow[0];
    const axisY = forearm.wrist[1] - forearm.elbow[1];
    const axisLength = Math.max(1, Math.hypot(axisX, axisY));
    const armLength = axisLength * scale * FP_ARM_LENGTH;
    const elbowX = gx - Math.cos(theta) * armLength;
    const elbowY = gy - Math.sin(theta) * armLength;
    // The sprite was cut from a real drawing, so its own axis is a few degrees
    // off horizontal. Undoing that here is what stops the glove hanging off
    // the side of its own wrist.
    const axisAngle = Math.atan2(axisY, axisX);

    // **The negative y is the arm's roll, and it is not decoration.** The
    // sprite is a right arm drawn reaching away to the right, so its top edge
    // is the outside of the limb. Pointing it up and inward — which is where a
    // guard is — turns it past vertical, and a sprite rotated past vertical
    // has its top edge underneath. That reads as two things at once, and both
    // were reported: each arm upside down, *and* the pair swapped, because an
    // arm reflected along its own length is the other arm.
    ctx.save();
    ctx.translate(elbowX, elbowY);
    ctx.rotate(theta);
    ctx.scale(armLength / axisLength, -scale * FP_ARM_WIDTH);
    ctx.rotate(-axisAngle);
    ctx.drawImage(fpImages.forearm, -forearm.elbow[0], -forearm.elbow[1]);
    ctx.restore();

    // The glove takes the same roll. Its own axis tilt changes sign with it,
    // which is why this adds `axisAngle` where the forearm subtracts it.
    ctx.save();
    ctx.translate(gx, gy);
    ctx.rotate(theta + axisAngle);
    ctx.scale(scale, -scale);
    ctx.drawImage(fpImages.glove, -glove.wrist[0], -glove.wrist[1]);
    ctx.restore();
    ctx.restore();
  }

  function mapBody(body, centreX, shoulderY) {
    const left = body.left_shoulder, right = body.right_shoulder;
    if (!left || !right) return {};
    const centre = (left[0] + right[0]) / 2;
    const span = Math.max(.06, Math.hypot(right[0] - left[0], right[1] - left[1]));
    const sy = (left[1] + right[1]) / 2;
    const mapped = {};
    for (const [name, point] of Object.entries(body)) {
      // PoseService has already mirrored camera x coordinates.  Preserve that
      // screen direction here so the player's anatomical left arm drives the
      // avatar's left arm instead of applying a second mirror and swapping it.
      mapped[name] = [centreX + ((point[0] - centre) / span) * 380,
                      shoulderY + ((point[1] - sy) / span) * 290];
    }
    return mapped;
  }

  let lastFx = 0;
  function stepAndDrawEffects(ctx, now) {
    const dt = Math.min(.05, Math.max(0, (now - (lastFx || now)) / 1000));
    lastFx = now;
    for (const effect of effects) {
      if (effect.impact) {
        effect.age += dt;
        drawImpactEffect(ctx, effect);
        continue;
      }
      effect.age += dt; effect.x += effect.vx * dt; effect.y += effect.vy * dt;
      effect.vy += 340 * dt; effect.vx *= Math.pow(.9, dt * 10);
      const alpha = Math.max(0, 1 - effect.age / effect.life);
      ctx.globalAlpha = alpha; ctx.fillStyle = effect.colour;
      ctx.beginPath(); ctx.arc(effect.x, effect.y, effect.r * (.7 + alpha), 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    effects = effects.filter(effect => effect.age < effect.life);
  }

  function drawImpactEffect(ctx, effect) {
    const t = clamp01(effect.age / effect.life);
    const alpha = 1 - smoothStep(t);
    const radius = (22 + t * 72) * effect.power;
    ctx.save(); ctx.translate(effect.x, effect.y); ctx.globalAlpha = alpha;
    ctx.shadowColor = effect.colour; ctx.shadowBlur = 18;
    ctx.strokeStyle = effect.colour; ctx.fillStyle = effect.colour;
    if (effect.kind === "star") {
      ctx.beginPath();
      for (let i = 0; i < 16; i++) {
        const angle = -Math.PI / 2 + i * Math.PI / 8;
        const r = i % 2 ? radius * .34 : radius;
        const px = Math.cos(angle) * r, py = Math.sin(angle) * r;
        i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
      }
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#fff7dc"; ctx.globalAlpha *= .82;
      ctx.beginPath(); ctx.arc(0, 0, radius * .22, 0, Math.PI * 2); ctx.fill();
    } else if (effect.kind === "shield") {
      ctx.lineWidth = 8; ctx.beginPath();
      ctx.moveTo(0, -radius); ctx.quadraticCurveTo(radius, -radius * .5, radius * .65, radius * .35);
      ctx.quadraticCurveTo(0, radius, -radius * .65, radius * .35);
      ctx.quadraticCurveTo(-radius, -radius * .5, 0, -radius); ctx.stroke();
    } else {
      ctx.lineWidth = effect.kind === "parry" ? 11 : 7;
      ctx.beginPath(); ctx.arc(0, 0, radius, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha *= .48; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(0, 0, radius * .58, 0, Math.PI * 2); ctx.stroke();
      if (effect.kind === "parry") {
        for (let i = 0; i < 8; i++) {
          const a = i * Math.PI / 4;
          ctx.beginPath(); ctx.moveTo(Math.cos(a) * radius * .7, Math.sin(a) * radius * .7);
          ctx.lineTo(Math.cos(a) * radius * 1.45, Math.sin(a) * radius * 1.45); ctx.stroke();
        }
      }
    }
    ctx.restore(); ctx.globalAlpha = 1;
  }

  const skeletonLinks = [
    ["left_shoulder", "right_shoulder"], ["left_shoulder", "left_elbow"],
    ["left_elbow", "left_wrist"], ["right_shoulder", "right_elbow"],
    ["right_elbow", "right_wrist"], ["nose", "left_shoulder"], ["nose", "right_shoulder"],
  ];
  function drawDebugCanvas(ctx, payload) {
    const body = payload.pose && payload.pose.player && payload.pose.player.body || {};
    const mapped = mapBody(body, 640, 620);
    ctx.save(); ctx.strokeStyle = "#52f5ff"; ctx.fillStyle = "#52f5ff"; ctx.lineWidth = 3;
    for (const [a, b] of skeletonLinks) {
      if (!mapped[a] || !mapped[b]) continue;
      ctx.beginPath(); ctx.moveTo(...mapped[a]); ctx.lineTo(...mapped[b]); ctx.stroke();
    }
    for (const point of Object.values(mapped)) {
      ctx.beginPath(); ctx.arc(point[0], point[1], 6, 0, Math.PI * 2); ctx.fill();
    }
    ctx.strokeStyle = "#ffdd52"; ctx.strokeRect(575, 195, 130, 145);
    ctx.strokeStyle = "#ff8a52"; ctx.strokeRect(515, 340, 250, 245);
    const attack = payload.game.ai && payload.game.ai.attack;
    if (attack && attack.trajectory) {
      ctx.strokeStyle = "#ff4e78"; ctx.lineWidth = 6; ctx.beginPath();
      ctx.moveTo(attack.trajectory[0] * W, attack.trajectory[1] * H);
      ctx.lineTo(attack.trajectory[2] * W, attack.trajectory[3] * H); ctx.stroke();
    }
    ctx.restore();
  }

  function drawDebug(payload) {
    const panel = gameEl("game-debug");
    if (!payload.debug) { panel.classList.remove("on"); return; }
    const stats = payload.motion && payload.motion.stats || {};
    const game = payload.game || {};
    const motion = game.motion || {};
    const hands = motion.hands || {};
    const ai = game.ai || {};
    const attack = ai.attack || {};
    const left = hands.left || {}, right = hands.right || {};
    panel.innerHTML =
      `<b>BOXING MOTION DEBUG</b><br>` +
      `gesture <b>${motion.gesture || "none"}</b> · confidence ${motion.confidence || 0}<br>` +
      `left (${left.x || 0}, ${left.y || 0}) v ${left.speed || 0} · right (${right.x || 0}, ${right.y || 0}) v ${right.speed || 0}<br>` +
      `guard ${motion.guard || "none"} · torso ${motion.posture ? motion.posture.torso_angle : 0}°<br>` +
      `AI ${ai.state || "off"} · attack ${attack.kind || "none"} · impact ${attack.impact_in ?? "—"}<br>` +
      `parry window ${attack.parry_window ?? "—"} · scale ${game.simulation_scale || 1}<br>` +
      `camera ${stats.camera_fps || 0} FPS · pose ${stats.inference_fps || 0} FPS · game ${Math.round(lastRenderFps)} FPS<br>` +
      `pipeline ${stats.pipeline_ms || 0} ms · dropped ${stats.dropped || 0}`;
    panel.classList.add("on");
  }

  gameEl("boxing-training").addEventListener("click", () => gameCommand("mode-training"));
  gameEl("boxing-fight").addEventListener("click", () => gameCommand("mode-fight"));
  for (const button of document.querySelectorAll("[data-boxing-difficulty]")) {
    button.addEventListener("click", () => {
      for (const peer of document.querySelectorAll("[data-boxing-difficulty]")) peer.classList.remove("selected");
      button.classList.add("selected");
      gameCommand(`difficulty-${button.dataset.boxingDifficulty}`);
    });
  }
  gameEl("boxing-fullscreen").addEventListener("click", () => {
    const target = gameEl("page-game");
    if (!document.fullscreenElement && target.requestFullscreen) target.requestFullscreen().catch(() => {});
    else if (document.exitFullscreen) document.exitFullscreen().catch(() => {});
  });
  gameEl("boxing-mode-back").addEventListener("click", () => show("games"));

  window.BoxingUI = {
    onOpen, stop, resetRound, applyState, handleEvent, render,
    positions: () => ({ playerX: 640,
                        opponentX: Math.round(opponentScreenX) }),
  };
})();
