"""AIPI5 — the voice loop.

Wake word → capture → transcribe → route → act or answer → speak. The shape is
AIA's, because AIA's is the one that was measured on this hardware and lands
inside the latency budget, and the parts that do the work here *are* AIA's:
`aia.audio.wake`, `aia.audio.capture`, `aia.audio.vad`, `aia.stt`,
`aia.tts.piper`, `aia.router.fast` and `aia.plugins.kodama` are imported and
used unchanged.

What this file adds is the fork the router's decline used to fall off the end
of. In AIA, an utterance nothing matched is repeated back — "You said: …" — and
that was M2's job, left undone. Here it goes to OpenAI with tools, and comes
back as an answer, the weather, the news, a story, or a description of what the
camera can see.

Two paths, and the difference between them is the whole performance story:

    fast    wake → STT → phrase match (~9 ms) → Kodama/system → done
    slow    wake → STT → declined → OpenAI (+ tools) → reply → Piper

Run it:

    systemctl --user stop aia        # the microphone allows exactly one reader
    .venv/bin/python -m aipi5.main

Environment variables, all AIA's and honoured here for the same reasons:

    AIA_NO_WAKE=1     any speech is a command
    AIA_DEBUG=1       debug logging
    AIA_SAVE_AUDIO=1  keep captured utterances for diagnosis
    AIPI5_NO_UI=1     no web UI (the voice loop does not depend on it)
    AIPI5_NO_LLM=1    behave exactly like AIA — decline instead of answering
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from contextlib import ExitStack
from dataclasses import replace

from aipi5.core import aia_bridge  # noqa: F401  — puts AIA on sys.path

from aia.audio import wake as wake_mod
from aia.audio.capture import Microphone
# The confirmation prompt and the yes/no reader, taken from AIA rather than
# rewritten. `is_affirmative` in particular is not the two-line function it
# looks like: it compares Chinese by *sound*, because the same speaker saying
# the same 确定 came back simplified three times and traditional three times in
# one evening, and character matching therefore cancelled shutdowns that had
# been clearly authorised. Reimplementing that would mean rediscovering it.
from aia.main import CONFIRM_PROMPT, is_affirmative
from aia.audio.ducking import Ducker
from aia.audio.vad import Endpointer
from aia.core.state import Machine, State
from aia.plugins.base import Registry, Result
from aia.plugins.kodama import KodamaLite
from aia.plugins.system import System
from aia.router.fast import FastRouter
from aia.stt import build as build_stt
from aia.tts.language import reply_language
from aia.tts.piper import Speaker
from aia.ui.history import ConversationLog
from aia.ui.retention import Retention

from aipi5 import __version__
from aipi5.core import config as config_mod
from aipi5.core import preflight
from aipi5.core.presence import Presence, PresenceTracker, ScreensaverPolicy
from aipi5.kodama.launcher import KodamaLauncher
from aipi5.llm import prompts
from aipi5.llm.client import OpenAIClient
from aipi5.llm.conversation import Conversation
from aipi5.llm.tools import ToolBox
from aipi5.tools.clock import Clock
from aipi5.tools.news import NewsService
from aipi5.tools.story import instructions as story_instructions
from aipi5.tools.story import parse as parse_story
from aipi5.tools.weather import WeatherService
from aipi5.ui.server import WebUI
from aipi5.ui.state import UiState
from aipi5.vision.camera import Camera
from aipi5.vision.describe import VisionDescriber
from aipi5.vision.person_detection import PresenceWatcher, build as build_detector

log = logging.getLogger("aipi5")

# Reused from AIA verbatim, including the reasoning: a wake word that fires on
# silence otherwise loops — open a turn, wait out `max_wait_ms`, close it, open
# another — ducking and un-ducking the music on every pass.
EMPTY_TURN_REFRACTORY_S = 1.0

LISTENING_TEXT = {"en": "Listening…", "zh": "我在听…"}
CONFIRM_LISTEN = {"en": "Say yes or no…", "zh": "请回答“确定”或“取消”…"}
THINKING_TEXT = {"en": "Thinking…", "zh": "让我想想…"}

# What a request that could not be answered sounds like. One sentence, because
# the person is standing there and a paragraph of apology is worse than a short
# one.
TROUBLE = {"en": "Sorry, something went wrong.", "zh": "抱歉，出错了。"}
NO_LLM = {
    "en": "I can't have a conversation right now, but music and the weather still work.",
    "zh": "我现在不能聊天，不过音乐和天气还可以用。",
}

# Utterances that are a story request rather than a question. Matched loosely
# and only to change the model's register — a false positive costs a slightly
# longer answer, not a wrong action, which is why this can afford to be a
# substring check rather than another router.
_STORY_MARKERS = ("bedtime story", "tell me a story", "tell a story",
                  "read me a story", "another story", "讲个故事", "讲一个故事",
                  "睡前故事", "讲故事", "说个故事")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("AIA_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("openwakeword", "numba", "urllib3", "httpx", "httpcore",
                  "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def wants_story(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _STORY_MARKERS)


class Assistant:
    """Everything that is built once, and the turn that uses it.

    A class rather than one long function because there are now four things
    that can start a turn — the wake word, a screen button, a spoken follow-up,
    and the confirmation prompt holding the floor — and they all need the same
    dozen objects. AIA could keep them as locals in `main()` because it had
    one entry point.
    """

    def __init__(self, settings):
        self.settings = settings
        self.aia = settings.aia_config()
        self.ui_state = UiState()
        self.started = time.time()
        self.save_audio = os.environ.get("AIA_SAVE_AUDIO") == "1"

        # Started first, so a restart also expires whatever the last session
        # left behind and the sweep has happened before the first turn.
        self.history = ConversationLog(self.aia.retention.database)
        self.retention = Retention(self.aia.retention, self.history)
        self.retention.start()

        # ── voice, all of it AIA's ───────────────────────────────────
        self.stt = build_stt(self.aia.stt, self.aia.audio.target_rate)
        self.speaker: Speaker | None = None
        self.detector_wake = None
        self.endpointer = Endpointer(self.aia.audio, self.aia.vad)
        self.confirm_endpointer = Endpointer(
            self.aia.audio, replace(self.aia.vad,
                                    max_wait_ms=self.aia.vad.confirm_wait_ms))
        self.ducker = Ducker()
        self.machine = Machine(self.aia.target_latency_ms)

        # ── the command set: AIA's, plus one launcher ────────────────
        self.player = KodamaLite()
        self.launcher = KodamaLauncher(settings.kodama, self.player)
        plugins = [self.player, System()]
        if settings.kodama.enabled:
            plugins.append(self.launcher)
        self.registry = Registry(plugins)
        self.router = FastRouter(self.registry, wake_words=self.aia.wake.variants)

        # ── what this project adds ───────────────────────────────────
        self.clock = Clock(settings.location.timezone)
        self.weather = WeatherService(settings.location, settings.weather)
        self.news = NewsService(settings.news)
        self.camera = Camera(settings.camera)

        llm_off = (os.environ.get("AIPI5_NO_LLM") == "1"
                   or not settings.assistant.llm_enabled)
        self.llm = None if llm_off else OpenAIClient(settings.openai,
                                                     config_mod.credentials())
        self.vision = VisionDescriber(self.llm) if self.llm else None
        self.conversation = Conversation(settings.openai.context_turns,
                                         settings.openai.context_idle_s)
        self.toolbox = ToolBox(
            weather=self.weather, news=self.news, clock=self.clock,
            camera=self.camera if settings.camera.enabled else None,
            vision=self.vision,
            registry=self.registry,
            launcher=self.launcher if settings.kodama.enabled else None,
            settings=settings,
        )

        # ── presence and the screen ──────────────────────────────────
        self.tracker = PresenceTracker(settings.person.frames_to_appear,
                                       settings.person.frames_to_disappear)
        self.screensaver = ScreensaverPolicy(settings.screensaver.timeout_seconds,
                                             settings.screensaver.enabled)
        self.watcher: PresenceWatcher | None = None
        self.web = WebUI(settings.display, state=self.ui_state,
                         history=self.history, info=self.system_info)
        self.report: preflight.Report | None = None
        # Filled in by `start()`; defaulted here so `verify()` and the settings
        # page are safe to call against an assistant that failed to finish
        # starting, which is exactly when somebody is looking at them.
        self._ui_started = False
        self._llm_ok = False
        self._llm_detail = ""

    # ── startup ──────────────────────────────────────────────────────

    def start(self) -> bool:
        """Bring everything up. False only when something critical failed."""
        log.info("AIPI5 %s starting (AIA %s)", __version__, aia_bridge.version())

        if not self.stt.wait_ready():
            log.error("speech recognition (%s) is not usable — see the error above",
                      self.aia.stt.backend)
            self.stt = None  # type: ignore[assignment]

        self.speaker = Speaker(self.aia.tts)
        self.speaker.warm()

        self.detector_wake = wake_mod.build(self.aia.wake, self.aia.audio)

        if self.settings.camera.enabled:
            self.camera.open()

        # Person detection needs the camera, so it is built after it and only
        # when there is one. A detector polling a camera that never opened
        # would run forever finding nothing and hold presence at UNKNOWN,
        # which looks exactly like a broken detector.
        detector = build_detector(self.settings.person)
        if self.camera.available() and detector.available():
            self.watcher = PresenceWatcher(
                self.camera, detector, self.tracker,
                self.settings.person.interval_ms, on_change=self.on_presence)
            self.watcher.start()
        elif self.settings.person.enabled:
            log.warning("person detection is configured but cannot run "
                        "(camera %s, detector %s)",
                        "ready" if self.camera.available() else "unavailable",
                        detector.name)

        ui_started = False
        if os.environ.get("AIPI5_NO_UI") != "1":
            ui_started = self.web.start()

        self._ui_started = ui_started

        # Asked once, at startup, off the voice path. A model name the API does
        # not recognise is then a line in the boot log naming the model, rather
        # than an apology to the first person who speaks.
        self._llm_ok = False
        self._llm_detail = "conversation is turned off in the configuration"
        if self.llm is not None and self.llm.available:
            self._llm_ok, self._llm_detail = self.llm.probe()
            if not self._llm_ok:
                # Not fatal. Everything deterministic still works and the
                # screen says which model was refused.
                log.error("conversation will not work: %s", self._llm_detail)
        elif self.llm is not None:
            self._llm_detail = self.llm.error or "no client"

        # The weather is fetched once at boot rather than on the first draw, so
        # the screensaver has something on it the moment it first appears
        # instead of a dash that fills in ten seconds later.
        self.refresh_weather()
        self.publish()
        return self.stt is not None

    def verify(self, mic) -> "preflight.Report":
        """The section 31 checks, run once the capture stream is actually open.

        Separate from `start()` and called after it for one reason: the
        microphone is the first thing on the critical list and there is no
        honest way to check it before the stream exists. Reporting it from
        `start()` would mean either a check that always fails or one that
        reports on a device it has not opened, and the second is the sort of
        green tick that makes a boot log worth less than no boot log.
        """
        self.report = preflight.run(
            self.settings,
            mic=mic,
            stt=self.stt,
            speaker=(self.speaker is not None, ""),
            camera=(self.camera.available(), self.camera.error or ""),
            detector=(self.watcher is not None, ""),
            llm=(self._llm_ok, self._llm_detail or ""),
            wake=self.detector_wake,
            ui_started=self._ui_started,
        )
        self.publish()
        return self.report

    # ── the screen ───────────────────────────────────────────────────

    def on_presence(self, event) -> None:
        """Called from the detector thread. Must not block."""
        self.screensaver.presence_changed(event)
        self.publish()

    def refresh_weather(self) -> None:
        weather = self.weather.current()
        self.ui_state.update(weather=weather.as_dict() if weather else None)

    def publish(self, **extra) -> None:
        """Push the current state to whatever the screen polls."""
        self.ui_state.update(
            assistant=self.machine.state.value,
            presence=self.tracker.state.value,
            screensaver=self.screensaver.should_show(),
            kodama_running=self.player.available(),
            degraded=self.report.degraded if self.report else [],
            **extra,
        )

    def system_info(self) -> dict:
        """The settings page, built fresh on every request."""
        return {
            "aipi5": {"version": __version__, "aia": aia_bridge.version(),
                      "uptime_s": round(time.time() - self.started),
                      "config": str(self.settings.source or "defaults")},
            "display": {"width": self.settings.display.width,
                        "height": self.settings.display.height},
            "location": self.settings.location.label,
            "stt": self.stt.describe() if self.stt else {"engine": "not started"},
            "tts": self.speaker.describe() if self.speaker else [],
            "llm": self.llm.describe() if self.llm else {"available": False},
            "credentials": config_mod.describe_credentials(),
            "camera": self.camera.describe(),
            "presence": self.watcher.describe() if self.watcher
            else {"backend": "not running", "state": self.tracker.state.value},
            "screensaver": {"enabled": self.settings.screensaver.enabled,
                            "timeout_s": self.settings.screensaver.timeout_seconds,
                            "showing": self.screensaver.showing},
            "conversation": self.conversation.describe(),
            "kodama": {"running": self.player.available(),
                       "service": self.settings.kodama.service},
            "checks": self.report.as_dict() if self.report else {},
        }

    # ── the slow path ────────────────────────────────────────────────

    def answer(self, text: str, language: str) -> str:
        """What to say to an utterance the router declined.

        The whole of the LLM integration is here, and it is short on purpose:
        assemble the facts the device already knows, pick a register, send,
        speak what comes back. Everything that could go wrong has been pushed
        into `OpenAIClient`, which never raises and always returns a sentence.
        """
        if self.llm is None or not self.llm.available:
            return NO_LLM.get(language, NO_LLM["en"])

        forgot = self.conversation.begin_turn()
        if forgot:
            log.info("starting a fresh conversation")

        moment = self.clock.as_dict()
        facts = {
            "time": moment["time"],
            "date": moment["date"],
            "kodama_running": self.player.available(),
        }
        system = prompts.with_facts(self.settings.location.label, language, facts)

        if wants_story(text):
            # A story is a different register and a different length, so it is
            # asked for as its own instruction rather than as a conversational
            # turn with a hint. The rules it is generated under are in
            # `aipi5/tools/story.py` where they can be read as a list.
            request = parse_story(text, language,
                                  self.settings.story.default_minutes,
                                  self.settings.story.max_minutes)
            log.info("bedtime story: %r, about %.1f minutes",
                     request.subject or "anything", request.minutes)
            system = system + "\n\n" + prompts.STORY_MODE
            self.conversation.user(story_instructions(request))
        else:
            self.conversation.user(text)

        reply = self.llm.respond(self.conversation, system, self.toolbox)
        self.conversation.trim()

        if reply.tool_calls:
            log.info("used %s", ", ".join(reply.tool_calls))
        if not reply:
            log.warning("no answer (%s)", reply.error)
            return _cannot(reply.error, language)

        log.info("llm %.0f ms: %r", reply.ms, reply.text[:100])
        # A camera description reached the screen through the tool result; the
        # spoken reply is the model's summary of it, and both belong on the
        # display. The description is what the tool put there.
        if self.vision is not None and "describe_camera_image" in reply.tool_calls:
            self.ui_state.update(camera_description=self.vision.last_description)
        return reply.text

    # ── shutdown ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Release everything, on any exit path, from any point in `start()`.

        Every entry is guarded individually and several are guarded against the
        thing not existing at all, because `start()` can fail at any line and
        this runs from `main()`'s `finally` regardless of how far it got.
        systemd papers over the difference by killing the whole cgroup;
        `python -m aipi5.main` by hand, which the README documents, does not —
        and a Piper process or a Hailo device left held is the kind of thing
        that makes the *next* start fail for an unrelated-looking reason.
        """
        for name, closer in (
            ("presence", lambda: self.watcher and self.watcher.stop()),
            ("camera", self.camera.close),
            ("wake", lambda: self.detector_wake and self.detector_wake.close()),
            ("stt", lambda: self.stt and self.stt.close()),
            ("speaker", lambda: self.speaker and self.speaker.close()),
            ("llm", lambda: self.llm and self.llm.close()),
            ("weather", self.weather.close),
            ("news", self.news.close),
            ("web", self.web.stop),
            ("retention", self.retention.stop),
            # Last, so anything the final turn queued is written before the
            # database connection goes.
            ("history", self.history.close),
        ):
            try:
                closer()
            except Exception:
                log.debug("closing %s failed", name, exc_info=True)


