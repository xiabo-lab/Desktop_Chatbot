"""The pose loop: one thread, from the Brio to a pair of filtered hands.

    CameraLease  ->  HailoPose  ->  PlayerSelector  ->  HandFilter  ->  you

This is the reusable half of AI Motion and the thing section 51 is about.
Nothing in it knows what a fruit is, and the callback it hands out carries the
**whole** `PersonPose` alongside the two filtered wrists — so Yoga, which wants
hips and knees and joint angles, needs a new consumer rather than a new
service.

**Consumers are called once per pose frame, and that is a contract.** A game
that instead sampled "the current hands" from its own 60 Hz render loop would
see each pose frame twice and test the same slash segment for collisions
twice, which at best double-scores and at worst slices a fruit the player
missed. Rendering interpolates; collision does not.

The loop deliberately does very little: waiting on a frame, one inference, and
the filters, which are microseconds. Everything expensive a game wants to do
belongs in the callback — but not so much of it that the loop misses the next
frame, which is why `stats.dropped` is published and worth watching.
"""

from __future__ import annotations

import logging
import threading
import time

from aipi5.motion.camera_lease import CameraLease, CameraLeaseError
from aipi5.motion.hailo_pose import HailoPose, PoseUnavailable
from aipi5.motion.pose_filter import HandFilter, PlayerSelector, readiness
from aipi5.motion.pose_types import PoseFrame, PoseStats

log = logging.getLogger(__name__)

#: How much of each new rate sample to believe, for the displayed FPS figures.
#: Low, because these are read by a person looking at a debug panel and a
#: number that flickers between 28 and 31 is harder to read than one that
#: settles.
RATE_SMOOTHING = 0.1


class MotionUnavailable(RuntimeError):
    """AI Motion could not start. The message is written for the screen."""


class PoseSnapshot:
    """What the pose loop published last. Cheap to build, safe to read anywhere.

    A plain object rather than a frozen dataclass because it is rebuilt thirty
    times a second and read from HTTP threads; the fields are only ever
    assigned at construction, so the swap in `PoseService._publish` is atomic
    enough for readers that tolerate being one frame behind — which every
    reader of this is.
    """

    __slots__ = ("timestamp", "person", "hands", "ready", "advice",
                 "player_present", "persons", "inference_s")

    def __init__(self, *, timestamp: float, person, hands, ready: bool,
                 advice: str, persons: int, inference_s: float):
        self.timestamp = timestamp
        self.person = person
        self.hands = hands
        self.ready = ready
        self.advice = advice
        self.player_present = person is not None
        self.persons = persons
        self.inference_s = inference_s

    def as_dict(self) -> dict:
        """Section 34's shape. Keypoints only — never a frame of video."""
        return {
            "timestamp": round(self.timestamp, 3),
            "player": {
                "present": self.player_present,
                "confidence": round(self.person.confidence, 3) if self.person else 0.0,
                "left_wrist": self.hands["left_wrist"].as_dict(),
                "right_wrist": self.hands["right_wrist"].as_dict(),
            },
            "ready": self.ready,
            "advice": self.advice,
            "persons": self.persons,
        }


