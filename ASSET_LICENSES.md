# Asset licences

Most Fruit Ninja visuals and every sound are still generated at runtime by
code in this repository. The exceptions are the ninja-head art and a retained
copy of the MediaPipe runtime/model used by the former palm-click control.
They are recorded here so their provenance remains clear while deployed
devices transition to the Hailo arms-crossed gesture.

## Bundled files

| File(s) | Author / source | Licence | Use and modifications |
|---|---|---|---|
| `aipi5/ui/web/assets/fruit-ninja/ninja-head.png` | Original output generated with OpenAI image generation for this project. The user-supplied Freepik picture was a style reference only. | Generated project artwork; no third-party pixels are embedded. | Chroma-key background removed, transparent bounds cropped, and resized to 512 px. Drawn over the live pose-driven neck; the body remains procedural. |
| `aipi5/ui/web/assets/boxing/arena-anime-v2.png` | Original output generated with OpenAI image generation for this project. The user-supplied boxing screenshots were composition references only. | Generated project artwork; no third-party pixels are embedded. | Empty 2.5D anime boxing arena background. Fighters, animation, impacts, lighting changes and HUD remain live Canvas/DOM layers. |
| `aipi5/ui/web/assets/boxing/player-red-torso.png`, `opponent-blue-torso.png` | Original outputs generated with OpenAI image generation for this project from the user-supplied red-player / blue-opponent design sheet. | Generated project artwork; no source-sheet pixels are embedded. | Chroma-key backgrounds removed with a soft alpha matte. Used as the clean sources for the baked damage bodies; live arms, gloves, motion and reactions remain Canvas-driven. |
| `artwork/boxing-damage/*.png` | Original damage progression and source artwork generated with OpenAI image generation for this project using the approved generated fighters as style references. | Generated project artwork; no third-party pixels are embedded. | Project-bound source sheets for the four-location damage matrix. |
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
