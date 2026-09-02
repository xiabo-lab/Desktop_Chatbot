# Fruit Ninja Player Experience Improvement Plan

Status: implementation-ready plan  
Scope: AIPI5's local, camera-controlled Fruit Ninja game  
Primary platform: 1280 x 800 Raspberry Pi kiosk with Logitech BRIO and Hailo pose estimation

## 1. Outcome

Make the game feel like the player's arms are directly connected to two reliable blades, teach the interaction without a controller, maintain a readable difficulty arc for a full two-minute round, and end every run with a clear sense of progress.

The work should improve five outcomes in this order:

1. Trust: a visually convincing hit scores, a deliberate dodge avoids a bomb, and feedback arrives immediately.
2. Learnability: a first-time player can frame themselves, start, slice, and understand hazards without reading a manual.
3. Feel: cuts, combos, power-ups, bombs, and the Ultimate have distinct, proportional audiovisual weight.
4. Flow: the game offers an explicit challenge level that matches the player without changing the rules invisibly mid-round.
5. Replay value: results explain what improved and give the player a reason to try once more.

## 2. Current baseline

The plan extends the current game rather than proposing a rewrite.

| Area | Current implementation | Implication |
|---|---|---|
| Runtime | Python owns simulation, collision, scoring, lives, phases, and RNG; the browser renders snapshots | Preserve this split; it minimizes pose-to-hit latency and makes rules deterministic |
| Input | Two filtered wrists from a 640 x 480 at 120 fps camera path; crossed-arm X starts and replays | Onboarding and settings must remain hands-free at the play boundaries |
| Rendering | Canvas 2D playfield at 60 Hz plus DOM HUD and sheets | Keep Canvas for the main plan; use DOM for readable text and controls |
| Core round | Fixed 120 seconds, five lives, bombs remove one life and 25 points, ordinary misses do not remove lives | Keep score comparability and the fairness distinction between missed fruit and bombs |
| Content | Ten fruit, bombs, heart fruit, ice cube, combos, streaks, splashes/halves, and an Ultimate dragon fruit | Improve hierarchy and teaching before adding more objects |
| Reach assist | The blade is drawn 54 px wide and fruit collision includes half that width; bombs do not | Preserve the reward-generous/hazard-exact convention |
| Performance | The project has already removed a large player silhouette and reduced motion-path latency; browser drawing is now the likely contention point under load | Profile effect cost before changing the camera or model again |
| Verification | Seeded Python simulation, collision and lifecycle tests, synthetic browser rendering, debug telemetry, and device playtest history | Extend the same evidence chain for each improvement |

Current verification for this plan: `tests/test_fruit_ninja.py`, `tests/test_slash_collision.py`, `tests/test_ultimate.py`, and `tests/test_ui_assets.py` pass: 245 tests and 309 subtests.

## 3. Experience definition

### Player promise

"Move like a ninja: your real swings become bright blades that cut exactly what they appear to touch."

### Primary audience

- A household player standing several feet from the display.
- A first-time or occasional player who does not know gesture controls.
- A returning player chasing a personal best.

### Target aesthetics

- Sensation: crisp contact, juicy cuts, distinct materials, and readable motion.
- Challenge: choosing arcs and steering around hazards under increasing pressure.
- Mastery: cleaner targeting, longer combos, two-hand alternation, and a stronger Ultimate finish.

### The 30-second loop

1. See one or more readable arcs.
2. Choose a target and a safe path around bombs.
3. Swing a hand through the target.
4. See and hear contact immediately.
5. Read points, combo, power-up, or penalty without losing sight of the next target.
6. Reposition both hands and make the next decision.

Every proposed feature must strengthen this loop or the start/results boundaries around it.

## 4. Success metrics and measurement

Capture a baseline from at least 10 complete rounds across at least three people before tuning balance. Keep raw camera images and raw pose frames out of analytics.

