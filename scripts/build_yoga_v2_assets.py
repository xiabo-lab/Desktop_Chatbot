#!/usr/bin/env python3
"""Pack the v2 clips and hold loops: adaptive key, every frame, 10 fps.

Two things separate this from `build_yoga_clips.py`, and both came out of the
representative generation run.

**Every frame is kept.** The v1 packer sampled seven frames out of thirty-nine,
which is about two frames a second over a three-second move. §14 asks for 10
fps, so a 3.9-second clip ships all 39 of its frames and a hold loop ships all
22 of its.

**The key is adaptive.** v1 keyed on `min(r, b) - g`, which assumes the backdrop
is still the magenta it was asked for. Measured over the generated set, that
holds for 39 frames and fails badly beyond: a 73-frame clip drifts to
(247,158,201) at its midpoint and a 90-frame one to (138,222,220) -- pink and
cyan. The model is conditioned on the two endpoint frames, so it holds *those*
and lets the middle wander. Sampling the backdrop from the four corners of each
frame and keying on distance from what is actually there cuts her out of any of
them.

Short clips are still preferable: a drifted backdrop tints the coach as well as
the frame, and no key removes light that was painted onto her.

    python scripts/build_yoga_v2_assets.py --clips star__triangle_left ...
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from PIL import Image, ImageChops, ImageMath

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
VIDEO = ROOT / "artwork" / "yoga" / "video"
SERVED = ROOT / "aipi5" / "ui" / "web" / "assets" / "yoga" / "v2"
STAGE_W, STAGE_H = 1280, 800
FPS = 10

#: How far a pixel has to be from the sampled backdrop before it is her,
#: summed across the three channels so the scale runs 0-765.
#:
#: **The ceiling matters more than the floor, and 150 was far too low.** An
#: edge pixel that is half coach and half backdrop sits about 135 from a
#: magenta wall, so a ramp saturating at 150 called it 86% opaque, the
#: un-premultiply then divided by an alpha far larger than the real one, and
#: the backdrop it failed to remove showed up as a pink rim around her --
#: measured at 9% of her visible pixels and plainly visible on the device.
#:
#: Swept against that measurement: 150 gave 9.15%, 300 gave 1.10%, and 400
#: gives **zero** while keeping 44214 of 45280 visible pixels. Past 400 the
#: fringe is already gone and the only thing still being eaten is her hair.
KEY_LOW, KEY_HIGH = 60, 400

#: The un-premultiply divides by alpha, so a nearly transparent pixel gets its
#: colour multiplied by a huge number and its noise with it. That noise is
#: high-frequency and WebP cannot compress it -- uncapped it cost 38.1 kB a
#: frame against 33.5 with the divisor floored here, for no visible gain, since
#: the pixels being amplified are almost invisible anyway.
ALPHA_FLOOR = 128
CORNER = 14


def backdrop(image: Image.Image) -> tuple[int, int, int]:
    """The colour actually behind her, averaged over the four corners."""
    width, height = image.size
    boxes = ((0, 0, CORNER, CORNER),
             (width - CORNER, 0, width, CORNER),
             (0, height - CORNER, CORNER, height),
             (width - CORNER, height - CORNER, width, height))
    pixels: list[tuple[int, int, int]] = []
    for box in boxes:
        pixels.extend(list(image.crop(box).convert("RGB").getdata()))
    return tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))


def key(image: Image.Image) -> Image.Image:
    """Cut her out of whatever the backdrop turned out to be, and un-mix it.

    Alpha alone is not enough. Every edge pixel is a *mixture* -- part coach,
    part backdrop -- so keying it to partial alpha and leaving its colour alone
    keeps the backdrop's hue in the result. Composited onto grass that shows up
    as a magenta rim around her, and measured over the first packed set it was
    6 to 14% of her visible pixels.

    So the colour is solved rather than nudged. Each pixel is

        seen = a*coach + (1 - a)*backdrop

    and the coach is what we want, so

        coach = (seen - (1 - a)*backdrop) / a

    which is an un-premultiply against the backdrop actually sampled. Fully
    opaque pixels come through untouched; the softer the edge, the more of the
    backdrop is taken back out of it.
    """
    image = image.convert("RGB")
    back = backdrop(image)
    red, green, blue = image.split()
    distance = ImageChops.add(
        ImageChops.add(
            ImageChops.difference(red, Image.new("L", image.size, back[0])),
            ImageChops.difference(green, Image.new("L", image.size, back[1]))),
        ImageChops.difference(blue, Image.new("L", image.size, back[2])))
    span = max(1, KEY_HIGH - KEY_LOW)
    alpha = distance.point(
        lambda v: min(255, max(0, round((v - KEY_LOW) * 255 / span))))

    fixed = []
    for channel, level in zip((red, green, blue), back):
        # max(a,1) keeps the divide safe; those pixels are fully transparent
        # and their colour is never seen anyway.
        # Pillow 11 renamed `eval`; `lambda_eval` is the supported form and
        # is what ships here (12.3.0).
        fixed.append(ImageMath.lambda_eval(
            # The image has to come first in min/max here -- these dispatch on
            # the left operand, and an int on the left has nothing to apply.
            lambda args: args["convert"](
                args["min"](
                    args["max"](
                        (args["c"] - (255 - args["a"]) * args["bg"] / 255)
                        * 255 / args["max"](args["a"], args["fl"]),
                        0),
                    255),
                "L"),
            c=channel, a=alpha, bg=level, fl=ALPHA_FLOOR))
    return Image.merge("RGBA", (*fixed, alpha))


def pack(source: pathlib.Path, out: pathlib.Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    frames = sorted(source.glob("*.png"))
    placed = []
    for index, path in enumerate(frames):
        cut = key(Image.open(path))
        box = cut.split()[3].getbbox() or (0, 0, cut.width, cut.height)
        cut.crop(box).save(out / f"{index:03d}.webp", "WEBP",
                           quality=86, method=3)
        placed.append({"x": box[0], "y": box[1],
                       "w": box[2] - box[0], "h": box[3] - box[1]})
    return {"frames": len(placed), "at": placed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", nargs="*", default=[])
    parser.add_argument("--loops", nargs="*", default=[])
    parser.add_argument("--out", default=str(SERVED))
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    manifest = {"fps": FPS, "width": STAGE_W, "height": STAGE_H,
                "clips": {}, "loops": {}}
    path = out / "manifest.json"
    if path.exists():
        manifest.update(json.loads(path.read_text(encoding="utf-8")))

    for name in args.clips:
        source = VIDEO / f"{name}-chroma"
        if not source.exists():
            print(f"  no frames for {name}")
            continue
        entry = pack(source, out / "clips" / name)
        kb = sum(f.stat().st_size for f in (out / "clips" / name).glob("*.webp")) / 1024
        entry["seconds"] = round(entry["frames"] / FPS, 1)
        manifest["clips"][name] = entry
        print(f"  clip {name:34} {entry['frames']:3} frames "
              f"{entry['seconds']:4.1f}s {kb:7.1f} kB")

    for name in args.loops:
        source = VIDEO / f"{name}__{name}-chroma"
        if not source.exists():
            print(f"  no frames for loop {name}")
            continue
        entry = pack(source, out / "loops" / name)
        kb = sum(f.stat().st_size for f in (out / "loops" / name).glob("*.webp")) / 1024
        entry["seconds"] = round(entry["frames"] / FPS, 1)
        manifest["loops"][name] = entry
        print(f"  loop {name:34} {entry['frames']:3} frames "
              f"{entry['seconds']:4.1f}s {kb:7.1f} kB")

    out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    total = sum(f.stat().st_size for f in out.rglob("*.webp")) / 1024
    print(f"\n  {total:.0f} kB packed in total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
