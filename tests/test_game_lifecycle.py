"""Camera ownership and the start/exit cycle — section 46's last three.

Section 46 asks that starting and stopping a game correctly acquire and
release the Brio, that repeated start/exit leaks nothing, and that Hailo
initialisation is confirmed. The first two are tested here against fakes,
because the property being checked is *who was asked for what, in what order*,
and that is a property of `GameManager` rather than of a camera.

The third — that the accelerator is really being used — cannot be honestly
faked, so what is tested here is the part that can be: that a missing
accelerator produces a refusal and no held hardware, rather than a quiet
fallback to something slower. The positive case is measured on the Pi, and
`scripts/probe_pose.py` is what does it.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aipi5.core.config import GamesConfig, MotionConfig
from aipi5.games import manager as manager_mod
from aipi5.games.manager import GameError, GameManager
from aipi5.motion.pose_types import (KEYPOINT_NAMES, KeyPoint, PersonPose,
                                     PoseStats)
from aipi5.motion.service import MotionUnavailable


class FakeCamera:
    """`Camera`'s lend/reclaim contract and nothing else.

    Records the sequence, because the ordering is what the real failures were
    about: a camera reclaimed before the reader stopped, or lent twice and
    given back once.
    """

    def __init__(self):
        self.calls: list[str] = []
        self.lent_to = ""
        self.running = True

    def lend(self, to: str = "a call") -> bool:
        self.calls.append(f"lend:{to}")
        if self.lent_to:
            return True          # idempotent, as the real one is
        self.lent_to = to
        self.running = False
        return True

    def reclaim(self) -> bool:
        self.calls.append("reclaim")
        self.lent_to = ""
        self.running = True
        return True

    @property
    def lent(self) -> bool:
        return bool(self.lent_to)

    def describe(self) -> dict:
        return {"device": "/dev/video0", "name": "Logitech BRIO",
                "running": self.running, "lent_to": self.lent_to or None}


class FakeAudio:
    """`AudioPriority`'s counting contract."""

    def __init__(self):
        self.depth = 0
        self.max_depth = 0
        self.calls: list[str] = []

    def acquire(self) -> None:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.calls.append("acquire")

    def release(self) -> None:
        self.depth -= 1
        self.calls.append("release")


class FakeScreen:
    """`ScreensaverManager`'s hold/release contract."""

    def __init__(self):
        self.held: set[str] = set()
        self.calls: list[str] = []

    def hold(self, why: str) -> None:
        self.held.add(why)
        self.calls.append(f"hold:{why}")

    def release(self, why: str = "") -> None:
        self.held.discard(why)
        self.calls.append(f"release:{why}")


class FakePose:
    """Stands in for `PoseService`. Started and stopped, never inferring."""

    instances: list["FakePose"] = []
    fail_with: str = ""

    def __init__(self, cfg, camera, on_pose=None):
        self.cfg, self.camera, self.on_pose = cfg, camera, on_pose
        self.started = self.stopped = 0
        self.error = ""
        # A real `PoseStats`, not a stub: `GameManager.status` calls
        # `as_dict()` on it every poll, and a fake that only implements what
        # the tests happen to touch would let a change to that payload pass
        # here and fail on the device.
        self.stats = PoseStats()
        self._running = False
        FakePose.instances.append(self)

    def start(self) -> None:
        if FakePose.fail_with:
            raise MotionUnavailable(FakePose.fail_with)
        # The real one lends the camera through `CameraLease`; this stands in
        # for that, because the ordering against audio and the screensaver is
        # what these tests are about.
        self.camera.lend("an AI Motion game")
        self.started += 1
        self._running = True

    def stop(self) -> None:
        if self._running:
            self.camera.reclaim()
        self._running = False
        self.stopped += 1

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self):
        return None

    def last_frame(self):
        return None

    def preview_jpeg(self):
        return None

    def describe(self) -> dict:
        return {"running": self._running, "error": "", "stats": {},
                "accelerator": {"available": True, "architecture": "HAILO10H"},
                "pose": {}, "camera": {}}