| Metric | Target | Collection method |
|---|---:|---|
| Ready-to-first-valid-slice, new player | median <= 20 s; p90 <= 35 s | timestamps for player-ready, start, and first slice |
| Start gesture success | >= 95% within two attempts after readiness | count X attempts and accepted starts |
| Hand-to-visible-feedback | median <= 55 ms; p95 <= 85 ms | pose timestamp -> slice event -> displayed frame probe |
| Apparent fruit false miss | < 2% in instrumented calibration swipes | compare visible swept capsule with authoritative collision |
| Bomb readability | >= 90% of players can explain the bomb after one encounter | short observation prompt after play; no tutorial answer first |
| Early game over from bombs | < 10% of Classic rounds before 60 s | finish reason and elapsed time |
| Classic completion | >= 80% of started rounds reach the Ultimate | phase and finish reason |
| Ultimate completion | 50-75% for Classic target cohort | Ultimate hits/completed; adjust explicit mode parameters, not live score |
| Render performance | median >= 55 fps; 1% low >= 45 fps | browser debug telemetry during slice burst and Ultimate |
| Pose performance | median >= 30 fps during play | existing motion telemetry |
| Replay intent proxy | >= 40% choose replay after a completed round | results-sheet action |
| Accessibility | all critical state understandable with sound off and with reduced effects | scenario checklist and visual inspection |

Do not treat a single expert player's score as balance evidence. Segment results by first-time, casual, and experienced players.

## 5. Priority roadmap

### Phase 0 - Establish the evidence loop (P0, small)

Goal: make every later change measurable and reproducible.

#### Work

- Add a privacy-preserving per-round summary:
  - mode and duration;
  - start readiness time and X attempts;
  - fruit shown/sliced/missed by kind;
  - bombs shown/hit;
  - hearts and ice collected;
  - longest streak and combo histogram;
  - Ultimate hits/completion/points;
  - finish reason and elapsed time;
  - pose FPS, render FPS, and latency percentiles.
- Add a concise browser inspection hook, `window.render_game_to_text()`, that returns only current visible/interactable state and the screen coordinate convention.
- Add a test-only client FX clock such as `window.advanceGameRender(ms)`. It may advance particles, banners, and fades, but it must not advance authoritative gameplay.
- Create seven deterministic screenshot scenarios: opening, normal slice, three-fruit combo, bomb, heart/ice, Ultimate, and results.
- Add a balance-report script that runs many seeded Python rounds and prints spawn counts, hazard distribution, score components, life outcomes, and phase timing.

#### Files

- `aipi5/games/fruit_ninja/game.py`: summary fields and snapshot exposure.
- `aipi5/games/fruit_ninja/fruit.py`: no behavior change; expose stable kind categories if needed.
- `aipi5/ui/web/index.html`: text-state and client-FX test hooks.
- `tests/test_fruit_ninja.py`, `tests/test_ultimate.py`, `tests/test_ui_assets.py`: contracts.
- New `scripts/report_fruit_ninja_balance.py`: seeded, read-only report.

#### Exit criteria

- The same seed produces the same spawn and score summary.
- Text state matches the visible screenshot in all seven scenarios.
- No camera image, joint coordinates, or personally identifying data is persisted.

### Phase 1 - Teach through play (P0, medium)

Goal: remove uncertainty before the score begins.

#### Work

1. Replace the passive ready sheet with a three-state, hands-free flow:
   - Frame: show the existing live preview and one short dynamic instruction from pose advice.
   - Find blades: display left/right blade heads and ask the player to move each into an edge target.
   - Practice: throw three large harmless fruit, one at a time, with no score, bombs, lives, or timer.
2. After the third valid cut, show the X gesture and start Classic. Returning players can skip practice after one successful completed round.
3. Introduce the first real bomb after the player has cut at least three scored fruit and after the existing time gate. Both conditions must be satisfied.
4. On the first heart and ice pickup, show one two-second contextual label near the pickup result, never a persistent tutorial panel.
5. If no valid slash occurs for eight seconds during the opening, show a single contextual cue: "Move through the fruit" with a short animated path. Remove it immediately after a valid slice.

#### UX details

- Keep one instruction on screen at a time.
- Use the same visual blade and collision reach in practice as in the round.
- Never require a pointer click to continue.
- Do not restore the removed player silhouette; blade heads and the live preview supply alignment.
- A player who loses pose readiness during practice returns to Frame without losing completed practice steps.

#### Exit criteria

- A new player can start and slice without verbal coaching.
- Practice cannot change high scores, lives, or round statistics.
- Ready -> practice -> start, readiness loss, pause, replay, and exit all reset correctly.

