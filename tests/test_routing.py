"""Routing, against AIA's real router and real command declarations.

Two things are checked here and they are the two the specification is most
insistent about.

**Section 6: do not break existing Kodama commands.** Every phrase AIA already
routes must still route, in both languages, through the router this project
builds — which now has an extra plugin in it. Adding commands to a fuzzy phrase
matcher is exactly the change that quietly breaks a neighbour, and this is a
regression test for that.

**The new launch command must not collide with them.** Launching an app and
resuming playback are different things said in similar words — 打开音乐 against
播放音乐 — and the router compares by sound, so which Mandarin phrases the
launcher may safely claim is a measurement rather than a matter of taste. The
numbers are in `aipi5/kodama/launcher.py`; the tests below are what checks
them, because the first version of that comment quoted a score that was wrong
by 0.19 and nothing but a test would have found it.
"""

from __future__ import annotations

import unittest

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.core.config import CONFIG
from aia.plugins.base import CommandSpec, Plugin, Registry, Result
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System
from aia.router.fast import FastRouter, normalise, similarity

from aipi5.core.config import KodamaLaunchConfig
from aipi5.kodama.launcher import KodamaLauncher


class StubPlayer(Plugin):
    """Stands in for the session bus, which a test machine does not have."""

    name = "kodama_probe"
    description = "probe"

    def available(self) -> bool:
        return True

    def commands(self):
        return []


def build_router() -> FastRouter:
    launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
    registry = Registry([KodamaLite(), System(), launcher])
    return FastRouter(registry, wake_words=CONFIG.wake.variants)


class TestExistingCommandsStillRoute(unittest.TestCase):
    """Section 6, phrase by phrase."""

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def assertRoutes(self, said: str, expect: str):
        intent = self.router.match(said)
        self.assertIsNotNone(intent, f"{said!r} routed to nothing")
        self.assertEqual(intent.command.name, expect,
                         f"{said!r} routed to {intent.command.name}, not {expect}")

    def test_english_transport(self):
        for said, expect in (
            ("pause", "pause"),
            ("play some music", "resume"),
            ("next", "next"),
            ("skip this song", "next"),
            ("previous", "previous"),
            ("stop", "stop"),
            ("what's playing", "now_playing"),
        ):
            with self.subTest(said=said):
                self.assertRoutes(said, expect)

    def test_mandarin_transport(self):
        for said, expect in (
            ("暂停", "pause"),
            ("播放歌曲", "resume"),
            ("下一首", "next"),
            ("上一首", "previous"),
            ("停止播放", "stop"),
            ("这是什么歌", "now_playing"),
        ):
            with self.subTest(said=said):
                self.assertRoutes(said, expect)

    def test_commands_with_arguments(self):
        intent = self.router.match("play hotel california")
        self.assertEqual(intent.command.name, "play")
        self.assertEqual(intent.arguments["query"], "hotel california")

        intent = self.router.match("音量调到五十")
        self.assertEqual(intent.command.name, "volume")

    def test_the_lyrics_trio_stays_apart(self):
        # AIA's hardest-won separation: 搜索歌词 and 搜索歌曲 are one syllable
        # apart and do unrelated things, and `save lyrics` writes.
        self.assertRoutes("show lyrics", "lyrics")
        self.assertRoutes("搜索歌词", "search_lyrics")
        self.assertRoutes("保存歌词", "save_lyrics")
        self.assertRoutes("搜索歌曲七里香", "search_song")

    def test_karaoke_and_leaving_it(self):
        self.assertRoutes("卡拉OK", "karaoke")
        self.assertRoutes("退出卡拉OK", "karaoke_exit")

    def test_destructive_commands_still_need_confirming(self):
        for said in ("shut down", "关机", "reboot", "close kodama", "退出软件"):
            with self.subTest(said=said):
                intent = self.router.match(said)
                self.assertIsNotNone(intent)
                self.assertTrue(intent.command.confirm,
                                f"{said!r} would run without asking")

    def test_two_commands_in_one_breath(self):
        chain = self.router.match_sequence("下一首 and 现在播放什么")
        self.assertEqual([i.command.name for i in chain], ["next", "now_playing"])


class TestTheLaunchCommand(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def test_it_routes_in_both_languages(self):
        for said in ("open kodama", "open the music player", "start the music player",
                     "打开音乐播放器", "启动播放器", "打开音乐"):
            with self.subTest(said=said):
                intent = self.router.match(said)
                self.assertIsNotNone(intent, f"{said!r} routed to nothing")
                self.assertEqual(intent.command.name, "open_kodama")

    def test_it_does_not_steal_resume(self):
        # The collision this command was shaped around. If these ever start
        # routing to `open_kodama`, saying "play some music" would launch an
        # app instead of resuming playback.
        for said in ("play music", "play some music", "播放音乐", "放歌", "放音乐"):
            with self.subTest(said=said):
                self.assertEqual(self.router.match(said).command.name, "resume")

    def test_the_mandarin_phrases_keep_their_measured_margin(self):
        # The numbers the launcher's phrase list is chosen from. 打开音乐 is
        # claimed because it sits 0.17 below the threshold against the nearest
        # `resume` phrase; 启动音乐 is not, because it sits 0.03 below and that
        # is not a margin.
        threshold = self.router.threshold
        for phrase in ("播放音乐", "放音乐", "播放歌曲", "放歌"):
            with self.subTest(phrase=phrase):
                score = similarity(normalise("打开音乐"), normalise(phrase))
                self.assertLess(score, threshold,
                                f"打开音乐 is too close to {phrase} to claim")

        self.assertGreater(similarity(normalise("启动音乐"), normalise("播放音乐")),
                           0.70,
                           "if this drops well clear of the threshold, 启动音乐 "
                           "could be added to the launcher's phrases")


class TestTheLauncherItself(unittest.TestCase):

    def test_it_is_available_even_when_the_player_is_not(self):
        # The point of the whole class. AIA's Kodama plugin reports itself
        # unavailable when the app is closed, and main.py refuses its commands
        # then — which is right for "next track" and would be absurd for
        # "open the music player".
        class Closed(StubPlayer):
            def available(self):
                return False

        launcher = KodamaLauncher(KodamaLaunchConfig(), Closed())
        self.assertTrue(launcher.available())
        self.assertFalse(launcher.running())

    def test_it_can_be_turned_off(self):
        launcher = KodamaLauncher(KodamaLaunchConfig(enabled=False), StubPlayer())
        self.assertFalse(launcher.available())
        self.assertFalse(launcher.open().ok)

    def test_already_open_is_not_a_restart(self):
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        result = launcher.open()
        self.assertIsInstance(result, Result)
        self.assertTrue(result.ok)
        self.assertIn("already", result.say("en"))

    def test_the_command_speaks(self):
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        command: CommandSpec = launcher.commands()[0]
        # For the several seconds a launch takes there is nothing on screen and
        # no sound, so silence is indistinguishable from being ignored.
        self.assertTrue(command.speaks)
        self.assertFalse(command.confirm)


if __name__ == "__main__":
    unittest.main()
