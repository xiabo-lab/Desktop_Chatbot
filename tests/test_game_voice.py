"""Starting a motion game by voice.

The touchscreen is the wrong input for this game: the player has to stand far
enough back for the camera to see their upper body, which is out of arm's reach
of the panel. So START is a button only somebody in the wrong place can press,
and the spoken command is what makes the game playable rather than a
convenience on top of it.

Two things are checked, and the first is the one that could hurt somebody's
device: **which phrases this command may safely claim is a measurement**,
because the router compares by sound. "Start the game" — the most natural
English phrasing there is — scores 0.71 against "restart the pi", and the
numbers written in `aipi5/games/voice.py` are asserted here rather than
trusted, because a comment in this project has been wrong by 0.19 before.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.core.config import CONFIG
from aia.plugins.base import Plugin, Registry
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System
from aia.router.fast import FastRouter, similarity

from aipi5.core.config import GamesConfig, KodamaLaunchConfig, MotionConfig
from aipi5.games import manager as manager_mod
from aipi5.games.fruit_ninja.game import State
from aipi5.games.manager import GameManager
from aipi5.games.voice import GameVoice
from aipi5.kodama.launcher import KodamaLauncher
from aipi5.motion.pose_types import PoseStats
from aipi5.motion.service import MotionUnavailable


class StubPlayer(Plugin):
    """Stands in for the session bus, which a test machine does not have."""

    name = "kodama_probe"
    description = "probe"

    def available(self) -> bool:
        return True

    def commands(self):
        return []


class StubManager:
    """Answers `voice_start` with whatever the test wants to hear."""

    def __init__(self, outcome=("started", "Fruit Ninja")):
        self.outcome = outcome
        self.calls = []

    def voice_start(self, game_id: str = "", timeout: float = 4.0,
                    fresh: bool = False):
        self.calls.append({"game_id": game_id, "timeout": timeout,
                           "fresh": fresh})
        return self.outcome


def build_router(manager=None) -> FastRouter:
    """The real router, with the real command declarations, plus this one."""
    launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
    voice = GameVoice(manager if manager is not None else StubManager())
    registry = Registry([KodamaLite(), System(), launcher, voice])
    return FastRouter(registry, wake_words=CONFIG.wake.variants)


class TestTheNewPhrasesRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def routed(self, utterance: str) -> str | None:
        chain = self.router.match_sequence(utterance)
        return chain[-1].command.name if chain else None

    def test_english(self):
        for phrase in ("start game", "begin game", "lets play", "let's play",
                       "start fruit ninja", "start the fruit game"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.routed(phrase), "start_game")

    def test_mandarin(self):
        for phrase in ("开始游戏", "开始游戏吧", "玩游戏", "游戏开始",
                       "开始水果忍者"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.routed(phrase), "start_game")

    def test_play_again_english(self):
        for phrase in ("play again", "again", "one more", "one more time",
                       "another go", "play once more"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.routed(phrase), "play_again")

    def test_play_again_mandarin(self):
        for phrase in ("重来", "再来一次", "再玩一次", "再来", "再来一局",
                       "重新开始"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.routed(phrase), "play_again")

    def test_start_and_again_do_not_shadow_each_other(self):
        self.assertEqual(self.routed("start game"), "start_game")
        self.assertEqual(self.routed("play again"), "play_again")
        self.assertEqual(self.routed("开始游戏"), "start_game")
        self.assertEqual(self.routed("重来"), "play_again")


class TestItStealsNothing(unittest.TestCase):
    """Adding to a fuzzy phrase matcher is how a neighbour quietly breaks."""

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def routed(self, utterance: str) -> str | None:
        chain = self.router.match_sequence(utterance)
        return chain[-1].command.name if chain else None

    def test_reboot_is_still_reboot(self):
        """The dangerous one.

        "Start the game" scores 0.71 against "restart the pi", which is why it
        is not one of this command's phrases. This asserts the consequence:
        asking to restart the Pi still restarts the Pi.
        """
        self.assertEqual(self.routed("restart the pi"), "reboot")
        self.assertEqual(self.routed("reboot the pi"), "reboot")

    def test_chongqi_still_reboots_despite_chonglai(self):
        """`重来` is 0.50 from `重启`. Close enough to be worth a test."""
        self.assertEqual(self.routed("重启"), "reboot")
        self.assertEqual(self.routed("重来"), "play_again")

    def test_playback_commands_are_untouched(self):
        for phrase, expected in (("play pause", "toggle"),
                                 ("play music", "resume"),
                                 ("play", "resume"),
                                 ("开始播放", "resume"),
                                 ("播放音乐", "resume"),
                                 ("next song", "next"),
                                 ("下一首", "next")):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.routed(phrase), expected)

    def test_every_existing_phrase_still_routes_to_its_own_command(self):
        """The whole of AIA's declared vocabulary, in both languages."""
        registry = Registry([KodamaLite(), System()])
        for _plugin, spec in registry.all_commands():
            for phrases in spec.phrases.values():
                for phrase in phrases:
                    if "{" in phrase:      # takes an argument; needs a value
                        continue
                    with self.subTest(command=spec.name, phrase=phrase):
                        self.assertEqual(self.routed(phrase), spec.name)