### Phase 2 - Strengthen action feedback (P0, medium)

Goal: make every event distinct while keeping the next target readable.

#### Feedback tiers

| Event | Tier | Visual | Audio | Timing |
|---|---|---|---|---|
| Ordinary fruit | Small | blade contact spark, existing halves/juice, 8-12% squash on the first debris frame, short score pop | material-specific cut with slight non-repeating pitch variance | start in the first rendered frame after the event; settle < 220 ms |
| Rare/high-value fruit | Small+ | brighter contact star and value-colored score edge | slightly brighter transient, not louder than a combo | settle < 250 ms |
| 3+ combo | Medium | trail briefly intensifies, combo text overshoots once, droplets inherit combo accent | short ascending motif; reset after combo window | peak < 100 ms; clear without covering targets |
| Heart | Medium | heart-to-HUD arc plus life slot pulse | warm pickup tone layered over cut | HUD update visible < 150 ms |
| Ice | Medium | existing frost frame plus a short onset wave | glass crack plus low-pass transition | onset < 150 ms; state remains visible for effect duration |
| Bomb | Large | local red shock ring, edge vignette, one life visibly empties, `-25` anchored near bomb | low impact plus warning transient; briefly duck routine SFX | no world displacement; main flash <= 80 ms |
| Ultimate hit | Medium | local recoil/crack growth/ring acceleration | alternating hit voices supporting two-hand rhythm | no authoritative hit-stop |
| Ultimate complete | Large | burst, banner, and a restrained full-edge pulse | unique resolved cadence | keep center clear again within 1.2 s |

#### Technical rules

- Drive effects from existing event names. Add an event only when the state transition is genuinely discrete.
- Define small/medium/large constants in one client feedback configuration object.
- Do not random-jitter the whole canvas. Local effects should use seeded or smoothly varying motion.
- If a 30-45 ms visual hold improves a heavy cut, hold only the new fruit halves; continue rendering blades, accepting input, and advancing Python simulation.
- Cap particle lists and reuse objects in hot paths. Use existing adaptive particle budget behavior.
- Add reduced-motion, reduced-flash, SFX level, and mute settings before enabling stronger effects by default.

#### Exit criteria

- Players can distinguish a fruit, combo, power-up, bomb, and Ultimate completion from either visuals alone or audio plus minimal vision.
- Effects return to rest, do not block input, and do not reduce the render 1% low below 45 fps on the Pi.

### Phase 3 - Make challenge explicit and fair (P1, medium)

Goal: let different players enter the flow channel without corrupting score comparability.

#### Modes

Add explicit, separately scored modes after Phases 0-2 validate the core feel:

| Mode | Duration | Intent | Initial tuning direction |
|---|---:|---|---|
| Gentle | 90 s | first-time, mobility-limited, or low-confidence player | larger fruit reach assist, 5 lives, fewer bombs, slower ramp, no triples; Ultimate requires fewer valid hits |
| Classic | 120 s | current comparable high-score game | preserve current baseline until telemetry supports a change |
| Expert | 120 s | experienced player seeking precision | standard blade reach, 3 lives, earlier doubles/triples, modestly higher bomb share, same hazard-exact collision |
| Practice | untimed | learn targeting and two-hand alternation | no bombs, no high score, selectable fruit/power-up drills |

#### Balance policy

- Store a separate best score per mode and duration.
- Never silently increase spawn rate from current score during a scored mode.
- It is acceptable to recommend a mode after a round based on completion, bomb hits, and accuracy; the player chooses it on the next start.
- Change one of interval, clump probability, bomb probability, reach, or lives at a time and compare seeded distributions.
- Keep at least 1.5 seconds between unavoidable hazard decisions and prevent bomb-only clumps.
- Add a recent-bomb spacing rule if telemetry shows clustered life loss, but do not guarantee a fixed pattern that removes uncertainty.

#### Exit criteria

- Mode name is visible on start and results screens.
- Scores cannot leak between modes.
- Seeded reports show each mode's intended spawn, bomb, and completion envelope.
- At least three players in each target cohort complete two rounds without needing hidden tuning.

### Phase 4 - Improve results and replay motivation (P1, small to medium)

Goal: convert "game over" into understandable progress and one clear next goal.

