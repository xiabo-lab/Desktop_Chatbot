"""Draw the Yoga Coach on a local ComfyUI instead of a hosted image API.

Two things move here, and only one of them is "a different provider".

**The pose stops being a suggestion.** The hosted pipeline sent a stick-figure
card as a *reference image* and asked the model politely to match it; it mostly
did, and sometimes drew a fold with no legs. ControlNet is not a request -- the
skeleton conditions every denoising step, so the drawn body lands on the joints
`rig.forward_kinematics` computed. That is the property this whole game rests
on: the shape demonstrated is the shape scored.

**The character stops drifting.** A LoRA trained on the drawings already
accepted (`TrainLoraNode` is built into this ComfyUI) is the same face, hair and
outfit in every image by construction rather than by asking. This file takes
`--lora` for when that exists; without one it still works, just less strictly.

The client is `urllib` and nothing else, like `imagegen.py`: upload the
skeleton, queue a prompt, poll the history, fetch the PNG.

    python scripts/comfy_coach.py --url http://10.0.0.115:8188 --poses mountain
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.build_yoga_coach import (  # noqa: E402
    ARTWORK, CARD_H, CARD_W, by_id, clean, placed, sources)

ROOT = pathlib.Path(__file__).resolve().parents[1]

# -- the skeleton ------------------------------------------------------
#
# OpenPose COCO-18, in the colours the ControlNet was trained on. The colours
# are not decoration: the model reads which limb is which from them, and a
# skeleton drawn in tasteful greys conditions almost nothing.

#: Our rig's joints, in COCO-18 order. Indices 14-17 are eyes and ears, which
#: the rig does not have and which the body ControlNet does not need.
#:
#: **Our left is OpenPose's right.** The coach faces the player and the pose
#: stream is mirrored, so anatomical left sits at the smaller x -- which is
#: where a *camera-facing* person's right side appears, and that is what the
#: ControlNet was trained on. Mapping left to right here keeps the colours
#: conventional and costs nothing anywhere else.
COCO_FROM_RIG: tuple[tuple[int, str], ...] = (
    (0, "head"), (1, "shoulder_mid"),
    (2, "left_shoulder"), (3, "left_elbow"), (4, "left_wrist"),
    (5, "right_shoulder"), (6, "right_elbow"), (7, "right_wrist"),
    (8, "left_hip"), (9, "left_knee"), (10, "left_ankle"),
    (11, "right_hip"), (12, "right_knee"), (13, "right_ankle"),
)

LIMBS: tuple[tuple[int, int], ...] = (
    (1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10),
    (1, 11), (11, 12), (12, 13), (1, 0),
)

COLOURS: tuple[tuple[int, int, int], ...] = (
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170), (255, 0, 85),
)


def openpose_card(pose, size: tuple[int, int]) -> Image.Image:
    """One pose as an OpenPose skeleton on black, at the generation size.

    Drawn the way OpenPose's own renderer draws it: each limb is a *filled
    ellipse* whose major axis is the bone, at full opacity, with a filled
    circle at every joint. Thin translucent lines are what a diagram looks
    like, not what the ControlNet was trained on, and they condition
    correspondingly weakly -- the first version of this drew a coach with her
    arms by her sides while the skeleton held them straight out.
    """
    width, height = size
    points = placed(pose)
    scale = min(width / CARD_W, height / CARD_H)
    offset_x = (width - CARD_W * scale) / 2
    offset_y = (height - CARD_H * scale) / 2

    joints: dict[int, tuple[float, float]] = {}
    for index, name in COCO_FROM_RIG:
        x, y = points[name]
        joints[index] = (offset_x + x * scale, offset_y + y * scale)

    # **The neck is lifted off the shoulder line.**
    #
    # OpenPose defines the neck as the midpoint of the shoulders, and the rig
    # agrees -- which is fine until the arms are held straight out at shoulder
    # height. Then the neck, both shoulders, both elbows and both wrists are
    # all on one horizontal line, six limbs are drawn on top of each other, and
    # the skeleton stops saying "arms" at all: every Warrior II and every
    # T-pose came back with her arms hanging by her sides while every other
    # pose tracked perfectly. Ten per cent of the way towards the head is
    # enough to make the arm chain readable and far too little to change the
    # pose.
    neck_x, neck_y = joints[1]
    head_x, head_y = joints[0]
    joints[1] = (neck_x + (head_x - neck_x) * 0.10,
                 neck_y + (head_y - neck_y) * 0.10)

    # **And an upper arm lying along the shoulder line is tilted off it.**
    #
    # Lifting the neck fixes every pose whose arms are merely *near* the
    # shoulder line. It does not fix the ones lying exactly on it -- Warrior II
    # with its arms straight out, Cactus Arms with its upper arms level -- where
    # the shoulder-to-elbow bone is drawn on top of the neck-to-shoulder bone
    # and the model reads one wide shoulder line instead of two arms. Both came
    # back with her hands at her sides while every angled pose tracked exactly.
    #
    # Seven degrees is invisible in the finished drawing and is the difference
    # between a line and a limb. It is applied to the whole chain, so the elbow
    # angle the pose is actually about is preserved.
    shoulder_line = math.atan2(joints[5][1] - joints[2][1],
                               joints[5][0] - joints[2][0])
    for shoulder, elbow, wrist in ((2, 3, 4), (5, 6, 7)):
        sx, sy = joints[shoulder]
        ex, ey = joints[elbow]
        upper = math.atan2(ey - sy, ex - sx)
        offset = (upper - shoulder_line + math.pi) % math.pi
        if min(offset, math.pi - offset) > math.radians(8):
            continue                       # already off the line; leave it
        away = math.radians(7) * (1 if ex >= sx else -1)
        cosine, sine = math.cos(away), math.sin(away)
        for joint in (elbow, wrist):
            jx, jy = joints[joint]
            dx, dy = jx - sx, jy - sy
            joints[joint] = (sx + dx * cosine - dy * sine,
                             sy + dx * sine + dy * cosine)

    card = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(card)
    # Proportional to the frame, which is how the reference implementation
    # scales it: 4 px of stick at 512 is 12 at 1536.
    stick = max(6, round(height / 100))
    for i, (a, b) in enumerate(LIMBS):
        (x1, y1), (x2, y2) = joints[a], joints[b]
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1:
            continue
        angle = math.atan2(y2 - y1, x2 - x1)
        # The bone as a capsule: a rectangle of the limb's length and the
        # stick's width, rotated, with rounded ends.
        half = stick / 2
        dx, dy = math.cos(angle), math.sin(angle)
        nx, ny = -dy * half, dx * half
        draw.polygon([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny),
                      (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)],
                     fill=COLOURS[i % len(COLOURS)])
    for index in sorted(joints):
        x, y = joints[index]
        radius = stick * 0.62
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                     fill=COLOURS[index % len(COLOURS)])
    return card


# -- the client --------------------------------------------------------


class Comfy:
    """The four calls this needs, over plain urllib."""

    def __init__(self, url: str, timeout: float = 600.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def _json(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def upload(self, path: pathlib.Path, name: str) -> str:
        """Put an image where `LoadImage` can see it. Returns its name."""
        return self.upload_to(path, name, "")

    def upload_to(self, path: pathlib.Path, name: str, subfolder: str) -> str:
        """The same, into a subfolder of ComfyUI's `input/`."""
        boundary = "----comfy" + uuid.uuid4().hex
        body = bytearray()
        for field, value in (("type", "input"), ("overwrite", "true"),
                             ("subfolder", subfolder)):
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{field}"\r\n\r\n{value}\r\n').encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="image"; filename="{name}"\r\n'
                 "Content-Type: image/png\r\n\r\n").encode()
        body += path.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            self.url + "/upload/image", data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
        return answer.get("name", name)

    def run(self, workflow: dict, poll: float = 1.5,
            allow_empty: bool = False) -> list[bytes]:
        """Queue a workflow and wait for it. Returns the images it saved."""
        queued = self._json("/prompt", {"prompt": workflow,
                                        "client_id": self.client_id})
        prompt_id = queued["prompt_id"]
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            history = self._json(f"/history/{prompt_id}")
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(_first_error(entry))
                images = [image
                          for output in entry.get("outputs", {}).values()
                          for image in output.get("images", [])]
                if images:
                    return [self.fetch(image) for image in images]
                if status.get("completed"):
                    # Training writes a LoRA, not a picture.
                    if allow_empty:
                        return []
                    raise RuntimeError("the workflow finished without an image")
            time.sleep(poll)
        raise TimeoutError(f"{prompt_id} did not finish in {self.timeout:.0f}s")

    def fetch(self, image: dict) -> bytes:
        query = urllib.parse.urlencode({
            "filename": image["filename"],
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output")})
        with urllib.request.urlopen(self.url + "/view?" + query,
                                    timeout=self.timeout) as response:
            return response.read()


def _first_error(entry: dict) -> str:
    for message in entry.get("status", {}).get("messages", []):
        if message and message[0] == "execution_error":
            detail = message[1]
            return (f"{detail.get('node_type')}: {detail.get('exception_message')}")
    return "the workflow failed"


# -- the workflow ------------------------------------------------------

#: Generated on a flat green wall and keyed out afterwards.
#:
#: SDXL cannot emit an alpha channel and this ComfyUI has no background-removal
#: model installed, so the background is made *uniform and far from her palette*
#: instead — she is silver, skin, lavender-grey and charcoal, and none of that
#: is near this green. The key is done here with a tolerance and a despill;
#: `--matte` turns it off if a matting model appears later.
CHROMA = (0, 177, 64)

POSITIVE = (
    "masterpiece, best quality, very aesthetic, absurdres, "
    "1girl, solo, full body, standing on flat ground, complete figure from "
    "head to bare feet, whole body visible, "
    "long pale silver-lavender hair in a single braid over her left shoulder, "
    "pointed elf ears, green eyes, calm friendly face, "
    "light lavender-grey sports bra, charcoal-grey full-length leggings, "
    "barefoot, slim athletic build, "
    "clean anime line art, soft cel shading, even lighting, "
    "flat chroma-key green background, plain background, no shadow on the floor")

NEGATIVE = (
    "worst quality, low quality, jpeg artifacts, blurry, sketch, "
    "cropped, out of frame, cut off, close-up, portrait, upper body, "
    "extra limbs, missing limbs, missing legs, missing feet, extra fingers, "
    "deformed hands, bad anatomy, multiple people, 2girls, text, watermark, "
    "signature, mat, yoga mat, props, furniture, shadow, dark background")


def workflow(skeleton: str, checkpoint: str, controlnet: str, *, seed: int,
             width: int, height: int, steps: int, cfg: float, strength: float,
             lora: str | None, positive: str, negative: str) -> dict:
    """The graph, in the API format `/prompt` takes."""
    graph: dict[str, dict] = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": skeleton}},
        "5": {"class_type": "ControlNetLoader",
              "inputs": {"control_net_name": controlnet}},
        "5u": {"class_type": "SetUnionControlNetType",
               "inputs": {"control_net": ["5", 0], "type": "openpose"}},
        "6": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["2", 0], "negative": ["3", 0],
                         "control_net": ["5u", 0], "image": ["4", 0],
                         "strength": strength, "start_percent": 0.0,
                         "end_percent": 1.0}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "seed": seed, "steps": steps,
                         "cfg": cfg, "sampler_name": "dpmpp_2m",
                         "scheduler": "karras", "positive": ["6", 0],
                         "negative": ["6", 1], "latent_image": ["7", 0],
                         "denoise": 1.0}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": "yoga_coach"}},
    }
    if lora:
        graph["11"] = {"class_type": "LoraLoader",
                       "inputs": {"model": ["1", 0], "clip": ["1", 1],
                                  "lora_name": lora, "strength_model": 0.9,
                                  "strength_clip": 0.9}}
        graph["2"]["inputs"]["clip"] = ["11", 1]
        graph["3"]["inputs"]["clip"] = ["11", 1]
        graph["8"]["inputs"]["model"] = ["11", 0]
    return graph


