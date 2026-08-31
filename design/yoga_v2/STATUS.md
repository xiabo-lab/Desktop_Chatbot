# Yoga Coach v2 — where the work stands

**Last updated:** 24 August 2026. Written as a handover: read this
first, then `coach3d.md` for how the 3D coach works.

---

## Course transition pass complete and deployed to the Pi

24 August twisted-ankle follow-up: the final ankle solve no longer measures an
unsigned foot-to-shin cone or preserves the current toe-side tangent. Both
behaviours allowed a 180-degree foot reversal to appear numerically valid. It
now reconstructs every out-of-range foot from a bind-relative neutral frame,
separately constraining signed plantar/dorsiflexion (-50/+20 degrees), toe-in/
toe-out (-15/+20), and eversion/inversion (-20/+30). A rear-hemisphere request
is folded continuously back into the forward cone; unstable sole roll and
side deviation fade before the 90-degree singularity. Explicit left- and
right-foot 180-degree offsets can no longer reverse either toe axis.

The regression pass covers both feet at 40 frames through all 206 unique
transition segments used by all 21 courses (16,480 generated foot samples),
plus both feet in all 91 held poses. It checks each anatomical range, requires
the toes to remain in the forward hemisphere, and rejects a per-frame terminal
reversal. The reported Full Body Reset moves 15 and 19 were visually checked
at the supplied timestamps from both sides. The JavaScript rig suite is
**40/40**; the full suite is **1001 passed, 22 skipped, 718 subtests**. Current
browser cache tag: `20260824-ankles5`.

Deployed to `aipi5` at 13:45 PDT on 24 August 2026. Both `aipi5.service`
and `aipi5-ui.service` are active and enabled, user lingering is enabled, the
live server returns cache tag `20260824-ankles5`, and startup preflight reports
camera, person detection, display, wake word and remote calls ready. Local/Pi
SHA-256 values match for `coach3d.js`
(`df976cccdf30c49486d1254e15098e473610afddef6711eb5a2abe07e1a70f33`),
`stage.html` (`6e519385a25abe5ffda572575b0105bba95f5b40ffdf11576c418e4c3245db05`)
and `yoga3d.js` (`f3ff11db9e0a3d10e7e1de1339e57ba70abb14372011e2a3603f997d225c1ced`).

## How the transition action problem was solved

1. **Route human actions, not pose-to-pose morphs.** The 91 approved poses are
   grouped into standing, lunge, wide-standing, quadruped, kneeling, seated,
   prone and supine families. A shared route graph inserts approved neutral
   stations such as Mountain, Forward Fold, Tabletop, Staff and Savasana.
   Large family changes therefore happen as actions a person can perform.
   Left/right swaps unload the active side before the other side moves.
2. **Interpolate joints in their anatomical frame.** Root, pose plane and 3D
   corrections use quaternion shortest-path interpolation. Knees and elbows
   interpolate as parent-relative hinges; large leg changes first release the
   knee, move from the hip on separated arcs, then bend again. Large shoulder
   sweeps travel through the body's front/outside space instead of through the
   back or torso. Time is divided by movement effort, not equally per station.
3. **Solve palms and feet after the limb chains.** Supporting palms are aligned
   to the floor, excess palm turn is routed through forearm pronation, and the
   remaining wrist bend is clamped. Feet use a bind-relative signed frame for
   flexion, toe direction and sole roll. The final terminal pass runs on every
   transition frame and held pose, so hand/foot offsets cannot reintroduce a
   backward bend or 180-degree flip.
4. **Validate volume and continuity, not only endpoint angles.** Intermediate
   frames are sampled for new capsule-to-core and limb collisions. Natural
   contact already present in an approved endpoint is allowed; newly created
   penetration is rejected. Separate checks cover hinge bounds, forward arm
   travel, knee hyperextension, terminal hemisphere and abrupt flips.
5. **Review the same runtime that ships.** The black-background course reviewer
   accelerates every complete course to under two minutes, supports pause and
   move/course stepping, and permits multi-angle inspection. Problems found in
   review became shared solver constraints and full 21-course regressions,
   rather than pose-specific animation patches.