class GameManagerCase(unittest.TestCase):
    """Shared fixture: a manager wired to fakes, with no threads of its own."""

    def setUp(self):
        FakePose.instances = []
        FakePose.fail_with = ""
        self._patched = manager_mod.PoseService
        manager_mod.PoseService = FakePose
        # The watchdog is a real thread in the real thing and would take the
        # camera back mid-test. Disabled by pushing the timeout out of reach
        # rather than by stubbing the thread, so the thread itself is still
        # started and joined — which is the part that could leak.
        self._timeout = manager_mod.IDLE_TIMEOUT_S
        manager_mod.IDLE_TIMEOUT_S = 10_000

        self._dir = TemporaryDirectory()
        self.camera = FakeCamera()
        self.audio = FakeAudio()
        self.screen = FakeScreen()
        self.manager = GameManager(
            GamesConfig(scores=Path(self._dir.name) / "scores.json"),
            motion_cfg=MotionConfig(),
            camera=self.camera, audio=self.audio, screen=self.screen)

    def tearDown(self):
        self.manager.close()
        manager_mod.PoseService = self._patched
        manager_mod.IDLE_TIMEOUT_S = self._timeout
        self._dir.cleanup()


class TestCameraOwnership(GameManagerCase):
    """Section 46: starting/stopping must acquire and release the Brio."""

    def test_opening_a_game_takes_the_camera(self):
        self.manager.open("fruit-ninja")
        self.assertTrue(self.camera.lent)
        self.assertEqual(self.camera.lent_to, "an AI Motion game")

    def test_boxing_uses_the_same_single_pose_and_camera_lease(self):
        self.manager.open("boxing")
        self.assertTrue(self.camera.lent)
        self.assertEqual(self.camera.lent_to, "an AI Motion game")
        self.assertEqual(len(FakePose.instances), 1)
        self.manager.close()
        self.assertFalse(self.camera.lent)

    def test_closing_gives_it_back(self):
        self.manager.open("fruit-ninja")
        self.manager.close()
        self.assertFalse(self.camera.lent)
        self.assertTrue(self.camera.running)

    def test_the_camera_is_given_back_exactly_once(self):
        self.manager.open("fruit-ninja")
        self.manager.close()
        self.assertEqual(self.camera.calls.count("reclaim"), 1)

    def test_closing_twice_does_not_reclaim_twice(self):
        """An extra reclaim would take the camera off whoever has it next."""
        self.manager.open("fruit-ninja")
        self.manager.close()
        self.manager.close()
        self.assertEqual(self.camera.calls.count("reclaim"), 1)

    def test_closing_a_game_that_never_opened_does_nothing(self):
        self.manager.close()
        self.assertEqual(self.camera.calls, [])

    def test_a_game_that_fails_to_start_holds_nothing(self):
        """Section 41: a refusal must not cost the assistant its camera."""
        FakePose.fail_with = "the Logitech Brio is being used by another feature"
        with self.assertRaises(GameError):
            self.manager.open("fruit-ninja")

        self.assertFalse(self.camera.lent)
        self.assertEqual(self.audio.depth, 0)
        self.assertEqual(self.screen.held, set())
        self.assertEqual(self.manager.active, "")

    def test_the_camera_is_only_taken_once_when_reopening_the_same_game(self):
        self.manager.open("fruit-ninja")
        self.manager.open("fruit-ninja")
        self.assertEqual(len(FakePose.instances), 1)