class TestTheMeasurementsInTheComment(unittest.TestCase):
    """The numbers in `voice.py` are asserted, not trusted.

    Each of these is a phrase that was considered and rejected. If a future
    edit adds one of them back, this fails and says why.
    """

    def worst(self, candidate: str) -> tuple[float, str]:
        registry = Registry([KodamaLite(), System()])
        existing = [(spec.name, phrase)
                    for _p, spec in registry.all_commands()
                    for phrases in spec.phrases.values() for phrase in phrases]
        score, name = max((similarity(candidate, p), n) for n, p in existing)
        return score, name

    def test_start_the_game_is_too_close_to_restart_the_pi(self):
        score, name = self.worst("start the game")
        self.assertEqual(name, "reboot")
        self.assertGreater(score, 0.70)

    def test_play_game_is_too_close_to_play_pause(self):
        score, name = self.worst("play game")
        self.assertEqual(name, "toggle")
        self.assertGreater(score, 0.70)

    def test_bare_kaishi_is_too_close_to_resume(self):
        score, name = self.worst("开始")
        self.assertEqual(name, "resume")
        self.assertGreater(score, 0.60)

    def test_replay_is_too_close_to_play(self):
        score, name = self.worst("replay")
        self.assertEqual(name, "resume")
        self.assertGreater(score, 0.70)

    def test_the_phrases_that_were_kept_have_margin(self):
        for phrase in ("start game", "begin game", "开始游戏", "玩游戏",
                       "游戏开始", "开始游戏吧", "开始水果忍者",
                       "play again", "again", "one more", "another go",
                       "重来", "再来一次", "再玩一次", "再来"):
            with self.subTest(phrase=phrase):
                score, _name = self.worst(phrase)
                self.assertLess(score, 0.70)


