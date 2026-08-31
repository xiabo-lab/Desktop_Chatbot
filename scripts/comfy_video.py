"""Generate the movement between two poses as video, on the local ComfyUI.

The still-image pipeline fights for character consistency and loses a little
every time: the model redraws her from the prompt for each picture. A
first-last-frame video model does not have that problem, because it is not
drawing a character -- it is interpolating between two frames it was handed.
Both of those frames are artwork already approved, composited onto the field
at the exact place the game draws them, so:

- the **endpoints are exact**, which is what keeps the demonstrated pose the
  same as the scored pose;
- everything between them is one continuous shot, so the coach cannot change
  face, hair or size in the middle of a transition;
- the frames come out at 1280x800 with the background already in them, so the
  game draws them directly -- no alpha, no keying, no anchors, no manifest.

**Twenty clips cover all seventy-four transitions.** Each clip runs from
standing to one pose. Entering a pose plays its clip forwards, leaving one
plays it backwards, and Mountain Pose is the shared middle.

    python scripts/comfy_video.py --pose warrior_two_left
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.build_yoga_coach import ARTWORK, SERVED  # noqa: E402
from scripts.comfy_coach import Comfy  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIELD = ROOT / "aipi5" / "ui" / "web" / "assets" / "yoga" / "field-forest.webp"

#: The stage, and where the coach stands on it. The same three numbers
#: `yoga.js` draws her with, so a generated frame is a finished game frame.
STAGE_W, STAGE_H = 1280, 800
COACH_X, FLOOR_Y = 640, 726


#: The wall the coach is generated against when the clip is going to be keyed.
#:
#: The model **redraws the background** as well as the figure, so two clips
#: generated on the forest do not agree about where the trees are and the seam
#: shows the moment one clip follows another. Generating on a flat colour and
#: compositing onto the one real field afterwards makes every clip share a
#: background exactly, and costs a key.
#: Magenta, not green. The model lights the figure with the wall it stands
#: against, and green bounce turned her charcoal leggings olive. Her palette is
#: silver hair, warm skin, lavender-grey and charcoal -- magenta is the one
#: strong colour none of that can collide with.
CHROMA = (255, 0, 255)


def framed(pose_id: str, background: str = "field") -> Image.Image:
    """One pose, composited exactly where the game puts her."""
    manifest = json.loads((SERVED / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["poses"][pose_id]
    source = entry.get("mirror_of", pose_id)
    figure = Image.open(SERVED / (source + ".webp")).convert("RGBA")
    if entry.get("mirror_of"):
        figure = figure.transpose(Image.FLIP_LEFT_RIGHT)
    card = (Image.open(FIELD).convert("RGBA").resize((STAGE_W, STAGE_H))
            if background == "field"
            else Image.new("RGBA", (STAGE_W, STAGE_H), CHROMA + (255,)))
    card.alpha_composite(figure, (round(COACH_X - entry["hip_x"]),
                                  round(FLOOR_Y - entry["floor_y"])))
    return card.convert("RGB")


def graph(first: str, last: str, prompt: str, *, length: int, width: int,
          height: int, steps: int, cfg: float, seed: int, turbo: bool) -> dict:
    """Load the H3 stack, interpolate between two frames, save every frame."""
    nodes: dict[str, dict] = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                         "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                         "type": "minimax"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": first}},
        "5": {"class_type": "LoadImage", "inputs": {"image": last}},
        "6": {"class_type": "MiniMaxH3ImageToVideo",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                         "width": width, "height": height, "length": length,
                         "first_frame": ["4", 0], "last_frame": ["5", 0]}},
        "7": {"class_type": "MiniMaxH3SigmaShift",
              "inputs": {"model": ["1", 0], "shift_video": 12.0,
                         "shift_audio": 3.0}},
        # The model is conditioned by the two frames; the text side only has to
        # not fight them, so the negative is the positive with its content
        # zeroed rather than a second opinion about what a yoga class is.
        "8": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["7", 0], "seed": seed, "steps": steps,
                         "cfg": cfg, "sampler_name": "euler",
                         "scheduler": "simple", "positive": ["6", 0],
                         "negative": ["8", 0], "latent_image": ["6", 1],
                         "denoise": 1.0}},
        "10": {"class_type": "VAEDecode",
               "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage",
               "inputs": {"images": ["10", 0],
                          "filename_prefix": "yoga_video/frame"}},
    }
    if turbo:
        nodes["12"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["1", 0], "strength_model": 1.0,
                                  "lora_name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"}}
        nodes["7"]["inputs"]["model"] = ["12", 0]
    return nodes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://10.0.0.115:8188")
    parser.add_argument("--pose", default="warrior_two_left")
    parser.add_argument("--from-pose", default="mountain")
    parser.add_argument("--length", type=int, default=25)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-turbo", action="store_true")
    parser.add_argument("--background", choices=("field", "chroma"),
                        default="field")
    parser.add_argument("--out", default=str(ARTWORK / "video"))
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ends = {}
    for tag, pose_id in (("first", args.from_pose), ("last", args.pose)):
        path = out / f"_{tag}_{pose_id}.png"
        framed(pose_id, args.background).save(path)
        ends[tag] = path

    client = Comfy(args.url, timeout=3600.0)
    first = client.upload(ends["first"], f"yoga_first_{args.from_pose}.png")
    last = client.upload(ends["last"], f"yoga_last_{args.pose}.png")

    prompt = ("A calm yoga instructor moves smoothly and slowly from standing "
              "into the next yoga pose, one continuous movement, steady "
              "camera, no cuts, plain flat magenta background."
              if args.background == "chroma" else
              "A calm yoga instructor in a forest clearing moves smoothly and "
              "slowly from standing into the next yoga pose, one continuous "
              "movement, steady camera, no cuts.")
    images = client.run(graph(first, last, prompt, length=args.length,
                              width=STAGE_W, height=STAGE_H, steps=args.steps,
                              cfg=args.cfg, seed=args.seed,
                              turbo=not args.no_turbo), poll=5.0)
    # Named by BOTH endpoints. v2 routes a pose through its family hub, so
    # `triangle_left` can be entered from Star and from Mountain and those are
    # different movements -- naming the folder after the destination alone
    # silently overwrites one with the other, which it did once.
    pair = f"{args.from_pose}__{args.pose}"
    clip = out / (pair + ("-chroma" if args.background == "chroma" else ""))
    clip.mkdir(parents=True, exist_ok=True)
    for index, data in enumerate(images):
        (clip / f"{index:03d}.png").write_bytes(data)
    print(f"{len(images)} frames in {clip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