class TestAudioAndScreensaver(GameManagerCase):
    """Sections 38 and 39."""

    def test_a_game_holds_the_audio_floor(self):
        self.manager.open("fruit-ninja")
        self.assertEqual(self.audio.depth, 1)

    def test_and_gives_it_back(self):
        self.manager.open("fruit-ninja")
        self.manager.close()
        self.assertEqual(self.audio.depth, 0)

    def test_the_floor_is_taken_once_however_many_times_open_is_called(self):
        """A second acquire would leave the music paused after one release."""
        self.manager.open("fruit-ninja")
        self.manager.open("fruit-ninja")
        self.manager.open("fruit-ninja")
        self.assertEqual(self.audio.max_depth, 1)

    def test_a_game_holds_the_screensaver_off(self):
        """Section 39: a player is moving, not touching."""
        self.manager.open("fruit-ninja")
        self.assertIn("a game", self.screen.held)

    def test_and_lets_it_go(self):
        self.manager.open("fruit-ninja")
        self.manager.close()
        self.assertEqual(self.screen.held, set())

    def test_the_screensaver_is_held_before_the_floor_is_taken(self):
        """A player told to raise their hands is standing still, so the idle
        timer has to be stopped before anything slower happens."""
        self.manager.open("fruit-ninja")
        self.assertEqual(self.screen.calls[0], "hold:a game")


class TestRepeatedLifecycle(GameManagerCase):
    """Section 46: start, exit, start, exit, start, exit — no leaks."""

    def test_ten_cycles_leave_nothing_held(self):
        for _ in range(10):
            self.manager.open("fruit-ninja")
            self.manager.close()

        self.assertFalse(self.camera.lent)
        self.assertEqual(self.audio.depth, 0)
        self.assertEqual(self.screen.held, set())
        self.assertEqual(self.manager.active, "")
        self.assertIsNone(self.manager.session)

    def test_every_pose_service_that_was_started_was_stopped(self):
        for _ in range(10):
            self.manager.open("fruit-ninja")
            self.manager.close()

        self.assertEqual(len(FakePose.instances), 10)
        for pose in FakePose.instances:
            self.assertEqual(pose.started, 1)
            self.assertEqual(pose.stopped, 1)
            self.assertFalse(pose.running)

    def test_lends_and_reclaims_are_balanced(self):
        for _ in range(10):
            self.manager.open("fruit-ninja")
            self.manager.close()
        lends = sum(1 for call in self.camera.calls if call.startswith("lend"))
        self.assertEqual(lends, 10)
        self.assertEqual(self.camera.calls.count("reclaim"), 10)

    def test_no_threads_are_left_behind(self):
        before = {t.name for t in threading.enumerate()}
        for _ in range(6):
            self.manager.open("fruit-ninja")
            self.manager.close()
        # The watchdog is joined by `close`, so nothing of ours should remain.
        time.sleep(0.05)
        after = {t.name for t in threading.enumerate()}
        self.assertEqual(after - before, set())


