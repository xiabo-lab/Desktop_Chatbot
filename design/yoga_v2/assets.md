# D5 — Reuse, regenerate, and what it costs

§34 says the curriculum decides the assets, not the reverse — but also that
existing work should be reused wherever it still satisfies the requirements, and
that nothing useful gets deleted until the new system is verified. This is that
accounting, computed from the catalog and checked against the shipped manifests
rather than counted by hand.

Regenerate with `python -m design.yoga_v2.export`; every figure below comes out
of `animation_generation_manifest.json`.

---

## The headline

| | Count |
|---|---:|
| Poses the 21 courses use | **89** |
| — of which right-hand, **mirrored at runtime, zero generation** | **30** |
| Source poses needing an asset | **59** |
| — still already shipped and reusable | **21** |
| — transition clip already shipped and reusable | **20** |
| — new **scored** poses (bone table first) | **3** |
| — new **guided** poses (no bone table, no scoring) | **35** |
| Bone tables to author | **5** |

**Every shipped asset is used.** All 32 poses in `aipi5/games/yoga/poses.py`,
all 21 coach stills and all 20 transition clips carry into v2. Nothing in the
current library is orphaned and nothing is regenerated because it is merely old.
`crescent_moon` and `goddess` were idle in an earlier draft and were given work
rather than left as waste.

### The tracking review changed these numbers substantially

An earlier draft counted **9 new scored poses and 15 bone tables**. Classifying
each new standing pose against the seventeen metrics `rig.measure` actually
produces cut that to **3 and 5**:

| Pose | Verdict | Why |
|---|---|---|
| Half Lift | **scored** | hand height separates it from Forward Fold |
| Reverse Warrior | **scored** | torso lean plus one arm up, one down |
| Standing Knee | **scored** | hip, knee and foot height all move together |
| High Lunge | guided | differs from Warrior I only by a lifted **heel**, and the model reports ankles |
| Warrior III | guided | torso and lifted leg both lie along the camera axis |
| Pyramid | guided | staggered feet project to no `stance` separation |
| Standing Twist | guided | a rotation about the axis the camera looks down |
| Gentle Back Reach | guided | a few percent of spine shortening, nothing else moves |
| Weight Shift | guided | a hip translation with no joint angle change |

Six fewer bone tables to author, six fewer poses to tune tolerances for — and
six poses that will not be scored badly. Guided-only is the better failure mode
than unreliable scoring.

## Tier 1 — reuse unchanged (21 stills, 20 clips)

These arrive free. Artwork, bone tables, tuned tolerances and the `__50`
transition frames all carry over untouched.

`mountain` · `mountain_breath` · `upward_salute` · `cactus_arms` ·
`shoulder_opener` · `neck_tilt_left` · `side_bend_left` · `crescent_moon_left` ·
`forward_fold` · `chair` · `star` · `goddess` · `wide_leg_fold` ·
`warrior_one_left` · `warrior_two_left` · `triangle_left` · `side_angle_left` ·
`tree_heart_left` · `tree_overhead_left` · `half_moon_left` · `hand_to_toe_left`

Plus their 30 right-hand twins, which are these same files mirrored at draw
time — see `mirroring.md`.

### One caveat that must not be skipped

The shipped clips were generated **before** this curriculum existed, and each is
a movement between *standing* and that pose. v2's transition graph is
family-based (§16), so a clip that runs Mountain → Warrior II is still exactly
right for entering Warrior II from standing, and still plays backwards to leave
it. **But a v2 course that goes Warrior II → Triangle directly** — 43% of pairs
stay inside one family — is asking for a movement no shipped clip contains.

The reused clips are therefore correct but **incomplete**: they cover the
standing↔pose spokes, not the within-family shortcuts the new curriculum wants.
That gap is counted under Tier 3 below, not hidden inside "reuse".

---

## Tier 2 — new scored poses (3 sources, 5 with mirrors)

