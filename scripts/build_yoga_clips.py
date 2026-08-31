"""Turn generated clips into the frames the Yoga Coach plays.

`comfy_video.py` produces 39 frames of the coach moving from standing into one
pose, on a magenta wall, at the exact size and placement the game draws. This
takes that and produces what ships: a handful of keyed frames per pose, and a
manifest saying how many.

**One clip per pose, played both ways.** Entering a pose plays its clip
forwards, leaving one plays it backwards, and Mountain Pose is the shared
middle - so twenty clips cover all seventy-four transitions the three lessons
contain, and the coach is the same person throughout each one because each one
is a single continuous shot.

The frames keep the full 1280x800 stage rather than being cropped to her and
placed by anchors like the still poses are. They were generated *in* that
frame, so the anchor is already right, and a file that needs no placement
cannot be placed wrongly.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

from PIL import Image, ImageChops

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.build_yoga_coach import ARTWORK  # noqa: E402
from scripts.comfy_video import CHROMA, STAGE_H, STAGE_W  # noqa: E402

SERVED = (pathlib.Path(__file__).resolve().parents[1] / "aipi5" / "ui" / "web"
          / "assets" / "yoga" / "clips")

#: How many frames of a clip ship. A transition lasts three to five and a half
#: seconds, so seven frames is a step every half second or so -- enough to read
#: as a movement, few enough that a pose's whole transition is a couple of
#: hundred kilobytes.
FRAMES = 7


def key(image: Image.Image, low: int = 40, high: int = 150) -> Image.Image:
    """Cut the magenta wall out, and take its colour off her edges.

    **The discriminator is `min(r, b) - g`.** On the wall that is 255; on
    everything she is made of it is nothing -- warm skin has more green than
    blue, silver hair is neutral, charcoal leggings are neutral, and the
    lavender top is barely above zero. So one subtraction separates her from
    the background without a distance calculation per pixel, which matters:
    the per-pixel version took over a minute per clip and there are twenty
    clips of seven frames.

    The same number drives the despill. A pixel that is part her and part wall
    has magenta added to it in proportion, so subtracting a fraction of it from
    red and blue takes the wall back out of her edges. A hard threshold instead
    of this leaves the magenta rim that the older chroma-keyed art here has.
    """
    image = image.convert("RGB")
    red, green, blue = image.split()
    magenta = ImageChops.subtract(ImageChops.darker(red, blue), green)

    span = max(1, high - low)
    alpha = magenta.point(
        lambda v: 255 - min(255, max(0, round((v - low) * 255 / span))))
    spill = magenta.point(lambda v: round(v * 0.6))
    return Image.merge("RGBA", (ImageChops.subtract(red, spill), green,
                                ImageChops.subtract(blue, spill), alpha))


def sample(frames: list[pathlib.Path], count: int) -> list[pathlib.Path]:
    """`count` frames spread evenly over the clip, ends included."""
    if len(frames) <= count:
        return frames
    step = (len(frames) - 1) / (count - 1)
    return [frames[round(i * step)] for i in range(count)]


def pack(source: pathlib.Path, out: pathlib.Path, count: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {"width": STAGE_W, "height": STAGE_H,
                                 "frames": count, "poses": {}}
    for clip in sorted(source.glob("*-chroma")):
        pose_id = clip.name.replace("-chroma", "")
        frames = sorted(clip.glob("*.png"))
        if not frames:
            continue
        folder = out / pose_id
        folder.mkdir(parents=True, exist_ok=True)
        chosen = sample(frames, count)
        placed = []
        for index, path in enumerate(chosen):
            keyed = key(Image.open(path))
            # Cropped to her, with the offset written down. The frame is
            # 1280x800 and she is a fifth of it: encoding the empty four
            # fifths costs more time than every other step in this file put
            # together, and the offset puts her back exactly.
            box = keyed.split()[3].getbbox() or (0, 0, keyed.width, keyed.height)
            keyed.crop(box).save(folder / f"{index:02d}.webp", "WEBP",
                                 quality=86, method=3)
            placed.append({"x": box[0], "y": box[1],
                           "w": box[2] - box[0], "h": box[3] - box[1]})
        manifest["poses"][pose_id] = {"frames": len(chosen), "at": placed}
        print(f"  {pose_id:22} {len(chosen)} frames")
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def preview(out: pathlib.Path, field: pathlib.Path, into: pathlib.Path) -> int:
    """An animated WebP per pose, on the real field, for looking at."""
    into.mkdir(parents=True, exist_ok=True)
    background = Image.open(field).convert("RGBA").resize((STAGE_W, STAGE_H))
    made = 0
    for folder in sorted(p for p in out.iterdir() if p.is_dir()):
        frames = []
        entry = json.loads((out / "manifest.json").read_text(
            encoding="utf-8"))["poses"].get(folder.name, {})
        for index, path in enumerate(sorted(folder.glob("*.webp"))):
            card = background.copy()
            at = (entry.get("at") or [{}])[min(index, len(entry.get("at") or [{}]) - 1)]
            card.alpha_composite(Image.open(path).convert("RGBA"),
                                 (at.get("x", 0), at.get("y", 0)))
            frames.append(card.convert("RGB").resize((STAGE_W // 2,
                                                      STAGE_H // 2)))
        if not frames:
            continue
        # Forwards then backwards: the way the class actually plays it.
        loop = frames + frames[-2:0:-1]
        loop[0].save(into / (folder.name + ".webp"), save_all=True,
                     append_images=loop[1:], duration=500, loop=0, quality=80)
        made += 1
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(ARTWORK / "video"))
    parser.add_argument("--out", default=str(SERVED))
    parser.add_argument("--frames", type=int, default=FRAMES)
    parser.add_argument("--preview", default=str(ARTWORK / "video" / "_preview"))
    args = parser.parse_args()

    manifest = pack(pathlib.Path(args.source), pathlib.Path(args.out),
                    args.frames)
    print(f"{len(manifest['poses'])} clips packed into {args.out}")
    field = (pathlib.Path(__file__).resolve().parents[1] / "aipi5" / "ui"
             / "web" / "assets" / "yoga" / "field-forest.webp")
    made = preview(pathlib.Path(args.out), field, pathlib.Path(args.preview))
    print(f"{made} animated previews in {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