class TestCatalogueAndCommands(GameManagerCase):

    def test_the_catalogue_lists_four_games(self):
        self.assertEqual(len(self.manager.catalogue()), 4)

    def test_three_of_the_four_games_are_playable(self):
        playable = [g["id"] for g in self.manager.catalogue() if g["playable"]]
        self.assertEqual(playable, ["fruit-ninja", "yoga", "boxing"])

    def test_boxing_requires_and_accepts_mode_selection(self):
        self.manager.open("boxing")
        self.assertEqual(self.manager.status()["game"]["mode"], "")
        self.manager.command("mode-training")
        status = self.manager.status()
        self.assertEqual(status["game"]["mode"], "training")
        self.assertEqual(status["game"]["duration"], 90.0)

    def test_boxing_difficulty_is_configuration_not_a_second_game(self):
        self.manager.open("boxing")
        self.manager.command("difficulty-hard")
        self.manager.command("mode-fight")
        self.manager.command("restart")
        self.assertEqual(self.manager.status()["game"]["difficulty"], "hard")

    def test_yoga_requires_and_accepts_a_level(self):
        self.manager.open("yoga")
        status = self.manager.status()
        self.assertEqual(status["game"]["kind"], "yoga")
        self.assertEqual(status["game"]["difficulty"], "")
        # No level chosen means the crossed-arms gesture must stay inert, the
        # same contract Boxing's mode sheet has.
        self.assertEqual(self.manager.session.gesture_phase, "select")

        self.manager.command("difficulty-advanced")
        status = self.manager.status()
        self.assertEqual(status["game"]["difficulty"], "advanced")
        self.assertEqual(status["game"]["duration"], 1200.0)
        self.assertEqual(self.manager.session.gesture_phase, "ready")

    def test_an_unknown_yoga_level_is_refused(self):
        self.manager.open("yoga")
        with self.assertRaises(GameError):
            self.manager.command("difficulty-expert")

    def test_yoga_courses_keep_separate_records(self):
        """Scores from two different authored courses are not competitors."""
        self.manager.open("yoga")
        self.manager.command("course-beginner_03")
        session = self.manager.session
        self.assertEqual(
            self.manager._session_score_key("yoga", session), "yoga:beginner_03")
        self.manager.command("course-advanced_06")
        self.assertEqual(
            self.manager._session_score_key("yoga", session), "yoga:advanced_06")

    def test_yoga_uses_the_same_single_pose_and_camera_lease(self):
        self.manager.open("yoga")
        self.assertTrue(self.camera.lent)
        self.assertEqual(len(FakePose.instances), 1)
        self.manager.close()
        self.assertFalse(self.camera.lent)

    def test_switching_between_the_three_games_leaks_nothing(self):
        for game in ("fruit-ninja", "yoga", "boxing", "yoga", "fruit-ninja"):
            self.manager.open(game)
            self.assertTrue(self.camera.lent)
        self.manager.close()
        self.assertFalse(self.camera.lent)
        self.assertEqual(self.camera.calls.count("reclaim"),
                         self.camera.calls.count("lend:an AI Motion game"))

    def test_a_coming_soon_game_refuses_to_open(self):
        with self.assertRaises(GameError) as caught:
            self.manager.open("workout")
        self.assertIn("not built yet", str(caught.exception))
        self.assertFalse(self.camera.lent)

    def test_an_unknown_game_refuses_to_open(self):
        with self.assertRaises(GameError):
            self.manager.open("pinball")
        self.assertFalse(self.camera.lent)

    def test_commands_need_an_open_game(self):
        with self.assertRaises(GameError):
            self.manager.command("start")

    def test_start_is_refused_with_no_player(self):
        """Section 20: START must not work without somebody in front of it.

        `FakePose.snapshot` returns None, which is what the real one does
        before the first frame — so this is also the "pressed it too fast"
        case.
        """
        self.manager.open("fruit-ninja")
        with self.assertRaises(GameError) as caught:
            self.manager.command("start")
        self.assertIn("no player", str(caught.exception))

    def test_restart_works_without_a_player(self):
        """Play Again after a game over should not need re-detection.

        The player is standing right there — they just finished a game — and
        making them re-qualify to press a button on a screen they are looking
        at would be a worse experience than the check is worth.
        """
        self.manager.open("fruit-ninja")
        self.manager.command("restart")
        self.assertEqual(self.manager.session.state.value, "playing")

    def test_crossed_arms_automatically_start_the_round(self):
        points = {name: KeyPoint(0.5, 0.5, 0.0)
                  for name in KEYPOINT_NAMES}
        for name, value in {
            "left_shoulder": (0.35, 0.30, 0.9),
            "right_shoulder": (0.65, 0.30, 0.9),
            "left_elbow": (0.27, 0.53, 0.9),
            "right_elbow": (0.73, 0.53, 0.9),
            "left_wrist": (0.62, 0.39, 0.9),
            "right_wrist": (0.38, 0.39, 0.9),
        }.items():
            points[name] = KeyPoint(*value)
        snapshot = SimpleNamespace(
            person=PersonPose(confidence=0.9, keypoints=points), hands={})

        self.manager.open("fruit-ninja")
        self.manager._start_gesture.hold_s = 0.0
        self.manager._on_pose_frame(snapshot, None)
        self.manager._on_pose_frame(snapshot, None)

        self.assertEqual(self.manager.session.state.value, "playing")

    def test_status_describes_the_crossed_arms_control(self):
        self.manager.open("fruit-ninja")
        gesture = self.manager.status()["gesture"]
        self.assertEqual(gesture["name"], "arms-crossed-x")
        self.assertEqual(gesture["hold_ms"], 650)

    def test_an_unknown_command_is_refused(self):
        self.manager.open("fruit-ninja")
        with self.assertRaises(GameError):
            self.manager.command("juggle")

    def test_high_scores_survive_a_close(self):
        self.manager.open("fruit-ninja")
        self.manager.command("restart")
        self.manager.session.score = 240
        self.manager.close()
        self.assertEqual(self.manager.scores.best("fruit-ninja"), 240)

    def test_the_status_payload_names_the_active_game(self):
        self.manager.open("fruit-ninja")
        status = self.manager.status()
        self.assertEqual(status["active"], "fruit-ninja")
        self.assertIn("game", status)
        self.assertIn("games", status)


