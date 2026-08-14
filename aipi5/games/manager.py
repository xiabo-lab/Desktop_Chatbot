"""One object that knows a game is running, and everything that implies.

Sections 30, 31, 38 and 39 are all the same requirement seen from four sides:
a game takes things that belong to the rest of the assistant, and it must give
every one of them back — including when it ends in a way nobody planned. So
all four live here rather than spread across the game, the page and the API:

    the Brio          `PoseService` -> `CameraLease` -> `Camera.lend/reclaim`
    the accelerator   `PoseService` -> `HailoPose` -> `accelerator` refcount
    the music         `AudioPriority.acquire/release`, which restores exactly
                      what it paused and nothing it did not
    the screensaver   `ScreensaverManager.hold/release`

**The music is the one worth spelling out.** Section 38 says not to blindly
issue Play on exit if the music was already paused before the game began, and
this needs no special handling *because* it goes through `AudioPriority`: AIA's
`Ducker` records the players it actually paused and resumes exactly those. A
game started during silence resumes silence. Reaching for `playerctl` here
instead would have reintroduced a bug this project already fixed once.

**Every exit path is `stop()`.** Pressing Exit, navigating away, the camera
being unplugged, the page vanishing without a word, the assistant shutting
down. The last two are why `_watchdog` exists: a browser that is closed mid-game
posts nothing, and without a deadline the Brio would stay lent forever — the
same failure the video call had and fixed with a liveness poll.
"""

from __future__ import annotations

import logging
import threading
import time

from aipi5.games.fruit_ninja.game import HighScores, Session, State
from aipi5.motion.service import MotionUnavailable, PoseService

log = logging.getLogger(__name__)

#: Every game the library shows. `playable` is what makes the tile a Play
#: button rather than "Coming Soon" — section 19 wants the future ones visible
#: and inert, and one list here is what stops the page and the API disagreeing
#: about which those are.
CATALOGUE: tuple[dict, ...] = (
    {"id": "fruit-ninja", "name": "Fruit Ninja", "glyph": "🍉",
     "blurb": "Slice the fruit with your hands. Mind the bombs.",
     "playable": True},
    {"id": "yoga", "name": "Yoga Coach", "glyph": "🧘",
     "blurb": "Hold the pose. Get it checked.", "playable": False},
    {"id": "boxing", "name": "Boxing", "glyph": "🥊",
     "blurb": "Punch the pads as they appear.", "playable": False},
    {"id": "workout", "name": "Workout", "glyph": "🏋",
     "blurb": "Counted reps, called out loud.", "playable": False},
)

#: How long the page may go without asking for state before the game gives
#: everything back. Generous enough to survive a slow reload, short enough that
#: a closed browser does not hold the camera through the evening.
IDLE_TIMEOUT_S = 25.0

#: How often the watchdog looks.
WATCHDOG_S = 5.0


class GameError(RuntimeError):
    """A game could not start. The message is written to go on screen."""