Standing, frontal-plane, and scored the existing proven way. Each needs a **bone
table written first** — the artwork pipeline renders its guide from the rig, so
there is nothing to generate until the table exists.

| Pose | Why the curriculum needs it | Note for the table |
|---|---|---|
| `half_lift` | the second half of every half-salutation; used in 16 courses | needs the `forward_fold` treatment: a spine `scales` entry and a widened `tolerance_scale` |
| `reverse_warrior_left/right` | the Warrior II → Reverse → Side Angle chain §16 names | straightforward; the arms carry the shape |
| `standing_knee_left/right` | the balance step before Tree, in four courses | straightforward; among the clearest shapes in the library |

Work per pose: bone table → guide render → art generation → pack → clip. **The
table is the gating item and needs no ComfyUI.**

The other six new standing poses are guided and appear in Tier 3 — they need
artwork but no table, no targets and no tolerance tuning.

---

## Tier 3 — new guided poses (35 sources)

Floor, seated, supine — **and six standing poses the camera cannot distinguish**.
None needs a bone table, targets or tolerance tuning, which makes a guided pose
cheaper per unit than a scored one, not more expensive. Each needs a still and a
transition clip, and the coach must look like the same person doing it.

| Family | n | Poses |
|---|---:|---|
| seated | 8 | `boat` `butterfly` `easy_seat` `easy_seat_breath` `seated_forward_fold` `seated_hamstring_left` `seated_side_stretch_left` `seated_twist_left` |
| kneeling | 6 | `child_pose` `half_split_left` `lizard_left` `low_lunge_left` `low_lunge_reach_left` `pigeon_prep_left` |
| quadruped | 5 | `bird_dog_left` `cat` `cow` `table` `thread_needle_left` |
| supine | 5 | `bridge` `figure_four_left` `knees_to_chest` `savasana` `supine_twist_left` |
| prone | 4 | `cobra` `plank` `side_plank_left` `sphinx` |
| standing-neutral | 4 | `gentle_back_reach` `standing_twist_left` `warrior_three_left` `weight_shift` |
| standing-lunge | 2 | `high_lunge_left` `pyramid_left` |
| inverted | 1 | `downward_dog` |

The six standing ones — High Lunge, Warrior III, Pyramid, Standing Twist, Gentle
Back Reach, Weight Shift — sit in the standing families for transition purposes
and are demonstrated in full. They simply are not measured.

### Three things that make these harder than the standing art

1. **There is no guide diagram to render.** Standing poses are drawn on top of a
   skeleton produced by `rig.forward_kinematics`. Guided poses have no bone
   table, so the prompt must carry the shape — a reference sketch or a
   hand-authored pose diagram, of the kind `probe_floor_tracking.py` already
   draws for six of them.
2. **The floor line changes meaning.** `build_yoga_coach.placed()` anchors the
   *lowest ankle* to `FLOOR_Y`. A supine figure would be anchored by an ankle
   and described as standing on it. Floor art needs its own placement rule.
3. **Horizontal figures are wider than anything in the library.** `yoga.js`
   places the HUD clear of "the widest pose — Half Moon stops around x=970".
   A lying body is wider than that, and the accuracy dial is hidden during
   guided poses anyway — so the layout must reflow, not just dim.

---

## Transition clips

| | Count |
|---|---:|
| Unique ordered pose pairs across all 21 courses | **258** |
| — inside a single posture family | **112 (43%)** |
| Clips if every pair were generated | 258 |
| **Clips under hub-and-spoke + play-both-ways** | **60** (50 spokes + 10 bridges) |

Two mechanisms already proven in the shipping engine do the work: one clip per
pose to its family hub, played forwards to enter and backwards to leave; and
right-hand poses mirroring the left clip. That turns a per-pair count into a
per-pose one — **60 clips against 258, a 4.3× reduction**.

