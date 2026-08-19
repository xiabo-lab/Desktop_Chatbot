#!/usr/bin/env python3
"""Cut the player's own glove and forearm out of the approved pose artwork.

Boxing is played from behind the player's eyes, so the only part of them that
is ever on screen is a pair of gloves coming up from the bottom of the frame.
That is one sprite and its mirror, not a body — and the sprite already exists
inside artwork this project has already generated and licensed.

**Sheet 06, the right straight to the head.** The rear-view fighter has his arm
fully extended away from the camera, which is exactly the view a player has of
their own punching arm. Nothing else on that half of the sheet touches it, so a
crop and an alpha trim get a clean cutout with no matting to hand-fix.

Split into two pieces rather than kept as one, because they have to move
differently: the glove sits at the tracked wrist and is scaled by how far away
the hand is, while the forearm is a bridge from the edge of the frame to
whatever the glove is doing and has to stretch to whatever length that needs.
One rigid sprite could do neither.

Run from anywhere; writes into the served asset folder and prints what it did.

    python3 scripts/build_boxing_first_person.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SHEET = (ROOT / "artwork" / "boxing-poses" / "transparent-sheets"
         / "06-right-straight-head.png")
OUTPUT = ROOT / "aipi5" / "ui" / "web" / "assets" / "boxing" / "fp"

#: The extended arm on the player's half of the sheet, before trimming.
#:
#: The left edge is where it is to keep the fighter's hair out of the crop —
#: the arm passes close under it, and a stray black tuft on the tail of a
#: first-person forearm reads as dirt on the lens. Everything else is trimmed
#: to the alpha bounding box rather than guessed.
ARM_BOX = (478, 138, 792, 300)

#: Where the forearm ends and the glove begins, in trimmed-crop pixels.
#: Measured by eye against three candidates: 180 is the last column before the
#: glove's red cuff starts, so neither piece carries a sliver of the other.
WRIST_X = 180

#: How much of the forearm's elbow end is faded out to nothing, as a fraction
#: of its length.
#:
#: **This is what stops the arm looking like a plank.** The renderer draws the
#: forearm at whatever length the glove's distance calls for, and on a straight
#: punch that length is shorter than the gap to the bottom of the frame — so
#: without a fade the arm ends in mid-air on a hard stretched edge. Baked in
#: here rather than done per frame with a gradient and a scratch canvas,
#: because it is the same ramp on every frame of every round.
ELBOW_FADE = .42

#: Both pieces are stored at twice the size they are cut at, so the good
#: resampling happens once here rather than every frame in the browser. The art
#: is flat cel shading with heavy outlines, which upscales cleanly — checked at
#: 3x before settling on 2x, which is more than the largest on-screen size.
UPSCALE = 2


def axis_centre(image: Image.Image, x: int) -> float:
    """Vertical centre of the limb in one column, weighted by opacity.

    The arm is drawn at a slight angle, so the sprite's own axis is not simply
    the middle of its bounding box. The renderer rotates about this axis; a few
    pixels of error here shows up as a glove that hangs off its own wrist.
    """
    alpha = image.split()[3]
    weighted = total = 0.0
    for y in range(image.height):
        value = alpha.getpixel((x, y))
        if value > 40:
            weighted += y * value
            total += value
    return weighted / total if total else image.height / 2


def fade_elbow(image: Image.Image) -> Image.Image:
    """Ramp the forearm's alpha to nothing at the elbow end.

    Squared rather than linear, so the arm stays solid for most of its length
    and then goes quickly — a linear ramp is visible as a grey haze over half
    the limb, which reads as a rendering fault rather than as distance.
    """
    alpha = image.split()[3]
    pixels = alpha.load()
    span = max(1, int(image.width * ELBOW_FADE))
    for x in range(span):
        weight = (x / span) ** 2
        for y in range(image.height):
            value = pixels[x, y]
            if value:
                pixels[x, y] = int(value * weight)
    image.putalpha(alpha)
    return image


def main() -> int:
    if not SHEET.exists():
        print(f"missing source sheet: {SHEET}")
        return 1

    sheet = Image.open(SHEET).convert("RGBA")
    arm = sheet.crop(ARM_BOX)
    arm = arm.crop(arm.getbbox())
    width, height = arm.size

    elbow_y = axis_centre(arm, 0)
    wrist_y = axis_centre(arm, WRIST_X - 1)

    forearm = arm.crop((0, 0, WRIST_X, height))
    forearm = fade_elbow(forearm)
    glove = arm.crop((WRIST_X, 0, width, height))
    glove = glove.crop(glove.getbbox())
    # The glove's own wrist end, in its own pixels, after that second trim.
    glove_wrist_y = axis_centre(glove, 0)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, image in (("forearm", forearm), ("glove", glove)):
        scaled = image.resize((image.width * UPSCALE, image.height * UPSCALE),
                              Image.LANCZOS)
        path = OUTPUT / f"{name}.webp"
        scaled.save(path, "WEBP", quality=92, method=6)
        print(f"  {path.relative_to(ROOT)}  {scaled.width}x{scaled.height}  "
              f"{path.stat().st_size / 1024:.1f} kB")

    manifest = {
        "source": str(SHEET.relative_to(ROOT)).replace("\\", "/"),
        "upscale": UPSCALE,
        # Everything below is in *stored* pixels, so the renderer never has to
        # know that the pieces were cut at half this size.
        "forearm": {
            "width": forearm.width * UPSCALE,
            "height": forearm.height * UPSCALE,
            # The two ends of the limb's axis. The renderer maps these onto the
            # line from the edge of the frame to the glove.
            "elbow": [0, round(elbow_y * UPSCALE, 1)],
            "wrist": [forearm.width * UPSCALE, round(wrist_y * UPSCALE, 1)],
        },
        "glove": {
            "width": glove.width * UPSCALE,
            "height": glove.height * UPSCALE,
            # Where the forearm plugs in. The sprite is drawn with this point
            # on the wrist and rotated about it.
            "wrist": [0, round(glove_wrist_y * UPSCALE, 1)],
        },
    }
    path = OUTPUT / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
