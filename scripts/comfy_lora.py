"""Teach the checkpoint who the coach is, on the machine with the GPU.

Everything else in this pipeline can be made exact by construction -- the pose
comes from the rig, the scale from her eyes, the placement from the manifest.
Her *face* cannot: a text-to-image model redraws the character from the prompt
every time and drifts, which is what "only the first and third look like the
same person" means. A LoRA is the fix that is not a request: the character
becomes weights, and every generation after it is the same person because it
is the same tensor.

The training set is the artwork already accepted -- `artwork/yoga/poses` -- so
the LoRA learns the coach as she has already been signed off, not a fresh
interpretation of the prompt.

    python scripts/comfy_lora.py dataset      # prepare and upload
    python scripts/comfy_lora.py train        # train and save the LoRA
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.build_yoga_coach import ARTWORK, clean, sources
from scripts.comfy_coach import Comfy

#: Where the images land inside ComfyUI's `input/`, and the word that will mean
#: "this character" in a prompt.
FOLDER = "yoga_coach"
TRIGGER = "yogacoach"

#: One square size for the whole set. The trainer can bucket by aspect, but a
#: single size costs nothing here: every drawing is one figure on a plain
#: ground and padding is cheaper than teaching it about crops.
SIZE = 1024

#: The caption every image shares. A character LoRA wants the *invariant* said
#: once -- what she is, not what she is doing -- because anything that varies
#: between the images and appears in the caption gets learned as part of her.
CAPTION = (f"{TRIGGER}, 1girl, solo, full body, pale silver-lavender hair in a "
           "single braid over her shoulder, pointed elf ears, green eyes, "
           "light lavender-grey sports bra, charcoal-grey leggings, barefoot, "
           "clean anime line art, soft cel shading, plain background")


def prepare(source: pathlib.Path, out: pathlib.Path) -> list[pathlib.Path]:
    """Every drawing, cut out, centred on white, one square size.

    White rather than transparent: the encoder sees RGB, and alpha dropped
    against black would teach the model a black rectangle behind her.
    """
    out.mkdir(parents=True, exist_ok=True)
    written = []
    # Only the shapes the game actually ships. `artwork/yoga/poses` still holds
    # the retired quarter and three-quarter frames, and those are the drawings
    # with the *most* character drift in them -- they were made before the
    # chained reference. Training on drift teaches drift.
    wanted = {shape.id for shape in sources()}
    for path in sorted(source.glob("*.png")):
        if path.stem not in wanted:
            continue
        figure = clean(Image.open(path))
        scale = min(SIZE * 0.86 / figure.width, SIZE * 0.92 / figure.height)
        figure = figure.resize((max(1, round(figure.width * scale)),
                                max(1, round(figure.height * scale))),
                               Image.LANCZOS)
        card = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
        card.alpha_composite(figure, ((SIZE - figure.width) // 2,
                                      (SIZE - figure.height) // 2))
        target = out / (path.stem + ".png")
        card.convert("RGB").save(target)
        written.append(target)
    return written


def upload(client: Comfy, files: list[pathlib.Path], folder: str) -> int:
    sent = 0
    for path in files:
        client.upload_to(path, path.name, folder)
        sent += 1
    return sent


def training_graph(checkpoint: str, *, folder: str, steps: int, rank: int,
                   learning_rate: float, batch: int, accumulation: int,
                   prefix: str) -> dict:
    """Encode the folder, then train a LoRA against one shared caption."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "LoadImageDataSetFromFolder",
              "inputs": {"folder": folder}},
        "3": {"class_type": "VAEEncode",
              "inputs": {"pixels": ["2", 0], "vae": ["1", 2]}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": CAPTION, "clip": ["1", 1]}},
        "5": {"class_type": "TrainLoraNode",
              "inputs": {"model": ["1", 0], "latents": ["3", 0],
                         "positive": ["4", 0], "batch_size": batch,
                         "grad_accumulation_steps": accumulation,
                         "steps": steps, "learning_rate": learning_rate,
                         "rank": rank, "optimizer": "AdamW",
                         "loss_function": "MSE", "seed": 0,
                         "training_dtype": "bf16", "lora_dtype": "bf16",
                         "quantized_backward": False, "algorithm": "LoRA",
                         "gradient_checkpointing": True, "checkpoint_depth": 1,
                         "offloading": False, "existing_lora": "[None]",
                         "bucket_mode": False, "bypass_mode": False}},
        "6": {"class_type": "SaveLoRA",
              "inputs": {"lora": ["5", 0], "prefix": prefix, "steps": steps}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dataset", "train"))
    parser.add_argument("--url", default="http://10.0.0.115:8188")
    parser.add_argument("--source", default=str(ARTWORK / "poses"))
    parser.add_argument("--staging", default=str(ARTWORK / "lora-dataset"))
    parser.add_argument("--folder", default=FOLDER)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--checkpoint", default="Illustrious-XL-v1.0.safetensors")
    parser.add_argument("--steps", type=int, default=1800)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--accumulation", type=int, default=2)
    parser.add_argument("--prefix", default="yoga_coach")
    args = parser.parse_args()

    client = Comfy(args.url, timeout=7200.0)
    if args.command == "dataset":
        files = prepare(pathlib.Path(args.source), pathlib.Path(args.staging))
        print(f"{len(files)} images prepared in {args.staging}")
        sent = upload(client, files, args.folder)
        print(f"{sent} uploaded to ComfyUI input/{args.folder}/")
        return 0

    graph = training_graph(args.checkpoint, folder=args.folder, steps=args.steps,
                           rank=args.rank, learning_rate=args.learning_rate,
                           batch=args.batch, accumulation=args.accumulation,
                           prefix=args.prefix)
    print(f"training {args.steps} steps at rank {args.rank} — this is the slow part")
    client.run(graph, poll=10.0, allow_empty=True)
    print(f"saved as {args.prefix}*.safetensors in ComfyUI/models/loras/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
