# Yoga Coach v2 — curriculum frozen

**Approved and frozen 2026-08-20.** Fingerprint `2dc9d27290eaa535`.

    python -m design.yoga_v2.freeze     # exits 0 while disk matches approval

From here the curriculum is an *input* to the animation work, not something the
animation work may renegotiate. The fingerprint hashes every course id, step,
hold, side, segment and scored/guided flag — prose can be fixed freely, a hold
cannot move by a second without the check failing.

| | |
|---|---:|
| Courses | 21 |
| Steps | 605 |
| Scored poses | 37 |
| Guided poses | 54 |
| Bone tables to author | 5 |
| Duration range | 15:07 – 19:40 |
| Total practice | 5.8 h |

**Approved with these decisions on the record:** beginner Chair at 20 s and Tree
at 16 s stay short by design; Back and Shoulder Release stays at 15:07 and is
not padded; and Warrior III, High Lunge, Pyramid and Standing Twist stay
guided-only rather than being forced into a scoring system that cannot
distinguish them.

---

## The full library

| Asset | Count | Note |
|---|---:|---|
| Poses used by the 21 courses | 89 | |
| — right-hand, **mirrored at runtime** | 30 | zero generated frames |
| Source poses | 59 | |
| Stills to generate | **38** | 21 already ship |
| Transition clips, total | 60 | 50 pose→hub spokes + 10 hub bridges |
| — reusable from the shipped set | **12** | |
| — **new** | **48** | |
| Hold loops | **59** | none exist today |
| **Frames to generate** | **1044** | ~20.4 MB |

### Why only 12 clips are reusable, not 20

The shipped clips each run **Mountain → pose**. v2 routes a pose through its
*family* hub, and only STANDING and LUNGE poses hub on Mountain. A WIDE pose now
routes through Star, so the shipped `Mountain → Warrior II` clip is the wrong
movement for the `Star → Warrior II` spoke — however good the artwork in it is.

The still art from all 20 is still reused. It is the *motion* that does not
transfer. An earlier draft counted these as reusable and was wrong by eight
clips; the manifest now marks each one with the hub it actually needs.

---

## The representative test set — 64 frames, 6% of the library

§32 Phase 6: prove the pipeline and the device before generating the rest.

**Nothing in this set is blocked on a bone table.** Every newly generated pose
in it is guided, and a guided pose needs no rig, no targets and no tolerance
tuning. The scored pose in the set is one whose artwork already ships — which is
exactly what makes it a reuse test.

### What to generate

| Job | Frames | Covers |
|---|---:|---|
| `still:warrior_three_left` | 1 | guided **standing** pose, new artwork |
| `still:child_pose` | 1 | floor/kneeling, new floor placement rule |
| `still:savasana` | 1 | supine — the widest figure in the library |
| `transition:star->triangle_left` | 7 | **same-family direct** (WIDE→WIDE, 3.0 s) |
| `transition:mountain->warrior_three_left` | 7 | standing spoke to a new guided pose |
| `transition:child_pose->easy_seat` | 7 | floor-to-floor bridge (kneeling→seated) |
| `transition:mountain->savasana` | 7 | **largest bridge** (STANDING→SUPINE, 8.0 s), and **reused art at one end, new at the other** |
| `hold_loop:warrior_two_left` | 12 | normal breathing loop over **shipped** artwork |
| `hold_loop:child_pose` | 12 | floor hold loop |
| `hold_loop:savasana` | 12 | **long relaxation** — must survive 115 s without reading as a cycle |
| **Total** | **64** | ~1.25 MB |

### The case with no generation at all

**Left/right mirroring** is tested by `warrior_three_right`, drawn from the left
clip at runtime. It costs nothing to make and everything to get wrong — see
`mirroring.md`, and note that a lying figure must be centred on `W/2` or the
whole-stage flip misplaces it.

### What to measure on the Pi afterwards

- 1280×800 output, 10 fps playback with no stall
- visual quality, and **coach identity across new and reused assets in one
  movement** — the `mountain->savasana` clip is the test
- transition continuity at both ends; hold loop seamless over a full
  relaxation hold; mirrored pose stays centred
- CPU, RAM, browser memory, preload behaviour, first-play latency
- **pose tracking still at its measured rate while all of the above runs**

---

## Order from here

1. ~~Finish D4 mirroring~~ · 2. ~~Finish D5 reuse/regenerate~~ ·
3. ~~Review and refine all 21 courses~~ · 4. ~~Freeze the curriculum~~ ·
5. ~~Finalise the manifest~~ · 6. ~~Smallest representative set~~
7. **Turn on the ComfyUI PC** ← here
8. Generate the representative set at 1280×800 @ 10 fps
9. Deploy only that set to the Pi
10. Measure performance, RAM, CPU, loading, visual quality
11. Fix the pipeline if needed
12. Only then: bulk generation of the remaining 48 clips and 59 loops
13. Integrate and test the complete 21-course system

The 5 bone tables (Half Lift, Reverse Warrior ×2, Standing Knee ×2) are needed
for step 12, not for step 8, and need no ComfyUI.
