"""Opening a website by voice, without a Chromium and without a compositor.

Three things are checked, and they are the three that can go wrong quietly.

**The phrases route, and route to this.** The utterance that started this work
was heard as `打开 youtu 。` — SenseVoice dropping the tail of an English word
inside a Mandarin sentence — so the transcripts pinned below are the ones the
device actually produces, not the ones a person would type.

**The phrases do not steal a neighbour.** The router compares by sound across
every command in every language at once, so a new phrase list is a change to
all of them. `tests/test_routing.py` builds its router with this plugin in it
for that reason; what is measured here is the margin.

**Nothing a person says reaches the command line.** The URL comes from `SITES`
and the site is chosen by the router, so the argv is fixed except for the
profile path — and that is checked here rather than trusted.

**And the page goes to the browser a hand can drive.** That was the whole
second half of this feature: a Chromium started here is a window nothing holds
a CDP pipe to, so `aipi5/agent/pilot.py` has nothing to send gestures to and
`Housekeeping` never lends it the camera. Reported from the room as "the hand
tracker is not activated". The launcher hands the URL to the agent instead, and
only falls back to a browser of its own when there is no agent to hand it to.
"""

from __future__ import annotations

import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.core.config import CONFIG
from aia.plugins.base import Plugin, Registry, Result
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System
from aia.router.fast import FastRouter, normalise, similarity

from aipi5.agent.helper_client import OpResult
from aipi5.agent.proxy import AgentProxy
from aipi5.agent.runtime import AgentService
from aipi5.browser.launcher import SITES, BrowserLauncher
from aipi5.core.config import BrowserConfig, KodamaLaunchConfig
from aipi5.kodama.launcher import KodamaLauncher

YOUTUBE = SITES[0]


class StubPlayer(Plugin):
    """Stands in for the session bus, which a test machine does not have."""

    name = "kodama_probe"
    description = "probe"

    def commands(self):
        return []


def build_router() -> FastRouter:
    launcher = KodamaLauncher(KodamaLaunchConfig(), StubPlayer())
    registry = Registry([KodamaLite(), System(), launcher,
                         BrowserLauncher(BrowserConfig())])
    return FastRouter(registry, wake_words=CONFIG.wake.variants)


def launcher(agent=None, **overrides) -> BrowserLauncher:
    return BrowserLauncher(BrowserConfig(**overrides), agent=agent)


class FakeAgent:
    """An `AgentProxy` that answers without a socket.

    `reachable` is separate from what `open_page` returns, because the two
    failures are different: no agent at all is the committed default and takes
    the fallback silently-by-design, while an agent that refuses is a fault
    worth a line in the journal.
    """

    def __init__(self, reachable: bool = True, answer=(200, {"ok": True,
                                                             "title": "YouTube"})):
        self.reachable = reachable
        self.answer = answer
        self.opened: list[str] = []

    def available(self) -> bool:
        return self.reachable

    def open_page(self, url, timeout=None):
        self.opened.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Launched:
    """A `subprocess.Popen` that never starts anything."""

    def __init__(self, alive: bool = True):
        self.alive = alive
        self.argv: list[str] = []
        self.env: dict = {}
        self.kwargs: dict = {}

    def __call__(self, argv, env=None, **kwargs):
        self.argv = argv
        self.env = env or {}
        self.kwargs = kwargs
        return self

    def poll(self):
        return None if self.alive else 0


def with_chromium(fake: Launched):
    """Patch out both the browser lookup and the launch itself."""
    return (mock.patch("aipi5.browser.launcher.shutil.which",
                       return_value="/usr/bin/chromium"),
            mock.patch("aipi5.browser.launcher.subprocess.Popen", fake))


class FakeHelper:
    """A `HelperClient` that answers without root and without a socket."""

    def __init__(self, answer=None):
        # `is None`, never `or`. `OpResult.__bool__` is its `ok` flag, so a
        # refusal handed in here is falsey and `answer or default` silently
        # swaps it for a success — which is how this fixture first reported
        # that a refused URL had opened.
        self.answer = answer if answer is not None else OpResult(
            op="browser_open", ok=True, result={"title": "YouTube"})
        self.calls = []

    def call(self, op, args, timeout=None):
        self.calls.append((op, args))
        return self.answer


class TestItRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def routed(self, said: str) -> str | None:
        intent = self.router.match(said)
        return intent.command.name if intent else None

    def test_english(self):
        for said in ("open youtube", "open you tube", "start youtube",
                     "launch youtube", "go to youtube"):
            with self.subTest(said=said):
                self.assertEqual(self.routed(said), "open_youtube")

    def test_mandarin(self):
        for said in ("打开YouTube", "打开油管", "打开YouTube网站"):
            with self.subTest(said=said):
                self.assertEqual(self.routed(said), "open_youtube")

    def test_the_transcript_the_device_actually_produced(self):
        # Captured from the journal, 2026-09-01. The recogniser reached
        # `youtu` and stopped, and the router folds that to `dakaiyoutu`
        # against this command's `dakaiyoutube`. If this ever stops routing,
        # the sentence goes to the model, which cannot open anything.
        self.assertEqual(self.routed("打开 youtu 。"), "open_youtube")

    def test_the_ways_the_same_sounds_get_written(self):
        # Pinyin is what is compared, so the characters the recogniser guesses
        # for those syllables do not matter — which is the whole reason the
        # fast path compares sound.
        for said in ("打开优兔", "打开有兔"):
            with self.subTest(said=said):
                self.assertEqual(self.routed(said), "open_youtube")

    def test_it_does_not_steal_the_music_player(self):
        # The nearest neighbour by construction: both are "打开" plus a name.
        for said in ("打开音乐", "打开音乐播放器", "打开播放器", "open kodama",
                     "open the music player", "open music"):
            with self.subTest(said=said):
                self.assertEqual(self.routed(said), "open_kodama")

    def test_it_does_not_steal_anything_else(self):
        for said, expect in (("播放音乐", "resume"), ("暂停", "pause"),
                             ("下一首", "next"), ("打开随机播放", "shuffle"),
                             ("关机", "shutdown")):
            with self.subTest(said=said):
                self.assertEqual(self.routed(said), expect)

    def test_the_two_mandarin_phrases_are_not_each_other(self):
        # 打开油管 is a phrase of its own rather than a near-miss of
        # 打开YouTube, because it is not near enough to be one: `dakaiyouguan`
        # against `dakaiyoutube` is 0.75, under the router's 0.78 floor. Drop
        # it and "open the oil pipe" simply does not route.
        self.assertLess(similarity(normalise("打开油管"), normalise("打开YouTube")),
                        self.router.threshold)

    def test_every_claimed_phrase_keeps_its_measured_margin(self):
        """The margin, measured against the whole command set rather than
        against the neighbours somebody thought to list.

        A phrase list is a change to every other command at once, so the
        number worth pinning is the nearest thing anywhere — which for these is
        the music player, because both are "打开" plus a name. 打开YouTube
        against 打开音乐 is 0.70 and 打开油管 against 打开播放器 is 0.64.
        """
        registry = self.router.registry
        elsewhere = [(command.name, phrase)
                     for _, command in registry.all_commands()
                     if command.name != "open_youtube"
                     for phrases in command.phrases.values()
                     for phrase in phrases]
        self.assertTrue(elsewhere)
        for claimed in YOUTUBE.phrases["en"] + YOUTUBE.phrases["zh"]:
            mine = normalise(claimed)
            score, name, phrase = max(
                (similarity(mine, normalise(other)), name, other)
                for name, other in elsewhere)
            with self.subTest(claimed=claimed):
                self.assertLess(
                    score, self.router.threshold,
                    f"{claimed!r} scores {score:.2f} against {name}'s "
                    f"{phrase!r}, at or over the {self.router.threshold} floor")