24 August palm/wrist follow-up: final hand orientation is now constrained
relative to the solved forearm after semantic palm placement and manual hand
offsets. Ordinary wrists allow 80 degrees flexion, 70 extension, 20 radial
deviation and 35 ulnar deviation. An explicit weight-bearing `floor` palm may
reach 90 degrees while it is loaded, then the allowance fades as the hand
leaves the floor. Excess sideways palm turn is routed through lower-forearm
pronation/supination rather than bending the wrist. Floor contact is applied
separately, so terminal roll offsets cannot lift a supporting palm. Semantic
palm modes now quaternion-blend instead of switching at the midpoint. All 91
holds and 16,068 intermediate hand samples across the 206 unique segments used
by all 21 courses pass. Visual checks cover Tabletop/Downward Dog support,
palm-up/down, inward knee grasp, behind-back palms and Thread the Needle. The
JavaScript rig suite is **39/39**; full suite **1001 passed, 22 skipped, 718
subtests**. Browser cache tag: `20260824-palms4`.

24 August ankle follow-up: all displayed frames now pass a final
parent-relative foot solve after semantic sole/palm alignment and manual 3D
offsets. Dorsiflexion is limited to +20 degrees, plantar flexion to -50,
inversion to +30 and eversion to -20. The constraint runs during moves,
waypoints and held poses, so a course boundary cannot show one invalid frame.
The reported Full Body Reset moves 18, 19 and 21 were checked again from the
same low/rear viewpoints; their heel-up/backward foot fold is gone. Automated
coverage now checks both feet in all 91 held poses and 39 intermediate frames
of every unique transition segment used by all 21 courses. The JavaScript rig
suite is **35/35** and the full suite remains **1001 passed, 22 skipped, 718
subtests**. Browser cache tag: `20260824-ankles2`.

24 August transition-safety follow-up: the supplied 30-rule anatomy guideline
is now implemented in the shared browser solver rather than as pose-specific
exceptions. Generated motion uses quaternion SLERP for root, plane, head,
wrist, ankle and authored 3D corrections; all chains remain body-relative
while the pelvis/torso turns. Large legs release the knee, move from the hip on
separated left/right arcs, then reapply flexion. Large arms travel forward and
outward, while an explicitly behind-the-back destination is approached around
the outside of the ribs. Knees, elbows and neck are interpolated relative to
their parent joint. Transition time is divided by anatomical movement effort
instead of equally per waypoint. A reusable `JOINT_SAFETY` table records the
practical limits, hyperextension allowance and target movement rates.

The complete 21-course validation samples 28,291 intermediate frames for new
deep capsule-core penetration and 40 frames across every unique station pair
for hinge range and rotational continuity. The initial safety pass exposed 41
transition-created collision pairs; the final pass has zero. Visual checks
cover front, side and rear views of the reported arm raise, behind-back
shoulder entry, folds, and Savasana-to-Knees-to-Chest leg sequencing. The
approved 91 bone/body shapes remain unchanged; only final foot orientation is
constrained where an authored terminal would exceed human ankle range.

24 August anatomical interpolation follow-up: transition arms no longer use a
flat shortest-angle sweep for large shoulder changes. They travel through the
coach's body-relative front hemisphere, including when the body is turned or
lying down. Shins now interpolate as knee flexion relative to their thighs,
so two valid endpoints cannot create a backwards knee between them. The
complete 21-course route was sampled at more than 40,000 knee positions across
over 500 station-to-station segments with no transition-created
hyperextension. Visual checks cover the reported moves 5–7 and 25–27. The
full suite remains **1001 passed, 22 skipped, 718 subtests** and the expanded
JavaScript rig suite is **30/30**.

24 August follow-up: the reviewer and production 3D mount now use a completely
black scene with no ground circle and no yoga mat. Transition routing was made
more conservative after owner review: every substantial same-family change
unloads through its neutral approved pose, and cross-family routes now use
Mountain/Upward Salute, Forward Fold/Downward Dog/Tabletop, Staff, or Savasana
as appropriate. Visual midpoint checks from front and 90° caught a remaining
Tabletop-to-Easy-Seat thigh sweep; that segment was removed. Supported floor
poses now lower through Tabletop and form Staff with both legs parallel before
any crossed seated pose. Supine side changes return through Savasana. The
updated full suite is **1001 passed, 22 skipped, 718 subtests**; the JavaScript
rig suite remains 27/27.

All 605 entries into poses across the 21 courses (260 unique directed edges)
now use the shared transition planner in `transitions.py`. Large changes move
through approved human-scale stations such as Forward Fold, Low Lunge,
Tabletop, Child's Pose, Easy Seat, Staff, Knees-to-Chest, Downward Dog and
Plank. Left/right swaps unload through a neutral pose instead of sweeping the
active limbs through the body. The runtime also interpolates `bones3d` and
`terminal3d` continuously, removing the old halfway pop in head, palm, and
foot corrections.

