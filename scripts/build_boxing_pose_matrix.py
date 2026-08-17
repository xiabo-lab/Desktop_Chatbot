#!/usr/bin/env python3
"""Build the approved full-body Boxing pose and damage-image matrix.

The 21 source sheets have already passed through the ImageGen chroma-key
helper.  Each half is kept on a fixed registered canvas so an extended arm
does not make the fighter's torso shrink.  Damage is then baked into every
pose image; the browser only selects a complete WebP and never paints a live
bruise overlay.
"""

from __future__ import annotations

import argparse
from itertools import product
import json
from pathlib import Path
import shutil

from PIL import Image

from build_boxing_damage_matrix import _damage


ROOT = Path(__file__).resolve().parents[1]
SHEETS = ROOT / "artwork" / "boxing-poses" / "transparent-sheets"
OUTPUT = ROOT / "aipi5" / "ui" / "web" / "assets" / "boxing" / "poses"

PLAYER_ZONES = ("left_shoulder", "right_shoulder")
OPPONENT_ZONES = (
    "left_eye", "right_eye", "left_shoulder", "right_shoulder")

# One paired sheet supplies one player pose and (except sheet 20) one opponent
# pose.  This is deliberately explicit: pose names are a stable runtime API.
SHEET_POSES = (
    ("00-idle.png", "idle", "idle"),
    ("01-high-guard.png", "high_guard", "high_guard"),
    ("02-left-block.png", "left_block", "body_guard"),
    ("03-right-block.png", "right_block", "head_hit_left"),
    ("04-left-straight-head.png", "left_straight_head", "left_jab_head"),
    ("05-left-straight-body.png", "left_straight_body", "left_jab_body"),
    ("06-right-straight-head.png", "right_straight_head", "right_cross_head"),
    ("07-right-straight-body.png", "right_straight_body", "right_cross_body"),
    ("08-left-hook-head.png", "left_hook_head", "left_hook_head"),
    ("09-left-hook-body.png", "left_hook_body", "left_hook_body"),
    ("10-right-hook-head.png", "right_hook_head", "right_hook_head"),
    ("11-right-hook-body.png", "right_hook_body", "right_hook_body"),
    ("12-dodge-left.png", "dodge_left", "dodge_left"),
    ("13-dodge-right.png", "dodge_right", "dodge_right"),
    ("14-duck.png", "duck", "duck"),
    ("15-lean-back.png", "lean_back", "lean_back"),
    ("16-left-parry.png", "left_parry", "head_hit_right"),
    ("17-right-parry.png", "right_parry", "parried"),
    ("18-head-hit.png", "head_hit", "body_hit"),
    ("19-body-hit.png", "body_hit", "knockout"),
    ("20-knockout.png", "knockout", None),
)

SIZES = {"player": (480, 640), "opponent": (360, 480)}


def _registered_half(sheet: Image.Image, kind: str) -> Image.Image:
    """Return one complete half-sheet on its fixed runtime canvas."""
    midpoint = sheet.width // 2
    crop = ((0, 0, midpoint, sheet.height) if kind == "player" else
            (midpoint, 0, sheet.width, sheet.height))
    return sheet.crop(crop).resize(SIZES[kind], Image.Resampling.LANCZOS)


