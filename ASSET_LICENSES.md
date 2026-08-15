# Asset licences

Every visual and every sound in AIPI5 is generated at runtime by code in this
repository. **No third-party artwork, audio, font or texture is bundled,
downloaded or referenced.** This file exists to record that, and to record the
one thing that *is* borrowed, which is an idea rather than a file.

If that changes — if a sprite sheet or an `.ogg` is ever added — every entry
belongs in the table at the bottom, filled in per file, before the file is
committed.

## Why there are no asset files

The upgrade plan for the Fruit Ninja game recommended several CC0 packs
(OpenGameArt fruit sprites, the Kenney splat and particle packs, a swish
sound pack, a Freesound squelch recording, OpenGameArt background music). None
of them were used, for two reasons.

**The UI server has no static directory.** `aipi5/ui/server.py` serves exactly
one file from one fixed path and says so in its own comments — there is no
static route, no MIME table and no path-traversal defence, because it has never
needed any. Adding assets would mean building all three, plus a preloader, plus
about fifteen megabytes on a deploy that is an `scp` of source files onto a
Raspberry Pi. That is a lot of new surface for artwork the canvas can draw for
nothing.

**The project already had this decision, and it was deliberate.** The original
game shipped no artwork on purpose. Keeping that through the ten-fruit
expansion means the repository still has no licence to argue about and no
attribution that can drift out of date.

The requirements the packs were suggested for are met without them:

| Requirement | How it is met | Where |
|---|---|---|
| Wood / dojo background | Painted procedurally into an offscreen canvas once per session: radial base gradient, ~2600 grain strokes, four panel boards, a 64 px lattice and a vignette. Blitted with one `drawImage` per frame. | `buildWood()` in `aipi5/ui/web/index.html` |
| Ten distinct fruit | One canvas path routine per shape — crescent, cluster, cone, rind-and-stripe, and so on. Not ten tints of one disc. | `SHAPES` in `aipi5/ui/web/index.html` |
| Sliced halves | Half-disc of flesh with a rind arc on the curved side only, thrown apart perpendicular to the cut with independent velocity, spin and gravity. | `spawnHalves()`, `drawHalves()` |
| Per-fruit juice and splash | One parameterised splash system: colour, size, particle count, speed and lifetime all vary per fruit, from a table in the Python. | `splash()` / `FruitKind.juice`, `.wetness` |
| Particles, sparks, glow | Circles and additive blending. | `sparks()`, `drawDrops()` |
| Ultimate aura, cracks, particle ring | Radial gradient, golden-angle crack strokes, orbiting points. | `drawDragon()` |
| The player's ninja silhouette | Torso polygon, limb strokes, hood ellipse, sash rectangle and ribbon curves, all built from eleven live pose keypoints. No sprite, no rig, no traced outline. | `drawPlayerShadow()` |
| Per-fruit slice sounds | Web Audio: band-passed noise swept between two frequencies, plus a pitched body, per fruit, with per-hit pitch and level variation. This is exactly what the plan permitted in place of recordings — "swish + fruit impact/squelch + pitch variation + volume variation". | `SLICE_VOICES`, `sweptNoise()`, `playSliceSound()` |
| Bomb, combo, warning, completion sounds | The same synthesiser, different figures. | `playGameSound()` |
| Music | None. The game plays over whatever the assistant's audio system is doing, and `AudioPriority` ducks it. Adding a music bed would fight the thing it ducked. | `aipi5/games/manager.py` |

Because nothing is loaded from disk, the plan's asset-preloading requirement is
satisfied by there being nothing to preload: the first fruit of a round is
drawn and sounded by code that is already parsed.

## Emoji glyphs

Each `FruitKind` carries an emoji (`🍉`, `🍌`, …). It is **a garnish drawn on
top of the procedural shape, never the thing that identifies the fruit** — on a
device without a colour-emoji font the glyph draws nothing and the fruit is
still the right shape, the right size and the right colour.

Emoji are rendered from whichever font the system provides (on the Pi, Noto
Color Emoji, from Debian's `fonts-noto-color-emoji`, SIL Open Font License
1.1). Nothing is bundled; the characters themselves are Unicode code points,
which are not copyrightable.

## What is borrowed

| What | Source | Licence | How it is used |
|---|---|---|---|
| The *idea* of the player's silhouette being a hooded ninja — black cloth, red waist sash, trailing red headband ribbons | A stock illustration supplied by the user as a visual reference, credited on its face to Freepik | Unknown; **not relied on** | Looked at, and not used. The figure in `drawPlayerShadow` is constructed from eleven live pose keypoints — torso polygon, limb strokes, hood ellipse, sash rectangle, ribbon curves — and shares no pixels, path data or proportions with the reference. A costume convention (ninjas wear black and a red sash) is not protectable; the drawing of one is, which is why none of it was traced, copied or embedded. |
| Gameplay shape: fruit thrown on arcs, sliced by tracked wrists, bombs to avoid, points per fruit; the original five fruit colours and the launch-speed range as a starting point | [`hailo-ai/hailo-rpi5-examples`](https://github.com/hailo-ai/hailo-rpi5-examples) | MIT | Design reference only. No code was copied: that project ties physics to the frame rate and tests collision against a single wrist sample, and both were reimplemented — see `fruit.Fruit.advance` and `collision.slash_hits_fruit`. |
| Constants for decoding the YOLOv8-pose output tensors | `/usr/include/hailo/tappas/pose_estimation/yolov8pose_postprocess.cpp` | LGPL | **Read, not copied.** The decode in `aipi5/motion/yolov8_pose.py` is an independent numpy implementation; the reference was consulted for anchor strides and channel layout. |
| `yolov8s_pose_h10.hef` | Debian package `hailo-models`, installed on the device | Vendor licence, as installed | Loaded from `/usr/share/hailo-models/` at runtime. Not vendored into this repository. |

## Trademark

The Game library tile reads "Fruit Ninja" because that is what a person in the
room calls this kind of game. The code, the module names and this document call
it Fruit Slice. **Nothing from the commercial game of that name is used or
imitated** — not its art, its sounds, its music, its UI, its fonts or its
wording. If the name is a problem, it is one string in
`CATALOGUE` in `aipi5/games/manager.py`.

## Bundled asset files

None.

| File | Pack | Author | Source | Licence | Modifications |
|---|---|---|---|---|---|
| _(none)_ | | | | | |