# -- keying ------------------------------------------------------------


def key_out(image: Image.Image, colour=CHROMA, tolerance: int = 78
            ) -> Image.Image:
    """Cut the flat background out, and take the green off her edges.

    A hard threshold leaves a green fringe on every antialiased pixel, which is
    the halo the older assets in this repository have. Distance from the key
    colour is used as a *soft* alpha instead, and any pixel where green still
    leads is pulled back towards its own red/blue — a despill.
    """
    image = image.convert("RGBA")
    pixels = image.load()
    kr, kg, kb = colour
    width, height = image.size
    for y in range(height):
        for x in range(width):
            r, g, b, _ = pixels[x, y]
            distance = math.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2)
            if distance <= tolerance:
                pixels[x, y] = (r, g, b, 0)
                continue
            alpha = 255
            if distance < tolerance * 2:
                alpha = round(255 * (distance - tolerance) / tolerance)
                # Despill only the *edge*: a partly transparent pixel is part
                # her and part wall, and the wall's green has to come out of
                # it. Doing this to solid pixels as well turned the shadows on
                # her skin red, which is a bruise, not a matte.
                if g > r and g > b:
                    g = round((r + b) / 2 + (g - (r + b) / 2) * 0.35)
            pixels[x, y] = (r, g, b, alpha)
    return image


