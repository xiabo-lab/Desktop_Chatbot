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

import json
import logging
import threading
import time

from aipi5.games.boxing.game import BoxingSession
from aipi5.games.fruit_ninja.game import HighScores, Session as FruitSession, State
from aipi5.motion.gestures import CrossedArmsGesture
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
     "blurb": "Train your reactions or fight an adaptive opponent.",
     "playable": True},
    {"id": "workout", "name": "Workout", "glyph": "🏋",
     "blurb": "Counted reps, called out loud.", "playable": False},
)

#: How long the page may go without asking for state before the game gives
#: everything back. Generous enough to survive a slow reload, short enough that
#: a closed browser does not hold the camera through the evening.
IDLE_TIMEOUT_S = 25.0

#: How long `voice_start` waits for the camera to find the player before giving
#: up. This is the whole reason the voice command exists, so refusing instantly
#: would defeat it: somebody who has just said "start game" is by definition
#: standing in front of the camera, but the pose service may have been running
#: for a fraction of a second when the utterance finishes — opening a game
#: costs about 1.5 s for the camera and the HEF, and the model then needs a
#: frame or two to see anybody.
#:
#: Four seconds because the wait only begins *after* `open()` has returned, so
#: it is bounded against detection rather than against startup, and because it
#: blocks the voice loop — see `voice_start`.
PLAYER_WAIT_S = 4.0

#: How often to look, while waiting. Two pose frames at 30 fps.
PLAYER_POLL_S = 0.066

#: How often the watchdog looks.
WATCHDOG_S = 5.0