The browser reviewer is at
`/assets/yoga/v3/stage.html?transitions=1&course=beginner_01`. It contains all
21 courses in curriculum order, with previous/next course, previous/next move,
pause and restart controls. Each movement gets 2.6 seconds plus a 0.4-second
arrival. Complete course reels range from 69 to 102 seconds, so every one is
under two minutes. Deep links can add `&move=N&at=0.5&paused=1` to freeze a
specific transition midpoint.

Visual checks covered standing-to-Plank, standing-lunge-to-supine,
supine-to-kneeling, standing side swaps, and supine side swaps. That pass
caught a crossed-leg-to-supine thigh sweep; all seated/supine routes now
unfold through approved Staff Pose before rolling down. The full suite is
**1000 passed, 22 skipped, 718 subtests**, and the JavaScript rig suite is 27/27.
The PC reviewer is live on port 8774. The same transition build is now live on
the Pi; Windows SSH uses the configured `aipi5` alias for deployment.

---

## All 91 poses approved; final head review imported and deployed

The final six-pose export, `yoga-pose-review (3).json` (SHA-256
`5683701d2882afb309a6b17f4157d05988f63b086b88ffefeb62221c86f3b823`),
approved and edited Seated Hamstring L/R, Standing Knee L/R and Supine Twist
L/R. `approved_pose_edits_fixed_6.json` preserves all six objects verbatim,
including the owner's ±80° Supine Twist face turns. There are now 89 protected
owner approvals; Sphinx and Staff had no status in the prior 31–91 export.

The PC/Pi reviewer now starts a fresh all-catalog pass: exactly 91 poses,
original numbering #1–#91, zero carried review decisions, and a new pose-edit
namespace. The imported body definitions remain the base, so this pass can
concentrate on head direction without rebuilding approved anatomy.

That pass is now complete. `yoga-pose-review (4).json` contains 91 approvals,
zero needs-fix records, and 27 exact edited poses. Its SHA-256 is
`03eb9ef00ed088e1b3ca26c133026366f67b28538802f4d0424ebffbb09909ac`.
`approved_pose_edits_head_91.json` is an exact compact copy of its approval
order and `pose_edits`; the loader de-duplicates historical approval names and
applies this final batch last. Sphinx and Staff are now approved, so the
protected approval set is exactly 91 unique pose IDs.

The editor exposes bounded three-axis head controls: look up/down (±60°), look
left/right (±80°), and ear tilt (±45°). They write the neck's `bones3d` XYZ
array into the exported exact pose; neck-axis Y rotation turns the face itself
instead of only changing the neck chain direction. The rig regression suite
tests independent face turn and head bend.

All 91 anatomy checks, 25 JavaScript rig tests, and the full suite pass:
**997 passed, 22 skipped, 718 subtests**. The corrected v3 assets are deployed
to the Pi; `aipi5` and `aipi5-ui` are active and enabled. Local and Pi SHA-256
values match for `rigdata.json`
(`9aee3e920457210f956084ab59abfa4f1ca045b2baf6e958caaf9e1829a2b123`)
and `stage.html`
(`347fcdd00a272e0f469a45def62c7b6766f6f42e409de21c9481db9ffcf4c914`).

---

## One paragraph

The Yoga Coach has been rebuilt as a **runtime 3D character** — a rigged VRM
posed from the same bone tables the player is scored against — replacing 97 MB
of generated 2D clips with 117 kB of JSON. It runs at **60 fps on the Pi, even
with the camera and the Hailo pose pipeline running alongside it**. The
production browser now mounts that same runtime coach, offers all 21 courses,
and uses each course's authored 15–20 minute duration. Guided poses show their
cue without accuracy or score. The 2D coach remains an automatic fallback if
WebGL or the VRM cannot load.

---

## Done, and verified

### The 3D coach
- `aipi5/ui/web/assets/yoga/v3/coach3d.js` — retargeting, the `root`/`plane`
  split, `bones3d` timing, orbiting camera, blending, breathing. No per-pose
  hacks anywhere in it.
- `design/yoga_v2/poses3d.py` — 60 authored guided poses, five camera presets,
  47 poses with a camera angle chosen to teach them.