class TestTheLauncher(unittest.TestCase):

    def test_it_is_available_even_though_nothing_is_running(self):
        # The point of the class: the command exists for the case where there
        # is no browser, so `main.py` must not refuse it for that reason.
        plugin = launcher()
        self.assertTrue(plugin.available())
        self.assertFalse(plugin.running())

    def test_it_can_be_turned_off(self):
        plugin = launcher(enabled=False)
        self.assertFalse(plugin.available())
        result = plugin.open(YOUTUBE)
        self.assertFalse(result.ok)
        self.assertIn("turned off", result.say("en"))
        # And it says so in Mandarin too, rather than half a sentence in the
        # wrong language.
        self.assertNotEqual(result.say("zh"), result.say("en"))

    def test_no_browser_on_the_device_is_a_sentence_not_a_crash(self):
        plugin = launcher()
        with mock.patch("aipi5.browser.launcher.shutil.which", return_value=None):
            result = plugin.open(YOUTUBE)
        self.assertIsInstance(result, Result)
        self.assertFalse(result.ok)
        self.assertIn("browser", result.say("en"))

    def test_a_launch_that_raises_is_reported_and_survived(self):
        plugin = launcher()
        with mock.patch("aipi5.browser.launcher.shutil.which",
                        return_value="/usr/bin/chromium"), \
                mock.patch("aipi5.browser.launcher.subprocess.Popen",
                           side_effect=OSError("no")):
            result = plugin.open(YOUTUBE)
        self.assertFalse(result.ok)
        self.assertFalse(plugin.running())

    def test_the_first_launch_opens_and_is_tracked(self):
        plugin = launcher()
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen:
            result = plugin.open(YOUTUBE, "a spoken command")
        self.assertTrue(result.ok)
        self.assertIn("Opening YouTube", result.say("en"))
        self.assertIn("YouTube", result.say("zh"))
        self.assertTrue(plugin.running())

    def test_the_second_launch_raises_the_window_and_is_not_tracked(self):
        """A second Chromium on one profile hands its command line to the
        first and exits, so tracking it would report the browser as closed a
        moment after opening a page in it. What the first one then does is open
        a tab and raise itself, which is what the reply has to say."""
        plugin = launcher()
        first, second = Launched(), Launched(alive=False)
        which, popen = with_chromium(first)
        with which, popen:
            plugin.open(YOUTUBE)
        which, popen = with_chromium(second)
        with which, popen:
            result = plugin.open(YOUTUBE)
        self.assertTrue(result.ok)
        self.assertIn("new tab", result.say("en"))
        self.assertIs(plugin._proc, first)
        self.assertTrue(plugin.running())

    def test_a_closed_window_is_opened_again(self):
        plugin = launcher()
        dead = Launched(alive=False)
        which, popen = with_chromium(dead)
        with which, popen:
            plugin.open(YOUTUBE)
            self.assertFalse(plugin.running())
            result = plugin.open(YOUTUBE)
        self.assertIn("Opening YouTube", result.say("en"))

    def test_every_launch_says_who_asked_for_it(self):
        # Same reason `KodamaLauncher` takes this: journald here is volatile,
        # so a launch nobody can attribute is one nobody can ever settle.
        plugin = launcher()
        which, popen = with_chromium(Launched())
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="INFO") as caught:
            plugin.open(YOUTUBE, "the settings page")
        self.assertTrue(any("the settings page" in line for line in caught.output),
                        caught.output)

    def test_an_unattributed_launch_is_visible_as_one(self):
        plugin = launcher()
        which, popen = with_chromium(Launched())
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="INFO") as caught:
            plugin.open(YOUTUBE)
        self.assertTrue(any("something unnamed" in line for line in caught.output),
                        caught.output)

    def test_the_spoken_command_names_itself(self):
        # The router calls the handler with no arguments, so both the site and
        # the caller's name have to be bound at declaration. A late-bound loop
        # variable would give every command the last site in the table.
        plugin = launcher()
        which, popen = with_chromium(Launched())
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="INFO") as caught:
            for command in plugin.commands():
                command.handler()
        joined = "\n".join(caught.output)
        self.assertIn("a spoken command", joined)
        for site in SITES:
            self.assertIn(site.url, joined)

    def test_the_command_speaks_and_needs_no_confirmation(self):
        command = launcher().commands()[0]
        # A browser takes seconds to paint, and until it does silence is
        # indistinguishable from having been ignored.
        self.assertTrue(command.speaks)
        self.assertFalse(command.confirm)
        # Opening a page is not destructive, but the confirmation question
        # still has to exist in both languages if it is ever asked.
        self.assertEqual(set(command.speech), {"en", "zh"})