class PoseService:
    """Owns the camera lease, the pose model and the thread between them.

    Started when an AI Motion page opens and stopped when it closes — section
    32's Option B. Deliberately not a singleton and not started at boot: a
    device nobody is playing on should not be holding the Brio away from the
    camera page.
    """

    def __init__(self, cfg, camera, *, on_pose=None):
        self.cfg = cfg
        self._camera = camera
        self._on_pose = on_pose
        self._lease: CameraLease | None = None
        self._pose: HailoPose | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._selector = PlayerSelector()
        self._hands = HandFilter(confidence=cfg.keypoint_confidence,
                                 stale_s=cfg.stale_ms / 1000,
                                 smoothing=cfg.smoothing)
        self.stats = PoseStats()
        self._snapshot: PoseSnapshot | None = None
        self._error = ""
        #: The most recent full pose frame, for the debug overlay's skeleton.
        #: Kept separately because it is much larger than a snapshot and only
        #: one page ever asks for it.
        self._last_frame: PoseFrame | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Acquire everything and start the loop. Raises `MotionUnavailable`.

        The order is camera then accelerator, and it matters for what the
        player is told: a Brio held by a video call is a much more common and
        much more fixable problem than a missing HAT, so it is the one that
        should be discovered and reported first. Section 41 gives them
        different screens.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.cfg.enabled:
                raise MotionUnavailable("AI Motion is turned off in the "
                                        "configuration")

            lease = CameraLease(self._camera)
            try:
                lease.acquire()
            except CameraLeaseError as exc:
                self._error = str(exc)
                raise MotionUnavailable(str(exc)) from exc

            try:
                pose = HailoPose(self.cfg.pose_model,
                                 score_threshold=self.cfg.person_confidence,
                                 iou_threshold=self.cfg.iou)
            except PoseUnavailable as exc:
                # The camera goes straight back. A game that cannot start must
                # not also cost the assistant its camera.
                lease.release()
                self._error = str(exc)
                raise MotionUnavailable(str(exc)) from exc

            self._lease, self._pose = lease, pose
            self._error = ""
            self._selector.reset()
            self._hands.reset()
            self.stats = PoseStats()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="aipi5-motion-pose",
                                            daemon=True)
            self._thread.start()
            log.info("AI Motion: pose inference started")

    def stop(self) -> None:
        """Stop the loop and give back the camera and the model. Idempotent.

        Section 30, and the order is the reverse of `start`: the thread stops
        first, because both of the things it touches are about to be torn down
        and an inference in flight against a released model is a segfault
        rather than an exception.
        """
        with self._lock:
            thread, self._thread = self._thread, None
            lease, self._lease = self._lease, None
            pose, self._pose = self._pose, None

        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
            if thread.is_alive():
                log.warning("AI Motion: the pose thread did not stop")

        if pose is not None:
            pose.close()
        if lease is not None:
            lease.release()
        with self._lock:
            self._snapshot = None
            self._last_frame = None
        if thread is not None:
            log.info("AI Motion: pose inference stopped")

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def error(self) -> str:
        lease = self._lease
        if lease is not None and lease.lost:
            return lease.lost
        return self._error

    # ── the loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        previous_publish = 0.0
        while not self._stop.is_set():
            lease, pose = self._lease, self._pose
            if lease is None or pose is None:
                return

            frame, captured_at = lease.frame()
            if frame is None:
                if lease.lost:
                    # Section 43: the camera was unplugged. Publish an empty
                    # snapshot so the page stops showing a player who is not
                    # there, and end the loop — `stop()` is what tears down,
                    # and it is called by whoever notices `error`.
                    self._publish(None, PoseFrame(timestamp=time.monotonic()))
                    log.warning("AI Motion: pose loop ending — %s", lease.lost)
                    return
                if self._stop.is_set():
                    return
                continue

            try:
                result = pose.infer(frame, captured_at=captured_at)
            except Exception:
                # `infer` does not raise, but this thread must outlive anything
                # that goes wrong in it regardless — a game that stops getting
                # hands shows a lost player, which is recoverable; a dead
                # thread is not.
                self.stats.errors += 1
                log.exception("AI Motion: pose frame failed")
                continue

            person = self._selector.choose(result)
            now = time.monotonic()
            self._hands.update(person, now)
            snapshot = self._publish(person, result)

            # Rates, smoothed. Measured across publishes rather than from the
            # inference time, so a loop that is falling behind for any reason
            # shows it here rather than reporting the accelerator's happy 30.
            if previous_publish:
                interval = now - previous_publish
                if interval > 0:
                    self._blend("inference_fps", 1.0 / interval)
            previous_publish = now
            self.stats.frames += 1
            self.stats.last_inference_s = result.inference_s
            self.stats.pipeline_s = now - captured_at
            self.stats.camera_fps = lease.fps
            self.stats.dropped = lease.dropped

            if self._on_pose is not None:
                try:
                    self._on_pose(snapshot, result)
                except Exception:
                    self.stats.errors += 1
                    log.exception("AI Motion: a pose consumer failed")

    def _blend(self, field: str, value: float) -> None:
        current = getattr(self.stats, field)
        setattr(self.stats, field,
                value if not current
                else current + RATE_SMOOTHING * (value - current))

    def _publish(self, person, result: PoseFrame) -> PoseSnapshot:
        state = readiness(person, self.cfg.keypoint_confidence)
        snapshot = PoseSnapshot(
            timestamp=result.timestamp,
            person=person,
            hands=dict(self._hands.hands),
            ready=bool(state["ready"]),
            advice=str(state["advice"]),
            persons=len(result.persons),
            inference_s=result.inference_s,
        )
        with self._lock:
            self._snapshot = snapshot
            self._last_frame = result
        return snapshot

    # ── reading ──────────────────────────────────────────────────────

    def snapshot(self) -> PoseSnapshot | None:
        """The most recent published pose. For pages, not for collision."""
        with self._lock:
            return self._snapshot

    def last_frame(self) -> PoseFrame | None:
        """The full skeleton, for the debug overlay only."""
        with self._lock:
            return self._last_frame

    def preview_jpeg(self):
        """One camera frame as JPEG, for the start screen. None if not running.

        The only route by which a game ever shows the camera image. Section 22
        is explicit that the player's room must not be on screen during play —
        this is for the "stand where the camera can see you" screen and the
        debug overlay, and nothing else asks for it.
        """
        lease = self._lease
        return lease.preview_jpeg() if lease is not None else None

    def describe(self) -> dict:
        """Section 44's panel: proof that the AI HAT+ 2 is really being used."""
        from aipi5.core import accelerator

        detail = {
            "running": self.running,
            "error": self.error,
            "stats": self.stats.as_dict(),
        }
        detail["accelerator"] = accelerator.identify()
        pose, lease = self._pose, self._lease
        detail["pose"] = pose.describe() if pose is not None else {}
        detail["camera"] = lease.describe() if lease is not None else {}
        return detail
