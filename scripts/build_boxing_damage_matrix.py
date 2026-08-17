#!/usr/bin/env python3
"""Build the Boxing fighter damage-image matrices.

The browser never paints a bruise onto a live fighter.  This build step bakes
every supported combination into a complete, registered body image; the game
only selects an image by its four base-4 damage digits.

Opponent key order::

    left_eye, right_eye, left_shoulder, right_shoulder

The rear-view player cannot show either eye, so its meaningful matrix contains
only ``left_shoulder, right_shoulder``.  That is 4**4 + 4**2 = 272 images.
"""

from __future__ import annotations

import argparse
from itertools import product
import json
import math
from pathlib import Path
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
BOXING_ASSETS = ROOT / "aipi5" / "ui" / "web" / "assets" / "boxing"
OUTPUT = BOXING_ASSETS / "damage"

OPPONENT_ZONES = (
    "left_eye", "right_eye", "left_shoulder", "right_shoulder")
PLAYER_ZONES = ("left_shoulder", "right_shoulder")

# The body layers are cropped and stretched this way by boxing.js.  Baking at
# twice the opponent's on-screen size preserves its ink while reducing the
# decoded matrix image from 6.3 MB to 1.5 MB.  The much larger foreground
# player is baked at its exact display size.
FIGHTERS = {
    "opponent": {
        "source": BOXING_ASSETS / "opponent-blue-torso.png",
        "crop": (240, 18, 890, 1402),
        "size": (488, 768),
        "zones": OPPONENT_ZONES,
        "points": {
            # Anatomical sides: a front-facing boxer's left is screen-right.
            "left_eye": (273, 118, 19, 10),
            "right_eye": (209, 118, 19, 10),
            "left_shoulder": (337, 214, 26, 16),
            "right_shoulder": (151, 214, 26, 16),
        },
    },
    "player": {
        "source": BOXING_ASSETS / "player-red-torso.png",
        "crop": (280, 55, 850, 1345),
        "size": (510, 540),
        "zones": PLAYER_ZONES,
        "points": {
            # Rear view: anatomical and screen sides are the same.
            "left_shoulder": (129, 154, 28, 16),
            "right_shoulder": (381, 154, 28, 16),
        },
    },
}


def _blob_mask(size: tuple[int, int], centre: tuple[float, float],
               radii: tuple[float, float], seed: int,
               blur: float = 2.0) -> Image.Image:
    """Return one soft, irregular, deterministic injury mask."""
    rng = random.Random(seed)
    cx, cy = centre
    rx, ry = radii
    points = []
    count = 34
    for index in range(count):
        angle = math.tau * index / count
        ripple = 1 + rng.uniform(-0.13, 0.13) + math.sin(angle * 3.0) * 0.035
        points.append((cx + math.cos(angle) * rx * ripple,
                       cy + math.sin(angle) * ry * ripple))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.filter(ImageFilter.GaussianBlur(blur))