# -- the command -------------------------------------------------------


def draw(client: Comfy, shape, args) -> pathlib.Path:
    skeletons = ARTWORK / "openpose"
    skeletons.mkdir(parents=True, exist_ok=True)
    card = openpose_card(shape, (args.width, args.height))
    card_path = skeletons / (shape.id + ".png")
    card.save(card_path)
    name = client.upload(card_path, f"yoga_{shape.id}.png")

    graph = workflow(name, args.checkpoint, args.controlnet,
                     seed=args.seed if args.seed >= 0 else abs(hash(shape.id)) % 2**31,
                     width=args.width, height=args.height, steps=args.steps,
                     cfg=args.cfg, strength=args.strength, lora=args.lora,
                     positive=POSITIVE, negative=NEGATIVE)
    started = time.monotonic()
    images = client.run(graph)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / (shape.id + ".png")
    raw.write_bytes(images[0])
    if not args.no_key:
        keyed = clean(key_out(Image.open(raw)))
        keyed.save(raw)
    print(f"   {shape.id}: {time.monotonic() - started:.1f}s  {raw}")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://10.0.0.115:8188")
    parser.add_argument("--checkpoint", default="Illustrious-XL-v1.0.safetensors")
    parser.add_argument("--controlnet",
                        default="controlnet-openpose-sdxl.safetensors")
    parser.add_argument("--lora", default=None)
    parser.add_argument("--poses", default="", help="comma-separated ids")
    parser.add_argument("--out", default=str(ARTWORK / "comfy"))
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1536)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--cfg", type=float, default=5.5)
    parser.add_argument("--strength", type=float, default=1.0,
                        help="how hard the skeleton constrains the drawing")
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--no-key", action="store_true")
    args = parser.parse_args()

    client = Comfy(args.url)
    shapes = by_id()
    ids = ([p.strip() for p in args.poses.split(",") if p.strip()]
           or [shape.id for shape in sources()])
    failed = []
    for pose_id in ids:
        shape = shapes.get(pose_id)
        if shape is None:
            print(f"   {pose_id}: not a pose or transition frame")
            failed.append(pose_id)
            continue
        try:
            draw(client, shape, args)
        except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
            print(f"   {pose_id}: {exc}")
            failed.append(pose_id)
    if failed:
        print("FAILED: " + ",".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
