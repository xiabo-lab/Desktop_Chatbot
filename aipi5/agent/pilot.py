"""Hand control driven by the accelerator instead of by the browser.

The first version put MediaPipe in the kiosk page and fed it the camera over
MJPEG. It worked, eventually, and everything about it was wrong:

    recognition   57-70 ms a frame of WebAssembly, on the main thread of the
                  page the touchscreen talks to -- 96% of a core, and one frame
                  permanently behind itself
    the picture   camera -> JPEG -> HTTP -> decode -> shared memory, thirty
                  times a second, for a hand
    the HAT       two person-detections a second, and otherwise asleep

Five separate faults were found and fixed in that pipeline over one afternoon
-- a stale CDP session, a dead video stream, an unreleasable in-flight guard, a
recognizer that failed to load in silence, and a descriptor leak that stopped
the whole thing after a quarter of an hour. Every one of them existed only
because the recognition was on the far side of a video stream from the camera.

**So it moves to where the camera already is.** `PoseService` is running
YOLOv8-pose on the AI HAT+ at thirty frames a second for the motion games, it
already owns the camera lease, and its own configuration describes it as asking
"where are their wrists, thirty times a second". This asks it the same
question, decides what the movement means, and sends the answer on.

    camera -> Hailo -> wrists -> here -> the helper -> the page

No video leaves Python, no model runs in the browser, and the kiosk page has
nothing to do with hand control any more.

**What the accelerator cannot do, stated plainly.** There is no hand-landmark
model for a HAILO10H. `hand_landmark_lite` is published for Hailo-8 only --
every HAILO10H URL in the model zoo answers 403 across four releases -- and
loading the Hailo-8 build on this chip returns `HAILO_NOT_IMPLEMENTED`,
measured rather than assumed. Compiling one needs the Dataflow Compiler, which
is x86-only and licensed.

So the pose model gives wrists and no fingers, and a closed fist is not
something this device can see. Everything that is a *movement* -- the pointer
and all four sweeps -- comes off the wrist and is better than it was. Clicking
is a **dwell**: hold the pointer still on a target and it fires, the way every
eye-tracker and head-pointer has done it for thirty years. It needs no fingers,
it cannot be confused with a sweep, and it is honest about the hardware.
`aipi5/agent/pilot.py` is the only place that would change if a hand model for
this chip ever ships.
"""

from __future__ import annotations

import logging
import math
import threading
import time

log = logging.getLogger(__name__)

BORROWER = "hand control"
HOLD = "hand control"

#: How long the feed stays up after the browser closes, so the agent closing
#: one page and opening another does not cost a camera acquisition.
LINGER_S = 8.0

#: Nothing is driven by a joint the model is unsure of. Higher than the
#: person threshold for the same reason the games use a higher one: a wrist
#: guessed from a sleeve steers as confidently as a wrist that was seen.
WRIST_CONFIDENCE = 0.45

#: The middle of the camera's view, stretched over the whole page. Ten percent
#: a side rather than the twenty-two the browser version used: a hand outside
#: the kept band clamps to the edge, and a pointer that stops while the arm
#: keeps moving is the single most confusing thing this feature can do.
REACH = 0.10

#: A sweep is fast; pointing and repositioning are not. Fractions of the frame
#: per second, measured from real sweeps on this device: theirs ran 0.85 to
#: 1.52 and aiming ran about a tenth of that.
SWEEP_SPEED = 0.55
SWEEP_TRAVEL_Y = 0.22
#: Sideways is held higher than up and down. A scroll nobody meant is undone by
#: scrolling back; a Back nobody meant loses the page they were reading.
SWEEP_TRAVEL_X = 0.30
SWEEP_DOMINANCE = 1.6
#: How much history a sweep is measured over.
TRAIL_S = 0.6

#: After a sweep, the next one waits until the hand has been slow for this
#: long. Not a fixed lockout: the arm coming back is itself a sweep, and how
#: long somebody takes to reset varies from a flick to a deliberate lift.
SETTLE_S = 0.26
SETTLE_SPEED = 0.35