class GameManager:
    """The only thing that starts and stops a game, and the only thing that
    holds the assistant's hardware on a game's behalf.

    One lock covers the session and the lifecycle flags. It is held across
    `tick`, which runs on the pose thread, and across the control methods,
    which run on HTTP threads — but never across `PoseService.start()` or
    `stop()`, which take seconds and take their own locks. See `_begin`.
    """

    def __init__(self, cfg, *, motion_cfg, camera, audio=None, screen=None,
                 on_change=lambda: None):
        self.cfg = cfg
        self.motion_cfg = motion_cfg
        self._camera = camera
        self._audio = audio
        self._screen = screen
        self._on_change = on_change

        self._lock = threading.RLock()
        self.scores = HighScores(cfg.scores)
        self.session: Session | None = None
        self.active: str = ""
        self._pose: PoseService | None = None
        self._held_audio = False
        self._held_screen = False
        self._last_seen = 0.0
        self._error = ""
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self.debug = bool(cfg.debug)

    # ── the library ──────────────────────────────────────────────────

    def catalogue(self) -> list[dict]:
        """Section 35's `GET /api/games`, with the best score on each tile."""
        games = []
        for entry in CATALOGUE:
            item = dict(entry)
            item["best"] = self.scores.best(entry["id"])
            item["active"] = (self.active == entry["id"])
            games.append(item)
        return games

    # ── starting and stopping ────────────────────────────────────────

    def open(self, game_id: str) -> dict:
        """Bring up the camera and the pose model for `game_id`.

        Opening is separate from starting a round on purpose: section 20 wants
        a start screen that shows a live "Player Detected" before any fruit is
        thrown, which means pose inference has to be running while the game is
        still not being played.
        """
        entry = next((g for g in CATALOGUE if g["id"] == game_id), None)
        if entry is None:
            raise GameError(f"there is no game called {game_id!r}")
        if not entry["playable"]:
            raise GameError(f"{entry['name']} is not built yet")

        with self._lock:
            if self.active == game_id and self._pose is not None:
                self._last_seen = time.monotonic()
                return self.status()
            if self.active and self.active != game_id:
                # Switching games without closing the first. Tear the old one
                # down completely rather than reusing its pose service — the
                # next game may want a different model.
                self._teardown()

        self._begin(game_id)
        return self.status()

    def _begin(self, game_id: str) -> None:
        """Acquire everything, outside the lock. Raises `GameError`.

        Outside the lock because `PoseService.start()` opens a camera and
        configures a 13 MB HEF, which together take on the order of a second,
        and holding the lock across it would block every status poll the page
        makes while it waits — which is exactly when the page most wants an
        answer.
        """
        pose = PoseService(self.motion_cfg, self._camera,
                           on_pose=self._on_pose_frame)
        try:
            pose.start()
        except MotionUnavailable as exc:
            with self._lock:
                self._error = str(exc)
            log.warning("Game: %s would not start — %s", game_id, exc)
            raise GameError(str(exc)) from exc

        with self._lock:
            self._pose = pose
            self.active = game_id
            self._error = ""
            self._last_seen = time.monotonic()
            self.session = Session(best=self.scores.best(game_id))

            # The screensaver first, because a player standing still while the
            # start screen tells them to raise their hands is exactly the
            # inactivity the idle timer is watching for. Section 39.
            if self._screen is not None and not self._held_screen:
                self._screen.hold("a game")
                self._held_screen = True
            # Then the floor. `AudioPriority` counts holders, so this nests
            # correctly with a turn already in progress rather than fighting
            # it, and it restores exactly what it paused — section 38.
            if self._audio is not None and not self._held_audio:
                self._audio.acquire()
                self._held_audio = True

        self._start_watchdog()
        log.info("Game: %s started", game_id)
        self._on_change()

    def close(self) -> dict:
        """Leave the game entirely. Section 30's six steps.

        Idempotent, and safe to call from anywhere — the page leaving, the
        assistant shutting down, the watchdog giving up on a browser that
        stopped talking.
        """
        with self._lock:
            if not self.active and self._pose is None:
                return self.status()
            game_id = self.active
        self._teardown()
        if game_id:
            log.info("Game: %s stopped", game_id)
        self._on_change()
        return self.status()

    def _teardown(self) -> None:
        """Give everything back. Never raises; order matters.

        The pose service goes first and outside the lock, because stopping it
        joins a thread that may be mid-inference and may be calling back into
        `_on_pose_frame`, which wants this lock. Taking the lock first is a
        deadlock, and it is the obvious way to write this.
        """
        self._watchdog_stop.set()
        with self._lock:
            pose, self._pose = self._pose, None
            # Captured before it is cleared. Recording the score afterwards
            # against `self.active` would file every game's high score under
            # the empty string, which is the sort of bug that only shows up as
            # "my best score keeps resetting".
            game_id, self.active = self.active, ""
            session = self.session

        if session is not None and session.state is State.PLAYING:
            # Ending mid-round still counts. A score somebody actually reached
            # should not be forgotten because they walked away rather than
            # losing their last life.
            session.finish(time.monotonic())

        if pose is not None:
            pose.stop()

        with self._lock:
            if session is not None and session.score and game_id:
                self.scores.record(game_id, session.score)
            if self._held_audio:
                self._held_audio = False
                if self._audio is not None:
                    self._audio.release()
            if self._held_screen:
                self._held_screen = False
                if self._screen is not None:
                    self._screen.release("a game")
            self.session = None

        thread, self._watchdog_thread = self._watchdog_thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ── playing ──────────────────────────────────────────────────────

    def command(self, action: str) -> dict:
        """start / pause / resume / restart, from the page. Section 35."""
        now = time.monotonic()
        with self._lock:
            self._last_seen = now
            session = self.session
            if session is None:
                raise GameError("no game is open")

            if action in ("start", "restart"):
                if not self._ready_to_start() and action == "start":
                    raise GameError("no player is in front of the camera")
                session.start(now)
                log.info("Game: round started")
            elif action == "pause":
                session.pause(now)
            elif action == "resume":
                session.resume(now)
            elif action == "end":
                session.finish(now)
                self.scores.record(self.active, session.score)
            else:
                raise GameError(f"{action!r} is not a game command")

        self._on_change()
        return self.status()

    def _ready_to_start(self) -> bool:
        """Section 20: START stays disabled until a player is really there."""
        pose = self._pose
        if pose is None:
            return False
        snapshot = pose.snapshot()
        return bool(snapshot and snapshot.ready)

    def _on_pose_frame(self, snapshot, frame) -> None:
        """Called from the pose thread, once per frame. Must not block.

        This is where collision happens — at the moment the pose arrives,
        rather than after a round trip through the browser. Every millisecond
        spent in here is a millisecond the pose loop is not reading the next
        camera frame, so it does exactly two things.
        """
        with self._lock:
            session = self.session
            if session is None:
                return
            session.tick(time.monotonic(), snapshot.hands.values())
            if session.state is State.OVER and session.score:
                self.scores.record(self.active, session.score)

    # ── what the page reads ──────────────────────────────────────────

    def status(self, seen: bool = True) -> dict:
        """The whole game state, for `GET /api/game/status`.

        Also the liveness signal: a page that is polling this is a page that
        still has somebody in front of it, which is what keeps the watchdog
        from taking the camera back.
        """
        now = time.monotonic()
        with self._lock:
            if seen:
                self._last_seen = now
            pose = self._pose
            session = self.session

            payload = {
                "active": self.active,
                "error": self._error,
                "debug": self.debug,
                "games": self.catalogue(),
            }
            if session is not None:
                payload["game"] = session.snapshot(now)
                payload["sounds"] = session.take_events()
            if pose is not None:
                snapshot = pose.snapshot()
                payload["pose"] = snapshot.as_dict() if snapshot else None
                payload["motion"] = {
                    "running": pose.running,
                    "error": pose.error,
                    "stats": pose.stats.as_dict(),
                }
                if self.debug:
                    # Only in debug mode, and only then: the full skeleton is
                    # seventeen points per person per poll, which is a lot of
                    # JSON to send thirty times a second to draw nothing.
                    last = pose.last_frame()
                    payload["skeleton"] = last.as_dict() if last else None
            return payload

    def preview_jpeg(self):
        """One camera frame from the running game, or None. See section 20."""
        with self._lock:
            pose = self._pose
        return pose.preview_jpeg() if pose is not None else None

    def hardware(self) -> dict:
        """Section 44's panel. Proof the AI HAT+ 2 is what is being used."""
        with self._lock:
            pose = self._pose
        if pose is not None:
            return pose.describe()

        # Nothing is running, so answer from the device itself rather than
        # from a service that does not exist. This is what the settings page
        # shows when nobody is playing.
        from aipi5.core import accelerator
        return {"running": False, "error": self._error,
                "accelerator": accelerator.identify(),
                "pose": {"model": self.motion_cfg.pose_model.name},
                "camera": {}, "stats": {}}

    def set_debug(self, on: bool) -> None:
        with self._lock:
            self.debug = bool(on)
        log.info("Game: debug overlay %s", "on" if on else "off")

    # ── the watchdog ─────────────────────────────────────────────────

    def _start_watchdog(self) -> None:
        self._watchdog_stop.clear()
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="aipi5-game-watchdog", daemon=True)
        self._watchdog_thread.start()

    def _watchdog(self) -> None:
        """Give the hardware back if the page stops talking to us.

        A kiosk browser that crashed, a page reloaded into a different view, a
        Chromium killed by a restart — none of them post anything, and without
        this the Brio stays lent and the assistant has no camera until somebody
        notices. The video call learned this the same way, from a phone that
        vanished without hanging up.

        Also the place a camera unplugged mid-game is noticed, because the pose
        loop ends itself when that happens and nothing else is watching.
        """
        while not self._watchdog_stop.wait(WATCHDOG_S):
            with self._lock:
                if not self.active:
                    return
                idle = time.monotonic() - self._last_seen
                pose = self._pose
                lost = pose.error if pose is not None else ""
                dead = pose is not None and not pose.running

            if lost or dead:
                log.warning("Game: ending — %s", lost or "the pose loop stopped")
                with self._lock:
                    self._error = lost or "the pose loop stopped"
                    session = self.session
                    if session is not None:
                        session.pause(time.monotonic())
                # Deliberately not a full teardown: section 43 wants the game
                # paused with a message and a Retry, not thrown away. The
                # camera is already gone; the model is cheap to hold for the
                # moment somebody takes to decide.
                self._on_change()
                return

            if idle > IDLE_TIMEOUT_S:
                log.info("Game: nothing has asked for state in %.0fs — "
                         "giving the camera back", idle)
                self.close()
                return