**Only 12 of the shipped 20 clips transfer, not 20.** Each shipped clip runs
*Mountain → pose*, and v2 routes a pose through its **family** hub — only
STANDING and LUNGE poses hub on Mountain. A WIDE pose now routes through Star,
so the shipped `Mountain → Warrior II` clip is the wrong movement for the
`Star → Warrior II` spoke however good its artwork is. The still art from all
twenty is still reused; it is the *motion* that does not transfer. So **48 new
transition clips**, not 39.

## Hold loops (§15)

New asset class; none exists today. §15 asks for 3–6 s of breathing motion at
10 fps that does **not** move any joint enough to change the demonstrated pose.

**Recommendation: a 12-frame ping-ponged loop**, played forwards then backwards
for a seamless 2.4 s cycle, rather than a 30–60 frame one-way loop. Half the
frames, no seam to hide, and the shipping clip player already plays sequences
both ways.

Needed for the 59 source poses. At the measured packing cost of the shipped
clips (~20 kB per keyed, cropped frame) that is ~14 MB.

---

## Budget

| Item | Frames | Disk |
|---|---:|---:|
| New transition clips (48 × 7) | 336 | ~6.6 MB |
| Hold loops (59 × 12) | 708 | ~13.8 MB |
| **New total** | **1044** | **~20.4 MB** |
| Already shipped (20 clips + 21 stills) | — | 2.8 MB + 1.4 MB |

**RAM is not the constraint.** The shipping LRU (`CLIPS_KEPT = 3` in `yoga.js`)
holds three poses' clips decoded at a time — about 5 MB — regardless of how many
exist on disk. That cache is why 19.6 MB of assets does not become 19.6 MB of
browser memory, and it is already verified on the Pi.

**Generation time**, at the measured 66 s per clip on the 4090:
~48 transition clips ≈ 53 min; ~59 hold loops, being shorter, materially less.
Well inside one session — but §32 Phase 6 says generate a representative set
first and prove it on the Pi, and that is what should happen.

---

## Nothing is deleted

§34's last instruction. `artwork/yoga/poses/*.png`, the `__50` transition frames
and the `_first_`/`_last_` clip endpoints all stay until the v2 system is
verified end to end on the device. The 518 MB of raw `*-chroma/` frames remain
gitignored but present on disk, which is what makes any clip re-derivable
without re-running the model.

---

## What has to be decided before generation starts

1. **The 5 bone tables** for the three new scored poses (Half Lift, and the
   Reverse Warrior and Standing Knee pairs). No ComfyUI needed; this is the
   gating item for Tier 2.
2. **The pose reference for each of the 35 guided poses** — a diagram or sketch
   the generator can be pointed at, since there is no rig to render one from.
3. **The floor-art placement rule**, replacing "anchor the lowest ankle to
   `FLOOR_Y`".
4. **The representative test set** (§32 Phase 6) — the smallest set that
   exercises every risk: a scored standing pose, a guided floor pose, a
   mirrored pose, a within-family transition, a floor↔standing bridge, and one
   hold loop.

---

## Superseded: the coach is no longer drawn

The manifest above describes a generated 2D coach — stills, transition clips
and hold loops, about 97 MB of WebP, one GPU night per batch.

**None of it is needed any more.** The coach is now a rigged model posed at
runtime from the same bone tables the player is scored against; see
`design/yoga_v2/coach3d.md`. The whole of its asset pipeline is
`scripts/build_yoga_v3_data.py`, which writes 103 kB of JSON in under a second,
plus one 15 MB `.vrm`.

Read this document as the record of what the generated route required, and of
the two quality problems that were solved inside it before the change: the
magenta fringe (fixed by un-premultiplying against the sampled backdrop) and
the backdrop drift on long clips (fixed by splitting them). Both fixes were
real and both are now moot, because there is no longer an edge to key or a
backdrop to drift.

Nothing has been deleted. The generated stills and clips are still on disk and
still shipped, and `yoga.js` still plays them; the 3D coach lives beside them
under `assets/yoga/v3/` and is not yet wired into the game.