def _dark_head_centre(image: Image.Image, kind: str) -> tuple[int, int]:
    """Locate the uppermost dense dark-hair cluster in a transparent sprite."""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    rows: list[tuple[int, int]] = []
    for y in range(height):
        count = 0
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a > 170 and r < 125 and g < 90 and b < 80:
                count += 1
        rows.append((y, count))

    # Ignore isolated ink pixels; hair produces several dense adjacent rows.
    start = next((y for y in range(max(1, height - 7))
                  if sum(rows[index][1] for index in range(y, y + 7)) >=
                  (80 if kind == "player" else 55)), int(height * .10))
    band_end = min(height, start + int(height * .16))

    candidates: list[tuple[int, int]] = []
    for y in range(start, band_end):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a > 170 and r < 125 and g < 90 and b < 80:
                candidates.append((x, y))
    if not candidates:
        return (width // 2, int(height * .16))

    # A glove or a distant outline can enter the same band.  Select the
    # densest horizontal window, then take the centre of that hair cluster.
    window = max(34, int(width * .16))
    histogram = [0] * width
    for x, _ in candidates:
        histogram[x] += 1
    running = sum(histogram[:window])
    best_x, best_value = 0, running
    for left in range(1, width - window + 1):
        running += histogram[left + window - 1] - histogram[left - 1]
        if running > best_value:
            best_x, best_value = left, running
    chosen = [(x, y) for x, y in candidates if best_x <= x < best_x + window]
    return (round(sum(x for x, _ in chosen) / len(chosen)),
            round(sum(y for _, y in chosen) / len(chosen)))


def _anchors(image: Image.Image, kind: str, pose: str) -> dict:
    """Derive stable anatomical damage anchors from the registered pose."""
    width, height = image.size
    hx, hy = _dark_head_centre(image, kind)
    if kind == "opponent":
        points = {
            # Front view: anatomical left is screen-right.
            "left_eye": (hx + round(width * .025), hy + round(height * .055),
                         round(width * .034), round(height * .018)),
            "right_eye": (hx - round(width * .025), hy + round(height * .055),
                          round(width * .034), round(height * .018)),
            "left_shoulder": (hx + round(width * .145), hy + round(height * .145),
                              round(width * .050), round(height * .030)),
            "right_shoulder": (hx - round(width * .145), hy + round(height * .145),
                               round(width * .050), round(height * .030)),
        }
        # The horizontal KO pose needs shoulder points along the fallen torso.
        if pose == "knockout":
            points["left_shoulder"] = (round(width * .68), round(height * .44),
                                       round(width * .050), round(height * .030))
            points["right_shoulder"] = (round(width * .58), round(height * .49),
                                        round(width * .050), round(height * .030))
    else:
        points = {
            "left_shoulder": (hx - round(width * .145), hy + round(height * .145),
                              round(width * .052), round(height * .030)),
            "right_shoulder": (hx + round(width * .145), hy + round(height * .145),
                               round(width * .052), round(height * .030)),
        }
        if pose == "knockout":
            points["left_shoulder"] = (round(width * .34), round(height * .42),
                                       round(width * .052), round(height * .030))
            points["right_shoulder"] = (round(width * .46), round(height * .47),
                                        round(width * .052), round(height * .030))
    return points


def _write_pose(kind: str, pose: str, base: Image.Image,
                points: dict, zones: tuple[str, ...]) -> int:
    base_dir = OUTPUT / "base" / kind
    matrix_dir = OUTPUT / "matrix" / kind / pose
    base_dir.mkdir(parents=True, exist_ok=True)
    matrix_dir.mkdir(parents=True, exist_ok=True)
    base.save(base_dir / f"{pose}.png", optimize=True)

    count = 0
    for levels in product(range(4), repeat=len(zones)):
        image = base.copy()
        for index, (zone, level) in enumerate(zip(zones, levels)):
            _damage(image, zone, level, points[zone],
                    seed=2501 + index * 317 + level * 53)
        image.putalpha(base.getchannel("A"))
        key = "".join(str(level) for level in levels)
        image.save(matrix_dir / f"{key}.webp", "WEBP", quality=88,
                   method=0, exact=True)
        count += 1
    return count


def build() -> dict:
    global OUTPUT
    missing = [name for name, _, _ in SHEET_POSES if not (SHEETS / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing transparent sheets: {', '.join(missing)}")

    resolved_output = OUTPUT.resolve()
    assets_root = (ROOT / "aipi5" / "ui" / "web" / "assets").resolve()
    resolved_output.relative_to(assets_root)
    staging = OUTPUT.with_name(f"{OUTPUT.name}-building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    final_output = OUTPUT
    OUTPUT = staging
    pose_points = {"player": {}, "opponent": {}}
    counts = {"player": 0, "opponent": 0}
    try:
        for filename, player_pose, opponent_pose in SHEET_POSES:
            sheet = Image.open(SHEETS / filename).convert("RGBA")
            player = _registered_half(sheet, "player")
            player_points = _anchors(player, "player", player_pose)
            pose_points["player"][player_pose] = {
                key: list(value) for key, value in player_points.items()}
            counts["player"] += _write_pose(
                "player", player_pose, player, player_points, PLAYER_ZONES)

            if opponent_pose:
                opponent = _registered_half(sheet, "opponent")
                opponent_points = _anchors(opponent, "opponent", opponent_pose)
                pose_points["opponent"][opponent_pose] = {
                    key: list(value) for key, value in opponent_points.items()}
                counts["opponent"] += _write_pose(
                    "opponent", opponent_pose, opponent,
                    opponent_points, OPPONENT_ZONES)

        player_poses = [player for _, player, _ in SHEET_POSES]
        opponent_poses = [opponent for _, _, opponent in SHEET_POSES if opponent]
        manifest = {
            "version": 2,
            "states_per_location": 4,
            "levels": {"0": "normal", "1": "first-punch",
                       "2": "second-punch", "3": "third-punch"},
            "player": {
                "key_order": list(PLAYER_ZONES),
                "hidden_locations": ["left_eye", "right_eye"],
                "poses": player_poses,
                "pose_count": len(player_poses),
                "count_per_pose": 4 ** len(PLAYER_ZONES),
                "count": counts["player"],
                "size": list(SIZES["player"]),
                "pattern": "matrix/player/{pose}/{key}.webp",
                "anchors": pose_points["player"],
            },
            "opponent": {
                "key_order": list(OPPONENT_ZONES),
                "poses": opponent_poses,
                "pose_count": len(opponent_poses),
                "count_per_pose": 4 ** len(OPPONENT_ZONES),
                "count": counts["opponent"],
                "size": list(SIZES["opponent"]),
                "pattern": "matrix/opponent/{pose}/{key}.webp",
                "anchors": pose_points["opponent"],
            },
            "clean_pose_bases": len(player_poses) + len(opponent_poses),
            "total": sum(counts.values()),
        }
        (OUTPUT / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    finally:
        OUTPUT = final_output

    if final_output.exists():
        shutil.rmtree(final_output)
    staging.rename(final_output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(build(), indent=2))


if __name__ == "__main__":
    main()
