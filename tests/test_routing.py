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
from unittest import mock

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.core.config import CONFIG
from aia.plugins.base import CommandSpec, Plugin, Registry, Result
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System
from aia.router.fast import FastRouter, normalise, similarity

from aipi5.browser.launcher import BrowserLauncher
from aipi5.core.config import BrowserConfig, KodamaLaunchConfig
from aipi5.games.voice import GameVoice
from aipi5.kodama import launcher as launcher_mod
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
    """The registry the assistant actually builds — see `main.py`.

    `GameVoice` and `BrowserLauncher` are in here because they are in
    production, and the whole point of this file is that adding to a fuzzy
    phrase matcher breaks neighbours. A router assembled from four of the five
    plugins would pass this suite while the device failed it. Their own phrase
    measurements are in `tests/test_game_voice.py` and `tests/test_browser.py`;
    what they must not do to *these* commands is checked below with everything
    else.
    """
    launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
    registry = Registry([KodamaLite(), System(), launcher,
                         BrowserLauncher(BrowserConfig()),
                         GameVoice(lambda: None)])
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
        for said in ("reboot", "close kodama", "退出软件"):
            with self.subTest(said=said):
                intent = self.router.match(said)
                self.assertIsNotNone(intent)
                self.assertTrue(intent.command.confirm,
                                f"{said!r} would run without asking")

    def test_shutdown_is_answered_by_a_touch_instead(self):
        # Not a gap in the rule above: `poweroff` takes the audio stack down
        # with it, so a spoken confirmation is a question this device cannot
        # reliably finish asking. It is answered on the screen instead — see
        # `ShutdownCountdown`, and `tests/test_shutdown.py` for the policy.
        # Routing is still what has to hold: whatever answers it, both
        # languages have to reach the same command.
        for said in ("shut down", "关机"):
            with self.subTest(said=said):
                intent = self.router.match(said)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.command.name, "shutdown")

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
    """The launcher, with nothing of the device underneath it.

    **`setUp` patches the launch, and it is in `setUp` on purpose.** Every test
    in this class that calls `open()` reaches `raise_window`, which runs
    `/usr/bin/kodama-lite` — and on the Pi that binary exists. Four passing
    tests here started the music player every time the suite ran on the device,
    including the run the agent uses as its gate before any source change. That
    is what "Kodama-Lite opens randomly" was.

    The launcher refuses to run the binary now when no such process exists, so
    this patch is the second of two locks rather than the only one. It stays
    because a test that spawns an installed application is wrong even when the
    application happens to be harmless, and because putting it in `setUp`
    covers the next test somebody adds here without their having to know any
    of the above.
    """

    def setUp(self):
        patch = mock.patch("aipi5.kodama.launcher.subprocess.Popen")
        self.popen = patch.start()
        self.addCleanup(patch.stop)

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

    def test_every_launch_says_who_asked_for_it(self):
        """The log line that makes "it started on its own" answerable.

        Without this every caller produced the same sentence, so a button
        press, a spoken command and — while it existed — the model deciding by
        itself were indistinguishable afterwards. On a device whose journal is
        volatile, an unattributed launch is one nobody can ever settle.
        """
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        with self.assertLogs("aipi5.kodama.launcher", level="INFO") as caught:
            launcher.open("the Music button")
        self.assertTrue(any("the Music button" in line for line in caught.output),
                        caught.output)

    def test_the_spoken_command_names_itself(self):
        # The router calls the handler with no arguments, so the name has to be
        # bound at declaration. If that binding is ever lost the launch goes
        # back to being anonymous, which is the bug this pins.
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        command: CommandSpec = launcher.commands()[0]
        with self.assertLogs("aipi5.kodama.launcher", level="INFO") as caught:
            command.handler()
        self.assertTrue(any("a spoken command" in line for line in caught.output),
                        caught.output)

    def test_an_unattributed_launch_is_visible_as_one(self):
        # The default names nothing rather than guessing, so a caller that
        # forgets to identify itself shows up in the log as exactly that.
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        with self.assertLogs("aipi5.kodama.launcher", level="INFO") as caught:
            launcher.open()
        self.assertTrue(any("something unnamed" in line for line in caught.output),
                        caught.output)

    def test_the_command_speaks(self):
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        command: CommandSpec = launcher.commands()[0]
        # For the several seconds a launch takes there is nothing on screen and
        # no sound, so silence is indistinguishable from being ignored.
        self.assertTrue(command.speaks)
        self.assertFalse(command.confirm)

    def test_raising_a_window_does_not_start_a_player(self):
        """The regression, stated as the property rather than as the incident.

        `raise_window` is safe only because a second launch of the binary hands
        its argv to the copy already running. With no copy running the same
        line is a cold start of an app that resumes its last queue and begins
        playing — so the question "is there such a process" is read from
        `/proc` and cannot be answered by anything a caller passed in.

        A stub player that says it is running is exactly what made the check
        that used to be here say yes.
        """
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        self.assertTrue(launcher.running(), "the stub is meant to lie")
        with mock.patch("aipi5.kodama.launcher._binary_is_running",
                        return_value=False):
            with self.assertLogs("aipi5.kodama.launcher", level="INFO") as caught:
                self.assertFalse(launcher.raise_window())
        self.popen.assert_not_called()
        self.assertTrue(any("would start the player" in line
                            for line in caught.output), caught.output)

    def test_it_does_raise_a_window_when_there_is_one(self):
        # The other half: with the process there, the binary is run, because
        # that is the only way to raise a window in a Wayland session where
        # wmctrl and xdotool are both X11 clients.
        launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
        with mock.patch("aipi5.kodama.launcher._binary_is_running",
                        return_value=True):
            self.assertTrue(launcher.raise_window())
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertEqual(argv, [launcher_mod.KODAMA_BINARY])

    def test_the_process_check_reads_proc_and_not_a_command_line(self):
        """`pgrep -f` matches the shell that ran it and reports a process that
        is not there — the same trap `pkill -f` sets, which has already cost
        this project an ssh session. So the check walks `/proc` itself, skips
        its own pid, and matches argv[0] exactly.
        """
        self.assertFalse(launcher_mod._binary_is_running("/usr/bin/definitely-not-here"))
        # Whatever is running this suite is running *something*, and matching
        # on a substring rather than on argv[0] is how that becomes a hit.
        self.assertFalse(launcher_mod._binary_is_running("kodama"))


if __name__ == "__main__":
    unittest.main()
