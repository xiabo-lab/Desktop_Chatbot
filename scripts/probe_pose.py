#!/usr/bin/env python3
"""Prove Brio -> Hailo-10H -> 17 keypoints, and measure it. Section 7.

Run on the Pi, with the assistant stopped, because that service owns both the
camera and the accelerator:

    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user stop aipi5
    ~/AIPI5/.venv/bin/python scripts/probe_pose.py --frames 120
    systemctl --user start aipi5

This is a development tool and is not imported by anything. It exists because
section 7 makes proving the pipeline a gate in front of writing the game, and
because "the game does not work" is a much harder thing to debug than "the
accelerator produced no wrists".
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipi5.core import config as config_mod
from aipi5.motion import hailo_pose
from aipi5.motion.camera_lease import CameraLease, CameraLeaseError
from aipi5.motion.pose_types import KEYPOINT_NAMES, WRISTS
from aipi5.vision.camera import Camera


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--confidence", type=float, default=0.5,
                        help="keypoint confidence to count as 'seen'")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    settings = config_mod.load(args.config)

    print("── accelerator ─────────────────────────────────────────")
    identity = hailo_pose.identify()
    for key, value in identity.items():
        print(f"  {key:16} {value}")
    if not identity.get("available"):
        print("\nFAIL: no Hailo device. Nothing else here can be trusted.")
        return 1

    print("\n── camera ──────────────────────────────────────────────")
    camera = Camera(settings.camera)
    camera.open()
    lease = CameraLease(camera)
    try:
        lease.acquire()
    except CameraLeaseError as exc:
        print(f"FAIL: {exc}")
        return 1
    for key, value in lease.describe().items():
        print(f"  {key:16} {value}")

    print("\n── pose model ──────────────────────────────────────────")
    try:
        pose = hailo_pose.HailoPose(settings.motion.pose_model,
                                    score_threshold=settings.motion.person_confidence)
    except hailo_pose.PoseUnavailable as exc:
        print(f"FAIL: {exc}")
        lease.release()
        return 1
    for key, value in pose.describe().items():
        print(f"  {key:16} {value}")

    print(f"\n── running {args.frames} frames ─────────────────────────")
    inference_times: list[float] = []
    total_times: list[float] = []
    # Capture to decoded pose. The half of hand-to-screen latency that happens
    # on this side of the browser, and the number section 48 actually cares
    # about — it includes however long the frame sat waiting for the
    # accelerator, which the inference figure on its own hides.
    pipeline_times: list[float] = []
    seen = {name: 0 for name in KEYPOINT_NAMES}
    detected = 0
    started_all = time.monotonic()

    for index in range(args.frames):
        loop_started = time.monotonic()
        frame, captured_at = lease.frame()
        if frame is None:
            print(f"  frame {index}: no camera frame")
            continue

        result = pose.infer(frame, captured_at=captured_at)
        inference_times.append(result.inference_s)
        total_times.append(time.monotonic() - loop_started)
        pipeline_times.append(time.monotonic() - captured_at)

        if result.persons:
            detected += 1
            person = max(result.persons, key=lambda p: p.area)
            for name, point in person.keypoints.items():
                if point.confidence >= args.confidence:
                    seen[name] += 1
            if index % 20 == 0:
                left = person.point("left_wrist")
                right = person.point("right_wrist")
                print(f"  frame {index:3}: person {person.confidence:.2f}  "
                      f"L({left.x:.3f},{left.y:.3f})@{left.confidence:.2f}  "
                      f"R({right.x:.3f},{right.y:.3f})@{right.confidence:.2f}  "
                      f"{result.inference_s * 1000:.1f} ms")
        elif index % 20 == 0:
            print(f"  frame {index:3}: nobody in shot "
                  f"({result.inference_s * 1000:.1f} ms)")

    wall = time.monotonic() - started_all
    capture = lease.describe()
    lease.release()
    pose.close()
    camera.close()

    if not total_times:
        print("\nFAIL: not one frame completed.")
        return 1

    def report(label: str, values: list[float]) -> None:
        ordered = sorted(values)
        print(f"  {label:22} mean {statistics.mean(values) * 1000:6.1f} ms   "
              f"median {statistics.median(values) * 1000:6.1f} ms   "
              f"p95 {ordered[int(len(ordered) * 0.95) - 1] * 1000:6.1f} ms")

    print("\n── timing ──────────────────────────────────────────────")
    report("hailo inference", inference_times)
    report("loop (wait + infer)", total_times)
    report("capture -> pose", pipeline_times)
    print(f"  {'pose rate':22} {len(total_times) / wall:.1f} FPS "
          f"over {wall:.1f} s")
    print(f"  {'camera rate':22} {capture['fps']} FPS, "
          f"{capture['frames']} frames, {capture['dropped']} dropped")

    print("\n── keypoints seen (of "
          f"{detected} frames with a person) ─────")
    ok = True
    for name in KEYPOINT_NAMES:
        share = seen[name] / detected if detected else 0.0
        mark = "ok " if share >= 0.5 else "LOW"
        if name in WRISTS and share < 0.5:
            ok = False
        print(f"  {mark} {name:16} {seen[name]:4} / {detected}  ({share:.0%})")

    print()
    if detected == 0:
        print("FAIL: no person was detected in any frame. Stand in front of "
              "the camera and run this again.")
        return 1
    if not ok:
        print("FAIL: a wrist was not seen in at least half the frames.")
        return 1
    print("PASS: Brio -> Hailo-10H -> 17 COCO keypoints, both wrists tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