#### Results sheet

Keep the first view compact:

- score and personal best;
- new-best celebration when applicable;
- accuracy = sliced / reachable ordinary fruit shown;
- longest streak and best combo;
- bombs hit and lives remaining;
- Ultimate hits and completion;
- one personalized next goal.

Examples of next goals:

- "Avoid one more bomb to reach the Ultimate."
- "Your best combo was 5. Try for 7."
- "Alternate hands during the Ultimate for faster hits."

Put the detailed score breakdown behind a secondary "Details" action:

- fruit points by kind;
- combo contribution;
- power-ups collected;
- Ultimate points;
- bomb penalty;
- comparison with the player's previous five local rounds.

Do not add an account, cloud profile, currency, daily streak pressure, or loot system. Local mastery and personal bests are sufficient for this household game.

#### Exit criteria

- The displayed components reconcile exactly to the final score.
- The next-goal rule is deterministic and never shames a low-performing player.
- Replay and return-to-menu remain hands-free and teardown-safe.

### Phase 5 - Accessibility and physical comfort (P1, medium)

Goal: keep the game playable for different vision, hearing, motion tolerance, and reach.

#### Settings

- Reduced motion: remove overshoot and strong pulses; keep state changes visible.
- Reduced flash: replace white flashes with solid edge rings and longer low-intensity fades.
- High contrast: increase fruit/bomb rim separation and HUD backing contrast.
- Color support: never encode fruit value, power-up state, lives, or hazards by hue alone; retain shape, icon, and motion differences.
- Sound: master mute, SFX level, and optional spoken short cues for start, bomb loss, and final ten seconds.
- Reach calibration: derive a comfortable left/right/top envelope during onboarding and map it to the playfield for Gentle and Practice. Never use it to enlarge bomb collision.
- Rest support: offer 60-90 second modes and a clear pause without penalizing the clock.

#### Physical-safety constraints

- Avoid instructions that encourage maximal-speed swings near the display or furniture.
- Detect repeated edge-reaching failures and recommend one step back or Gentle mode rather than asking for harder swings.
- Keep two-hand alternation a bonus strategy, not a requirement outside the Ultimate.

#### Exit criteria

- The complete game can be understood with sound muted.
- Reduced-flash mode contains no full-screen whiteout.
- Reduced-motion mode changes presentation only, never timing, collision, or score.
- Reach calibration is local to the session or local profile and stores no body imagery.

### Phase 6 - Optional content and technology gates (P2, only after core metrics pass)

#### Generated asset pipeline

Use an image-generation tool such as Higgsfield only for concept exploration or licensed asset production.

Gate requirements:

- define a style formula, palette, outline weight, light direction, and material language first;
- keep source prompt, model/tool, date, license/terms, and post-processing record;
- export optimized WebP/PNG sprite sheets with deterministic fallback Canvas shapes;
- test memory, decode time, and frame rate on the Pi;
- do not add network calls to gameplay or make the game unavailable without the generator.

Recommended first experiment: one alternate dojo background and one consistent fruit-cut sprite treatment, A/B tested against the current procedural art. Do not regenerate all assets before one slice proves better.

#### Three.js or WebGL spike

Do not migrate the game to 3D as part of the main roadmap. Run a time-boxed spike only if a specific requirement cannot be met in Canvas, such as lit 3D fruit with convincing cut planes.

The spike passes only if it:

- preserves Python-authoritative collisions and DOM HUD;
- sustains the render and pose budgets under a worst-case Ultimate;
- loads offline and resets without leaked GPU resources;
- improves player-rated clarity or feel, not only screenshot detail.

#### Multiplayer and social play

Prefer low-risk asynchronous features in this order:

1. same-device turn-taking with separate local names;
2. household challenge seed and score comparison;
3. recorded input ghost made from abstract blade paths, not camera video;
4. opt-in remote leaderboard;
5. real-time multiplayer only after a separate product decision.

If networking is added, the server validates seeds, scores, timestamps, and game events. Do not transmit raw camera frames or full pose skeletons by default.

## 6. Architecture and ownership