def _tint(image: Image.Image, mask: Image.Image,
          colour: tuple[int, int, int], opacity: int,
          silhouette: Image.Image) -> None:
    mask = ImageChops.multiply(mask, silhouette)
    alpha = mask.point(lambda value: value * opacity // 255)
    layer = Image.new("RGBA", image.size, (*colour, 0))
    layer.putalpha(alpha)
    image.alpha_composite(layer)


def _damage(image: Image.Image, zone: str, level: int,
            point: tuple[int, int, int, int], seed: int) -> None:
    """Paint one approved sports-anime injury into a complete body image."""
    if level <= 0:
        return
    level = min(3, level)
    silhouette = image.getchannel("A")
    cx, cy, base_rx, base_ry = point
    growth = (1.0, 1.36, 1.62)[level - 1]
    rx, ry = base_rx * growth, base_ry * growth
    is_eye = zone.endswith("eye")

    # Swelling is part of the baked shading rather than a floating oval: a
    # low shadow, warm upper rim and narrow reflected edge curve the skin.
    if level >= 2:
        shadow = _blob_mask(image.size, (cx + 2, cy + level * 2.2),
                            (rx * 1.05, ry * 1.10), seed + 101, 2.8)
        _tint(image, shadow, (48, 15, 39),
              30 if level == 2 else 48, silhouette)

    bruise = _blob_mask(image.size, (cx, cy), (rx, ry), seed, 2.1)
    _tint(image, bruise,
          (118, 53, 92) if level == 1 else
          (92, 35, 78) if level == 2 else (70, 24, 62),
          (48, 78, 100)[level - 1], silhouette)
    red = _blob_mask(image.size, (cx - rx * .24, cy - ry * .08),
                     (rx * .55, ry * .52), seed + 37, 2.0)
    _tint(image, red, (147, 52, 77),
          (22, 34, 44)[level - 1], silhouette)
    blue = _blob_mask(image.size, (cx + rx * .28, cy + ry * .18),
                      (rx * .45, ry * .44), seed + 73, 1.6)
    _tint(image, blue, (45, 29, 76),
          (18, 32, 42)[level - 1], silhouette)

    detail = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(detail)
    if level >= 2:
        box = (cx - rx * .78, cy - ry * .74,
               cx + rx * .78, cy + ry * .62)
        draw.arc(box, 202, 326, fill=(255, 175, 144, 90 if level == 2 else 118),
                 width=2 if is_eye else 3)
        if is_eye:
            draw.arc((cx - rx * .72, cy - ry * .28,
                      cx + rx * .72, cy + ry * .62),
                     8, 172, fill=(46, 17, 43, 155), width=3)
        else:
            draw.arc((cx - rx * .64, cy - ry * .55,
                      cx + rx * .64, cy + ry * .66),
                     22, 166, fill=(55, 18, 49, 96), width=3)

    if level >= 3:
        # Exactly two small, non-graphic sports-anime bleeding strokes.
        stroke = (203, 29, 47, 225)
        width = 2 if is_eye else 3
        length = 16 if is_eye else 24
        for offset, extra in ((-rx * .16, 0), (rx * .18, 5)):
            x = cx + offset
            y = cy + ry * .35
            draw.line([(x, y), (x - 1, y + length * .48),
                       (x + 1, y + length + extra)],
                      fill=stroke, width=width, joint="curve")
            draw.ellipse((x - width, y + length + extra - width,
                          x + width, y + length + extra + width),
                         fill=(225, 38, 52, 215))
    detail.putalpha(ImageChops.multiply(detail.getchannel("A"), silhouette))
    image.alpha_composite(detail)


def _base_sprite(spec: dict) -> Image.Image:
    source = Image.open(spec["source"]).convert("RGBA")
    cropped = source.crop(spec["crop"])
    return cropped.resize(spec["size"], Image.Resampling.LANCZOS)


def _write_matrix(kind: str, spec: dict) -> int:
    directory = OUTPUT / kind
    directory.mkdir(parents=True, exist_ok=True)
    base = _base_sprite(spec)
    count = 0
    for levels in product(range(4), repeat=len(spec["zones"])):
        image = base.copy()
        for index, (zone, level) in enumerate(zip(spec["zones"], levels)):
            _damage(image, zone, level, spec["points"][zone],
                    seed=1301 + index * 211 + level * 43)
        # Preserve the exact fighter silhouette.  Injury artwork may alter
        # skin colour, but can never create pixels outside the registered body.
        image.putalpha(base.getchannel("A"))
        key = "".join(str(level) for level in levels)
        # Method 4 keeps the complete matrix build below a minute on the
        # development machine while remaining visually indistinguishable from
        # method 6 at this cel-shaded resolution.
        image.save(directory / f"{key}.webp", "WEBP", quality=92,
                   method=4, exact=True)
        count += 1
    return count


def build() -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    counts = {kind: _write_matrix(kind, spec)
              for kind, spec in FIGHTERS.items()}
    manifest = {
        "version": 1,
        "states_per_location": 4,
        "levels": {"0": "normal", "1": "first-punch",
                   "2": "second-punch", "3": "third-punch"},
        "opponent": {
            "key_order": list(OPPONENT_ZONES),
            "pattern": "opponent/{key}.webp",
            "count": counts["opponent"],
            "size": list(FIGHTERS["opponent"]["size"]),
        },
        "player": {
            "key_order": list(PLAYER_ZONES),
            "hidden_locations": ["left_eye", "right_eye"],
            "pattern": "player/{key}.webp",
            "count": counts["player"],
            "size": list(FIGHTERS["player"]["size"]),
        },
        "total": sum(counts.values()),
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    manifest = build()
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