- `design/yoga_v2/check3d.py` — validates every pose by arithmetic: floor
  contact, size against its camera, knees and elbows bending the right way.
  Currently prints `every pose stands up`.
- `scripts/build_yoga_v3_data.py` — writes `rigdata.json` (117 kB): rig, 32
  scored tables, 91 catalog entries, 21 courses with per-step transitions,
  60 3D poses, camera views.
- `tests/coach3d.test.mjs` — 21 tests, no GPU needed, wired into `pytest` via
  `tests/test_yoga_coach3d.py`.

### Production Python — **integrated and deployed**
- `aipi5/games/yoga/curriculum.py` *(new)* — loads `rigdata.json`, the same file
  the browser draws from, so the coach and the game cannot name different sets
  of poses.
- `aipi5/games/yoga/poses.py` — now holds **91** poses in four named sets:
  `AUTHORED` (32, hand-tuned, the only ones with 2D artwork), `SCORED` (36),
  `PROMOTED` (5, scored off demonstration tables at `tolerance_scale=1.25` —
  untuned, see below), `GUIDED` (55). `Pose.scored` is the flag;
  `Pose.targets` **raises** for a guided pose rather than returning nonsense.
- `aipi5/games/yoga/lesson.py` — the **21 courses**, built from the curriculum,
  with per-step reviewed transitions. All 21 land **15.0–19.6 minutes**. The
  original three are kept as `LESSONS` and come back with
  `AIPI5_YOGA_LEGACY=1`.
- `aipi5/games/yoga/game.py` — `_follow()` runs guided poses: the hold is spent
  by the clock, nothing is measured, the cue stays on screen. Guided steps leave
  no result, so the overall score is an average of poses actually marked.
  `summary["total"]` is now scored poses; `summary["poses_shown"]` is all of
  them. The snapshot carries `pose.scored`, `course` and `scored_poses`.
- Tests updated to say which set each invariant is about. **988 passed, 22
  skipped.**

### Measured on the Pi
| | |
|---|---|
| 3D coach, idle machine | median **16.7 ms (60 fps)**, p95 17.1 ms, 19 draw calls |
| 3D coach **+ camera + Hailo** | median **16.7 ms**, p95 17.1 ms, worst 33 ms |
| Pose pipeline under that load | **46.1 fps**, 30.7 ms capture→pose, Hailo 95% busy |
| CPU / temp / RAM | **31% of 4 cores**, 62 °C, 5.5 GB free |
| Full lesson, 12 min | flat 16.7 ms every minute, heap flat at 118 MB, no leak |

### Rollback
- Git tag **`yoga-2d-fallback`** at `3d571c3` — the last all-2D commit.
- Pi tarball **`~/AIPI5-backup/yoga-2d-20260821-124025.tgz`** (4.2 MB): the
  whole `games/yoga` package, `coach/`, `clips/`, `yoga.js`, `index.html`.
- `AIPI5_YOGA_LEGACY=1` puts the original three lessons back with no deploy.
- **No 2D artwork has been deleted.** It still ships and `yoga.js` still uses it.

---

## Not done

### 1. The browser side of production — **done and deployed**
- `yoga3d.js` mounts the checked VRM renderer in `#game-stage` and consumes
  the production snapshot's pose ids and blend clock.
- The selector reads `rigdata.json` and shows seven courses under each level
  tab: 21 total, with name, actual minutes, pose count and guided count.
- A `course-*` command selects the exact class. Session duration is now
  `lesson.total_seconds`, not a fixed 1,200 seconds, and records are separated
  by course id.
- Guided poses hide score, accuracy and result while keeping the cue visible.
- The old 2D renderer and assets remain and take over automatically if the 3D
  module, WebGL context, rig data or VRM fails.
- The asset server serves `.vrm` as `model/gltf-binary`; live verification
  caught the missing type before handoff (the model had otherwise returned
  404).

Deployed to `aipi5` on 21 August 2026. Local and remote hashes match for the
production page, both Yoga renderers, session and game manager. Both user
services are active and enabled; user lingering remains enabled.

Follow-up UI fixes deployed the same evening: all four course cards now fit
each selector row, Pause is explicitly layered above the 3D canvas and leads
to Resume/Restart/Return to Game Menu, and Yoga no longer shows the legacy
“1200 seconds (20 minutes)” copy on its start sheet. “Review 91 Poses” opens a
manual one-pose-at-a-time reviewer with side/id labels, approval and needs-fix
states, notes, progress totals and JSON export. Review state persists in the
kiosk browser.

