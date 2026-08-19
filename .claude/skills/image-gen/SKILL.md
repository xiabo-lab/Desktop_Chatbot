---
name: image-gen
description: "Generate or edit artwork with OpenAI's image models and save it as a project asset. Use when a picture is needed and none exists (game sprites, backgrounds, textures, icons, character poses), when an existing asset must be redrawn in a new pose, colour or style, or when something needs a transparent background. Triggers - generate an image, make artwork, regenerate the picture, create a sprite, draw a background, we need a new asset."
---

# Making pictures for AIPI5

Every image in this repository was generated, and the ones that work were
generated the same way: drafted cheaply, iterated on, then produced at full
quality against a reference so the new picture matches the ones already there.
This skill is that loop.

```bash
python .claude/skills/image-gen/scripts/imagegen.py models      # what this key can reach
python .claude/skills/image-gen/scripts/imagegen.py generate --help
python .claude/skills/image-gen/scripts/imagegen.py edit --help
```

The key comes from `aipi5.core.config.credentials()` — the project's one
audited path for it. Nothing here reads a credential and nothing here prints
one. If it says there is no key, the fix is in that file, not this one.

## The loop that works

**1. Draft.** `--draft` swaps in a small model that costs a fraction of the
real one. Prompts are wrong on the first try and almost always on the second;
find out cheaply.

```bash
python .claude/skills/image-gen/scripts/imagegen.py generate --draft \
  --prompt "a red boxing glove seen from behind, flat cel shading, heavy black outline" \
  --transparent --trim --size 1024x1024 --out draft.png
```

Draft somewhere scratch, not into the repository. `/tmp` is not a usable path
for the Python on the development machine — it resolves to `	mp` and fails —
so use the session scratchpad or a relative name you delete afterwards.

**Look at what came back.** Read the file. An image nobody looked at is not a
finished asset, and the faults that matter here — a limb at the wrong angle, a
highlight on the wrong side, a style that does not match its neighbours — are
invisible in a success message.

**2. Match the neighbours.** New artwork that does not sit beside the existing
artwork is worse than no artwork. `edit` takes one or more reference images and
is how a new picture inherits a line weight, a palette and a level of detail:

```bash
python .claude/skills/image-gen/scripts/imagegen.py edit \
  --image artwork/boxing-poses/transparent-sheets/00-idle.png \
  --prompt "the same fighter, the same line weight and palette, throwing an uppercut" \
  --transparent --out artwork/boxing-poses/uppercut.png
```

Repeat `--image` to pass several references. `--mask` takes a PNG whose
*transparent* areas are the parts to redraw; everything opaque is kept exactly,
which is the tool for changing one detail without re-rolling the whole picture.

**3. Produce.** Drop `--draft`, raise `--quality`, keep the prompt that worked.

## What this project expects of an asset

- **Transparency comes from `--transparent`, not from chroma-keying.** The
  older assets here were generated on a flat background and keyed out
  afterwards, which is why several of them have haloes. Ask for the alpha.
- **`--trim` unless you have a reason not to.** A sprite with fifty invisible
  pixels down one side has its centre somewhere other than its picture, and
  everything drawn at a computed position is then slightly wrong.
- **PNG for sources, WebP for what ships.** Measured on one 1024-square with
  alpha: **PNG 1235 kB, WebP 86 kB.** The device is fed by `scp` and already
  carries a 200 MB pose matrix; a page that pulls PNGs will feel it.
- **`aipi5/ui/server.py` serves a fixed extension list** — `.js .mjs .json
  .png .webp .task .wasm`. Anything else 404s however correctly it is placed.
- **Sources live in `artwork/`, served files in `aipi5/ui/web/assets/<game>/`.**
- **Anything derived from a source belongs in a script**, not in hand edits —
  see `scripts/build_boxing_pose_matrix.py` and
  `scripts/build_boxing_first_person.py`. When the source is regenerated, the
  derivative has to be reproducible or it silently goes stale.
- **Every generated file gets a row in `ASSET_LICENSES.md`**: what it is, what
  made it, what it is derived from, and what was done to it afterwards. That
  file is the record that no third-party pixels are in this repository, and it
  is only true while it is complete.

## Spending

Each image costs real money, and `-n 4` costs four times as much as `-n 1`.
Draft first, and **ask before generating a batch** — a pose matrix is hundreds
of images, and the ones here were built by compositing a handful of generated
sheets rather than by generating hundreds of pictures. Prefer that: generate
the few things that must be drawn, and derive the rest with PIL.

## Gotchas already paid for

- The API answers with base64 in `data[].b64_json`. There is no URL to fetch.
- `--transparent` without a PNG or WebP output format quietly returns an opaque
  picture; the script sets the format for you, but a hand-rolled call must.
- A 404 with an empty body from `/images/edits` is almost never the multipart
  encoding. It was a shadowed variable putting the filename into the URL.
- Large uploads over this link have dropped mid-request once. Retrying worked;
  shrinking the reference image also worked and is cheaper in input tokens.