class TestItGoesToTheBrowserAHandCanDrive(unittest.TestCase):
    """The half of this feature that was missing, and how it failed.

    A Chromium started here paints a window and loads the page, so from the
    room it looks finished. What it cannot do is be driven by a hand, because
    hand control is not a property of a window: the pilot sends gestures to the
    root helper, which drives Chromium over a CDP pipe *it* holds, and
    `Housekeeping` only lends the camera while the agent's `browser_state` says
    a page is open. A browser nobody holds a pipe to is invisible to all three.
    """

    def test_the_agent_gets_the_url_and_no_chromium_is_started(self):
        agent = FakeAgent()
        plugin = launcher(agent=agent)
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen:
            result = plugin.open(YOUTUBE, "a spoken command")
            self.assertTrue(plugin.settled())
        self.assertTrue(result.ok)
        self.assertEqual(agent.opened, [YOUTUBE.url])
        # The point. A second browser here is the bug being fixed.
        self.assertEqual(fake.argv, [])
        self.assertFalse(plugin.running())

    def test_the_reply_does_not_wait_for_the_page(self):
        """Measured on the device at 3.0 s warm and 5.3 s cold, against a turn
        budget of 2.5 s. The window appears about a second in, so the sentence
        lands over a page that is already painting."""
        started = threading.Event()
        release = threading.Event()

        class Slow(FakeAgent):
            def open_page(self, url, timeout=None):
                started.set()
                release.wait(5)
                return super().open_page(url, timeout)

        agent = Slow()
        plugin = launcher(agent=agent)
        result = plugin.open(YOUTUBE)
        # Returned while the hand-off is still blocked.
        self.assertTrue(started.wait(5))
        self.assertTrue(result.ok)
        self.assertIn("Opening YouTube", result.say("en"))
        release.set()
        self.assertTrue(plugin.settled())

    def test_a_lone_agent_call_is_made_for_two_rapid_asks(self):
        # Two asks in the same breath are one person repeating themselves, not
        # a reason to queue a second three-second round trip.
        release = threading.Event()

        class Slow(FakeAgent):
            def open_page(self, url, timeout=None):
                release.wait(5)
                return super().open_page(url, timeout)

        agent = Slow()
        plugin = launcher(agent=agent)
        plugin.open(YOUTUBE)
        while not agent.opened and not release.is_set():
            time.sleep(0.01)
            if plugin._handing_over.locked():
                break
        plugin.open(YOUTUBE)
        release.set()
        self.assertTrue(plugin.settled())
        self.assertLessEqual(len(agent.opened), 1)

    def test_no_agent_falls_back_and_says_what_is_lost(self):
        plugin = launcher(agent=FakeAgent(reachable=False))
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="WARNING") as caught:
            result = plugin.open(YOUTUBE)
        self.assertTrue(result.ok)
        self.assertEqual(fake.argv[-1], YOUTUBE.url)
        joined = "\n".join(caught.output)
        self.assertIn("hand control", joined)

    def test_an_agent_that_refuses_falls_back_rather_than_leaving_nothing(self):
        agent = FakeAgent(answer=(200, {"ok": False,
                                        "error": "the browser refused"}))
        plugin = launcher(agent=agent)
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="ERROR") as caught:
            plugin.open(YOUTUBE)
            self.assertTrue(plugin.settled())
        # Somebody who asked for YouTube gets YouTube, in the worse window.
        self.assertEqual(fake.argv[-1], YOUTUBE.url)
        self.assertIn("the browser refused", "\n".join(caught.output))
        # By now the reply has been spoken, so the settings page is the only
        # place left that can say what went wrong.
        self.assertIn("refused", plugin.describe()["last_error"])

    def test_an_agent_that_is_not_answering_falls_back(self):
        # What `AgentProxy` returns when the socket is there and the runtime is
        # not: a 503 with a sentence, never an exception.
        agent = FakeAgent(answer=(503, {"error": "the agent is not answering."}))
        plugin = launcher(agent=agent)
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen, self.assertLogs("aipi5.browser.launcher", level="ERROR"):
            plugin.open(YOUTUBE)
            self.assertTrue(plugin.settled())
        self.assertEqual(fake.argv[-1], YOUTUBE.url)

    def test_an_unforeseen_exception_on_the_thread_is_not_lost(self):
        """`AgentProxy` does not raise, so anything that does is a surprise —
        and a surprise on a daemon thread is one nobody would ever see."""
        agent = FakeAgent(answer=RuntimeError("boom"))
        plugin = launcher(agent=agent)
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen, \
                self.assertLogs("aipi5.browser.launcher", level="ERROR") as caught:
            plugin.open(YOUTUBE)
            self.assertTrue(plugin.settled())
        self.assertIn("RuntimeError", "\n".join(caught.output))
        self.assertEqual(fake.argv[-1], YOUTUBE.url)

    def test_the_agent_is_late_bound(self):
        """`main.py` builds the command set before the proxy, so the launcher
        is handed a callable. If that ever became the object itself, a device
        with an agent would silently take the fallback."""
        holder = {}
        plugin = launcher(agent=lambda: holder.get("agent"))
        self.assertFalse(plugin.describe()["through_agent"])
        holder["agent"] = FakeAgent()
        self.assertTrue(plugin.describe()["through_agent"])

    def test_the_settings_page_says_which_browser_would_be_used(self):
        self.assertFalse(launcher()
                         .describe()["through_agent"])
        self.assertTrue(launcher(agent=FakeAgent())
                        .describe()["through_agent"])


