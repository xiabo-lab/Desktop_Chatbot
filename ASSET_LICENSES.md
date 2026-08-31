# Asset licences

Most Fruit Ninja visuals and every sound are still generated at runtime by
code in this repository. The exceptions are the Boxing artwork and a retained
copy of the MediaPipe runtime/model used by the former palm-click control.
They are recorded here so their provenance remains clear while deployed
devices transition to the Hailo arms-crossed gesture.

## Bundled files

| File(s) | Author / source | Licence | Use and modifications |
|---|---|---|---|
| `aipi5/ui/web/assets/boxing/arena-anime-v2.png` | Original output generated with OpenAI image generation for this project. The user-supplied boxing screenshots were composition references only. | Generated project artwork; no third-party pixels are embedded. | Empty 2.5D anime boxing arena background. Fighters, animation, impacts, lighting changes and HUD remain live Canvas/DOM layers. |
| `aipi5/ui/web/assets/boxing/player-red-torso.png`, `opponent-blue-torso.png` | Original outputs generated with OpenAI image generation for this project from the user-supplied red-player / blue-opponent design sheet. | Generated project artwork; no source-sheet pixels are embedded. | Chroma-key backgrounds removed with a soft alpha matte. Used as the clean sources for the baked damage bodies; live arms, gloves, motion and reactions remain Canvas-driven. |
| `artwork/boxing-damage/*.png` | Original damage progression and source artwork generated with OpenAI image generation for this project using the approved generated fighters as style references. | Generated project artwork; no third-party pixels are embedded. | Project-bound source sheets for the four-location damage matrix. |
| `aipi5/ui/web/assets/boxing/fp/glove.webp`, `forearm.webp` | Deterministic crops of the generated `06-right-straight-head` pose sheet, whose rear-view fighter has one arm extended away from the camera. | Generated project artwork. | The player's own glove and forearm for the first-person view, cut apart at the wrist and stored at 2x by `scripts/build_boxing_first_person.py`. One right arm; the left is the same sprite reflected at draw time. |
| `aipi5/ui/web/assets/boxing/damage/{opponent,player}/*.webp` | Deterministic derivatives of the generated transparent fighter bodies and generated damage art direction. | Generated project artwork. | 256 complete opponent bodies and 16 meaningful rear-player bodies. Bruising, swelling and restrained stage-three bleeding are baked into the body pixels; runtime Canvas overlays are not used. |
| `artwork/boxing-poses/{previews,sheets,transparent-sheets}/*.png` | Original outputs generated with OpenAI image generation for this project from the user-supplied red-player / blue-opponent design sheet and approved preview direction. | Generated project artwork; no source-sheet pixels are embedded. | Project-bound sources for 21 paired action/reaction sheets. Flat chroma backgrounds were removed with the ImageGen soft-matte helper before deterministic splitting and registration. |
| `aipi5/ui/web/assets/boxing/poses/**` | Deterministic derivatives of the generated full-body pose sheets and approved damage progression. | Generated project artwork. | 41 clean full-body pose bases and 5,456 complete pose/damage WebPs: 21 rear-player poses × 16 visible shoulder states, plus 20 front-opponent poses × 256 four-location states. Runtime selects whole images and does not overlay bruises or construct fighter arms. |
| `aipi5/ui/web/assets/mediapipe/vision_bundle.mjs`, `wasm/*` | Google MediaPipe, npm package [`@mediapipe/tasks-vision@1.0.0`](https://www.npmjs.com/package/@mediapipe/tasks-vision) | Apache-2.0; bundled text in `aipi5/ui/web/assets/mediapipe/LICENSE` | Unmodified browser runtime, retained for provenance but no longer loaded by the game. |
| `aipi5/ui/web/assets/mediapipe/gesture_recognizer.task` | Google MediaPipe [Gesture Recognizer model bundle](https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task) | Apache-2.0 (MediaPipe model bundle) | Unmodified float16 model from the former `Open_Palm` / `Closed_Fist` control; no longer loaded. |

The replacement crossed-arms detector uses only the local Hailo pose skeleton.
No browser hand model runs and camera frames are not sent to any service.

## Runtime-generated assets

| Requirement | How it is met | Where |
|---|---|---|
| Wood / dojo background | Offscreen canvas with gradients, grain strokes, panels, lattice, and vignette | `buildWood()` in `aipi5/ui/web/index.html` |
| Ten distinct fruit and sliced halves | Canvas paths parameterized by the authoritative Python fruit state | `SHAPES`, `drawFruit()`, `spawnHalves()` |
| Juice, particles, Ultimate aura, cracks | Canvas particle and compositing systems | `splash()`, `drawDrops()`, `drawDragon()` |
| Ninja body animation | Curved torso, tapered sleeves and trousers, wrapped hands, red sash, all fitted to live pose joints | `drawPlayerShadow()` |
| Slice, bomb, combo, warning, and completion sounds | Web Audio oscillators and filtered noise; no recordings | `playGameSound()`, `playSliceSound()` |
| Boxing three-lane pose selection/crossfades, parallax, crowd lights, training targets and impacts | Original Canvas 2D transforms, gradients and bounded particles; no third-party pixels | `aipi5/ui/web/assets/boxing/boxing.js` |
| Boxing bell, punch, body/head hit, block, parry, dodge, heartbeat, crowd reaction, KO, win and lose sounds | Web Audio oscillators and filtered noise; no recordings | `playSound()` in `aipi5/ui/web/assets/boxing/boxing.js` |

## References that are not bundled

| What | Source | Licence | How it is used |
|---|---|---|---|
| Ninja costume and friendly flat-cartoon direction: black cloth, warm eye opening, red sash, trailing headband ribbons | Stock illustration supplied by the user, credited on its face to Freepik | Unknown; not relied on for redistribution | Style reference only. The file is not copied into the repository. The head asset was newly generated and the animated body is built from live pose geometry. |
| Third-person boxing composition: rear player, centered front-facing opponent, ring depth and audience hierarchy | Four boxing screenshots supplied by the user | Reference provenance only; not redistributed | Composition reference only. The screenshots are not copied into the repository. The shipped arena is newly generated and the characters, HUD and effects are independently drawn in code. |
| Red rear-view player and blue front-view opponent character design | Character reference sheet supplied by the user as `box game player opponent.png` | Reference provenance only; not redistributed | Character-design reference. The sheet is not bundled. Two new modular torso layers were generated from it; no labels, panels or original pixels are copied. |
| Fruit-slicing gameplay shape and the original launch-speed range | [`hailo-ai/hailo-rpi5-examples`](https://github.com/hailo-ai/hailo-rpi5-examples) | MIT | Design reference only. Physics and swept collision were independently implemented. |
| Constants for decoding YOLOv8-pose output tensors | `/usr/include/hailo/tappas/pose_estimation/yolov8pose_postprocess.cpp` | LGPL | Consulted for anchor strides and channel layout; the NumPy decoder is independent. |
| `yolov8s_pose_h10.hef` | Debian package `hailo-models` on the device | Vendor licence, as installed | Loaded from `/usr/share/hailo-models/`; not vendored here. |

## Yoga Coach

The coach was drawn at runtime from the bone tables until 2026-08-19, when she
became a character with pictures. The rig is still there and still draws her
whenever a picture is missing or has not loaded yet, and it is still the only
source of the shapes: every picture below was generated *from* a skeleton
rendered by `rig.forward_kinematics`, the same function the player is scored
through.

| File(s) | Author / source | Licence | Use and modifications |
| --- | --- | --- | --- |
| `artwork/yoga/coach-reference-anime.png` | Original output generated with OpenAI image generation (`gpt-image-1.5`) for this project, from a written description supplied by the user. No reference image was uploaded to the model. | Generated project artwork; no third-party pixels are embedded. | The coach's character sheet: the one picture every pose is style-matched against. |
| `artwork/yoga/coach-reference.png`, `coach-reference-front.png` | Original outputs generated with OpenAI image generation (`gpt-image-1.5`) for this project. | Generated project artwork. | Superseded character sheets, kept as the record of the two earlier looks. Not served. |
| `artwork/yoga/guides/*.png` | Rendered by `scripts/build_yoga_coach.py` from `aipi5/games/yoga/rig.py`. | This project's own code and data. | Skeleton diagrams of the twenty-one distinct poses, used as the geometry the image model must match. Not served. |
| `artwork/yoga/poses/*.png` | Original outputs generated with OpenAI image generation (`gpt-image-1.5`) for this project, each from the character sheet plus that pose's generated skeleton guide. | Generated project artwork; no third-party pixels are embedded. | The coach demonstrating each pose, at full resolution with alpha. Sources for the served WebP. |
| `artwork/yoga/poses/*__50.png` | The same pipeline, from a guide rendered halfway between standing and the pose, and generated with that pose's own accepted drawing passed in as a second reference so the frame either side of it is the same person. | Generated project artwork; no third-party pixels are embedded. | The transition frames, one per pose. A movement between any two poses is assembled from these — out of one pose, through standing, into the next — rather than drawn per pair, of which the three lessons contain 74. `*__25.png` and `*__75.png` are earlier, unused frames from when there were three per pose. |
| `aipi5/ui/web/assets/yoga/coach/*.webp`, `manifest.json` | Deterministic derivatives of `artwork/yoga/poses/*.png`, produced by `scripts/build_yoga_coach.py pack`. | Generated project artwork. | Trimmed, scaled to 160 px per spine length and packed with the hip and floor anchors the page places her by. Left/right pairs share one file; the right-hand version is drawn mirrored. |
| `artwork/yoga/video/_first_*.png`, `_last_*.png` | Copies of the accepted drawings in `artwork/yoga/poses/`, uploaded to the local ComfyUI as the two ends of a clip by `scripts/comfy_video.py`. | Generated project artwork; no third-party pixels are embedded. | The endpoints every clip is interpolated between. Kept because they are what makes a clip reproducible: the same two frames give the same movement back. |
| `artwork/yoga/video/*-chroma/*.png` | Original outputs generated locally with ComfyUI (`MiniMaxH3ImageToVideo`, `minimax_h3_fl2v_turbo_8step`) on the user's own GPU, from the two endpoint drawings above. No third-party image was supplied to the model. | Generated project artwork; no third-party pixels are embedded. | The raw 39-frame clips on a magenta wall, before keying and packing. Not committed — 518 MB of intermediates that `scripts/build_yoga_clips.py` reduces to 2.8 MB, and regenerable from the endpoints above. |
| `artwork/yoga/video/_preview/*.webp` | Rendered by `scripts/build_yoga_clips.py preview` from the packed frames and `field-forest.webp`. | Generated project artwork. | One animated loop per pose, played forwards then backwards on the real field, for judging a clip before it ships. Not served. |
| `aipi5/ui/web/assets/yoga/clips/*/*.webp`, `manifest.json` | Deterministic derivatives of `artwork/yoga/video/*-chroma/*.png`, produced by `scripts/build_yoga_clips.py`. | Generated project artwork. | The twenty transition clips the class actually plays: seven frames per pose, keyed off the magenta wall with `min(r, b) - g`, cropped to the coach with the offset written down. Entering a pose plays one forwards and leaving one plays it backwards, so twenty clips cover all seventy-four transitions. Left/right pairs share one clip; the right-hand version is drawn mirrored. |
| `artwork/yoga/openpose/*.png`, `artwork/yoga/comfy/*.png`, `artwork/yoga/lora-dataset*/*.png` | Skeleton cards rendered by `scripts/comfy_coach.py` from `aipi5/games/yoga/rig.py`; one ControlNet test output; and a training set assembled by `scripts/comfy_lora.py` from `artwork/yoga/poses/`. | This project's own code and data, and generated project artwork. | Two abandoned routes to the same problem, kept as the record of why the clips exist: ControlNet would not hold the character across separate images, and a LoRA trained on the accepted poses did not fix it either. Nothing here is served. |
| `artwork/yoga/field-forest.png`, `aipi5/ui/web/assets/yoga/field-forest.webp` | Original output generated with OpenAI image generation (`gpt-image-2`) for this project. | Generated project artwork; no third-party pixels are embedded. | The mown clearing ringed by forest that the class is held in. Cut from 1536x1024 to the 1280x800 stage by `scripts/build_yoga_coach.py field`. |

The coach's design — pale hair in a side ponytail, pointed ears, a sports top
and leggings — follows a look the user asked for in conversation. She is an
original character generated from a written description for this project; no
existing character, artwork or franchise is copied, referenced by name, or
bundled, and no uploaded image was sent to the image model.

**The class music is synthesised, not recorded.** `yoga.js` builds it from
oscillators at run time: a three-voice pad gliding around four chords with an
occasional bell over it. There is no audio file in this repository and none is
fetched. It was written that way for three reasons and licensing is one of
them — the others are that a twenty-minute recording is a twenty-minute
download to a device fed by `scp`, and that anything looped has a seam the
fourth time round.

Pose names and their Sanskrit names are the common vocabulary of the practice
and are not anyone's property.

Tummee's beginner sequence library was read as a reference for class *structure*
— full-body warm-up, standing sequence, a progression into Tree Pose, hip and
chest opening, cooldown. No Tummee image, sequence file, text or artwork is
copied or bundled; the pose choices, holds, transitions, coach and lesson data
here are this project's own.

## Emoji and trademark note

Fruit emoji are optional garnish rendered from the system font (Noto Color
Emoji on the Pi, SIL Open Font License 1.1); no font is bundled.

The library tile says “Fruit Ninja” because that is the familiar name for the
activity. No artwork, audio, music, UI, font, or code from the commercial game
is used.

The Boxing game likewise contains no art, audio, characters, code, or other
assets from the linked reference games. Those links informed only broad
interaction goals (motion training and timed parry/counter play); all shipped
visuals and sounds are original procedural work in this repository.

## Calendar

| File(s) | Author / source | Licence | Use and modifications |
| --- | --- | --- | --- |
| `aipi5/ui/web/assets/calendar/solarlunar.min.js` | [`yize/solarlunar` 3.1.0](https://github.com/yize/solarlunar), copyright © 2015 yize | ISC | Pinned browser build bundled for fully offline Gregorian ↔ Chinese lunar conversion, leap-month data and solar terms for 1900–2100. Used unmodified; the missing source map is development metadata and is not required at runtime. |

ISC License

Copyright (c) 2015, yize

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.

### Yoga Coach v2 — representative animation test set (2026-08-20)

| File(s) | Author / source | Licence | Use and modifications |
| --- | --- | --- | --- |
| `artwork/yoga/guides-floor/*.png` | Rendered by `scripts/build_yoga_floor.py` from hand-authored joint layouts. | This project's own code and data. | Skeleton diagrams for the guided poses, which have no bone table to render one from. Not served. |
| `artwork/yoga/poses/savasana.png`, `child_pose.png`, `warrior_three_left.png` | Original outputs generated with OpenAI image generation (`gpt-image-1.5`) for this project, each from the coach's character sheet plus that pose's hand-authored diagram. No third-party image was supplied. | Generated project artwork; no third-party pixels are embedded. | The first three guided poses of the v2 curriculum. The character sheet is passed on every call, which is the whole of the identity mechanism. |
| `artwork/yoga/video/*__*-chroma/*.png` | Original outputs generated locally with ComfyUI (`MiniMaxH3ImageToVideo`, `minimax_h3_fl2v_turbo_8step`) on the user's own GPU, interpolating between two of the drawings above. | Generated project artwork; no third-party pixels are embedded. | The representative transition clips and hold loops. Folder names carry both endpoints, because v2 routes a pose through its family hub and the same destination can be entered from more than one place. Not committed — regenerable from the endpoint drawings. |
