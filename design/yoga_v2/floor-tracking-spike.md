# D0 — Can a body on the floor be scored?

The Yoga Coach is standing-only, and 56% of the poses the new curriculum wants
are not standing. Whether those can be **scored** or only **demonstrated**
decides how the whole of v2 is built. This is the measurement that settles it.

Two halves. The first is geometry and is **already answered**, on any machine,
with no camera. The second needs the Pi, a mat and a person.

---

## Part 1 — the maths, answered offline

Reproduce with:

    python scripts/probe_floor_tracking.py --selftest

`rig.measure` normalises its seven ratio metrics by `span` (the shoulder
distance) and takes every one of them as an **image-axis** difference —
`stack`, both `foot_height`s and both `hand_height`s are raw `.y`; `stance` and
`reach` are raw `.x`. The proposed fix is to measure in the **body's** axis
instead: rotate so the hip-to-shoulder direction is up, and take the scale from
the torso (converted back into shoulder widths through the coach's own
proportion) when the shoulders are too foreshortened to supply it.

| Property | Result |
|---|---|
| Agrees with `measure` when upright (20 poses, \|torso\| < 15°) | worst disagreement **0.00000** — PASS |
| Invariant under image-plane rotation (all 32 poses, 45/90/180°) | worst drift **0.0000000** — PASS |
| `measure`'s own drift under the same rotation | **0.88 – 4.30** against tolerances of 0.2–0.5 |
| Angle metrics disturbed by rotation | **only `torso`**, by exactly the rotation — correct |

**So the normalisation works.** A body-frame `measure` would keep every
existing upright pose bit-identical and would stay stable on a body at any
orientation.

### The finding that changes the design

It cannot be switched on globally. On the 12 poses that lean, the two frames
legitimately disagree, and the disagreement is large:

| Pose | Lean | Largest ratio shift |
|---|---:|---:|
| `forward_fold` | −178° | 4.15 |
| `wide_leg_fold` | +180° | 3.75 |
| `half_moon_left/right` | ±58° | 2.15 |
| `triangle_left/right` | ±50° | 1.90 |
| `side_angle_left/right` | ±42° | 1.55 |
| `side_bend_left/right` | ±22° | 1.10 |
| `crescent_moon_left/right` | ±25° | 0.47 |

These are not errors. They are the lean moving *out of* the seven ratios and
*into* `torso` alone — so a global switch would rebuild every leaning pose's
discrimination around a single metric, and it disturbs `forward_fold` and
`wide_leg_fold` most, which are already the two poses needing special handling.

> **Design consequence.** Normalisation is a **per-pose property**, not a
> global mode. The standing library keeps image-axis measurement and every
> tuned tolerance it was measured with; the floor tier opts into body-frame.
> A `normalisation: "image" | "body"` field on the pose carries it.

---

## Part 2 — the camera, still to measure

What the offline proof cannot answer: whether the pose model **finds a body at
all** when it is folded, inverted or lying down and seen from a domestic room;
and whether `span` survives foreshortening well enough to be a usable scale.

### Protocol

On the Pi, with the assistant stopped — it owns `/dev/video0` and the Hailo
VDevice:

    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user stop aipi5

    # one run per pose; each gives a spoken countdown to get into position
    ~/AIPI5/.venv/bin/python scripts/probe_floor_tracking.py \
        --pose savasana --placement desk --countdown 10

    systemctl --user restart aipi5      # restart, not start

Run the six poses below at **two camera placements** — as-is on the desk, then
raised and tilted down at the mat — across the three candidate capture modes
the script sweeps by default.

| Pose | Posture family it stands for |
|---|---|
| Savasana | supine |
| Child's Pose | kneeling, folded |
| Downward Dog | inverted |
| Easy Seat | seated |
| Cat–Cow (hold Cow) | quadruped |
| Supine Twist | supine, rotated |

Add `--standing warrior_two_left` on at least one run per placement. Moving the
camera to see the floor changes what it sees of a standing player, and a floor
result bought by breaking the poses that already work is not a result.

### What the script reports

Per pose per mode: person-detection rate; per-keypoint confidence and
visibility across all 17; `span` against `MIN_SPAN = 0.055`; how often
`measure` returns nothing; which of the 17 metrics come back degenerate; and
the same joints re-measured in the body frame — so the "would the rewrite
help?" question is answered from captured data rather than by rewriting first.
Each run also saves an annotated frame, because a table saying eleven keypoints
were found does not say whether they were found in the *right places*.

It ends with a verdict per pose: `SCOREABLE`, `SCOREABLE WITH BODY-FRAME`,
`MARGINAL` or `DEMONSTRATE-ONLY`.

### On the capture modes

