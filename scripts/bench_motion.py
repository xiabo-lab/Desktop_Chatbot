#!/usr/bin/env python3
"""Where the motion-to-screen delay actually goes, per camera mode.

`probe_pose.py` next door answers "does the pipeline work". This answers "which
mode should it run in", which is a different question and needs the stages
broken apart: a mode that halves the frame age and doubles the JPEG decode has
not helped, and a single end-to-end number cannot tell you that happened.

Run on the Pi with the assistant stopped, because that service owns both the
camera and the accelerator:

    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user stop aipi5
    ~/AIPI5/.venv/bin/python scripts/bench_motion.py --seconds 12
    systemctl --user restart aipi5      # restart, not start — see the notes

**Stand in front of the camera while it runs.** An empty room is not a valid
measurement of this pipeline: `yolov8_pose.decode` thresholds before decoding
boxes, so a frame with nobody in it skips nearly all of the numpy work and
flatters the decode stage by several milliseconds.

The stages, in the order one frame passes through them:

    read        inside `VideoCapture.read()` on the capture thread. Waiting
                for the sensor, plus the MJPEG decode when compressed.
    age         how old the frame already was when the pose thread picked it
                up. Bounded by the capture period, which is what a faster
                camera mode actually buys.
    preprocess  resize into the model's square, pad, swap channels.
    inference   the accelerator, plus the host-side dequantise on the way out.
    decode      nine tensors to seventeen joints, in numpy.
    capture to pose   the sum that gameplay feels, measured end to end rather
                than added up, so anything unaccounted for shows.

Everything is read through `getattr` with a default so this same script runs
against an older checkout of `aipi5/motion/` — which is the only way to answer
"is the new code faster" on a device with no git history.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipi5.core import config as config_mod          # noqa: E402
from aipi5.motion import geometry, hailo_pose        # noqa: E402
from aipi5.motion.camera_lease import CameraLease, CameraLeaseError  # noqa: E402
from aipi5.vision.camera import Camera               # noqa: E402

#: What to try, and why each one is on the list.
#:
#: The camera offers exactly two modes above 30 fps — 1280x720 MJPG at 90 and
#: 640x480 MJPG at 120 — so the sweep is not a grid. Everything else is here to
#: separate one variable: 640x360 to remove the downscale entirely, NV12 to
#: remove the JPEG decode, YUYV to remove it at the highest bandwidth the bus
#: will carry.
DEFAULT_SWEEP = (
    "1280x720@90:MJPG",
    "640x480@120:MJPG",
    "1280x720@30:NV12",
    "640x360@30:MJPG",
    "640x360@30:NV12",
    "1280x720@30:YUYV",
)


def parse_mode(text: str) -> tuple[int, int, int, str]:
    """``1280x720@90:MJPG`` to ``(1280, 720, 90, "MJPG")``."""
    spec, _, fourcc = text.partition(":")
    size, _, fps = spec.partition("@")
    width, _, height = size.partition("x")
    return int(width), int(height), int(fps or 30), (fourcc or "MJPG").upper()


class Cpu:
    """Whole-machine CPU busy fraction across an interval, from /proc/stat.

    The process's own `getrusage` is not enough here and would mislead: most of
    the cost being measured is in threads this process owns but does not run —
    the V4L2 driver's, libhailort's — and some of it is not this process at
    all. What the device has left is the question, so the whole machine is the
    thing to measure.
    """

    def __init__(self):
        self.busy, self.total = self._read()

    @staticmethod
    def _read() -> tuple[int, int]:
        try:
            with open("/proc/stat", encoding="ascii") as handle:
                fields = [int(value) for value in
                          handle.readline().split()[1:]]
        except OSError:
            return 0, 0
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields) - idle, sum(fields)

    def since(self) -> float:
        busy, total = self._read()
        spent = total - self.total
        return (busy - self.busy) / spent if spent else 0.0


def temperature() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as h:
            return int(h.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def summarise(values: list[float]) -> dict:
    if not values:
        return {"mean": 0.0, "median": 0.0, "p95": 0.0}
    ordered = sorted(values)
    return {
        "mean": statistics.mean(values) * 1000,
        "median": statistics.median(values) * 1000,
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000,
    }


def run_mode(camera, pose, mode: str, seconds: float, warmup: float) -> dict:
    """One camera mode, start to finish. Returns a row for the table."""
    width, height, fps, fourcc = parse_mode(mode)
    try:
        lease = CameraLease(camera, width=width, height=height, fps=fps,
                            fourcc=fourcc)
    except TypeError:
        # An older `camera_lease.py` with no format argument. Skip rather than
        # silently measure MJPG and label it NV12.
        if fourcc != "MJPG":
            return {"mode": mode, "error": "this build cannot select a format"}
        lease = CameraLease(camera, width=width, height=height, fps=fps)

    try:
        lease.acquire()
    except CameraLeaseError as exc:
        return {"mode": mode, "error": str(exc)}

    ages: list[float] = []
    pre: list[float] = []
    infer: list[float] = []
    dec: list[float] = []
    total: list[float] = []
    people = 0

    # Warm up before anything is recorded: auto-exposure is still settling, the
    # first inference on a freshly configured model is slower than the rest,
    # and OpenCV's first resize at a new size allocates.
    deadline = time.monotonic() + warmup
    while time.monotonic() < deadline:
        frame, at = lease.frame()
        if frame is not None:
            pose.infer(frame, captured_at=at)

    lease.dropped = 0
    cpu = Cpu()
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        frame, captured_at = lease.frame()
        if frame is None:
            break
        picked_up = time.monotonic()
        result = pose.infer(frame, captured_at=captured_at)
        finished = time.monotonic()

        ages.append(picked_up - captured_at)
        pre.append(getattr(result, "preprocess_s", 0.0))
        infer.append(result.inference_s)
        dec.append(getattr(result, "decode_s", 0.0))
        total.append(finished - captured_at)
        people += bool(result.persons)

    wall = time.monotonic() - started
    busy = cpu.since()
    described = lease.describe()
    frame, _ = lease.frame(wait=False)
    shape = tuple(frame.shape[:2]) if frame is not None else (0, 0)
    lease.release()

    return {
        "mode": mode,
        "asked": f"{width}x{height}@{fps} {fourcc}",
        "got_size": f"{shape[1]}x{shape[0]}",
        "got_format": described.get("format") or "?",
        "negotiated_fps": described.get("negotiated_fps"),
        "camera_fps": described.get("fps"),
        "read_ms": described.get("read_ms"),
        "dropped": described.get("dropped"),
        "pose_fps": len(total) / wall if wall else 0.0,
        "frames": len(total),
        "seen": people,
        "age": summarise(ages),
        "pre": summarise(pre),
        "infer": summarise(infer),
        "decode": summarise(dec),
        "total": summarise(total),
        "cpu": busy,
        "temp": temperature(),
    }


def preprocess_variants(frame, target: int, rounds: int) -> list[tuple[str, float]]:
    """The letterbox, three ways, on one real frame.

    Isolated from the camera on purpose. The three differ only in how the
    channel swap and the padded buffer are handled, and the gap between them is
    a property of numpy and OpenCV rather than of any camera mode — measuring
    it inside the sweep would have every row paying for it and none of them
    explaining it.
    """
    import cv2
    import numpy as np

    results = []

    def timed(label, work):
        work()                                   # warm the allocator
        began = time.monotonic()
        for _ in range(rounds):
            work()
        results.append((label, (time.monotonic() - began) / rounds * 1000))

    def old_way():
        rgb = frame[:, :, ::-1]
        resized = cv2.resize(rgb, (target, int(round(target * frame.shape[0]
                                                     / frame.shape[1]))),
                             interpolation=cv2.INTER_LINEAR)
        padded = np.full((target, target, 3), 114, dtype=np.uint8)
        top = (target - resized.shape[0]) // 2
        padded[top:top + resized.shape[0], :] = resized
        return np.ascontiguousarray(padded, dtype=np.uint8)

    def swap_first():
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        geometry.letterbox_image(rgb, target, target)

    timed("reverse the axis, then resize (was)", old_way)
    timed("cvtColor at full size, then resize", swap_first)

    # Only on a build that has them. This script is expected to run against the
    # older `motion/` as well, which is where the "was" row comes from.
    if hasattr(geometry, "LETTERBOX_FILL"):
        buffer = np.full((target, target, 3), geometry.LETTERBOX_FILL,
                         dtype=np.uint8)

        def new_way():
            geometry.letterbox_image(frame, target, target, into=buffer,
                                     swap_rb=True)

        timed("resize into a kept buffer, swap after (now)", new_way)
    return results


def show(row: dict) -> None:
    if "error" in row:
        print(f"  {row['mode']:22} — {row['error']}")
        return
    # `read_ms` is absent on an older `camera_lease.py`, which is a build this
    # script is expected to run against — see the module docstring.
    read = row["read_ms"]
    read = f"{read:6.1f}" if isinstance(read, (int, float)) else "     ?"
    print(f"  {row['mode']:22} got {row['got_size']} {row['got_format']} "
          f"neg {row['negotiated_fps']}")
    print(f"    {'camera':14} {row['camera_fps']:>6} fps   "
          f"read {read} ms   dropped {row['dropped']}")
    print(f"    {'pose':14} {row['pose_fps']:6.1f} fps   "
          f"{row['frames']} frames, a person in {row['seen']}")
    for label in ("age", "pre", "infer", "decode", "total"):
        stat = row[label]
        print(f"    {label:14} mean {stat['mean']:6.1f}   "
              f"median {stat['median']:6.1f}   p95 {stat['p95']:6.1f}")
    print(f"    {'machine':14} cpu {row['cpu'] * 100:.0f}% of "
          f"{os.cpu_count()} cores   {row['temp']:.1f} °C")
    duty = row["infer"]["mean"] * row["pose_fps"] / 1000
    print(f"    {'accelerator':14} busy {duty * 100:.0f}% of wall "
          f"(inference x pose rate)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="how long to measure each mode")
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--modes", nargs="*", default=list(DEFAULT_SWEEP))
    parser.add_argument("--rounds", type=int, default=200,
                        help="repeats for the preprocess micro-benchmark")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    settings = config_mod.load(args.config)

    identity = hailo_pose.identify()
    print("── accelerator ─────────────────────────────────────────")
    for key, value in identity.items():
        print(f"  {key:16} {value}")
    if not identity.get("available"):
        print("\nFAIL: no Hailo device. Nothing here can be trusted.")
        return 1

    camera = Camera(settings.camera)
    camera.open()

    # One model for the whole sweep. Configuring it costs about a second and
    # nothing about it depends on the camera mode, so paying that per row would
    # only add a second of thermal idle between measurements.
    try:
        pose = hailo_pose.HailoPose(
            settings.motion.pose_model,
            score_threshold=settings.motion.person_confidence,
            iou_threshold=settings.motion.iou)
    except hailo_pose.PoseUnavailable as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"  {'model':16} {pose.describe()}")

    print(f"\n── {len(args.modes)} camera modes, {args.seconds:.0f} s each "
          "─────────────────")
    rows = []
    for mode in args.modes:
        row = run_mode(camera, pose, mode, args.seconds, args.warmup)
        rows.append(row)
        show(row)

    print("── the letterbox, three ways ───────────────────────────")
    best = next((r for r in rows if "error" not in r), None)
    if best is not None:
        lease = CameraLease(camera, width=1280, height=720, fps=90)
        try:
            lease.acquire()
            time.sleep(0.5)
            frame, _ = lease.frame()
            if frame is not None:
                print(f"  on a real {frame.shape[1]}x{frame.shape[0]} frame, "
                      f"{args.rounds} rounds each")
                for label, ms in preprocess_variants(frame, 640, args.rounds):
                    print(f"    {label:44} {ms:6.2f} ms")
        except CameraLeaseError as exc:
            print(f"  skipped: {exc}")
        finally:
            lease.release()

    pose.close()
    camera.close()

    print("\n── summary, by what gameplay feels ─────────────────────")
    print(f"  {'mode':22} {'pose fps':>9} {'capture->pose':>15} {'cpu':>6}")
    for row in rows:
        if "error" in row:
            continue
        print(f"  {row['mode']:22} {row['pose_fps']:9.1f} "
              f"{row['total']['mean']:12.1f} ms {row['cpu'] * 100:5.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
