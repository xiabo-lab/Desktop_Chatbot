# D4 — The mirror convention

§7 says the coach mirroring the player is mandatory, and asks for "one
centralized mirror-coordinate system rather than independent left/right hacks".

**The convention already exists, is already centralized, and already works.**
The work here is not to build it — it is to state it once, prove it, and fix
the documentation that currently contradicts itself about it.

---

## The one rule

> **The coach's own left limb is drawn at the smaller screen x.**

That is the whole convention. Every other statement about mirroring in this
project is a consequence of it.

It works because the player's pose stream arrives **already mirrored**
(`motion/geometry.mirror`), so an anatomical left hand also sits at the smaller
x. Coach and player therefore agree without a single conversion anywhere, and
"raise your left arm" is the same sentence for both of them. No part of the
game has to decide which way round a side is.

`tests/test_yoga.py:144-158` asserts exactly this invariant — `left_shoulder.x
< right_shoulder.x` and `left_hip.x < right_hip.x` — for every pose in the
library. It is the only mirroring fact the scorer depends on.

---

## The documentation contradicts itself, and one half is wrong

Two files tell the story in opposite ways and produce identical pixels:

| Source | Says |
|---|---|
| `rig.py:18-24` | "**The coach faces away from the player**" |
| `README.md:735` | "The coach is drawn from behind, like the Boxing player" |
| `scripts/build_yoga_coach.py:3-4` | "She **faces the player** and demonstrates mirrored" |
| `build_yoga_coach.py:265-274` | "she faces the player, which makes her a mirror rather than a body double" |

**The shipped artwork is front-facing.** All 41 WebP files in
`aipi5/ui/web/assets/yoga/coach/` are drawings of a coach facing the viewer,
generated with the prompt at `build_yoga_coach.py:284-288`: *"DO NOT MIRROR OR
FLIP THE DIAGRAM… whatever the diagram puts on the LEFT of the picture you must
also put on the LEFT of the picture."*

`rig.py:18` and `README.md:735` are describing the **JS fallback rig** — the
vector stick figure in `yoga.js:539-541` that draws only when a picture is
missing or has not loaded. That one *is* drawn from behind, and the comment
there says so and gives the reason.

Both descriptions satisfy the one rule, so nothing is broken. But a contributor
reading `rig.py` will believe the coach faces away, and they will be wrong about
every shipped pixel.

> **Action for the implementation phase:** correct `rig.py:18-24` and
> `README.md:735` to say that the *fallback rig* is drawn from behind while the
> *artwork* faces the player, and that both put the coach's left at the smaller
> x. Do not change the convention. This is a documentation fix.

---

## Why facing the player costs the scorer nothing

A teacher standing in front of a class raises the arm that *appears* on the
class's left when they want the class to raise their own left. That is what the
coach does.

The guide diagram fixes the **picture** side, never anatomy: when the diagram
puts the working limb on the left of the frame, the model paints the limb on the
left of the frame — which happens to be the coach's own right. Because the
player's stream is mirrored too, the player's left hand is also at the smaller
x. The two line up.

Nothing in `rig.py` or `scoring.py` changes for this, and nothing in v2 changes
it either.

---

## Where mirroring is implemented — four places, all suffix-driven

Every one keys off the `_left` / `_right` id suffix. There is no fifth place and
v2 adds none.

| # | Mechanism | Where | What it does |
|---|---|---|---|
| 1 | **Authoring** | `poses.py:88-95` `mirror_id` | `_left` ⇄ `_right` by literal string slice |
| 2 | **Stills** | `build_yoga_coach.py:127-133`, `:600-609` | `_right` is skipped at generation; the manifest records `mirror_of` pointing at the left drawing |
| 3 | **Clips** | `yoga.js:201-208` `clipSource` | a `_right` pose looks up the `_left` clip and sets `mirror: true` |
| 4 | **Scoring** | `rig.mirrored()` `:361-371`, `MIRROR_PAIRS` `:256-263` | scores the body against the pose *and* its mirror, to answer "beautifully, on the wrong side?" |

### The drawing transform

Stills and clips flip differently, and both are correct:

- **Stills** flip about the coach's own hip anchor: `ctx.translate(COACH_X,
  FLOOR_Y); ctx.scale(-1, 1); drawImage(image, -hip_x, -floor_y, w, h)`. The
  manifest deliberately keeps `hip_x` as the *source* drawing's value
  (`build_yoga_coach.py:595-599`), because the page flips the axis first.
  `tests/test_ui_assets.py` asserts `entry["hip_x"] == manifest[twin]["hip_x"]`.
- **Clips** flip the **whole 1280-wide stage**: `ctx.translate(W, 0);
  ctx.scale(-1, 1)`, so a frame's offset becomes `W - (x + w)`. This is only
  equivalent to flipping about the coach because **`COACH_X` is exactly `W/2`**.

> **Trap worth recording.** If the layout ever moves the coach off the centre
> line, the clip mirror silently stops matching the still mirror, and the coach
> will jump sideways at the moment a transition hands over to a held pose. The
> stills would still be right. Nothing currently asserts `COACH_X == W / 2`.
> **Action for the implementation phase:** add that assertion to
> `tests/test_ui_assets.py`.

### Why one drawing per pair is a guarantee, not just a saving

A left/right pair built from one drawing cannot disagree about anything except
which way round it is. Generating them separately would invite the image model
to produce two subtly different coaches — which is the exact failure that killed
the ControlNet route (see `aipi5-local-comfyui`).

§19 asks the same thing and adds a caution: verify clothing, hair, logos and
asymmetric accessories survive the flip. **The coach's design already satisfies
this** — no text, no logo, no asymmetric accessory. The side ponytail *does*
swap sides under mirroring, which is acceptable and invisible in practice, but
it is the one asymmetry in the character and worth not adding to.

---

## What v2 inherits, and the one thing it must add

v2 changes nothing about the convention. Its **30 right-hand poses** are all
mirrors of a left-hand source, and the generation manifest marks them
`kind: "mirror"` — so none of them costs a single generated frame.

**Guided poses mirror identically.** The convention is about drawing, not about
scoring, so a guided Supine Twist to the left is the right-hand clip flipped in
exactly the way a scored Warrior II is. The six standing poses that turned out
to be unscoreable — High Lunge, Warrior III, Pyramid, Standing Twist, and the
two singletons — keep their `_left`/`_right` ids and their mirrored artwork;
only `score_enabled` changed.

The one genuinely new obligation is **guided floor poses**. A supine twist to
the left is still the mirror of one to the right, and the same suffix rule
applies — but the clip flips the whole stage, and a body lying across the frame
is much wider than a standing one. A lying pose whose bounding box is not
centred on `W/2` will not survive the stage flip cleanly.

> **Action for the implementation phase:** floor-pose artwork must be composed
> so the figure is centred horizontally on the stage, exactly as the standing
> art is anchored to `COACH_X`. Add this to the generation prompt and to
> animation QA (§31 already asks that "mirrored version remains centered").

---

## Tests to add when the engine work happens

§7 says "test mirroring explicitly". Today only the scorer's invariant is
tested. The list:

1. `COACH_X == W / 2` in `yoga.js` — the assumption the clip mirror rests on.
2. Every `_right` pose in the v2 catalog resolves to an existing `_left` source,
   in the stills manifest, the clips manifest and `catalog.mirror_id` alike.
3. For a mirrored clip frame, `W - (x + w)` lands the figure at the same
   distance from centre as the unmirrored one — the property already verified by
   hand this session for `warrior_two`, made permanent.
4. Spoken left/right text matches the demonstrated side: a step whose pose id
   ends `_left` must speak an instruction containing "left".
5. The scorer's existing `left_shoulder.x < right_shoulder.x` invariant, scoped
   to the **scored tier only** — a guided quadruped pose legitimately has a
   vertical shoulder line and would fail it.

Item 5 is a change to an existing test and is the one place v2's floor poses
touch the mirroring code at all.

---

## The fifth implementation: the 3D coach

The runtime 3D coach (`design/yoga_v2/coach3d.md`) is a fifth place where
mirroring is implemented, and it is the first one where the rule is *proved*
rather than asserted.

It obeys the same one rule, by a route worth stating plainly. A VRM faces the
camera, so the limb that appears at the smaller screen x is the character's own
**right**. `RIG_TO_VRM` therefore maps the rig's left-side angles onto the
model's right-side bones:

```js
left_upper_arm:   ["rightUpperArm", "rightLowerArm"],
right_upper_arm:  ["leftUpperArm",  "leftLowerArm"],
```

That swap looks like a bug and is the convention. It is the same "she
demonstrates mirrored, the way a teacher faces a class" that the shipped
artwork has always meant; nothing else in the project changes, and the drawing
transform in `yoga.js` is untouched.

**The rule now survives a change of viewpoint for free.** It is stated about
which *bone* receives the angle, not about which way the screen happens to
point — so when a pose is turned side-on or laid flat on the grass, the left
limb is still the left limb without a single extra conversion. That was the
thing most likely to break in the move to 3D, and it did not have to be
handled at all.

Two tests in `tests/coach3d.test.mjs` hold it:

- *the coach demonstrates mirrored* — the limb driven by the rig's left angles
  ends up at the smaller world x;
- *a mirrored table gives a mirrored body* — feeding the mirrored table
  (`swap prefixes, θ → wrap(180 − θ)`, the same rule `test_yoga.py` enforces)
  produces a body that is the exact mirror image of the original in **all ten**
  solved bones, not merely in the shoulders.

The second is stronger than anything the 2D route could check, because in 2D
one drawing flipped horizontally is a mirror by construction and there is
nothing left to verify.