Resolution buys **no** keypoint precision — every frame is letterboxed to
640×640 before the accelerator sees it (`geometry.py:6`,
`hailo_pose.py:211`). The 1080p row is in the sweep to demonstrate that on this
device rather than assert it. What a 16:9 mode does buy is **33% more
horizontal field of view**, because the BRIO's 4:3 modes are a horizontal crop
of the sensor rather than a rescale — already verified in `config/aipi5.yaml`
by photographing one scene at both sizes. Frame rate is free to spend: yoga has
no blade to smooth, and the config's own sweep puts 30 fps modes within 2 ms of
the fast ones on displayed staleness.

---

## Results — measured 2026-08-20, living room, BRIO raised and tilted at the floor

12 captures across two runs, 1280x720@30 MJPG, ~172 frames each. Every pose was
verified by eye against its saved annotated frame rather than trusted from the
numbers, which is how three-quarters of the first run turned out to be measuring
the wrong thing.

| Pose | run | span | span ok | `measure()` empty | torso | body-frame ratios |
|---|---|---:|---:|---:|---:|---:|
| savasana | A | 0.064 | 99% | 1% | −92° | **7/7** |
| savasana | B | 0.065 | 95% | 5% | −103° | **7/7** |
| childs-pose | A | 0.072 | 83% | 25% | −84° | **7/7** |
| childs-pose | B | 0.024 | 1% | 99% | −94° | **7/7** |
| easy-seat | A | 0.035 | 0% | 100% | −9° | **7/7** |
| easy-seat | B | 0.108 | 100% | 0% | −0° | **7/7** |
| cow | A | 0.111 | 100% | 0% | −0° ✗ | **7/7** |
| cow | B | 0.023 | 0% | 100% | −34° ✗ | **7/7** |
| downward-dog | A | 0.017 | 0% | 100% | −10° ✗ | **7/7** |
| downward-dog | B | 0.010 | 0% | 100% | −127° ✗ | **7/7** |
| supine-twist | A | 0.114 | 100% | 0% | 0° ✗ | **7/7** |
| supine-twist | B | 0.023 | 4% | 96% | −56° ✗ | **7/7** |

`✗` marks a torso angle inconsistent with the pose named — the capture measured
some other shape and its numbers are not evidence about that pose.

### What is established

1. **The model finds a body on the floor.** A person was detected in 100% of
   frames in all twelve captures, folded, inverted or flat. Keypoint confidence
   on a good capture runs 0.9+ on shoulders, hips, knees and ankles.
2. **`span` is the binding constraint, and it is unreliable.** Shoulder
   distance ranged **0.010 – 0.114** against `MIN_SPAN = 0.055`, swinging on
   body orientation rather than on anything about the pose. Below it,
   `rig.measure` returns `{}` and the player is told to step into view — which
   is what happened in 100% of frames for half these captures.
3. **The body-frame normalisation produced a complete set of seven ratios in
   12 of 12 captures — including every case where `measure()` produced
   nothing at all.** This is the offline proof (`--selftest`) holding up on
   real bodies, and it is the headline result of the spike.
4. **Savasana is the most repeatable floor pose**: torso −92°/−103°, span
   0.064/0.065 across two independent runs, `measure()` working 95–99%.

### What is NOT established

- **Per-pose scoreability.** Pose execution was inconsistent — half the
  captures show a torso angle that does not match the pose asked for. That is a
  fact about the session, not about the poses, and it means no per-pose verdict
  here should be treated as final for Cow, Downward Dog or Supine Twist.
- **Whether the body-frame ratios are *correct*.** They are always *present*;
  proving they are *right* needs captures where the pose is known-good, which
  this session did not reliably produce.
- **The cost to standing poses.** Warrior II scored 0.161 at the raised
  placement. That is far below what a good Warrior II should score, but the
  same execution problem applies — there is no clean baseline, so this cannot
  yet be blamed on the camera move.

### Recommendation

**Keep floor poses `score_enabled: false` for the v2 curriculum**, as originally
planned. The tracking is not ruled out — the normalisation demonstrably works
and the model sees the body — but nothing here justifies betting 21 courses on
scoring the floor, and the fallback costs nothing: the coach demonstrates, the
voice guides, the timer runs.

**Carry the body-frame work forward as a funded follow-up.** It is proven in
geometry and produces complete data in practice. The remaining work is a
validation session with known-good poses, then `normalisation: "image" | "body"`
as a per-pose property.

### What this session cost, and what fixed it

Three failure modes, all found only by looking at the pictures:

- The room contains other people; the subject is chosen by largest bounding box,
  so a child could become the subject the moment the adult lay down.
- **Framing verified standing does not hold lying down.** A standing body is
  tall and narrow, a lying one wide and low. Two runs were lost to the subject
  lying outside the frame while the model tracked a fragment at the edge.
- **Naming a pose is not teaching it.** Half the captures were the wrong shape
  until the probe drew a stick figure of the target on screen.

The probe now guards all three: it rejects a capture whose body touches the
left, right or top edge, rejects one whose torso angle contradicts the pose, and
shows a diagram plus a live skeleton during every countdown.