| Concern | Owner | Notes |
|---|---|---|
| Fruit physics, spawn rules, scoring, lives, phases, RNG | `aipi5/games/fruit_ninja/` | Authoritative and deterministic |
| Slash geometry and assist radius | `collision.py` plus `_blade_reach` | Reward reach may be assisted; bomb reach stays exact |
| Ultimate rules | `ultimate.py` | Per-hand cooldown and movement gates remain server-side |
| Snapshot and event contract | `game.py` | Version fields additively; avoid high-frequency histories |
| Canvas playfield and client effects | `aipi5/ui/web/index.html` | Render/extrapolate only; pool effects and cap delta |
| HUD, start, pause, settings, results | DOM/CSS in `index.html` | Protect center playfield; one primary and one secondary corner cluster |
| Motion tracking and filters | `aipi5/motion/` | Change only with profiling and cross-game regression tests |
| High scores and local history | game manager/high-score store | Namespace by mode and duration; bounded local history |

## 7. Verification strategy

### Unit and simulation tests

- Collision at fruit center, edge, assisted edge, and bomb unassisted edge.
- Low-speed hand, reappeared hand, stale hand, and path-crossing moving fruit.
- Each mode's start state, duration, lives, spawn policy, score namespace, and Ultimate threshold.
- Practice produces no score, best, life, or persistent history mutation.
- Heart at full lives, ice overlap/expiry, simultaneous fruit and bomb, last-life multi-hit, and pause during effects.
- Every results component reconciles to final score.
- Seeded distribution assertions use broad statistical envelopes, not exact random sequences unless testing reproducibility.

### Browser and visual tests

- Inspect screenshots at 1280 x 800 for all seven deterministic states.
- Verify HUD contrast against the brightest and darkest wood regions.
- Verify every fruit, bomb, heart, and ice cube remains identifiable in grayscale and common color-vision simulations.
- Verify reduced-flash and reduced-motion snapshots.
- Exercise start, pause, resume, restart, replay, return, and page reload while effects are active.
- Check `render_game_to_text()` agrees with visible entities, score, lives, clock, phase, power-up, and overlay state.

### Device performance tests

Use four fixed scenarios on the Pi:

1. idle ready screen with a player detected;
2. ordinary play with six or more objects;
3. a burst of slices plus a bomb effect;
4. repeated Ultimate hits and completion.

For each, record pose FPS, render FPS, capture-to-pose latency, effect-list depths, CPU, memory, temperature, and dropped frames. Compare to the pre-change capture under the same camera mode and lighting.

### Human playtests

Run observation-first sessions:

- Ask the player to start without verbal instructions.
- Do not explain bombs, hearts, ice, or combos before the first round.
- Note hesitation, off-screen attention, false-miss complaints, accidental bombs, fatigue, and replay choice.
- After the round, ask what each special object did and which feedback felt strongest.
- Change only one material variable for the next build.

## 8. Rollout sequence

1. Land Phase 0 instrumentation and deterministic inspection hooks.
2. Capture the baseline rounds.
3. Land Phase 1 onboarding and validate first-slice time.
4. Land one Phase 2 feedback tier at a time: fruit, combo, power-ups, bomb, Ultimate.
5. Add accessibility controls before making large feedback the default.
6. Rebaseline performance and player behavior.
7. Add explicit modes only after Classic is trustworthy and readable.
8. Add the results breakdown and next-goal system.
9. Consider generated art, 3D, or social features only through their gates.

## 9. Definition of done

An improvement is complete only when:

- its player problem and expected metric movement were written before implementation;
- authoritative rules remain in Python and presentation remains in the browser;
- affected unit, lifecycle, UI, and seeded-balance tests pass;
- deterministic screenshots and text state were inspected;
- accessibility alternatives exist for strong motion, flash, or audio cues;
- the Pi meets the performance budgets in the worst relevant scene;
- at least one real player exercised the changed behavior end to end;
- the observed outcome, metrics, trade-offs, and any remaining risk are recorded.

## 10. Explicit non-goals

- No full engine rewrite.
- No 3D conversion without a measured product need.
- No invisible score-based difficulty manipulation in comparable modes.
- No cloud account, monetization, loot economy, or engagement-pressure system.
- No real-time multiplayer in the core improvement cycle.
- No raw camera or full-body pose retention for analytics.
- No algorithmic auction/mechanism-design work; that similarly named skill is unrelated to video-game feel.