class TestTheHandlerSpeaks(unittest.TestCase):
    """Five outcomes, five different things to say."""

    def handler(self, outcome, detail="Fruit Ninja"):
        manager = StubManager((outcome, detail))
        voice = GameVoice(manager)
        return voice.start(), manager

    def test_started_is_short(self):
        result, _ = self.handler("started")
        self.assertTrue(result.ok)
        # A starting gun. The first fruit is already on its way up while this
        # is being spoken, so a sentence would still be playing when the
        # player needs to swing.
        self.assertLess(len(result.say("en")), 40)

    def test_no_player_says_what_to_do(self):
        result, _ = self.handler("no-player")
        self.assertFalse(result.ok)
        self.assertIn("camera", result.say("en").lower())
        self.assertIn("摄像头", result.say("zh"))

    def test_already_playing_changes_nothing(self):
        result, _ = self.handler("already")
        self.assertTrue(result.ok)
        self.assertIn("already", result.say("en").lower())

    def test_unavailable_repeats_the_reason(self):
        result, _ = self.handler("unavailable",
                                 "the Logitech Brio is being used by another feature")
        self.assertFalse(result.ok)
        self.assertIn("Brio", result.say("en"))

    def test_no_games_is_not_a_crash(self):
        result, _ = self.handler("no-games", "")
        self.assertFalse(result.ok)

    def test_both_languages_are_answered(self):
        for outcome in ("started", "resumed", "already", "no-player",
                        "no-games", "unavailable"):
            with self.subTest(outcome=outcome):
                result, _ = self.handler(outcome)
                self.assertTrue(result.say("en"))
                self.assertTrue(result.say("zh"))
                # The failure that section 6 of the AIA config is about: half a
                # sentence in the wrong language.
                self.assertNotEqual(result.say("en"), result.say("zh"))

    def test_a_missing_manager_is_refused_rather_than_raising(self):
        self.assertFalse(GameVoice(lambda: None).start().ok)
        self.assertFalse(GameVoice(lambda: None).again().ok)

    def test_start_asks_for_a_normal_start_and_again_asks_for_a_fresh_one(self):
        """The one bit of wiring that decides whether a score is thrown away."""
        manager = StubManager()
        GameVoice(manager).start()
        self.assertIs(manager.calls[-1]["fresh"], False)

        GameVoice(manager).again()
        self.assertIs(manager.calls[-1]["fresh"], True)

    def test_the_plugin_is_available_even_with_no_game_running(self):
        """The trap this command would otherwise fall into.

        It exists for when no game is running, so reporting itself unavailable
        then would have the router refuse it with "The motion games is not
        currently running."
        """
        self.assertTrue(GameVoice(StubManager()).available())


# ── the manager side ────────────────────────────────────────────────


class FakeCamera:
    def __init__(self):
        self.lent_to = ""

    def lend(self, to: str = "a call") -> bool:
        self.lent_to = to
        return True

    def reclaim(self) -> bool:
        self.lent_to = ""
        return True

    @property
    def lent(self) -> bool:
        return bool(self.lent_to)

    def describe(self) -> dict:
        return {"device": "/dev/video0", "name": "Logitech BRIO",
                "running": not self.lent_to, "lent_to": self.lent_to or None}


class FakeSnapshot:
    def __init__(self, ready):
        self.ready = ready
        self.hands = {}

    def as_dict(self):
        return {}


class FakePose:
    """A pose service whose player appears after `ready_after` seconds."""

    fail_with = ""
    ready_after = 0.0

    def __init__(self, cfg, camera, on_pose=None):
        self.camera = camera
        self.stats = PoseStats()
        self.error = ""
        self._running = False
        self._started_at = 0.0

    def start(self):
        if FakePose.fail_with:
            raise MotionUnavailable(FakePose.fail_with)
        self.camera.lend("an AI Motion game")
        self._running = True
        self._started_at = time.monotonic()

    def stop(self):
        if self._running:
            self.camera.reclaim()
        self._running = False

    @property
    def running(self):
        return self._running

    def snapshot(self):
        if not self._running:
            return None
        ready = (time.monotonic() - self._started_at) >= FakePose.ready_after
        return FakeSnapshot(ready)

    def last_frame(self):
        return None

    def preview_jpeg(self):
        return None

    def describe(self):
        return {"running": self._running, "error": "", "stats": {},
                "accelerator": {}, "pose": {}, "camera": {}}


class VoiceStartCase(unittest.TestCase):

    def setUp(self):
        self._patched = manager_mod.PoseService
        manager_mod.PoseService = FakePose
        FakePose.fail_with = ""
        FakePose.ready_after = 0.0
        self._timeout = manager_mod.IDLE_TIMEOUT_S
        manager_mod.IDLE_TIMEOUT_S = 10_000

        self._dir = TemporaryDirectory()
        self.camera = FakeCamera()
        self.manager = GameManager(
            GamesConfig(scores=Path(self._dir.name) / "scores.json"),
            motion_cfg=MotionConfig(), camera=self.camera)

    def tearDown(self):
        self.manager.close()
        manager_mod.PoseService = self._patched
        manager_mod.IDLE_TIMEOUT_S = self._timeout
        self._dir.cleanup()