def _cannot(error: str, language: str) -> str:
    """A failed request, as one spoken sentence.

    The specific reason is deliberately not read out. "The API does not
    recognise the model 'gpt-5.6-luna'" is exactly right for the journal and
    means nothing to somebody standing in a kitchen; it is on the screen and in
    the log, and what they hear is what they can do about it.
    """
    if "key" in error or "rate limit" in error or "quota" in error:
        return ("I can't reach my language model right now."
                if language == "en" else "我现在联系不上语言模型。")
    if "time" in error or "server" in error or "answer" in error:
        return ("That took too long to answer. Try again?"
                if language == "en" else "回答超时了，再试一次好吗？")
    return ("Sorry, I couldn't work that one out."
            if language == "en" else "抱歉，这个我没想明白。")


def main() -> int:
    setup_logging()

    try:
        settings = config_mod.load()
    except config_mod.ConfigError as exc:
        # A configuration error is a person halfway through an edit. Say what
        # is wrong on one line rather than raising a traceback out of a
        # systemd unit, where it lands in the journal as forty lines of stack.
        log.error("%s", exc)
        return 2

    assistant = Assistant(settings)
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        log.info("shutting down")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not assistant.start():
        log.error("cannot start without speech recognition")
        assistant.close()
        return 1

    cfg = assistant.aia
    speaker = assistant.speaker
    machine = assistant.machine
    stt_language = cfg.stt.default_language

    # Opening the microphone is the one critical step that happens *after*
    # everything else is built, and it raises rather than returning a status —
    # so without this it is the only startup failure in the whole program that
    # reaches the journal as a traceback. Measured, on the day the capsule was
    # unplugged while the camera was being fitted: twenty-one lines of stack
    # whose last line was the only one that mattered.
    #
    # Exiting is still right. A voice assistant that cannot hear is not
    # degraded, it is off, and `Restart=on-failure` with `StartLimitIntervalSec=0`
    # means the service keeps trying — so plugging the microphone back in
    # recovers the device with nothing to run by hand.
    # Both halves are guarded: constructing raises when no capsule matches a
    # configured profile, and entering raises when one does but the device is
    # busy — the microphone allows exactly one reader, so a dying instance
    # still holding it lands here. The two want different advice and neither
    # wants a stack trace.
    opening = ExitStack()
    try:
        mic = opening.enter_context(Microphone(cfg.audio))
    except Exception as exc:
        log.error("cannot open the microphone: %s", exc)
        if "no input device matching" in str(exc):
            log.error("Plug the capture device back in — `arecord -l` should "
                      "list a card. The service keeps retrying, so it will "
                      "come back on its own.")
        else:
            log.error("Something else may be holding it: `systemctl --user "
                      "stop aia` and check for a stray `python -m aipi5.main`.")
        assistant.close()
        return 1

    try:
        with opening:
            frames = mic.frames()
            assistant.verify(mic)
            log.info("AIPI5 ready — say %s", cfg.wake.phrase)
            last_empty_turn = 0.0
            last_weather = time.monotonic()

            while not stopping:
                frame = next(frames)

                # Two ways a turn starts. The wake word, and a button on the
                # screen — which is the same event as far as everything below
                # is concerned, and is checked here rather than on its own
                # thread so there is still exactly one thing driving the
                # microphone.
                requested = assistant.ui_state.take_action()
                woke = assistant.detector_wake.detect(frame)

                if not woke and requested is None:
                    # The housekeeping that has to happen while nothing is
                    # being said, done here because this is the only place the
                    # loop is idle. The weather refresh is bounded by its own
                    # cache anyway; this is what stops the screensaver from
                    # showing a reading from two hours ago.
                    if time.monotonic() - last_weather > settings.weather.cache_seconds:
                        last_weather = time.monotonic()
                        assistant.refresh_weather()
                    assistant.publish()
                    continue

                if woke and time.monotonic() - last_empty_turn < EMPTY_TURN_REFRACTORY_S:
                    continue

                # Somebody is here, whatever the camera thinks. A person can
                # speak from outside its view, and answering onto a clock face
                # is answering into the wrong screen.
                #
                # Presence is passed so the countdown can restart rather than
                # stop: a command spoken from the next room does not put a
                # person in front of the camera, and the screen has to go back
                # to sleep afterwards. See `ScreensaverPolicy.suppress`.
                assistant.screensaver.suppress(
                    person_present=assistant.tracker.state is Presence.PERSON_PRESENT)

                turn = machine.begin_turn()
                machine.to(State.LISTENING)

                if requested is not None and requested != "wake":
                    # A button that names what it wants. It does not need the
                    # microphone at all, so the turn is served straight from
                    # the tools and the loop goes back to listening.
                    handle_button(assistant, requested, stt_language, turn)
                    machine.end_turn()
                    assistant.publish()
                    continue

                # Silence the music FIRST. The microphone and the speakers
                # share a room, so a command given over music is captured as
                # the command plus the song. AIA's reasoning, unchanged.
                if assistant.ducker.duck():
                    mic.drain()

                assistant.publish(listening_text=LISTENING_TEXT.get(
                    stt_language, LISTENING_TEXT["en"]))

                audio = assistant.endpointer.collect(frames)
                turn.mark("captured")
                if audio is None:
                    assistant.ducker.restore()
                    assistant.detector_wake.reset()
                    machine.end_turn()
                    assistant.publish(listening_text="")
                    last_empty_turn = time.monotonic()
                    continue

                machine.to(State.THINKING)
                intent = None
                try:
                    result = assistant.stt.listen(audio)
                    turn.mark("stt")
                    text = result.text.strip()

                    if not text:
                        machine.to(State.SPEAKING)
                        apology = ("I'm sorry, could you repeat that?"
                                   if stt_language == "en" else "抱歉，请再说一遍。")
                        say(assistant, apology, stt_language)
                        machine.end_turn()
                        assistant.detector_wake.reset()
                        mic.drain()
                        continue

                    language = reply_language(text, fallback=result.language)
                    stt_language = result.language
                    assistant.history.record("user", text, language)
                    assistant.publish(listening_text="")

                    chain = assistant.router.match_sequence(text)
                    intent = chain[-1] if chain else None
                    turn.mark("routed")

                    speak = False

                    if intent is not None and intent.command.confirm:
                        reply, speak, intent = confirm_and_run(
                            assistant, mic, frames, intent, language)
                        turn.mark("acted")
                    elif intent is not None:
                        machine.to(State.ACTING)
                        missing = next(
                            (s for s in chain if not s.plugin.available()), None)
                        if missing is not None:
                            reply = Result.failed(
                                f"{missing.plugin.description} is not currently running.",
                                f"{missing.plugin.description} 没有在运行。",
                            ).say(language)
                            speak = True
                        else:
                            for step in chain:
                                outcome = step.command.handler(**step.arguments)
                                reply = outcome.say(language)
                            speak = intent.command.speaks
                        turn.mark("acted")
                    else:
                        # The fork this whole project exists for. AIA repeats
                        # the utterance back here; AIPI5 answers it.
                        machine.to(State.THINKING)
                        assistant.publish(listening_text=THINKING_TEXT.get(
                            language, THINKING_TEXT["en"]))
                        reply = assistant.answer(text, language)
                        turn.mark("llm")
                        speak = True

                    machine.to(State.SPEAKING)
                    assistant.publish(listening_text="")
                    assistant.history.record("aia", reply, language)
                    if speak:
                        speaker.say(reply, language, blocking=False)
                        turn.mark("audio_out")
                        speaker.wait()
                    else:
                        turn.mark("audio_out")
                        log.info("reply not spoken (%s): %r",
                                 f"{intent.plugin.name}.{intent.command.name}"
                                 if intent is not None else "no command", reply)

                except Exception:
                    log.exception("turn failed")
                    machine.to(State.ERROR)
                    try:
                        say(assistant, TROUBLE.get(stt_language, TROUBLE["en"]),
                            stt_language)
                    except Exception:
                        log.exception("could not announce the failed turn")
                finally:
                    if intent is not None and intent.command.stops_playback:
                        assistant.ducker.forget()
                    else:
                        assistant.ducker.restore()
                    machine.end_turn()
                    assistant.detector_wake.reset()
                    mic.drain()
                    assistant.publish(listening_text="")

    finally:
        assistant.close()

    return 0


