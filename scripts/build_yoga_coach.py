"""Build the Yoga Coach's demonstration artwork from the rig that scores it.

She faces the player and demonstrates mirrored, which is what a teacher in
front of a class does; see `POSE_PROMPT` for why that costs the scorer nothing.

The coach used to be *drawn* by `yoga.js` from the same fourteen bone angles the
player is marked against, which made "what is demonstrated" and "what is scored"
the same number by construction. Replacing her with pictures gives that up
unless the pictures are made **from those angles**, so this script does exactly
that in two steps:

1. `guide` renders each pose's skeleton with `rig.forward_kinematics` -- the
   scorer's own function, not a second copy of the shapes -- onto a white card:
   thick bones, joint dots, a head, and the floor she stands on.
2. `art` sends that card to the image model together with the coach reference
   sheet, and asks for the same character redrawn in exactly that skeleton's
   pose. The reference keeps her one person across forty pictures; the skeleton
   keeps her honest.

Mirrored poses are **not** generated. `left` and `right` are the same shape seen
from behind, so the right-side image is the left-side one flipped, which halves
the batch and guarantees the pair actually match.

`pack` is the last step: trim, scale to a common spine height, and write the
WebP the page loads plus the manifest that tells it where her hips are.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
import sys
import time

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aipi5.games.yoga import rig
from aipi5.games.yoga.poses import AUTHORED, POSES

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTWORK = ROOT / "artwork" / "yoga"
SERVED = ROOT / "aipi5" / "ui" / "web" / "assets" / "yoga" / "coach"
IMAGEGEN = ROOT / ".claude" / "skills" / "image-gen" / "scripts" / "imagegen.py"

#: The guide card. Portrait, because a standing body is, and 1024x1536 is one
#: of the three sizes the image model accepts -- anything else is resampled by
#: the API and the skeleton loses its edges.
CARD_W, CARD_H = 1024, 1536

#: Pixels per spine length, and where the lowest foot lands. Sized so the
#: widest pose in the library (Warrior II's arms, Half Moon's lifted leg) keeps
#: a margin inside the card.
UNIT = 300.0
FLOOR_Y = 1330.0
CENTRE_X = CARD_W / 2


#: How far into a pose each transition frame is.
#:
#: **A class must not jump between shapes**, and the artwork for a jump-free
#: class cannot be one picture per *pair* of poses: the three lessons contain
#: 74 consecutive pairs and would need hundreds. So each pose gets a frame of
#: its own halfway between standing and the pose, and any transition is
#: assembled from them: out of the pose you are in, back through standing, into
#: the next one.
#:
#: **One frame per pose, not three.** It was 25/50/75 first. Three frames of a
#: pose are three separate visits to the image model, and the model does not
#: draw the same face twice — the drift between them was visible precisely
#: because they play back to back. One frame is one thing to keep consistent,
#: and the movement still reads: out of A, through standing, into B.
STEPS: tuple[int, ...] = (50,)

#: The one shape that needs no frames of its own: standing *is* where every
#: transition frame is measured from, so Mountain Pose at 25% of the way into
#: Mountain Pose is Mountain Pose.
NO_STEPS: frozenset[str] = frozenset({"mountain"})


def step_id(pose_id: str, percent: int) -> str:
    return pose_id + "__" + str(percent)


def _wrap(degrees: float) -> float:
    value = (degrees + 180.0) % 360.0
    if value < 0:
        value += 360.0
    return value - 180.0


def step_table(pose, percent: int) -> tuple[dict, dict]:
    """The bones and scales `percent` of the way from standing into `pose`.

    The same interpolation `yoga.js` tweens with -- shortest arc per bone --
    so a frame drawn here is a frame on the path the coach actually takes.
    """
    t = percent / 100.0
    table = dict(rig.STANDING)
    for name, value in pose.table.items():
        start = rig.STANDING.get(name, value)
        table[name] = start + _wrap(value - start) * t
    scales = {name: 1.0 + (value - 1.0) * t
              for name, value in (pose.scales or {}).items()}
    return table, scales


class StepPose:
    """One transition frame, shaped like a `Pose` for everything downstream."""

    def __init__(self, pose, percent: int):
        self.of = pose
        self.percent = percent
        self.id = step_id(pose.id, percent)
        self.name = pose.name + " (" + str(percent) + "% of the way in)"
        self.sanskrit = pose.sanskrit
        self.instruction = pose.instruction
        self.cue = pose.cue
        self.side = pose.side
        self.table, self.scales = step_table(pose, percent)


def mirror_source(pose_id: str) -> str | None:
    """The shape this one is a flip of, or None if it must be drawn itself."""
    base, _, step = pose_id.partition("__")
    if not base.endswith("_right"):
        return None
    twin = base[: -len("_right")] + "_left"
    return step_id(twin, int(step)) if step else twin


def sources() -> list:
    """Every shape that needs its own picture.

    One per pose rather than per side -- the right-hand version is the left one
    flipped -- plus that pose's three transition frames.
    """
    out = []
    for pose in POSES.values():
        # Only the poses this pipeline actually drew. The 3D coach shows the
        # other fifty-nine, and asking this script about them would report
        # missing pictures nobody is going to draw.
        if pose.id not in AUTHORED:
            continue
        if mirror_source(pose.id) is not None:
            continue
        out.append(pose)
        if pose.id in NO_STEPS:
            continue
        out.extend(StepPose(pose, percent) for percent in STEPS)
    return out


# -- the guide card ---------------------------------------------------

#: Arms are drawn lighter than legs. Not decoration: the first Standing
#: Forward Fold came back with no legs at all, because a body whose head is
#: below its hips puts four similar sticks in the same place and the model
#: drew the arms twice. Two tones and the limbs stop being interchangeable.
ARM_INK = (105, 105, 112)
LEG_INK = (24, 24, 30)

BONES = (
    ("left_shoulder", "left_elbow", 26, ARM_INK),
    ("left_elbow", "left_wrist", 22, ARM_INK),
    ("right_shoulder", "right_elbow", 26, ARM_INK),
    ("right_elbow", "right_wrist", 22, ARM_INK),
    ("left_hip", "left_knee", 34, LEG_INK),
    ("left_knee", "left_ankle", 28, LEG_INK),
    ("right_hip", "right_knee", 34, LEG_INK),
    ("right_knee", "right_ankle", 28, LEG_INK),
)


def placed(pose) -> dict[str, tuple[float, float]]:
    """The pose's joints in card pixels, lowest foot on the floor line.

    The same anchor rule the drawn coach used -- the *lowest ankle*, not the
    hips -- because widening a stance lowers the hips without moving the feet,
    and a guide whose feet float tells the model the coach is jumping.
    """
    joints = rig.forward_kinematics(pose.table, pose.scales)
    pts = {name: (CENTRE_X + x * UNIT, y * UNIT) for name, (x, y) in joints.items()}
    lift = FLOOR_Y - max(pts["left_ankle"][1], pts["right_ankle"][1])
    return {name: (x, y + lift) for name, (x, y) in pts.items()}


#: Above this, in spine lengths, a foot is genuinely off the floor.
#:
#: A frontal 2D rig raises the trailing ankle whenever the body leans -- Triangle
#: lifts it 0.46 and Warrior II 0.13 -- and none of those are poses anybody
#: stands on one leg for. Only Tree, Half Moon and Extended Hand to Toe clear
#: this, which is exactly the set that should. Below it the foot is drawn with a
#: sole on the floor line and described as planted, so the picture does not
#: teach a player to hover.
LIFTED = 0.6


def lifted_feet(pose) -> list[str]:
    """Which of her feet are really off the floor in this pose."""
    pts = placed(pose)
    lowest = max(pts["left_ankle"][1], pts["right_ankle"][1])
    return [side for side in ("left", "right")
            if (lowest - pts[side + "_ankle"][1]) / UNIT > LIFTED]


def guide_card(pose) -> Image.Image:
    """One pose as a skeleton on white, standing on a floor line."""
    pts = placed(pose)
    lifted = lifted_feet(pose)
    card = Image.new("RGB", (CARD_W, CARD_H), "white")
    draw = ImageDraw.Draw(card)
    draw.line([(60, FLOOR_Y), (CARD_W - 60, FLOOR_Y)], fill=(150, 150, 150), width=6)

    # Torso first, as a filled quad, so the model reads a body rather than four
    # loose sticks meeting at a point.
    draw.polygon([pts["left_shoulder"], pts["right_shoulder"],
                  pts["right_hip"], pts["left_hip"]], fill=(40, 40, 40))
    for a, b, width, ink in BONES:
        draw.line([pts[a], pts[b]], fill=ink, width=width, joint="curve")
    draw.line([pts["hip_mid"], pts["shoulder_mid"]], fill=(20, 20, 20), width=30)
    draw.line([pts["shoulder_mid"], pts["head"]], fill=(20, 20, 20), width=20)

    hx, hy = pts["head"]
    draw.ellipse([hx - 62, hy - 62, hx + 62, hy + 62], fill=(20, 20, 20))
    for name in ("left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
                 "left_wrist", "right_wrist", "left_hip", "right_hip",
                 "left_knee", "right_knee", "left_ankle", "right_ankle"):
        x, y = pts[name]
        # The left limbs are marked, because the one thing that cannot be
        # inferred from a symmetrical stick figure is which arm is which.
        colour = (208, 60, 60) if name.startswith("left") else (60, 90, 208)
        # Small. The first batch drew these onto the coach's arms as literal
        # painted spots, and a marker only has to be visible to be followed.
        draw.ellipse([x - 11, y - 11, x + 11, y + 11], fill=colour)

    # A sole under every foot that is on the ground. The rig lifts an ankle
    # whenever the body leans, and a diagram showing a foot 40 px in the air is
    # a diagram the model draws as a foot 40 px in the air.
    for side in ("left", "right"):
        if side in lifted:
            continue
        x = pts[side + "_ankle"][0]
        draw.line([(x - 46, FLOOR_Y), (x + 46, FLOOR_Y)],
                  fill=(20, 20, 20), width=18)
    return card


def write_guides(out: pathlib.Path) -> list[pathlib.Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for pose in sources():
        path = out / (pose.id + ".png")
        guide_card(pose).save(path)
        written.append(path)
    return written


# -- the pictures -----------------------------------------------------

STYLE = ("Soft flat vector illustration, clean edges, gentle cel shading, warm "
         "friendly colours, no text, no watermark, no signature.")

#: What the coach is asked for, once per pose.
#:
#: **She faces the player**, which makes her a mirror rather than a body double
#: and is the same thing a teacher standing in front of a class is: when the
#: diagram puts the working limb on the left of the picture, the coach raises
#: the arm that *appears* on the left of the picture, which is her own right.
#: That is exactly what the player has to copy, because the pose stream is
#: mirrored too -- the player's left hand is also at the smaller x. So the rule
#: given to the model is about the picture and never about anatomy: overlay the
#: diagram, do not flip it. Nothing in `rig.py` or `scoring.py` changes.
POSE_PROMPT = (
    "The FIRST image is the yoga coach character sheet. The SECOND image is a "
    "stick-figure diagram of a body pose. Draw the SAME coach -- same face, "
    "same pale silver-lavender side ponytail, same pointed ears, same green "
    "eyes, same lavender-grey sports-bra top, same charcoal-grey leggings, "
    "same bare feet, same proportions, same anime art style -- in EXACTLY the "
    "pose of the stick-figure diagram, FACING THE VIEWER with her face visible "
    "and calm, the way a teacher faces a class and mirrors the pose for them. "
    # Every fault worth a re-roll in the pilot was one of these two, and both
    # are failures of *layout*, not of drawing: a flipped pose teaches the
    # wrong side, and a body missing its feet cannot be stood on grass.
    "DO NOT MIRROR OR FLIP THE DIAGRAM: your drawing laid on top of the "
    "diagram must line up joint for joint, so whatever the diagram puts on the "
    "LEFT of the picture you must also put on the LEFT of the picture -- the "
    "red dots stay on the left, the blue dots stay on the right. "
    "Draw her WHOLE body, every part the diagram shows: both arms down to the "
    "hands, both legs down to the bare feet, head and face, nothing cut off, "
    "nothing left out, nothing cropped by the edge of the picture. A short "
    "black bar across a foot in the diagram means that foot is flat on the "
    "ground. One figure only, centred, transparent background, no floor, no "
    "shadow, no mat, no props, no other people. "
    "The diagram is a wireframe reference only: never draw its dots, bars, "
    "grey lines or colours onto the coach herself. " + STYLE)


#: The same instructions, for the case where the pose's own drawing is passed
#: in between the character sheet and the diagram.
POSE_PROMPT_CHAINED = POSE_PROMPT.replace(
    "The FIRST image is the yoga coach character sheet. The SECOND image is a "
    "stick-figure diagram of a body pose.",
    "The FIRST image is the yoga coach character sheet. The SECOND image is "
    "the SAME coach in the finished pose, and your drawing will be shown "
    "immediately before it. Take from the second image ONLY who she is — her "
    "face, her hair, her braid, her top, her leggings, her proportions, her "
    "size — and take the POSE ONLY from the stick-figure diagram. She is part "
    "way into that pose and has NOT arrived in it: if the diagram has her arms "
    "half raised and her stance half as wide as the second image, draw them "
    "half raised and half as wide. Copying the second image's pose would make "
    "the transition frame useless. The THIRD image is that stick-figure "
    "diagram.")


def describe(pose) -> str:
    """The sentence that says which shape this is.

    A transition frame is named as one -- "part way into Warrior II, still
    moving" -- because the alternative is asking for a pose that does not exist
    and getting the model's guess at what it should have been.
    """
    of = getattr(pose, "of", None)
    if of is None:
        return (" The pose is " + pose.name + " (" + pose.sanskrit + "): "
                + pose.instruction + " " + pose.cue)
    return (" This is a MID-MOVEMENT frame, " + str(pose.percent) + "% of the "
            "way from standing upright into " + of.name + " ("
            + of.sanskrit + "). She is still moving into it, not holding it: "
            "her body is exactly where the diagram puts it and no further. "
            "The pose she is moving into is described as: " + of.instruction
            + " " + of.cue)


def imagegen(args: list[str], attempts: int = 3) -> bool:
    """One picture, retried. Returns whether it was written.

    A batch is twenty-odd calls over half an hour and the far end rate-limits,
    so a run that aborts on the first refusal is a run that has to be started
    again from the beginning. Failures are collected and reported at the end
    instead: the missing poses can be asked for by name afterwards.
    """
    for attempt in range(1, attempts + 1):
        result = subprocess.run([sys.executable, str(IMAGEGEN), *args],
                                cwd=ROOT, text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        if result.returncode == 0:
            return True
        sys.stderr.write(result.stderr)
        if attempt < attempts:
            wait = 20 * attempt
            print("   retrying in " + str(wait) + "s")
            time.sleep(wait)
    return False


#: The transparency model. `gpt-image-2` is the newer and better draughtsman
#: and refuses `--transparent` outright ("Transparent background is not
#: supported for this model"), which a coach who has to stand in a forest
#: cannot do without. 1.5 takes the alpha and matches the reference well.
MODEL = "gpt-image-1.5"


def by_id() -> dict:
    """Every drawable shape by id, poses and transition frames alike."""
    return {shape.id: shape for shape in sources()}


def make_art(pose_ids: list[str], reference: pathlib.Path,
             guides: pathlib.Path, out: pathlib.Path, draft: bool,
             quality: str, model: str = MODEL, skip_existing: bool = True
             ) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    shapes = by_id()
    failed: list[str] = []
    for pose_id in pose_ids:
        pose = shapes[pose_id]
        if skip_existing and (out / (pose_id + ".png")).exists():
            print("-- " + pose_id + ": already drawn")
            continue
        target = out / (pose_id + ".png")
        print("-- " + pose_id + ": " + pose.name)
        lifted = lifted_feet(pose)
        footing = (" Both of her feet are flat on the floor at the same level."
                   if not lifted else
                   " Her " + " and ".join(lifted) + " foot is lifted right off "
                   "the floor and must not touch it; her other foot carries all "
                   "her weight.")
        guide = guides / (pose_id + ".png")
        if not guide.exists():
            # Not retried: this is a local file that is never going to appear
            # on its own, and three attempts with the backoff between them cost
            # nine minutes of a batch before anybody noticed.
            print("   no guide for " + pose_id + " - run `guide` first")
            failed.append(pose_id)
            continue
        # A transition frame is drawn from the *finished pose's own drawing*
        # as well as the character sheet. She has to look like the same person
        # in the frame immediately before and after it, and the frame before
        # and after it is that drawing — matching the sheet is not enough when
        # the two play half a second apart.
        family = out / (getattr(pose, "of", pose).id + ".png")
        chained = ([str(family)] if getattr(pose, "of", None) and family.exists()
                   else [])
        args = ["edit", "--image", str(reference)]
        for extra in chained:
            args += ["--image", extra]
        args += ["--image", str(guide),
                # The pose's own teaching words go in as well. They are what
                # the player is told to do, so a picture drawn from them is a
                # picture of the same pose the sentence describes.
                "--prompt", (POSE_PROMPT_CHAINED if chained else POSE_PROMPT)
                + describe(pose) + footing,
                "--size", "1024x1536", "--transparent", "--trim",
                "--quality", quality, "--model", model,
                "--out", str(target), "--timeout", "900"]
        if draft:
            args.append("--draft")
        if not imagegen(args):
            failed.append(pose_id)
    if failed:
        print("FAILED: " + ",".join(failed))
    return failed


# -- what the page loads ----------------------------------------------

#: How tall the coach is drawn on the 1280x800 canvas, in pixels from the top
#: of her head to her lowest foot when she is standing. Every pose is scaled by
#: the same number of pixels-per-spine rather than to a common image height, so
#: a forward fold is genuinely shorter than Mountain Pose instead of being
#: inflated to fill its own picture.
SERVED_UNIT = 160.0


def guide_ink(pose) -> tuple[float, float, float, float]:
    """The box the guide's skeleton occupies, in card pixels.

    Not the card, the *ink*: the same thing a trimmed drawing of the pose
    occupies, so the two can be compared. Raised hands count, because they are
    ink in the guide and they are ink in the picture.
    """
    pts = placed(pose)
    xs, ys = [], []
    for name, (x, y) in pts.items():
        pad = 62 if name == "head" else 17
        xs += [x - pad, x + pad]
        ys += [y - pad, y + pad]
    # Whatever is standing on it, the floor is the bottom of the drawing.
    return min(xs), min(ys), max(xs), max(FLOOR_Y, max(ys))


#: Alpha at or below this is nothing, and is erased before anything is measured.
#:
#: **The model returns a film of alpha 1-8 over the entire frame.** It is
#: invisible, `--trim` crops only *fully* transparent margins, and so every
#: drawing trimmed to exactly the frame it was generated in -- which made all
#: twenty-one of them the same size and the manifest a description of the
#: canvas rather than of the coach. Everything downstream is measured off the
#: cleaned picture, because a bounding box is only as honest as its alpha.
ALPHA_FLOOR = 8


def clean(image: Image.Image) -> Image.Image:
    """The drawing with its invisible film erased, cropped to what is left."""
    image = image.convert("RGBA")
    alpha = image.split()[3].point(lambda v: 0 if v <= ALPHA_FLOOR else v)
    image.putalpha(alpha)
    box = alpha.getbbox()
    return image.crop(box) if box else image


#: How far apart her eyes are, in packed pixels, in every drawing.
#:
#: **The scale of a drawing is measured off her face, not off its bounding
#: box.** Normalising the ink height to the guide's assumes the model drew the
#: pose with the rig's proportions, and it does not: a Standing Forward Fold
#: drawn with the head nearer the floor is *taller* in ink than the rig's
#: flattened one, so height-matching shrank the whole figure — she was 40%
#: smaller in a fold than standing, which is the "her body size is not
#: consistent" that this was reported as. Eye separation is a rigid measure of
#: the head, it is visible in every pose including upside-down ones, and it is
#: what an eye actually judges "same person, same size" by. 26 px is the median
#: of what the old rule produced, so nothing changes size on average.
EYE_SPAN = 26.0

#: The eyes are the only saturated green on her — the palette is silver hair,
#: warm skin, lavender-grey top and charcoal leggings.
def eye_span(image: Image.Image) -> float | None:
    """How far apart her eyes are in this drawing, or None if not found.

    The mask is built with channel arithmetic and lookup tables, which happen
    in C, and only the *cropped green region* — a few hundred pixels around
    her face — is then walked in Python. A per-pixel loop over the whole
    picture took the pack from seconds to a quarter of an hour, and there is
    no NumPy on the machine this runs on.
    """
    red, green, blue, alpha = image.split()
    over = lambda channel, limit: channel.point(
        lambda v, limit=limit: 255 if v > limit else 0)
    mask = ImageChops.multiply(
        ImageChops.multiply(over(ImageChops.subtract(green, red), 15),
                            over(ImageChops.subtract(green, blue), 15)),
        ImageChops.multiply(over(alpha, 200), over(green, 80)))

    box = mask.getbbox()
    if box is None:
        return None
    # A green region as tall as the body is not a pair of eyes.
    if (box[3] - box[1]) > image.height * 0.35:
        return None
    patch = mask.crop(box)
    width, height = patch.size
    pixels = patch.load()

    points = {(x, y) for y in range(height) for x in range(width)
              if pixels[x, y]}
    if len(points) < 16:
        return None
    blobs: list[tuple[int, float, float]] = []
    while points:
        stack = [points.pop()]
        cluster = [stack[0]]
        while stack:
            cx, cy = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (cx + dx, cy + dy)
                    if neighbour in points:
                        points.discard(neighbour)
                        stack.append(neighbour)
                        cluster.append(neighbour)
        if len(cluster) >= 8:
            blobs.append((len(cluster),
                          sum(p[0] for p in cluster) / len(cluster),
                          sum(p[1] for p in cluster) / len(cluster)))
    if len(blobs) < 2:
        return None
    blobs.sort(reverse=True)
    (_, x1, y1), (_, x2, y2) = blobs[0], blobs[1]
    return math.hypot(x1 - x2, y1 - y2)


def pack(source: pathlib.Path, out: pathlib.Path) -> dict:
    """Trim, scale and write the WebP the page loads, plus its manifest.

    **The picture is measured, not assumed.** The model is asked to match the
    guide's pose and it does, but it draws her wherever it likes inside the
    frame and at whatever size suits it -- the first packed batch came back
    with twenty-one figures at twenty-one different scales, all of which the
    manifest cheerfully described as identical. So each drawing is compared to
    its own guide: the guide's ink box says how many spine lengths tall this
    pose is, the drawing's ink box says how many pixels that came to, and the
    ratio is what makes every pose land at `SERVED_UNIT` pixels per spine.

    The two anchors written out are where her hips are across the picture and
    where the floor is down it. A trimmed drawing is only as big as its ink, so
    Standing Forward Fold and Tree Pose have their hips at completely different
    fractions of their own heights; stacking them by their centres makes the
    coach hop every time the pose changes.
    """
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {"unit": SERVED_UNIT, "poses": {}}
    for pose in sources():
        path = source / (pose.id + ".png")
        if not path.exists():
            continue
        image = clean(Image.open(path))

        span = eye_span(image)
        if span:
            scale = EYE_SPAN / span
        else:
            # No face found — a turned head or closed eyes. The old rule, which
            # is right on average and wrong per pose.
            gx0, gy0, gx1, gy1 = guide_ink(pose)
            scale = ((gy1 - gy0) * (SERVED_UNIT / UNIT)) / image.height
        size = (max(1, round(image.width * scale)),
                max(1, round(image.height * scale)))
        image = image.resize(size, Image.LANCZOS)
        image.save(out / (pose.id + ".webp"), "WEBP", quality=88, method=6)

        pts = placed(pose)
        gx0, gy0, gx1, gy1 = guide_ink(pose)
        hip_fraction = (pts["hip_mid"][0] - gx0) / max(1e-6, gx1 - gx0)
        manifest["poses"][pose.id] = {
            "w": size[0], "h": size[1],
            "hip_x": round(hip_fraction * size[0], 1),
            # Her lowest foot is the bottom of the ink, and the bottom of the
            # ink is the bottom of the trimmed picture.
            "floor_y": round(float(size[1]), 1),
        }
    # The right-hand side of every shape, poses and transition frames alike,
    # as the left one flipped.
    #
    # **`hip_x` is not mirrored with it.** The page flips the x axis about
    # `COACH_X` and then draws the picture at `-hip_x`, so the number it needs
    # is still the hip's distance from the *source* picture's left edge. This
    # was mirrored at first, which is invisible on a symmetric shape — Warrior
    # II's hips are dead centre — and puts Crescent Moon 190 px off to one side.
    for source_id in list(manifest["poses"]):
        base, _, step = source_id.partition("__")
        if not base.endswith("_left"):
            continue
        twin_base = base[: -len("_left")] + "_right"
        if twin_base not in POSES:
            continue
        twin_id = step_id(twin_base, int(step)) if step else twin_base
        manifest["poses"][twin_id] = dict(manifest["poses"][source_id],
                                          mirror_of=source_id)
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + chr(10),
        encoding="utf-8")
    return manifest


#: The canvas the page draws on, and so the size the field is packed to.
STAGE_W, STAGE_H = 1280, 800


def pack_field(source: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """The clearing, cut to the stage and written as WebP.

    Generated at 1536x1024 because those are the sizes the model offers, and
    the stage is 1280x800: fit the width and take the difference off the *top*,
    which is sky. Cropping it off the bottom would eat the grass the coach
    stands on and lift the horizon into her head.
    """
    image = Image.open(source).convert("RGB")
    scale = STAGE_W / image.width
    image = image.resize((STAGE_W, round(image.height * scale)), Image.LANCZOS)
    if image.height > STAGE_H:
        top = image.height - STAGE_H
        image = image.crop((0, top, STAGE_W, image.height))
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "WEBP", quality=86, method=6)
    return out


# -- reviewing what came back ------------------------------------------
#
# Eighty-one drawings is more than anybody looks at properly, and the three
# faults this pipeline actually produces are all measurable. Each check below
# was written after finding the fault by hand once.

def faults(image: Image.Image, pose) -> list[str]:
    """What looks wrong with one drawing, in one line each."""
    found: list[str] = []
    pixels = image.load()
    width, height = image.size

    # 1. The guide's floor bar or a sole, copied into the picture as a black
    #    slab under her foot. Found on Tree Pose, arms high.
    for y in range(max(0, height - 14), height):
        run = best = 0
        for x in range(width):
            r, g, b, a = pixels[x, y]
            run = run + 1 if (a > 150 and r < 70 and g < 70 and b < 70) else 0
            best = max(best, run)
        if best > 25:
            found.append(f"a dark bar {best}px wide near the bottom edge")
            break

    # There is no check here for the guide's joint markers being painted onto
    # her, and there was one: the marker red is (208, 60, 60) and the
    # anti-aliased edge of her own skin outline lands within thirty of it on
    # every drawing that has a body in it, so the check flagged all 81. Colour
    # alone cannot separate them, the prompt now forbids the markers, and a
    # test that fires on everything is worse than no test.

    # 3. A shape that is not the shape asked for. The guide says how wide this
    #    pose is relative to its height; a drawing half or double that is a
    #    different pose, whatever it is a drawing of.
    gx0, gy0, gx1, gy1 = guide_ink(pose)
    wanted = (gx1 - gx0) / (gy1 - gy0)
    got = width / height
    if wanted > 0 and not 0.55 <= got / wanted <= 1.9:
        found.append(f"shape is {got:.2f} wide/tall, the pose is {wanted:.2f}")
    return found


def review(source: pathlib.Path) -> dict:
    """Every drawing that has something measurably wrong with it."""
    out = {}
    for shape in sources():
        path = source / (shape.id + ".png")
        if not path.exists():
            out[shape.id] = ["not drawn"]
            continue
        problems = faults(clean(Image.open(path)), shape)
        if problems:
            out[shape.id] = problems
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("guide", help="render the skeleton guide cards")
    g.add_argument("--out", default=str(ARTWORK / "guides"))

    a = sub.add_parser("art", help="generate coach pictures from the guides")
    a.add_argument("--reference", required=True)
    a.add_argument("--guides", default=str(ARTWORK / "guides"))
    a.add_argument("--out", default=str(ARTWORK / "poses"))
    a.add_argument("--poses", default="", help="comma-separated ids; all if empty")
    a.add_argument("--draft", action="store_true")
    a.add_argument("--quality", default="high")
    a.add_argument("--redraw", action="store_true",
                   help="draw shapes that already have a file")
    a.add_argument("--model", default=MODEL)

    p = sub.add_parser("pack", help="scale the sources into served WebP")
    p.add_argument("--source", default=str(ARTWORK / "poses"))
    p.add_argument("--out", default=str(SERVED))

    r = sub.add_parser("review", help="flag drawings that came back wrong")
    r.add_argument("--source", default=str(ARTWORK / "poses"))

    f = sub.add_parser("field", help="cut the background to the stage")
    f.add_argument("--source", default=str(ARTWORK / "field-forest.png"))
    f.add_argument("--out", default=str(SERVED.parent / "field-forest.webp"))

    args = parser.parse_args()
    if args.command == "guide":
        written = write_guides(pathlib.Path(args.out))
        print(str(len(written)) + " guide cards in " + args.out)
        return 0
    if args.command == "art":
        ids = ([p.strip() for p in args.poses.split(",") if p.strip()]
               or [shape.id for shape in sources()])
        make_art(ids, pathlib.Path(args.reference), pathlib.Path(args.guides),
                 pathlib.Path(args.out), args.draft, args.quality,
                 args.model, skip_existing=not args.redraw)
        return 0
    if args.command == "review":
        problems = review(pathlib.Path(args.source))
        for pose_id, lines in sorted(problems.items()):
            print(pose_id + ": " + "; ".join(lines))
        print(str(len(problems)) + " of " + str(len(sources()))
              + " drawings want a second look")
        return 0
    if args.command == "field":
        written = pack_field(pathlib.Path(args.source), pathlib.Path(args.out))
        print("field written to " + str(written))
        return 0
    if args.command == "pack":
        manifest = pack(pathlib.Path(args.source), pathlib.Path(args.out))
        print(str(len(manifest["poses"])) + " poses packed into " + args.out)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