class TestTheCommandLine(unittest.TestCase):
    """What is actually handed to Chromium."""

    def argv(self, **overrides) -> list[str]:
        plugin = launcher(**overrides)
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen:
            plugin.open(YOUTUBE)
        return fake.argv

    def test_the_url_is_the_table_and_nothing_else(self):
        argv = self.argv()
        self.assertEqual(argv[-1], YOUTUBE.url)
        self.assertTrue(YOUTUBE.url.startswith("https://"))

    def test_it_never_shares_the_kiosk_profile(self):
        """A shared profile does not open a second window — Chromium hands the
        command line to the instance already running, which would put YouTube
        in a tab inside the kiosk, over the assistant's own page."""
        profile = Path("/tmp/aipi5-browser")
        argv = self.argv(profile=profile)
        # Built from the Path, not from the string, so the test says the same
        # thing on the Windows checkout the suite also runs on.
        self.assertIn(f"--user-data-dir={profile}", argv)
        self.assertFalse(any("aipi5-ui" in arg for arg in argv))

    def test_it_is_a_window_a_person_can_close(self):
        """Not --app and not --start-fullscreen. This project has twice built
        a window with no way out of it on a panel with no keyboard."""
        argv = self.argv()
        for trap in ("--app", "--start-fullscreen", "--kiosk"):
            self.assertFalse(any(arg.startswith(trap) for arg in argv), trap)
        # And short enough that the title bar fits on an 800 px screen.
        self.assertIn("--window-size=1280,770", argv)

    def test_pages_opened_here_get_no_camera_and_no_microphone(self):
        # The assistant owns the Brio and the TI capsule. A page asking for
        # either mid-turn is a fight nobody wins.
        self.assertIn("--use-fake-device-for-media-stream", self.argv())

    def test_only_one_disable_features_flag(self):
        # Chromium keeps the last occurrence and silently discards the rest,
        # so a second one here would quietly undo the first.
        flags = [a for a in self.argv() if a.startswith("--disable-features=")]
        self.assertEqual(len(flags), 1, flags)

    def test_it_does_not_reach_for_the_system_keyring(self):
        # Without this Chromium can raise a modal "choose a password for the
        # new keyring" dialog, centred and on top, on a device with nobody to
        # type into it. Same flag and reason as scripts/aipi5-ui.sh.
        self.assertIn("--password-store=basic", self.argv())

    def test_the_session_is_its_own(self):
        # So restarting the assistant does not close a window somebody is
        # watching a video in, and so nothing is left for a process nobody is
        # going to wait on.
        plugin = launcher()
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen:
            plugin.open(YOUTUBE)
        self.assertTrue(fake.kwargs.get("start_new_session"))

    def test_the_compositor_is_named_in_the_environment(self):
        plugin = launcher()
        fake = Launched()
        which, popen = with_chromium(fake)
        with which, popen:
            plugin.open(YOUTUBE)
        self.assertEqual(fake.env.get("WAYLAND_DISPLAY"), "wayland-0")


class TestTheSiteTable(unittest.TestCase):

    def test_no_command_takes_an_argument(self):
        """There is no spoken URL bar, and there is not going to be one.

        A `{site}` slot would make any page reachable by a mis-heard word.
        What this device will open is decided in source.
        """
        for command in launcher().commands():
            with self.subTest(command=command.name):
                self.assertFalse(command.takes_argument)
                self.assertEqual(command.params, {})
                self.assertFalse(any("{" in phrase
                                     for phrases in command.phrases.values()
                                     for phrase in phrases))

    def test_every_site_is_https_and_speakable_in_both_languages(self):
        for site in SITES:
            with self.subTest(site=site.name):
                self.assertTrue(site.url.startswith("https://"), site.url)
                self.assertEqual(set(site.phrases), {"en", "zh"})
                self.assertTrue(all(site.phrases.values()))

    def test_the_settings_page_can_see_it(self):
        described = launcher().describe()
        self.assertEqual(described["sites"], ["YouTube"])
        self.assertTrue(described["enabled"])
        self.assertFalse(described["running"])