The reviewer camera is independently orbitable: drag horizontally for a full
360° inspection, drag vertically through low and near-overhead views, use the
wheel to zoom, or use Reset View / Top View. The form minimizes to a small
floating tab so it cannot hide the body during inspection.

### 2. Anatomical QA across all 91 poses — **done and deployed**
- The runtime solve now continues into hands/fingers and feet/toes. Semantic
  `floor`, `point`, `flex`, and `relax` constraints are resolved from the real
  VRM axes, so palms and soles no longer rigidly inherit forearms and shins.
- `check3d.py` now checks the full **91**, not only the 60 authored poses. It
  carries explicit ranges for every requested joint family, body-core/contact
  checks, opposite-leg checks, floor penetration, and terminal semantics.
- Fourteen pose tables changed after the gate found excessive passive knee or
  neck flexion: Butterfly, Easy Seat/Breath, both Seated Side Stretches, both
  Seated Twists, both Seated Hamstrings, Cat, both Thread the Needles, and both
  Pigeon Preps. Terminal behavior changed across all weight-bearing and balance
  families through shared logic.
- All eight real-VRM contact sheets (91 poses) were visually reviewed locally.
  `all 91 poses pass anatomical QA`; 16/16 headless rig tests and the complete
  Yoga Python suite (50 tests + 18 subtests) pass. No Pi run was used.

Deployed code-only to `aipi5` on 21 August 2026 at 15:55 PDT. The protected
device configuration and device-specific models were preserved. The service
is active/running and all startup checks passed. HTTP-served `coach3d.js` and
`rigdata.json` match the local SHA-256 hashes. This health verification did not
consume one of the three remaining production lesson-test runs.

Post-deploy startup fix: the Windows-created code-only archive removed the
executable bit from `scripts/aipi5-ui.sh`, leaving `aipi5-ui.service` in a
203/EXEC restart loop even though the assistant itself was running. Permissions
were restored on the Pi; both units are enabled and active. User lingering is
enabled so the units are queued at machine boot, and both deployment scripts
now restore executable bits before restart/install.

### 3. The five production tests the user asked for
Complete Beginner, Intermediate and Advanced courses; a scored-pose test with
real body tracking; a guided-heavy course. **None run yet** — they need the
browser integration first.

---

## Standing constraints from the user

- **At most three test runs on the Pi from here.** Budget them; the offline
  node/`check3d` route costs nothing.
- Do **not** delete the 2D coach or its assets.
- Chair stays **guided-only**. Teaching clarity beats scoring for guided poses.
- Scored poses keep the **≈15° camera limit** — the player has to stay facing
  the BRIO to be tracked, and they copy what they see.
- Leave the two scored Forward Folds alone unless they prove hard to follow in
  a real lesson.
- Guided poses may use any camera — front, side, three-quarter, elevated,
  top-down — whatever teaches best.
- Camera moves must finish **before** the hold begins. (Already true: the camera
  is a pure function of the pose blend, and four tests hold it.)

---

## Things a future session should know

- **The production reviewer is intentionally gated at poses 1–5.** On 21
  August the first approval batch was rebuilt: Bird Dog left/right now give
  the supporting and reaching limbs distinct terminal orientation; Boat is
  the bent-knee preparation named by the course; Bridge is a normal supported
  shoulder bridge with planted feet and arms beside the torso; Butterfly has
  open knees, hands at the joined feet, and inward-facing soles.  The review
  selector uses `.slice(0, 5)` and must not be unlocked until the owner
  approves this batch. Review JSON is exported from the Pi with the page's
  **Export Review** button.
- **The first owner export was consumed on 21 August.** Boat Preparation was
  approved and remains unchanged. Bird Dog left/right were returned because
  both feet rolled away from the floor; both now use an explicit sole-down
  terminal frame. Bridge was returned for ignoring gravity and bad feet; it is
  now a lower bridge with researched 90–125° knee flexion, nine upper-body and
  foot ground contacts, and toes directed away from the head. Butterfly's
  feet were inside the torso; the shared solver now supports smoothly blended
  `depth3d`, putting its hips, knees, shins, hands and joined feet in a real
  three-dimensional configuration. `anatomy_sources.md` records the research
  and approval-batch constraints. Future pose batches must add a researched
  contract there before review.