class TestVoiceStart(VoiceStartCase):

    def test_it_opens_the_game_that_is_not_open(self):
        """The point of the whole feature: no walk to the screen first."""
        outcome, name = self.manager.voice_start()
        self.assertEqual(outcome, "started")
        self.assertEqual(name, "Fruit Ninja")
        self.assertEqual(self.manager.active, "fruit-ninja")
        self.assertTrue(self.camera.lent)
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_it_starts_a_game_that_is_already_open(self):
        self.manager.open("fruit-ninja")
        self.assertEqual(self.manager.session.state, State.READY)
        outcome, _ = self.manager.voice_start()
        self.assertEqual(outcome, "started")
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_it_waits_for_the_player_rather_than_refusing(self):
        """The failure this command was built to avoid.

        Somebody who has just finished saying "start game" is standing in front
        of the camera, but the pose service may have been running for a
        fraction of a second. Refusing instantly would make the command fail
        exactly when it is used correctly.
        """
        FakePose.ready_after = 0.4
        started = time.monotonic()
        outcome, _ = self.manager.voice_start(timeout=3.0)
        elapsed = time.monotonic() - started

        self.assertEqual(outcome, "started")
        self.assertGreaterEqual(elapsed, 0.4)

    def test_but_the_wait_is_bounded(self):
        FakePose.ready_after = 60.0
        started = time.monotonic()
        outcome, name = self.manager.voice_start(timeout=0.3)
        elapsed = time.monotonic() - started

        self.assertEqual(outcome, "no-player")
        self.assertEqual(name, "Fruit Ninja")
        self.assertLess(elapsed, 2.0)
        # The game stays open, so "start game" said again a moment later works
        # without paying for the camera and the model a second time.
        self.assertEqual(self.manager.active, "fruit-ninja")

    def test_a_round_in_progress_is_not_restarted(self):
        """Saying "start game" mid-game must not throw the score away."""
        self.manager.voice_start()
        self.manager.session.score = 250

        outcome, _ = self.manager.voice_start()
        self.assertEqual(outcome, "already")
        self.assertEqual(self.manager.session.score, 250)
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_but_play_again_does_restart_it(self):
        """"Again" is deliberate in a way "start" is not."""
        self.manager.voice_start()
        self.manager.session.score = 250

        outcome, _ = self.manager.voice_start(fresh=True)
        self.assertEqual(outcome, "started")
        self.assertEqual(self.manager.session.score, 0)
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_play_again_from_a_paused_round_starts_over(self):
        self.manager.voice_start()
        self.manager.session.score = 80
        self.manager.command("pause")

        outcome, _ = self.manager.voice_start(fresh=True)
        self.assertEqual(outcome, "started")
        self.assertEqual(self.manager.session.score, 0)
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_play_again_still_banks_the_previous_score(self):
        self.manager.voice_start()
        self.manager.session.score = 310
        self.manager.session.finish(time.monotonic())

        self.manager.voice_start(fresh=True)
        self.assertEqual(self.manager.scores.best("fruit-ninja"), 310)
        self.assertEqual(self.manager.session.score, 0)

    def test_play_again_opens_a_game_that_is_not_open(self):
        """Said after walking away and coming back, with nothing running."""
        outcome, _ = self.manager.voice_start(fresh=True)
        self.assertEqual(outcome, "started")
        self.assertEqual(self.manager.active, "fruit-ninja")

    def test_play_again_still_needs_a_player(self):
        FakePose.ready_after = 60.0
        outcome, _ = self.manager.voice_start(fresh=True, timeout=0.3)
        self.assertEqual(outcome, "no-player")

    def test_a_paused_round_is_resumed_not_restarted(self):
        self.manager.voice_start()
        self.manager.session.score = 120
        self.manager.command("pause")

        outcome, _ = self.manager.voice_start()
        self.assertEqual(outcome, "resumed")
        self.assertEqual(self.manager.session.score, 120)
        self.assertEqual(self.manager.session.state, State.PLAYING)

    def test_a_finished_round_is_played_again(self):
        self.manager.voice_start()
        self.manager.session.score = 90
        self.manager.session.finish(time.monotonic())
        self.assertEqual(self.manager.session.state, State.OVER)

        outcome, _ = self.manager.voice_start()
        self.assertEqual(outcome, "started")
        self.assertEqual(self.manager.session.state, State.PLAYING)
        self.assertEqual(self.manager.session.score, 0)
        # ...and the best is remembered across the restart.
        self.assertEqual(self.manager.scores.best("fruit-ninja"), 90)

    def test_hardware_refusal_is_reported_and_holds_nothing(self):
        FakePose.fail_with = "the Logitech Brio is being used by another feature"
        outcome, detail = self.manager.voice_start()

        self.assertEqual(outcome, "unavailable")
        self.assertIn("Brio", detail)
        self.assertFalse(self.camera.lent)
        self.assertEqual(self.manager.active, "")

    def test_an_unknown_game_is_refused(self):
        outcome, _ = self.manager.voice_start("pinball")
        self.assertEqual(outcome, "unavailable")

    def test_a_coming_soon_game_is_refused(self):
        outcome, _ = self.manager.voice_start("workout")
        self.assertEqual(outcome, "unavailable")
        self.assertFalse(self.camera.lent)

    def test_the_default_game_is_the_first_playable_one(self):
        self.assertEqual(self.manager.default_game(), "fruit-ninja")

    def test_the_default_is_whatever_is_already_open(self):
        self.manager.open("fruit-ninja")
        self.assertEqual(self.manager.default_game(), "fruit-ninja")

    def test_closing_still_gives_everything_back(self):
        self.manager.voice_start()
        self.manager.close()
        self.assertFalse(self.camera.lent)
        self.assertEqual(self.manager.active, "")

    def test_repeated_voice_starts_leak_nothing(self):
        for _ in range(5):
            self.manager.voice_start()
            self.manager.close()
        self.assertFalse(self.camera.lent)
        self.assertIsNone(self.manager.session)