class TestRoundDuration(GameManagerCase):

    def test_all_five_play_times_are_available(self):
        settings = self.manager.settings()
        self.assertEqual(settings["round_seconds"], 120)
        self.assertEqual([item["seconds"] for item in settings["choices"]],
                         [60, 120, 180, 240, 300])

    def test_selection_is_saved_and_restored(self):
        self.manager.set_round_seconds(300)
        restored = GameManager(
            GamesConfig(scores=Path(self._dir.name) / "scores.json"),
            motion_cfg=MotionConfig(), camera=FakeCamera(),
            audio=FakeAudio(), screen=FakeScreen())
        self.addCleanup(restored.close)
        self.assertEqual(restored.round_seconds, 300)

    def test_invalid_play_time_is_refused(self):
        for invalid in (0, 121, 301, "forever", None):
            with self.subTest(invalid=invalid), self.assertRaises(GameError):
                self.manager.set_round_seconds(invalid)

    def test_play_time_cannot_change_while_a_game_is_open(self):
        self.manager.open("fruit-ninja")
        with self.assertRaises(GameError):
            self.manager.set_round_seconds(180)
        self.assertEqual(self.manager.round_seconds, 120)

    def test_selected_play_time_sets_the_round_clock(self):
        self.manager.set_round_seconds(180)
        self.manager.open("fruit-ninja")
        self.assertEqual(self.manager.session.duration, 180.0)
        self.assertEqual(self.manager.session.time_left, 180.0)

    def test_each_play_time_has_its_own_best_score(self):
        self.manager.scores.record("fruit-ninja", 80)
        self.manager.scores.record("fruit-ninja:60", 25)
        choices = {item["seconds"]: item["best"]
                   for item in self.manager.settings()["choices"]}
        self.assertEqual(choices[120], 80)
        self.assertEqual(choices[60], 25)


class TestAcceleratorFailure(GameManagerCase):
    """Section 42: no silent CPU fallback, and a reason on screen."""

    def test_a_missing_accelerator_refuses_rather_than_degrading(self):
        FakePose.fail_with = ("HailoRT's Python bindings are not installed. "
                              "AI Motion games require the AI HAT+ 2.")
        with self.assertRaises(GameError) as caught:
            self.manager.open("fruit-ninja")

        # The message reaches the player rather than being swallowed, and
        # nothing started anyway.
        self.assertIn("AI HAT+ 2", str(caught.exception))
        self.assertEqual(self.manager.active, "")
        self.assertFalse(self.camera.lent)

    def test_the_reason_is_kept_for_the_status_payload(self):
        FakePose.fail_with = "no Hailo device found on the PCIe bus"
        with self.assertRaises(GameError):
            self.manager.open("fruit-ninja")
        self.assertIn("PCIe", self.manager.status()["error"])


if __name__ == "__main__":
    unittest.main()