- **The second owner export added exact authoring.** `yoga-pose-review (2).json`
  approved Boat and Butterfly, requested a literal 180° flip of both Bird Dog
  feet, and reported that Bridge's back still floated. Both Bird Dogs now ship
  with `[180, 0, 0]` terminal offsets. Bridge's four-degree left-arm stagger
  was making its hand settle before its shoulder; removing it puts the head,
  shoulders, elbows and hands at the floor (0 cm in the arithmetic pass).
  The five-pose reviewer now has a persistent live editor for all ten chain
  directions, eight out-of-plane depth controls, root pitch/yaw/roll, and
  independent XYZ offsets for both hands and feet. One-tap X/Y/Z foot flips
  are included. Export JSON contains both `pose_edits` and the `edited_pose`
  alongside its review, so the owner's exact shape can be validated and
  promoted instead of reconstructed from prose.

- **`rigdata.json` is the single source.** Both the browser and
  `curriculum.py` read it. Regenerate with
  `python scripts/build_yoga_v3_data.py` after touching `design/yoga_v2`.
- **Five poses are scored against untuned tolerances.** `half_lift`,
  `reverse_warrior_left/right`, `standing_knee_left/right` were frozen as
  scored but never had a scoring table authored, so their *demonstration*
  table is used at `tolerance_scale = 1.25`. They mark generously on purpose.
  If they misbehave with a real player, `guided_only` on the catalog entry is
  the one-line fix.
- **Course length arithmetic.** Production spends the design's preview, settle
  and result-card time inside the transition, because the shipping game has no
  settle or result *phase*. That is also what gives the camera room to arrive
  before the hold. Leaving it out put nine of the twenty-one under their floor.
- **`pkill -f` on the Pi matches the ssh command line itself** and kills the
  session. Match on argv shape instead — `scripts/v3_shot.sh` shows the pattern.
- **Screenshotting leaves a browser behind.** A hundred of them exhausted 8 GB
  and took the Pi off the network for fifteen minutes. `v3_shot.sh` now sweeps
  chromium before and after every shot.
- **Heredocs with apostrophes fail** in this Bash tool. Write patch scripts to a
  file and run them.
- `pytest` had to be installed on the laptop; it is in neither
  `requirements.txt` nor the Pi venv.

## Owner approval — first batch complete

The final Pi export, `yoga-pose-review (4).json`, approved Bird Dog Left,
Bird Dog Right, Boat, Bridge and Butterfly. The four edited definitions are
stored verbatim in `approved_pose_edits.json` and overlaid at the end of
`poses3d.py`; Butterfly had no edit and keeps its authored definition. The
generated `rigdata.json` therefore contains the exact approved shapes used by
the production game. A regression test checks both the approval list and every
edited value so later rig work cannot silently replace them.

The second owner batch, poses 6–10, was approved on 21 August from the laptop
reviewer. `approved_pose_edits_06_10.json` preserves the exact exported Cactus
Arms, Chair, Child's Pose and Cobra objects; Cat was approved unchanged. The
production overlay and regression test now protect all ten approved poses.

The third owner batch, poses 11–20, was approved on 22 August. All ten poses
were edited in the laptop reviewer, so `approved_pose_edits_11_20.json`
preserves every exported field verbatim. The flat arithmetic checker cannot
reconstruct root rotation, out-of-plane depth, or terminal offsets; for an
exact owner-approved object it therefore validates the terminal schema and
defers mesh geometry to the completed 360-degree visual review. All other
poses continue through the full arithmetic anatomy pass.

## Commands

```bash
python scripts/build_yoga_v3_data.py          # regenerate rigdata.json
cd design && python -m yoga_v2.check3d        # all 91 poses, by arithmetic
node --test tests/coach3d.test.mjs            # retargeting/anatomy, 40 tests
python -m pytest -q                           # 1001 pass, 22 skip, 718 subtests
bash scripts/v3_shot.sh <name> '<query>' [s] [page]   # deploy + shoot the Pi
```

Review pages: `stage.html?course=beginner_01&real=1`, `?pose=…`, `?bench=1`;
`contact.html?from=0&n=12`, `?tween=cat,cow`, `?ids=chair&sweep=0,25,45,70`.

## Reports

- Morning report artifact:
  <https://claude.ai/code/artifact/b91fff5c-1687-44ae-b05c-609048939f26>
- Design: `design/yoga_v2/coach3d.md`, `mirroring.md`, `assets.md`.