class TestTheRuntimeSideOfIt(unittest.TestCase):
    """`AgentService.open_page` — the hop that makes the window the same one.

    Built with `object.__new__` and two attributes, because everything else an
    `AgentService` holds is a mailbox, a model client and a state directory,
    and none of them is on this path. That is itself worth pinning: opening a
    page must not start a run.
    """

    def service(self, answer=None):
        service = object.__new__(AgentService)
        service._gesture_lock = threading.Lock()
        service._browser_seen = (time.monotonic(), {"open": False})
        service.toolbox = types.SimpleNamespace(helper=FakeHelper(answer))
        return service

    def test_it_asks_the_helper_for_exactly_one_operation(self):
        service = self.service()
        answer = service.open_page("https://www.youtube.com/")
        self.assertTrue(answer["ok"])
        self.assertEqual(service.toolbox.helper.calls,
                         [("browser_open", {"url": "https://www.youtube.com/"})])

    def test_the_cached_browser_flag_is_dropped_so_a_hand_starts_at_once(self):
        """Hand control follows `browser.open` in the snapshot, and that is
        cached for four seconds. Four seconds of a hand that does nothing is
        the interval in which somebody decides the feature is broken."""
        service = self.service()
        self.assertIsNotNone(service._browser_seen)
        service.open_page("https://www.youtube.com/")
        self.assertIsNone(service._browser_seen)

    def test_a_refusal_comes_back_as_a_sentence_and_keeps_the_cache(self):
        service = self.service(answer=OpResult(op="browser_open",
                                               refused="only http:// and "
                                                       "https:// addresses"))
        with self.assertLogs("aipi5.agent", level="WARNING"):
            answer = service.open_page("file:///etc/shadow")
        self.assertFalse(answer["ok"])
        self.assertIn("https://", answer["error"])
        self.assertIsNotNone(service._browser_seen)

    def test_the_page_itself_is_not_sent_back(self):
        """`browser_open` answers with the accessibility tree and up to six
        thousand characters of text. That is what a model reads; it is nothing
        but weight on a socket to an assistant about to say four words."""
        service = self.service(answer=OpResult(
            op="browser_open", ok=True,
            result={"title": "YouTube", "text": "x" * 6000,
                    "elements": [{"ref": n} for n in range(60)]}))
        answer = service.open_page("https://www.youtube.com/")
        self.assertEqual(set(answer), {"ok", "url", "title"})
        self.assertEqual(answer["title"], "YouTube")

    def test_the_message_type_is_wired_and_bounded(self):
        # Read from the handler rather than trusted: a method nothing routes to
        # is a feature that works in tests and not on the device.
        source = (Path(__file__).resolve().parent.parent
                  / "aipi5" / "agent" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn('kind == "agent.open"', source)
        self.assertIn("open_page", source)

    def test_the_proxy_sends_that_type_and_nothing_else(self):
        sent = {}

        proxy = object.__new__(AgentProxy)
        proxy._request = lambda method, path, body=None, timeout=None: (
            sent.update(method=method, path=path, body=body, timeout=timeout)
            or (200, {"ok": True}))
        proxy.open_page("https://www.youtube.com/", timeout=12.0)
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/agent/v1/say")
        self.assertEqual(sent["body"], {"type": "agent.open",
                                        "url": "https://www.youtube.com/"})
        # Its own timeout, because the default one is sized for a long poll
        # and this is a navigation measured at 3-5 s on the device.
        self.assertEqual(sent["timeout"], 12.0)

    def test_opening_a_page_is_not_a_gesture(self):
        """The gesture table is what a *web page* can reach through the kiosk.

        `browser_open` takes an address, so it must stay out of it — and
        `tests/test_agent_hands.py` already asserts that. This is the other
        half: the two are separate methods, so widening one does not widen the
        other by accident.
        """
        self.assertNotIn("browser_open",
                         {op for op, _ in AgentService.GESTURES.values()})
        self.assertNotIn("open", AgentService.GESTURES)


if __name__ == "__main__":
    unittest.main()