#: **The pointer may keep up with the arm; a gesture may not.**
#:
#: A tenth of a second is ten updates a second, and ten is visibly steppy --
#: reported as the marker "jumping around the screen". That number came from
#: the browser, where each update was a `fetch` through four hops. It is a
#: unix socket and a CDP write now, measured at five milliseconds in the
#: helper, so the pointer can run at the rate the camera does.
MOVE_INTERVAL_S = 0.04
GESTURE_INTERVAL_S = 0.45
#: Do not resend a pointer that has not really moved. Small, because at
#: twenty-five updates a second a large threshold is what makes movement look
#: like stepping rather than sliding.
MOVE_MIN = 0.004

#: **How hard the pointer is filtered, which is not one number.**
#:
#: A wrist keypoint moves a little every frame even from a hand held still, so
#: a raw pointer twitches. Filtering that away with a fixed strength buys the
#: stillness with lag, and lag on a fast hand is what makes a pointer feel like
#: it is chasing rather than following -- reported as jumping about.
#:
#: So the strength follows the speed, which is what a one-euro filter does and
#: why it is the standard answer here. A slow hand is smoothed hard, because
#: everything it is doing is noise. A quick one is barely smoothed at all,
#: because everything it is doing is signal.
SMOOTH_SLOW = 0.18          # a hand at rest: heavy, kills the twitch
SMOOTH_FAST = 0.85          # a hand travelling: light, no lag worth feeling
SMOOTH_AT = 0.9             # frame-widths a second at which it is fully light

#: Dwell: hold the pointer inside this radius for this long and it clicks.
#:
#: A second and a half, not the nine hundred milliseconds it started at. Short
#: dwells fire on a hand that merely paused -- while its owner was deciding,
#: or on the way to somewhere else -- and a click nobody meant on a web page
#: can navigate away from what they were reading.
DWELL_S = 1.5
DWELL_RADIUS = 0.035
#: And it will not start until the hand has travelled this far since the last
#: one. A hand resting where it already was is not somebody choosing again.
DWELL_REARM = 0.10

#: How long a hand may be missing before the pointer is taken off the page.
LOST_S = 0.8

#: How old a finger reading may be before it is ignored. Three frames: enough
#: that the reader can miss one without the gate flickering, short enough that
#: a hand which has gone is noticed as gone.
SHAPE_STALE_S = 0.1

#: **How long an open palm must be held before anything moves.**
#:
#: Reported, and the reason this gate exists: "it only sees my arm move then
#: does the scroll". A wrist alone cannot tell reaching for a cup from a
#: deliberate sweep, and a page that navigates because somebody scratched their
#: head is worse than one that needs asking twice.
#:
#: So the fingers decide. Nothing is sent until an open palm has been seen for
#: this long, and it stops as soon as the palm does. Four frames at thirty a
#: second: long enough that a single bad landmark read cannot arm it, short
#: enough to feel immediate.
PALM_FRAMES = 4

#: And how many frames without a palm end it. Higher than the arming count,
#: because a hand mid-sweep turns edge-on and the model briefly loses the
#: fingers -- dropping control there would cut every gesture in half.
PALM_GONE_FRAMES = 12

#: A fist for this many frames is a click. Two, because the shape is read from
#: geometry rather than a classifier and is steady; one would fire on a hand
#: passing through the shape on its way somewhere.
FIST_FRAMES = 2

#: Where the landmark model lives. Absent is survivable: sweeps still come off
#: the wrist, and `armed_by_palm` says whether the gate is real or assumed.
LANDMARKS = "/home/fuwenxu/AIPI5/models/hand_landmarker.task"


class _At:
    """A point that looks enough like a keypoint for the code below."""

    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float):
        self.x, self.y = x, y


def _span(value: float) -> float:
    """Camera space to page space, across the usable middle of the frame."""
    width = 1.0 - 2.0 * REACH
    return min(1.0, max(0.0, (value - REACH) / width))