class TestBriefForTheStatePoll(VoiceStartCase):
    """What `/api/state` carries so the screen can follow a voice start."""

    def test_nothing_open_is_none(self):
        self.assertIsNone(self.manager.brief())

    def test_an_open_game_names_itself(self):
        self.manager.open("fruit-ninja")
        self.assertEqual(self.manager.brief(),
                         {"active": "fruit-ninja", "state": "ready"})

    def test_it_follows_the_session_state(self):
        self.manager.voice_start()
        self.assertEqual(self.manager.brief()["state"], "playing")

    def test_it_is_small(self):
        """It rides on a poll that runs twice a second on every page.

        The whole game snapshot belongs on `/api/game/stream`, which only the
        game page opens.
        """
        self.manager.voice_start()
        self.assertEqual(set(self.manager.brief()), {"active", "state"})

    def test_closing_clears_it(self):
        self.manager.voice_start()
        self.manager.close()
        self.assertIsNone(self.manager.brief())


class TestWaitDoesNotBlockForever(VoiceStartCase):
    """`voice_start` runs on the voice loop. It must always come back."""

    def test_wait_for_player_returns_within_its_timeout(self):
        self.manager.open("fruit-ninja")
        FakePose.ready_after = 60.0
        started = time.monotonic()
        self.assertFalse(self.manager.wait_for_player(timeout=0.25))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_wait_with_no_game_open_returns_immediately(self):
        started = time.monotonic()
        self.assertFalse(self.manager.wait_for_player(timeout=0.2))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_voice_thread_is_released(self):
        """A handler that never returns is an assistant that never listens."""
        FakePose.ready_after = 60.0
        done = threading.Event()

        def run():
            self.manager.voice_start(timeout=0.3)
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(done.wait(5.0))


if __name__ == "__main__":
    unittest.main()