def say(assistant, text: str, language: str) -> None:
    """Speak and record in one call, so the two cannot drift apart."""
    assistant.history.record("aia", text, language)
    assistant.publish()
    if assistant.speaker is not None:
        assistant.speaker.say(text, language)


def confirm_and_run(assistant, mic, frames, intent, language):
    """Ask before a destructive command, holding the floor for the answer.

    AIA's logic, unchanged and for its reasons: asking and then returning to
    idle made the reply a separate request that needed the wake word again, so
    "确定" arrived as "小艾同学，确定" and the shutdown was silently dropped. A
    question you have to be re-summoned to answer is not a question.

    Returns (reply, speak, intent) — `intent` comes back as None when the
    action was cancelled, so the caller restores the music.
    """
    question = CONFIRM_PROMPT.get(language, CONFIRM_PROMPT["en"]).format(
        what=intent.command.describe(language))
    log.info("asking to confirm %s.%s", intent.plugin.name, intent.command.name)

    assistant.machine.to(State.SPEAKING)
    assistant.history.record("aia", question, language)
    assistant.publish()
    # Blocking: the answer must not be recorded over our own question.
    assistant.speaker.say(question, language, blocking=True)
    mic.drain()

    assistant.machine.to(State.LISTENING)
    assistant.publish(listening_text=CONFIRM_LISTEN.get(language,
                                                        CONFIRM_LISTEN["en"]))
    answer_audio = assistant.confirm_endpointer.collect(frames)
    decision = None
    if answer_audio is not None:
        answer = assistant.stt.listen(answer_audio, language=language)
        if answer.text.strip():
            assistant.history.record("user", answer.text, language)
            decision = is_affirmative(answer.text)
        log.info("confirmation answer %r -> %s", answer.text, decision)
    else:
        log.info("no answer to the confirmation")

    if decision is True:
        assistant.machine.to(State.ACTING)
        return intent.command.handler(**intent.arguments).say(language), True, intent

    # Silence, "no", or anything unclear all cancel. For an irreversible
    # action "I could not tell" must mean no.
    return ("Cancelled." if language == "en" else "已取消。"), True, None


