#!/usr/bin/env python3
"""Guide cards and artwork for the guided poses, which have no rig to render.

`build_yoga_coach.py` draws a pose's skeleton with `rig.forward_kinematics` and
sends that to the image model together with the character sheet. Guided poses
have no bone table -- that is what guided means -- so there is nothing to render
and the diagram has to be authored by hand. This is that.

**Floor poses get their own composition, and that was a correction.**

The first attempt drew them head-up, seen from directly overhead, so that the
whole-stage clip mirror would keep working for them exactly as it does for a
standing pose. It preserved the machinery and produced a Savasana that, dropped
onto grass drawn in perspective from standing eye-level, read as **a coach
standing on the lawn**. The convention was worth less than the illusion.

The rule now:

1. **Draw the pose from where a person would actually see it** -- a few paces
   away, slightly above, looking at somebody on a mat. Bodies lie and kneel
   *along* the ground with real perspective.
2. **Symmetric floor poses are never mirrored**, so they are free to use any
   composition. Savasana, Child's Pose, Easy Seat, Butterfly, Downward Dog,
   Table, Bridge and the rest are all symmetric.
3. **Sided floor poses get both sides drawn**, rather than one drawing flipped.
   A three-quarter view does not survive a horizontal flip -- the perspective
   would end up looking from the wrong side of the room. That is roughly a
   dozen extra generations and it buys correctness.

    python scripts/build_yoga_floor.py guide --poses savasana child_pose
    python scripts/build_yoga_floor.py art   --poses savasana child_pose

`art` needs the OpenAI credential, not ComfyUI. Character identity comes from
the character sheet being passed as the first reference image on every single
call -- the same mechanism that kept the shipped twenty-one one person.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.build_yoga_coach import ARTWORK, BONES, CARD_H, CARD_W

REFERENCE_DEFAULT = ARTWORK / "coach-reference-anime.png"

GUIDES = ARTWORK / "guides-floor"

#: Floor cards are landscape, because a body on the ground is.
FLOOR_W, FLOOR_H = 1536, 1024
POSES_OUT = ARTWORK / "poses"

#: Joint layouts, in fractions of the card, head at the top.
#:
#: Authored rather than derived. Each is the pose as a raised camera in front of
#: the mat would see it: the body foreshortened along its length, the limbs
#: still separated left and right so the shape reads.
LAYOUTS: dict[str, dict] = {
    "savasana": {
        "landscape": True,
        "note": "lying flat on her back on the grass, completely relaxed and "
                "still, eyes gently closed, face calm, arms resting a little "
                "away from her sides with the palms turned up, legs long and "
                "loose with the feet falling open",
        "grounded": (),
        # Head to the left, feet to the right, body along the ground. The near
        # side of her sits a little lower in the frame than the far side --
        # that small offset is what makes it read as perspective rather than
        # as a figure lying in a flat plane.
        "joints": {
            "head": (0.150, 0.470), "shoulder_mid": (0.268, 0.505),
            "left_shoulder": (0.262, 0.452), "right_shoulder": (0.274, 0.556),
            "left_elbow": (0.352, 0.418), "right_elbow": (0.360, 0.606),
            "left_wrist": (0.436, 0.404), "right_wrist": (0.444, 0.634),
            "hip_mid": (0.560, 0.520), "left_hip": (0.552, 0.474),
            "right_hip": (0.568, 0.566),
            "left_knee": (0.716, 0.482), "right_knee": (0.724, 0.574),
            "left_ankle": (0.868, 0.492), "right_ankle": (0.874, 0.582),
        }},
    "child_pose": {
        "landscape": True,
        "note": "kneeling on the grass with her knees wide and her hips "
                "settled back onto her heels, folded all the way forward so "
                "her chest rests between her thighs and her forehead rests on "
                "the ground, both arms stretched out along the ground in front "
                "of her with the palms flat, back long and rounded",
        "grounded": (),
        # Hips high on the right over the heels, the spine sloping down to a
        # head resting on the ground at the left, arms reaching past it.
        "joints": {
            "left_wrist": (0.108, 0.646), "right_wrist": (0.118, 0.712),
            "left_elbow": (0.230, 0.626), "right_elbow": (0.240, 0.694),
            "head": (0.336, 0.664),
            "left_shoulder": (0.436, 0.560), "right_shoulder": (0.448, 0.640),
            "shoulder_mid": (0.442, 0.600),
            "hip_mid": (0.686, 0.436), "left_hip": (0.676, 0.398),
            "right_hip": (0.700, 0.476),
            "left_knee": (0.700, 0.612), "right_knee": (0.740, 0.688),
            "left_ankle": (0.846, 0.596), "right_ankle": (0.878, 0.672),
        }},
    "easy_seat": {
        "landscape": True,
        "note": "sitting upright and cross-legged on the grass, facing the "
                "viewer, spine tall and shoulders relaxed, both hands resting "
                "on her knees, calm and settled",
        "grounded": (),
        # Seen from the front and slightly above, sitting on the ground: the
        # crossed shins are wide and low, the torso upright above them.
        "joints": {
            "head": (0.500, 0.180), "shoulder_mid": (0.500, 0.348),
            "left_shoulder": (0.436, 0.352), "right_shoulder": (0.564, 0.352),
            "left_elbow": (0.400, 0.500), "right_elbow": (0.600, 0.500),
            "left_wrist": (0.392, 0.640), "right_wrist": (0.608, 0.640),
            "hip_mid": (0.500, 0.640), "left_hip": (0.456, 0.642),
            "right_hip": (0.544, 0.642),
            "left_knee": (0.352, 0.694), "right_knee": (0.648, 0.694),
            "left_ankle": (0.520, 0.744), "right_ankle": (0.480, 0.744),
        }},
    "warrior_three_left": {
        "note": "STANDING and balancing on her straight left leg with the "
                "right leg lifted straight out behind her, her whole body and "
                "the lifted leg level like a letter T, arms reaching forward "
                "past her head, seen from the front so the lifted leg is "
                "foreshortened behind her",
        "grounded": ("left",),
        "joints": {
            # Foreshortened hard: the torso tips toward the lens, so it is
            # short, and the lifted leg is shorter still.
            "left_wrist": (0.318, 0.196), "right_wrist": (0.682, 0.196),
            "left_elbow": (0.372, 0.268), "right_elbow": (0.628, 0.268),
            "head": (0.500, 0.246),
            "left_shoulder": (0.424, 0.330), "right_shoulder": (0.576, 0.330),
            "shoulder_mid": (0.500, 0.330),
            "hip_mid": (0.500, 0.512), "left_hip": (0.446, 0.512),
            "right_hip": (0.554, 0.512),
            "left_knee": (0.452, 0.716), "left_ankle": (0.458, 0.930),
            "right_knee": (0.606, 0.470), "right_ankle": (0.664, 0.430),
        }},
}

FLOOR_PROMPT = (
    "The FIRST image is the yoga coach character sheet. The SECOND image is a "
    "stick-figure diagram of a body pose. Draw the SAME coach -- same face, "
    "same pale silver-lavender side ponytail, same pointed ears, same green "
    "eyes, same lavender-grey sports-bra top, same charcoal-grey leggings, "
    "same bare feet, same proportions, same anime art style -- in EXACTLY the "
    "pose of the stick-figure diagram. "
    "This is a FLOOR pose and she is ON THE GROUND, not standing. Draw her the "
    "way you would SEE her from a few paces away and a little above, standing "
    "near her mat and looking at her: her body lies ALONG the ground and runs "
    "ACROSS the picture, in natural perspective, with the parts of her nearer "
    "the viewer drawn slightly larger than the parts further away. "
    "Make it unmistakable that she is resting on the ground: her weight "
    "settles and flattens where she touches it, her hair falls and spreads "
    "onto the ground rather than hanging in the air, and her limbs rest rather "
    "than hold themselves up. "
    "DO NOT MIRROR OR FLIP THE DIAGRAM: your drawing laid on top of the "
    "diagram must line up joint for joint, so whatever the diagram puts on the "
    "LEFT of the picture you must also put on the LEFT of the picture -- the "
    "red dots stay on the left, the blue dots stay on the right. "
    "Draw her WHOLE body, every part the diagram shows: both arms to the "
    "hands, both legs to the bare feet, head and face, nothing cut off, "
    "nothing cropped by the edge of the picture. Fill the frame -- she should "
    "be large and clear enough for somebody to copy her from across a room. "
    "One figure only, transparent background, no ground, no grass, no shadow, "
    "no mat, no props, no other people, no text. The diagram is a wireframe "
    "reference only: never draw its dots, lines or markers onto her body or "
    "clothing. Clean flat anime cel shading with soft edges, full colour, no "
    "outline box, no border. The pose is: ")


def guide_card(pose_id: str) -> Image.Image:
    """The hand-authored skeleton, drawn exactly like the rig ones."""
    layout = LAYOUTS[pose_id]
    wide = layout.get("landscape")
    width, height = (FLOOR_W, FLOOR_H) if wide else (CARD_W, CARD_H)
    pts = {name: (x * width, y * height)
           for name, (x, y) in layout["joints"].items()}
    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)

    draw.polygon([pts["left_shoulder"], pts["right_shoulder"],
                  pts["right_hip"], pts["left_hip"]], fill=(40, 40, 40))
    for a, b, width, ink in BONES:
        if a in pts and b in pts:
            draw.line([pts[a], pts[b]], fill=ink, width=width, joint="curve")
    draw.line([pts["hip_mid"], pts["shoulder_mid"]], fill=(20, 20, 20), width=30)
    draw.line([pts["shoulder_mid"], pts["head"]], fill=(20, 20, 20), width=20)

    hx, hy = pts["head"]
    radius = 52 if wide else 62
    draw.ellipse([hx - radius, hy - radius, hx + radius, hy + radius],
                 fill=(20, 20, 20))
    for name in ("left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
                 "left_wrist", "right_wrist", "left_hip", "right_hip",
                 "left_knee", "right_knee", "left_ankle", "right_ankle"):
        if name not in pts:
            continue
        x, y = pts[name]
        colour = (208, 60, 60) if name.startswith("left") else (60, 90, 208)
        draw.ellipse([x - 11, y - 11, x + 11, y + 11], fill=colour)
    for side in layout["grounded"]:
        x, y = pts[side + "_ankle"]
        draw.line([(x - 46, y), (x + 46, y)], fill=(20, 20, 20), width=18)
    return card


# -- placement -------------------------------------------------------
#
# `build_yoga_coach.placed()` anchors the **lowest ankle** to `FLOOR_Y`, which
# is exactly right for a body standing on grass and exactly wrong for one lying
# on it: a supine figure would be hung from an ankle and described as standing
# on it. Guided poses get their own rule.
#
#   horizontally  centred on COACH_X, which is also what keeps the whole-stage
#                 clip mirror correct (see mirroring.md)
#   vertically    centred on GROUND_Y -- she is *on* the ground rather than
#                 standing on a line, so the figure sits in the grass rather
#                 than balancing on its lowest pixel
#   size          scaled so her longest dimension is FLOOR_SPAN, which keeps a
#                 lying body from being wider than the stage

SERVED = (pathlib.Path(__file__).resolve().parents[1] / "aipi5" / "ui" / "web"
          / "assets" / "yoga" / "coach")
COACH_X, FLOOR_Y = 640, 726
GROUND_Y = 470
#: A body on the ground is wide and low, so it is fitted to a box rather than
#: to a single span: wide enough to be followed from across a room, short
#: enough to leave the guided-mode panel and the hold clock somewhere to live.
FLOOR_MAX_W, FLOOR_MAX_H = 840, 560


def pack_floor(pose_ids: list[str], source: pathlib.Path) -> dict:
    import json
    manifest_path = SERVED / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for pose_id in pose_ids:
        png = source / f"{pose_id}.png"
        if not png.exists():
            print(f"  no artwork for {pose_id}")
            continue
        image = Image.open(png).convert("RGBA")
        # The model returns a film of alpha 1-8 over the whole frame, so a
        # bounding box has to ignore it -- the same trap `build_yoga_coach`
        # documents. Zero anything at or below 8 before measuring.
        alpha = image.split()[3].point(lambda v: 0 if v <= 8 else v)
        box = alpha.getbbox() or (0, 0, image.width, image.height)
        image = image.crop(box)
        scale = min(FLOOR_MAX_W / image.width, FLOOR_MAX_H / image.height)
        size = (max(1, round(image.width * scale)),
                max(1, round(image.height * scale)))
        image = image.resize(size, Image.LANCZOS)
        out = SERVED / f"{pose_id}.webp"
        image.save(out, "WEBP", quality=88, method=4)
        manifest["poses"][pose_id] = {
            "w": size[0], "h": size[1],
            "hip_x": size[0] / 2.0,
            "floor_y": FLOOR_Y - GROUND_Y + size[1] / 2.0,
            "placement": "floor",
        }
        print(f"  {pose_id:20} {size[0]}x{size[1]}  {out.stat().st_size/1024:6.1f} kB")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def imagegen(args: list[str]) -> bool:
    script = (pathlib.Path(__file__).resolve().parents[1] / ".claude" /
              "skills" / "image-gen" / "scripts" / "imagegen.py")
    if not script.exists():
        script = (pathlib.Path(__file__).resolve().parents[1] / ".agents" /
                  "skills" / "image-gen" / "scripts" / "imagegen.py")
    result = subprocess.run([sys.executable, str(script)] + args)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("guide", "art", "pack"))
    parser.add_argument("--poses", nargs="+", default=sorted(LAYOUTS))
    parser.add_argument("--reference", default=str(REFERENCE_DEFAULT))
    parser.add_argument("--model", default="gpt-image-1.5")
    parser.add_argument("--quality", default="high")
    parser.add_argument("--out", default=str(POSES_OUT))
    args = parser.parse_args()

    GUIDES.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.command == "pack":
        pack_floor(args.poses, pathlib.Path(args.out))
        return 0

    if args.command == "guide":
        for pose_id in args.poses:
            path = GUIDES / f"{pose_id}.png"
            guide_card(pose_id).save(path)
            print(f"  {pose_id:20} {path}")
        return 0

    failed = []
    for pose_id in args.poses:
        guide = GUIDES / f"{pose_id}.png"
        if not guide.exists():
            print(f"  no guide for {pose_id} - run `guide` first")
            failed.append(pose_id)
            continue
        target = out / f"{pose_id}.png"
        print(f"\n-- {pose_id} --")
        ok = imagegen([
            "edit",
            # The character sheet goes first on every call. This is the whole
            # of the identity mechanism and it is not optional.
            "--image", args.reference,
            "--image", str(guide),
            "--prompt", FLOOR_PROMPT + LAYOUTS[pose_id]["note"] + ".",
            # A body on the ground is wider than it is tall, so the floor
            # poses are generated landscape. Asking for a portrait frame is
            # what made the first attempt draw her upright to fill it.
            "--size", ("1536x1024" if LAYOUTS[pose_id].get("landscape")
                       else "1024x1536"),
            "--transparent", "--trim",
            "--quality", args.quality, "--model", args.model,
            "--out", str(target), "--timeout", "900"])
        if not ok:
            failed.append(pose_id)
    if failed:
        print("\nFAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