class HandPilot:
    """Watches the wrists the accelerator reports, and drives the browser."""

    def __init__(self, camera, motion_cfg, *, send, screen=None, clock=None,
                 fingers=None):
        self._camera = camera
        self._cfg = motion_cfg
        #: Called with (gesture, at) — the assistant's route to the agent.
        #: Injected so this module owns no transport and can be tested without
        #: a socket, a browser or an accelerator.
        self._send = send
        self._screen = screen
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pose = None
        self._held_screen = False
        self._wanted_at = 0.0
        self.error = ""
        self.generation = 0
        self.paused = False
        #: Reads the fingers on a crop the accelerator pointed at. Injected so
        #: the gesture logic can be tested without a model, a camera or an
        #: accelerator -- which is all of these tests.
        self._fingers = fingers
        #: The finger reader runs on its own thread; these are the hand-off.
        self._shape_lock = threading.Lock()
        self._shape_thread = None
        self._reset()
        #: Counters, for the settings page and for answering "is it seeing me".
        self.seen = 0
        self.gestures = 0

    def _reset(self) -> None:
        self._trail: list[tuple[float, float, float]] = []
        self._armed = True
        self._still_since = 0.0
        self._sent = (float("nan"), float("nan"))
        self._move_at = 0.0
        self._sent_hold = 0.0
        self._gesture_at = 0.0
        self._last_seen = 0.0
        self._dwell_from = None
        self._dwell_since = 0.0
        self._dwell_done = False
        self._pointing = False
        #: The filtered pointer. `None` until the first wrist arrives, so it
        #: starts where the hand is rather than sliding in from a corner.
        self._smooth: tuple[float, float] | None = None
        #: Where the last click landed, so another cannot fire until the hand
        #: has actually gone somewhere.
        self._clicked_at: tuple[float, float] | None = None
        #: The palm gate. Nothing is sent until `_palm_for` reaches
        #: `PALM_FRAMES`, and it ends when `_no_palm_for` reaches its own.
        self._palm_for = 0
        self._no_palm_for = 0
        self._fist_for = 0
        #: The hand-off with the finger thread: what it should look at next,
        #: what it last saw, and when. See `_shape`.
        self._shape_want = None
        self._shape_seen = None
        self._shape_at = 0.0

    # ── lifecycle, the same shape Housekeeping already calls ────────

    def sync(self, browser_open: bool) -> None:
        now = self._clock()
        with self._lock:
            if browser_open and not self.paused:
                self._wanted_at = now
                if self._pose is None:
                    self._start()
                return
            if self._pose is None:
                return
            if browser_open and now - self._wanted_at < LINGER_S:
                return
            self._stop()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = bool(paused)

    def close(self) -> None:
        with self._lock:
            self._stop()

    @property
    def active(self) -> bool:
        return self._pose is not None

    def describe(self) -> dict:
        return {"active": self._pose is not None, "error": self.error,
                "generation": self.generation, "paused": self.paused,
                "backend": "hailo pose + landmarks", "seen": self.seen,
                "gestures": self.gestures,
                "palm_gate": self.armed_by_palm,
                "fingers": (self._fingers.error if self._fingers is not None
                            else "not built")}

    # ── the accelerator ─────────────────────────────────────────────

    @property
    def armed_by_palm(self) -> bool:
        """Whether the five-finger gate is actually in force.

        False means the landmark model did not load, so sweeps are running off
        the wrist alone. Reported rather than hidden: the difference is exactly
        the false triggers this gate exists to stop.
        """
        return bool(self._fingers is not None and self._fingers.ready)

    def _start(self) -> None:
        """Caller holds the lock. Sets `error` rather than raising."""
        from aipi5.motion.service import MotionUnavailable, PoseService

        if self._fingers is None:
            from aipi5.agent.fingers import Fingers

            self._fingers = Fingers(LANDMARKS)
        if not self._fingers.ready and not self._fingers.start():
            # Survivable, and said out loud. Sweeps still work off the wrist;
            # what is lost is the gate and the fist.
            log.warning("hand control has no finger model: %s -- sweeps will "
                        "run off the wrist alone", self._fingers.error)

        if self._camera is None:
            self.error = "this device has no camera"
            return
        if getattr(self._camera, "lent", False):
            self.error = "the camera is being used by another feature"
            return

        pose = PoseService(self._cfg, self._camera, on_pose=self._saw)
        try:
            pose.start()
        except MotionUnavailable as exc:
            self.error = str(exc)
            log.info("hand control could not start: %s", exc)
            return
        except Exception as exc:                        # noqa: BLE001
            self.error = f"the pose service would not start ({exc})"
            log.warning("hand control could not start: %s", exc)
            return

        self._pose = pose
        self.error = ""
        self.generation += 1
        self._reset()
        # Holding the camera stops presence detection, so without this the idle
        # timer sees a quiet room and blanks the screen on somebody standing in
        # front of it driving a page.
        if self._screen is not None and not self._held_screen:
            try:
                self._screen.hold(HOLD)
                self._held_screen = True
            except Exception as exc:                    # noqa: BLE001
                log.debug("could not hold the screensaver: %s", exc)
        log.info("hand control running on the accelerator")

    def _stop(self) -> None:
        pose, self._pose = self._pose, None
        # The finger thread watches `_pose` and returns when it goes.
        if pose is not None:
            try:
                pose.stop()
            except Exception as exc:                    # noqa: BLE001
                log.warning("hand control could not stop cleanly: %s", exc)
            else:
                log.info("hand control gave the camera back")
        if self._held_screen and self._screen is not None:
            try:
                self._screen.release(HOLD)
            except Exception as exc:                    # noqa: BLE001
                log.debug("could not release the screensaver: %s", exc)
        self._held_screen = False
        self._reset()

    # ── one frame from the accelerator ──────────────────────────────

    def _saw(self, snapshot, result=None) -> None:
        """Called on the pose thread, thirty times a second. Never raises.

        Two arguments, because `PoseService` hands consumers the snapshot and
        the raw frame behind it. Only the snapshot is wanted here -- the frame
        is pixels, and the whole point of this module is that pixels stop at
        the pose thread.
        """
        try:
            self._read(snapshot)
        except Exception:                               # noqa: BLE001
            log.exception("hand control failed on a pose frame")

    def _read(self, snapshot) -> None:
        now = self._clock()
        wrist = self._driving_wrist(getattr(snapshot, "person", None))
        if wrist is None:
            if self._last_seen and now - self._last_seen > LOST_S:
                # Send first, then forget. `_reset` clears the very flag
                # `_deliver` uses to decide whether a pointer is on the page,
                # so doing it the other way round suppressed every hide and
                # left the marker sitting there after the hand had gone.
                self._deliver("hide", None)
                self._reset()
            return

        self.seen += 1
        self._last_seen = now

        shape = self._shape(wrist)
        if not self._gate(shape):
            return
        if shape is not None:
            # The palm's centre, not the wrist. It averages five landmarks
            # instead of trusting one, and it is where a person thinks they
            # are pointing -- the wrist trails the hand by its own length.
            from aipi5.motion import geometry

            wrist = _At(geometry.mirror(shape.x), shape.y)
        # **Not mirrored here.** `HailoPose._person` already mirrors every
        # keypoint as it decodes -- `geometry.mirror`, so the room behaves like
        # one -- and boxing.js:1419 warns about this exact mistake in the same
        # words: preserve it "instead of applying a second mirror and swapping
        # it". The browser version had to mirror because it read raw camera
        # frames; this reads poses, which arrive mirrored already.
        #
        # Doing it twice swapped left and right, which is how it was reported.
        x, y = wrist.x, wrist.y
        if shape is not None and shape.closed:
            # A fist is a click, aimed where the palm last was: closing a hand
            # pulls the landmarks inwards, so the fist's own centre is not
            # where its owner was pointing a moment earlier.
            self._fist_for += 1
            if self._fist_for == FIST_FRAMES:
                where = self._sent if self._sent[0] == self._sent[0] else None
                if where is not None:
                    self._gesture_at = now
                    self.gestures += 1
                    self._deliver("click", {"x": where[0], "y": where[1]})
            self._trail.clear()
            return
        self._fist_for = 0
        self._trail.append((now, x, y))
        cut = now - TRAIL_S
        while self._trail and self._trail[0][0] < cut:
            self._trail.pop(0)

        if not self._armed:
            if self._speed() < SETTLE_SPEED:
                if not self._still_since:
                    self._still_since = now
                if now - self._still_since >= SETTLE_S:
                    self._armed = True
                    self._trail = self._trail[-1:]
            else:
                self._still_since = 0.0
        else:
            sweep = self._sweep()
            if sweep and now - self._gesture_at >= GESTURE_INTERVAL_S:
                self._gesture_at = now
                self._move_at = now
                self._armed = False
                self._still_since = 0.0
                self._trail = self._trail[-1:]
                self._dwell_from = None
                self.gestures += 1
                self._deliver(sweep, None)
                return

        # Smoothed for the pointer and the dwell; the trail above stays raw so
        # a sweep is measured on the movement that was actually made.
        target = (_span(x), _span(y))
        if self._smooth is None:
            self._smooth = target
        else:
            sx, sy = self._smooth
            # How much of the new reading to take, from how fast the hand is
            # going. `_speed` is over the last few samples, so it reacts within
            # a frame or two of a movement starting.
            quick = min(1.0, self._speed() / SMOOTH_AT)
            alpha = SMOOTH_SLOW + (SMOOTH_FAST - SMOOTH_SLOW) * quick
            self._smooth = (sx + alpha * (target[0] - sx),
                            sy + alpha * (target[1] - sy))
        self._point(now, self._smooth[0], self._smooth[1])

    def _shape(self, wrist):
        """The most recent finger reading. **Never blocks the pose thread.**

        Sixteen milliseconds of landmark inference inside a thirty-three
        millisecond frame budget is half of it, and the pose loop also has a
        capture, a letterbox, the accelerator and a tensor decode to fit in the
        rest. Doing it here dropped the whole pipeline below thirty frames a
        second, and everything downstream -- the pointer most of all -- felt it.

        So the reading happens on its own thread and this takes whatever it
        last produced. The shape is then a frame or two old, which does not
        matter: a palm does not become a fist in sixty milliseconds, and the
        *position* the pointer follows is the fresh one from pose.
        """
        if self._fingers is None or not self._fingers.ready:
            return None
        with self._shape_lock:
            self._shape_want = wrist
            fresh = self._shape_seen
            if self._shape_at and self._clock() - self._shape_at > SHAPE_STALE_S:
                fresh = None
        if self._shape_thread is None:
            self._shape_thread = threading.Thread(
                target=self._shape_loop, name="aipi5-hand-fingers", daemon=True)
            self._shape_thread.start()
        return fresh

    def _shape_loop(self) -> None:
        """Reads fingers as fast as it can, on the latest wrist it was given."""
        from aipi5.motion import geometry

        while True:
            with self._shape_lock:
                wrist = self._shape_want
                self._shape_want = None
            if wrist is None:
                # Nothing to look at. Sleep a frame rather than spin; the pose
                # thread will leave another wrist along in 33 ms.
                time.sleep(0.02)
                if self._pose is None:
                    return
                continue
            pose = self._pose
            lease = getattr(pose, "_lease", None) if pose is not None else None
            if lease is None:
                return
            try:
                frame, _captured = lease.frame(wait=False)
            except Exception:                         # noqa: BLE001
                frame = None
            shape = None
            if frame is not None:
                # Pose keypoints arrive mirrored and the picture does not, so
                # the crop is taken where the hand really is.
                shape = self._fingers.read(frame, geometry.mirror(wrist.x),
                                           wrist.y)
            with self._shape_lock:
                self._shape_seen = shape
                self._shape_at = self._clock()

    def _gate(self, shape) -> bool:
        """Five fingers, held, before anything is allowed to act.

        Without a landmark model there is no gate and this passes everything,
        which is the old behaviour and is said out loud by `armed_by_palm`.
        """
        if not self.armed_by_palm:
            return True
        if shape is not None and (shape.open or shape.closed):
            self._no_palm_for = 0
            if shape.open:
                self._palm_for += 1
            # A fist counts as the hand still being there: it is what a click
            # looks like, and losing control mid-click would be absurd.
        else:
            self._no_palm_for += 1
            if self._no_palm_for >= PALM_GONE_FRAMES:
                if self._palm_for >= PALM_FRAMES:
                    self._deliver("hide", None)
                self._palm_for = 0
                self._fist_for = 0
                self._trail.clear()
                self._smooth = None
            return False
        return self._palm_for >= PALM_FRAMES

    def _driving_wrist(self, person):
        """The raised hand, or None.

        The higher of the two wrists, and only when it is above the hips --
        an arm hanging at somebody's side is not driving anything, and using
        it would mean the pointer wanders whenever they walk past.
        """
        if person is None:
            return None
        points = getattr(person, "keypoints", None) or {}
        best = None
        for side in ("left_wrist", "right_wrist"):
            point = points.get(side)
            if point is None or point.confidence < WRIST_CONFIDENCE:
                continue
            if best is None or point.y < best.y:
                best = point
        if best is None:
            return None
        hips = [points.get(n) for n in ("left_hip", "right_hip")]
        line = [h.y for h in hips if h is not None and h.confidence >= 0.3]
        if line and best.y > min(line):
            return None
        return best

    # ── what the movement meant ─────────────────────────────────────

    def _speed(self) -> float:
        """Over the last few samples, not the whole window.

        Averaged across the window a fast stroke followed by a pause reads as
        moderate for as long as the window lasts, which would let the settle
        expire while the hand was still travelling.
        """
        if len(self._trail) < 2:
            return 0.0
        first = self._trail[max(0, len(self._trail) - 5)]
        last = self._trail[-1]
        seconds = last[0] - first[0]
        if seconds <= 0:
            return 0.0
        return math.hypot(last[1] - first[1], last[2] - first[2]) / seconds

    def _sweep(self) -> str:
        if len(self._trail) < 4:
            return ""
        first, last = self._trail[0], self._trail[-1]
        seconds = last[0] - first[0]
        if seconds <= 0:
            return ""
        dx, dy = last[1] - first[1], last[2] - first[2]
        across, down = abs(dx), abs(dy)
        if math.hypot(dx, dy) / seconds < SWEEP_SPEED:
            return ""
        if across >= SWEEP_TRAVEL_X and across > down * SWEEP_DOMINANCE:
            return "forward" if dx > 0 else "back"
        if down >= SWEEP_TRAVEL_Y and down > across * SWEEP_DOMINANCE:
            # The camera's y grows downwards, so a hand going *up* the frame is
            # a falling y and means scroll up: the page moves the way the hand
            # does, which is the only mapping anybody predicts.
            return "scroll_up" if dy < 0 else "scroll_down"
        return ""

    def _point(self, now: float, x: float, y: float) -> None:
        """Move the pointer, and click if it has rested somewhere on purpose."""
        hold = self._dwell(now, x, y)
        if hold is None:
            return

        if now - self._move_at < MOVE_INTERVAL_S:
            return
        last_x, last_y = self._sent
        if (last_x == last_x                       # not NaN
                and math.hypot(x - last_x, y - last_y) < MOVE_MIN
                and abs(hold - self._sent_hold) < 0.12):
            return
        self._move_at = now
        self._sent = (x, y)
        self._sent_hold = hold
        self._pointing = True
        # `hold` rides along so the marker can show a click coming rather than
        # springing one. A dwell nobody saw building is a dwell that feels like
        # a misfire even when it was not.
        self._deliver("move", {"x": x, "y": y, "hold": round(hold, 2)})

    def _dwell(self, now: float, x: float, y: float):
        """How far through a click this is, 0..1, or None once it has fired.

        **A hand has to arrive somewhere to click it.** The first version only
        asked whether the pointer had been still, so a hand pausing on its way
        past a link -- or its owner thinking -- was a click. Now the hand must
        also have travelled `DWELL_REARM` from wherever the last click landed,
        which is the difference between resting and choosing.
        """
        if self._dwell_from is None:
            self._dwell_from = (x, y)
            self._dwell_since = now
            self._dwell_done = False
            return 0.0

        drift = math.hypot(x - self._dwell_from[0], y - self._dwell_from[1])
        if drift > DWELL_RADIUS:
            self._dwell_from = (x, y)
            self._dwell_since = now
            self._dwell_done = False
            return 0.0
        if self._dwell_done:
            return 0.0

        if self._clicked_at is not None:
            gone = math.hypot(x - self._clicked_at[0], y - self._clicked_at[1])
            if gone < DWELL_REARM:
                return 0.0

        if self.armed_by_palm:
            # **Dwell stands down when the fingers are readable.** It exists
            # because a wrist cannot make a fist; where one can, holding still
            # is just holding still. Keeping both would mean a hand resting
            # while its owner reads the page clicks the page -- which is the
            # over-eager clicking that was reported before the fist came back.
            return 0.0

        held = now - self._dwell_since
        if self._armed and held >= DWELL_S:
            self._dwell_done = True
            self._clicked_at = (x, y)
            self._gesture_at = now
            self.gestures += 1
            self._deliver("click", {"x": x, "y": y})
            return None
        return min(1.0, held / DWELL_S)

    def _deliver(self, gesture: str, at: dict | None) -> None:
        if gesture == "hide":
            if not self._pointing:
                return
            self._pointing = False
        try:
            self._send(gesture, at)
        except Exception as exc:                        # noqa: BLE001
            log.debug("hand control could not send %s: %s", gesture, exc)