def handle_button(assistant, action: str, language: str, turn=None) -> None:
    """A press on the touchscreen, served without the microphone.

    Each of these is the same work the equivalent spoken request does, reached
    by a different door. None of them is destructive — `aipi5/ui/state.py`
    holds the list and explains why.

    `turn` is marked the moment audio starts, for the same reason the voice
    path marks it there: reading a reply aloud is the answer arriving, not
    latency. Without it every button press logged a budget violation the length
    of whatever was said — a four-second news summary read out over twenty-five
    seconds was reported as `turn 28666ms [OVER by 26166ms]`, which is the sort
    of line that teaches people to stop reading the journal.
    """
    log.info("button: %s", action)
    assistant.machine.to(State.ACTING)

    if action == "weather":
        weather = assistant.weather.current()
        reply = (weather.summary(language) if weather else
                 ("I can't reach the weather right now." if language == "en"
                  else "现在联系不上天气服务。"))
        assistant.refresh_weather()
    elif action == "news":
        reply = assistant.answer(
            "What's the local news?" if language == "en" else "本地有什么新闻？",
            language)
    elif action == "camera":
        reply = assistant.answer(
            "What do you see?" if language == "en" else "你看到了什么？", language)
    elif action == "kodama":
        reply = assistant.launcher.open().say(language)
    else:
        return

    if turn is not None:
        turn.mark("acted")
    assistant.machine.to(State.SPEAKING)
    assistant.history.record("aia", reply, language)
    assistant.publish()
    if assistant.speaker is not None:
        # Non-blocking, then marked, then waited on — the same three lines the
        # voice path uses, and in that order for the same reason. What the
        # person experiences is the wait before hearing anything.
        assistant.speaker.say(reply, language, blocking=False)
        if turn is not None:
            turn.mark("audio_out")
        assistant.speaker.wait()
    elif turn is not None:
        turn.mark("audio_out")


if __name__ == "__main__":
    sys.exit(main())