# The complete set the touchscreen offers. Keeping the allow-list beside the
# decision means a crafted request cannot create a zero-second round or one
# that holds the camera all afternoon.
ROUND_SECONDS_OPTIONS: tuple[int, ...] = (60, 120, 180, 240, 300)
DEFAULT_ROUND_SECONDS = 120


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
        self.session: FruitSession | BoxingSession | None = None
        self.active: str = ""
        self._pose: PoseService | None = None
        self._held_audio = False
        self._held_screen = False
        self._last_seen = 0.0
        self._error = ""
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self.debug = bool(cfg.debug)
        # Tests commonly override only the score path; deriving the preference
        # path from it keeps those tests inside their temporary directory.
        self.settings_path = (getattr(cfg, "settings", None)
                              or cfg.scores.with_name("game-settings.json"))
        self.round_seconds = self._load_round_seconds(
            getattr(cfg, "round_seconds", DEFAULT_ROUND_SECONDS))
        self._start_gesture = CrossedArmsGesture(
            confidence=min(0.4, motion_cfg.keypoint_confidence))

    # ── the library ──────────────────────────────────────────────────

    def catalogue(self) -> list[dict]:
        """Section 35's `GET /api/games`, with the best score on each tile."""
        games = []
        for entry in CATALOGUE:
            item = dict(entry)
            if entry["id"] == "boxing":
                item["best"] = max(
                    self.scores.best(f"boxing:{mode}:{difficulty}")
                    for mode in ("training", "fight")
                    for difficulty in ("easy", "normal", "hard"))
            else:
                item["best"] = self.scores.best(self._score_key(entry["id"]))
            item["active"] = (self.active == entry["id"])
            if entry["id"] == "fruit-ninja":
                item["round_seconds"] = self.round_seconds
            games.append(item)
        return games

    def _load_round_seconds(self, fallback) -> int:
        try:
            configured = int(fallback)
        except (TypeError, ValueError):
            configured = DEFAULT_ROUND_SECONDS
        if configured not in ROUND_SECONDS_OPTIONS:
            log.warning("games.round_seconds=%r is not one of %s; using %d",
                        fallback, ROUND_SECONDS_OPTIONS, DEFAULT_ROUND_SECONDS)
            configured = DEFAULT_ROUND_SECONDS

        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            seconds = int(saved.get("round_seconds", configured))
            if seconds not in ROUND_SECONDS_OPTIONS:
                raise ValueError(f"unsupported duration {seconds}")
            return seconds
        except FileNotFoundError:
            return configured
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            log.warning("could not read the game settings at %s: %s",
                        self.settings_path, exc)
            return configured

    def _score_key(self, game_id: str, seconds: int | float | None = None) -> str:
        """A fair high-score table for each round length.

        The original 120-second table keeps its original key, so every score
        already on the deployed device remains visible after this upgrade.
        """
        duration = int(self.round_seconds if seconds is None else seconds)
        return game_id if duration == DEFAULT_ROUND_SECONDS else f"{game_id}:{duration}"

    def _session_score_key(self, game_id: str, session=None) -> str:
        """Keep records comparable within a game mode and difficulty."""
        if game_id == "boxing" and isinstance(session, BoxingSession):
            if not session.mode:
                return "boxing"
            return f"boxing:{session.mode}:{session.difficulty}"
        seconds = getattr(session, "duration", None)
        return self._score_key(game_id, seconds)

    def settings(self) -> dict:
        """The touchscreen-selectable game settings and per-length records."""
        with self._lock:
            selected = self.round_seconds
            active = bool(self.active)
        return {
            "round_seconds": selected,
            "choices": [
                {"seconds": seconds,
                 "best": self.scores.best(self._score_key("fruit-ninja", seconds))}
                for seconds in ROUND_SECONDS_OPTIONS
            ],
            "locked": active,
        }

    def set_round_seconds(self, value) -> dict:
        """Persist one allowed duration, for the next Fruit Ninja round."""
        try:
            seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise GameError("choose 60, 120, 180, 240 or 300 seconds") from exc
        if seconds not in ROUND_SECONDS_OPTIONS:
            raise GameError("choose 60, 120, 180, 240 or 300 seconds")

        with self._lock:
            if self.active:
                raise GameError("leave the current game before changing its play time")
            self.round_seconds = seconds

        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(self.settings_path.suffix + ".tmp")
            temporary.write_text(json.dumps({"round_seconds": seconds}, indent=2),
                                 encoding="utf-8")
            temporary.replace(self.settings_path)
        except OSError as exc:
            log.warning("could not save the game settings at %s: %s",
                        self.settings_path, exc)
        log.info("Game: Fruit Ninja round set to %d seconds", seconds)
        self._on_change()
        return self.settings()

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
            if game_id == "boxing":
                self.session = BoxingSession(
                    best=max(self.scores.best(f"boxing:{mode}:{difficulty}")
                             for mode in ("training", "fight")
                             for difficulty in ("easy", "normal", "hard")))
            else:
                duration = float(self.round_seconds)
                self.session = FruitSession(
                    duration=duration,
                    time_left=duration,
                    best=self.scores.best(self._score_key(game_id, duration)))
            self._start_gesture.reset()

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
                self.scores.record(self._session_score_key(game_id, session),
                                   session.score)
            if self._held_audio:
                self._held_audio = False
                if self._audio is not None:
                    self._audio.release()
            if self._held_screen:
                self._held_screen = False
                if self._screen is not None:
                    self._screen.release("a game")
            self.session = None
            self._start_gesture.reset()

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

            if action.startswith("mode-"):
                if not isinstance(session, BoxingSession):
                    raise GameError("mode selection is only available in Boxing")
                try:
                    session.select_mode(action.removeprefix("mode-"))
                except ValueError as exc:
                    raise GameError(str(exc)) from exc
                self._start_gesture.reset()
                log.info("Game: Boxing mode set to %s", session.mode)
            elif action.startswith("difficulty-"):
                if not isinstance(session, BoxingSession):
                    raise GameError("difficulty is only available in Boxing")
                try:
                    session.select_difficulty(action.removeprefix("difficulty-"))
                except ValueError as exc:
                    raise GameError(str(exc)) from exc
                log.info("Game: Boxing difficulty set to %s", session.difficulty)
            elif action in ("start", "restart"):
                if not self._ready_to_start() and action == "start":
                    raise GameError("no player is in front of the camera")
                # Bank the finished round before `start` zeroes it. The pose
                # loop records a game over on the frame it happens, so this is
                # normally redundant — but "normally" depends on a pose frame
                # having arrived between the last life being lost and Play
                # Again being pressed, and there is no rule that says one has.
                # A high score that survives only if the timing is right is a
                # high score somebody will one day watch disappear.
                if session.state is State.OVER and session.score:
                    self.scores.record(
                        self._session_score_key(self.active, session), session.score)
                try:
                    session.start(now)
                except ValueError as exc:
                    raise GameError(str(exc)) from exc
                log.info("Game: round started")
            elif action == "pause":
                session.pause(now)
            elif action == "resume":
                session.resume(now)
            elif action == "end":
                session.finish(now)
                self.scores.record(self._session_score_key(self.active, session),
                                   session.score)
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

    # ── starting by voice ────────────────────────────────────────────
    #
    # The touchscreen is the wrong input for this game and always was: the
    # player has to stand far enough back for the camera to see their whole
    # upper body, which is well out of arm's reach of a 1280x800 panel. So
    # START is reachable by touch only for somebody who then has to walk
    # backwards into shot before the first fruit arrives.
    #
    # This is the same lifecycle as the buttons — `open` then `command` — with
    # two differences that only matter because nobody is standing at the
    # screen: the game is opened if it is not already, and the player-detected
    # check *waits* instead of refusing.

    def default_game(self) -> str:
        """The game a bare "start game" means: the first playable one."""
        with self._lock:
            if self.active:
                return self.active
        return next((g["id"] for g in CATALOGUE if g["playable"]), "")

    @staticmethod
    def name_of(game_id: str) -> str:
        entry = next((g for g in CATALOGUE if g["id"] == game_id), None)
        return entry["name"] if entry else game_id

    def wait_for_player(self, timeout: float = PLAYER_WAIT_S) -> bool:
        """Block until the camera can see somebody, or `timeout` passes.

        Polled rather than driven by a condition variable set from the pose
        thread, and deliberately: the pose loop's one job is to keep up with
        the camera, and putting a notify on its critical path to save a few
        microseconds of polling here would be the wrong trade in the wrong
        place. Two pose frames per poll is cheap and the wait is bounded.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_to_start():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(PLAYER_POLL_S)

    def voice_start(self, game_id: str = "", timeout: float = PLAYER_WAIT_S,
                    fresh: bool = False) -> tuple[str, str]:
        """Start a game because somebody asked out loud.

        Returns `(outcome, detail)` rather than speech, so that what happens
        and what is said about it stay separate — the phrasing is the voice
        plugin's business and the two languages live there. Outcomes:

            started         a round is now running
            resumed         a paused round was resumed
            already         a round was already in progress; nothing changed
            no-player       the game is open but the camera cannot see anybody
            unavailable     the camera or the accelerator refused; detail says
            no-games        nothing playable is configured

        **`fresh` is the difference between "start game" and "play again".**
        Without it, a round already under way is left alone, because "start
        game" said mid-game is ambiguous and the harmless reading is the right
        one — nobody means "throw my score away". "Again" is not ambiguous: it
        is a deliberate request to begin over, and refusing it would leave the
        player with no way to restart from where they are standing.

        **Blocks for up to `timeout` plus however long opening takes**, on the
        voice loop's thread. That is the same trade `KodamaLauncher.open` makes
        for the same reason: the alternative is answering "starting" before
        anything has, and then failing silently where nobody is standing close
        enough to read the screen.
        """
        target = game_id or self.default_game()
        if not target:
            return ("no-games", "")

        try:
            # Idempotent when the game is already open, which is the common
            # case — somebody walked to the screen, tapped Fruit Ninja, and
            # walked back into shot.
            self.open(target)
        except GameError as exc:
            return ("unavailable", str(exc))

        name = self.name_of(target)

        with self._lock:
            session = self.session
            state = session.state if session is not None else None

        # A round already under way is not restarted — unless "again" was the
        # word. Somebody mid-game saying "start game" has not asked to throw
        # their score away, and doing it would be the most annoying possible
        # reading of an ambiguous phrase.
        if not fresh:
            if state is State.PLAYING:
                return ("already", name)

            if state is State.PAUSED:
                try:
                    self.command("resume")
                except GameError as exc:
                    return ("unavailable", str(exc))
                return ("resumed", name)

        if not self.wait_for_player(timeout):
            return ("no-player", name)

        # `restart` rather than `start` once a session exists in any state but
        # READY: same thing, except that it is the verb `Session` uses for Play
        # Again, and it skips the player check that `wait_for_player` has just
        # satisfied. For a fresh session the two are identical — `Session.start`
        # resets either way.
        again = fresh or state is State.OVER
        try:
            self.command("restart" if again else "start")
        except GameError:
            # The player stepped out between the wait and the command. Rare,
            # and reported honestly rather than retried into a loop.
            return ("no-player", name)
        return ("started", name)

    def _on_pose_frame(self, snapshot, frame) -> None:
        """Called from the pose thread, once per frame. Must not block.

        This is where collision happens — at the moment the pose arrives,
        rather than after a round trip through the browser. Every millisecond
        spent in here is a millisecond the pose loop is not reading the next
        camera frame, so it does exactly two things.
        """
        changed = False
        with self._lock:
            session = self.session
            if session is None:
                return
            now = time.monotonic()
            if isinstance(session, BoxingSession):
                session.tick_pose(now, snapshot.person, snapshot.hands,
                                  snapshot.timestamp)
            else:
                session.tick(now, snapshot.hands.values())
            # `self.active` is checked, not assumed. `_teardown` clears it
            # under this lock and then stops the pose service *outside* it, so
            # a frame already in flight arrives here with a live session and no
            # game name — and recorded the score under the empty string. Seen
            # in the wild: a scores file with both `"fruit-ninja": 283` and
            # `"": 283` in it.
            if self.active and session.state is State.OVER and session.score:
                self.scores.record(self._session_score_key(self.active, session),
                                   session.score)

            phase = getattr(session, "gesture_phase", session.state)
            if self._start_gesture.update(snapshot.person, phase, now):
                if session.state is State.OVER and session.score:
                    self.scores.record(
                        self._session_score_key(self.active, session), session.score)
                action = "Play Again" if session.state is State.OVER else "Start"
                session.start(now)
                self._last_seen = now
                changed = True
                log.info("Game: %s triggered by crossed arms", action)

        if changed:
            self._on_change()

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
                payload["gesture"] = self._start_gesture.describe(now)
                # `events` rather than the `sounds` this used to be called.
                # They were only ever sounds when they were bare strings; each
                # one now carries where it happened and what colour it was,
                # and the page draws far more from them than it plays.
                payload["events"] = session.take_events()
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

    def brief(self) -> dict | None:
        """The two fields `/api/state` carries, or None when nothing is open.

        Deliberately tiny. `/api/state` is polled twice a second by the page
        at all times, on every page, and the whole game snapshot — every fruit
        in flight, every slash — belongs on `/api/game/stream`, which only the
        game page opens. What the poll needs to carry is just enough for a
        page that is *not* on the game to notice that it should be: the same
        job `call` does when the phone rings.
        """
        with self._lock:
            if not self.active:
                return None
            session = self.session
            return {"active": self.active,
                    "state": session.state.value if session else "ready"}

    def preview_jpeg(self):
        """One camera frame from the running game, or None. See section 20."""
        with self._lock:
            pose = self._pose
        return pose.preview_jpeg() if pose is not None else None

    def hardware(self) -> dict:
        """Section 44's panel. Proof the AI HAT+ 2 is what is being used."""
        with self._lock:
            pose = self._pose
            debug = self.debug
        if pose is not None:
            detail = pose.describe()
        else:
            # Nothing is running, so answer from the device itself rather than
            # from a service that does not exist. This is what the settings
            # page shows when nobody is playing.
            from aipi5.core import accelerator
            detail = {"running": False, "error": self._error,
                      "accelerator": accelerator.identify(),
                      "pose": {"model": self.motion_cfg.pose_model.name},
                      "camera": {}, "stats": {}}
        # So the Settings page's toggle can show what the *game* will do rather
        # than what that page last asked for — different things if the
        # assistant restarted in between.
        detail["debug"] = debug
        return detail

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
