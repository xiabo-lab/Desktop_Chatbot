"""What the model is allowed to do, and the gate everything goes through.

This is the security boundary of the whole assistant, so it is worth being
explicit about the shape of it: **the model never executes anything.** It emits
a tool name and a JSON blob. This module looks the name up in a fixed table,
validates the arguments, and calls a Python function. There is no path from
model output to a shell, a filesystem path, a URL, or an argument that is
interpolated into a command line. Section 14 requires that, and the way to make
it true rather than intended is for the dispatch table to be a dictionary of
literals that a reader can check in one screen.

Four rules that are easy to lose sight of and expensive to lose:

**No destructive command is reachable.** `shutdown`, `reboot` and `quit` all
carry `confirm=True` in AIA's plugin declarations, and `_kodama_commands()`
filters on exactly that flag rather than on a hand-written deny list. A new
destructive command added to AIA is therefore excluded the moment it is
declared, with nothing here to remember to update — which is the opposite of
how a deny list ages.

**The model is not the router.** Ordinary commands — "pause", "next", "play
五月天" — never reach here at all; the fast path matched them in about nine
milliseconds and the turn was over. The Kodama tool exists for the cases the
phrase matcher legitimately cannot reach: "put on something quiet", "skip this,
I don't like it".

**Nothing is launched unless the person asked for it in this sentence.**
`open_known_app` can start the music player and open a known site, which the
model could not do at all until now — and the reason it could not is worth
restating, because the tool has to not reintroduce it. `execute_kodama_command`
used to answer "call open_kodama first" whenever the player was down, so any
music request at all, including one the model inferred from a half-heard
sentence, launched an app that resumes its previous queue on startup and begins
playing into the room. Reported as the player "starting on its own", which is
exactly what it was.

So the guard is not the prompt. `begin_turn` looks at the raw utterance for a
word that means *open* or *play* — "open", "put on", "打开", "放" — and the
launch tool is refused for the whole turn when there is none. That is a phrase
check on what the person actually said, decided before the model sees the
sentence and unchanged by anything it does with it, which is the same shape as
every other consent decision in this project: the model may ask, and something
that is not the model decides.

The tool is also not the only way, or even the usual one. The Music button, the
fast router's exact phrases, and `aipi5/kodama/launcher.py`'s own declarations
all still work in about nine milliseconds without a network. What this adds is
the sentence the phrase matcher legitimately cannot reach — "put some music on",
"can you bring up YouTube" — and nothing else.

**Every tool answers, and none of them raises.** A tool that throws leaves the
model with a dangling call and the turn with an exception; a tool that returns
`{"error": ...}` leaves the model able to say "I can't check the news right
now", which is what the person in the room actually needs to hear.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

log = logging.getLogger(__name__)

# How much of a tool's result is worth sending back. News with five stories and
# a forecast with four days are both comfortably inside this; the cap is here
# so that a feed which starts returning full article bodies cannot quietly
# multiply the cost of every conversation.
RESULT_LIMIT = 6000


def _ok(**payload) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)[:RESULT_LIMIT]


def _error(message: str) -> str:
    """A failure the model can say something sensible about.

    Phrased for the model rather than for a log: it is going to turn this into
    a sentence spoken out loud, so "the weather service did not answer" reads
    better once spoken than "HTTPSConnectionPool(...): Max retries exceeded".
    """
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


class ToolBox:
    """Everything the model may call, and the dispatch for it.

    Built once at startup with the already-constructed services, so a tool call
    costs a function call and not a service construction. Anything absent — no
    camera, no key, no Kodama — is simply not offered: `schemas()` leaves the
    tool out, which is a better failure than offering a tool that always
    returns an error and letting the model discover that at runtime.
    """

    def __init__(self, *, weather=None, news=None, clock=None, camera=None,
                 vision=None, registry=None, settings=None, web_search=False,
                 volume=None, birthdays=None, consent=None, agent=None,
                 calls=None, photos=None, on_capture=None, prices=None,
                 browser=None, launcher=None, delegate=None):
        self.weather = weather
        self.news = news
        self.clock = clock
        self.camera = camera
        self.vision = vision
        self.registry = registry
        self.settings = settings
        #: The master PipeWire sink, already built. Injected rather than
        #: reached for: the tool calls `VolumeControl.set()` and never shells
        #: out, which is what keeps "there is no path from model output to a
        #: command line" true of this file.
        self.volume = volume
        #: The family calendar's own store. Local, atomic, and **not** Google
        #: Calendar — the tool descriptions say so, because a model told it can
        #: "add to the calendar" will happily tell somebody their partner will
        #: see it.
        self.birthdays = birthdays
        #: Where an action that cannot be taken back waits for a person. See
        #: `aipi5/assistant/consent.py`; the rule that matters is that a model
        #: reporting "they said yes" is reporting, not deciding.
        self.consent = consent
        #: `AgentProxy`, for the three deterministic reminder messages and
        #: nothing else. **Not a way to reach the agent's tools**: it is a
        #: socket path and a `json.dumps`, the handlers below name three
        #: message types that are literals in this file, and
        #: `tests/test_agent_boundary.py` asserts that the voice service holds
        #: no client for the root helper and imports nothing else out of
        #: `aipi5.agent`. (It asserts that by looking for the class name as
        #: text, so this comment says it the long way round on purpose.)
        #:
        #: Reminders live over there because reboot survival, retry and the
        #: delivery handshake with `Housekeeping` all belong to one owner.
        #: Duplicating a `Schedule` here would be a second file of reminders
        #: that the phone never hears about.
        self.agent = agent
        #: `CallController`, or None where calling is off. The same object the
        #: panel's button uses, so there is one implementation of
        #: start-the-session, tell-the-page, send-the-push.
        #:
        #: It takes a *name that is already paired*, never a number, a URL or a
        #: push endpoint — see `aipi5/call/controller.py`. The enum below is
        #: built from that list at request time, so a model cannot name a phone
        #: that does not exist, and could not do anything with one if it did.
        self.calls = calls
        #: `PhotoCapture`, or None. Separate from `self.camera`, which is the
        #: device: "what do you see" borrows the camera for a moment and lets
        #: tmpfs reclaim the file, and "take a picture" keeps it. Two tools
        #: because they are two requests.
        self.photos = photos
        #: `on_capture(saved)` — how a photograph reaches the transcript, given
        #: the dict `PhotoCapture.take` returned. A callable rather than the UI
        #: state object, so this module still knows nothing about pages, and
        #: optional so a test can take a picture with no screen anywhere near
        #: it.
        self.on_capture = on_capture
        #: `PriceService`, or None. The rule it exists for is that **a price is
        #: never answered from the model's memory**: a model asked what Bitcoin
        #: is worth produces a confident number in the right currency with the
        #: right number of digits, and it is whatever was true when the model
        #: was trained. Nothing about it looks wrong from a kitchen.
        self.prices = prices
        #: `BrowserLauncher` and `KodamaLauncher`, for `open_known_app` and
        #: nothing else. Both take a *value from an enum built in code* — see
        #: `_known_apps` — never a URL, a command or a path.
        self.browser = browser
        self.launcher = launcher
        #: Whether the utterance this turn is answering asked for something to
        #: be opened or played. Set by `begin_turn` from the raw transcript,
        #: before the model sees it. False between turns, so a toolbox that is
        #: somehow called without one refuses rather than allows.
        self._explicit_launch = False
        #: `delegate(task, reason) -> dict` — `Coordinator.delegate`, which
        #: sends `agent.ask` down the socket and starts following the mailbox.
        #: A callable rather than the proxy, so this module cannot originate
        #: any other message to the agent, and so the coordinator stays the
        #: only thing that knows a run is in flight.
        self.delegate = delegate
        #: Whether the API's own web search is offered. Off by default and
        #: last in the list on purpose: it is the fallback for a current fact
        #: with no narrow provider, and a model given both will sometimes
        #: search the web for weather this device already has cached from a
        #: forecast API three hundred metres away. See `schemas()`.
        self.web_search = web_search

        self._handlers: dict[str, Callable[[dict], str]] = {
            "get_weather": self._get_weather,
            "get_local_news": self._get_local_news,
            "get_current_time": self._get_current_time,
            "describe_camera_image": self._describe_camera_image,
            "execute_kodama_command": self._execute_kodama_command,
            "get_master_volume": self._get_master_volume,
            "set_master_volume": self._set_master_volume,
            "list_birthdays": self._list_birthdays,
            "save_birthday": self._save_birthday,
            "delete_birthday": self._delete_birthday,
            "create_reminder": self._create_reminder,
            "read_reminders": self._read_reminders,
            "delete_reminder": self._delete_reminder,
            "call_phone": self._call_phone,
            "take_photo": self._take_photo,
            "get_asset_price": self._get_asset_price,
            "open_known_app": self._open_known_app,
            "delegate_agent_task": self._delegate_agent_task,
        }

    # ── the turn's own facts ─────────────────────────────────────────

    def begin_turn(self, text: str = "", source: str = "voice") -> None:
        """Told what the person actually said, before the model sees it.

        One flag comes out of this and it gates one tool. It is here rather
        than in the prompt because a prompt rule is a request and this is a
        control: the failure it prevents is an application starting in
        somebody's living room off a sentence that never asked for one, and
        that failure has already happened once on this device.

        Called by the coordinator, which is the single door every request goes
        through — see `aipi5/assistant/coordinator.py`. A turn that somehow
        arrives without it leaves the flag False, which refuses.
        """
        self._explicit_launch = asks_to_open(text)

    def end_turn(self) -> None:
        """Drop the turn's permission. Belt and braces beside `begin_turn`."""
        self._explicit_launch = False

    def _known_apps(self) -> dict:
        """What may be opened, by name. Built from code, never from YAML.

        A configuration file is editable by anything that can write to it, and
        an "apps" list in one is a list of things a model may launch that is
        one careless edit from being a list of arbitrary commands. These come
        from `SITES` and from the launcher this object was handed.
        """
        apps: dict[str, tuple[str, str]] = {}
        if self.launcher is not None:
            apps["music"] = ("kodama", "the Kodama-Lite music player")
        if self.browser is not None:
            from aipi5.browser.launcher import SITES

            for site in SITES:
                apps[site.name] = ("site", site.label)
        return apps

    # ── what the model is told exists ────────────────────────────────

    def _kodama_commands(self) -> list:
        """The Kodama commands the model may invoke.

        Filtered on `confirm`, not on a list of names. Anything AIA declares as
        needing confirmation is destructive by definition, and the person in
        the room has to be asked out loud before it happens — which is a
        conversation the model is not part of. See `aipi5/main.py`, where the
        confirmation is held.
        """
        if self.registry is None:
            return []
        return [
            (plugin, command)
            for plugin, command in self.registry.all_commands()
            if plugin.name == "kodama" and not command.confirm
        ]

    def schemas(self) -> list[dict]:
        """The tool list for the request. Only what is actually usable."""
        tools = []

        if self.weather is not None:
            tools.append(_schema(
                "get_weather",
                f"Current weather and a short forecast for "
                f"{self.settings.location.label if self.settings else 'the configured location'}. "
                f"Use this for any question about the weather; never answer from memory.",
                {"when": {
                    "type": "string",
                    "enum": ["now", "today", "forecast"],
                    "description": "'now' for current conditions, 'today' for today's "
                                   "high and low, 'forecast' for the next few days.",
                }},
            ))

        if self.news is not None:
            tools.append(_schema(
                "get_local_news",
                "Current local news headlines for San Jose, Santa Clara County and "
                "Silicon Valley. Returns headlines and one-line summaries; summarise "
                "three to five of them out loud, do not read them all.",
                {},
            ))

        if self.clock is not None:
            tools.append(_schema(
                "get_current_time",
                "The current local date and time on this device.",
                {},
            ))

        if self.camera is not None and self.vision is not None:
            tools.append(_schema(
                "describe_camera_image",
                "Take one picture with the camera on this device and describe what is "
                "in front of it. Use this when asked what you can see, what is in the "
                "room, or what is in front of the person. Takes a fresh picture every "
                "time.",
                {"question": {
                    "type": "string",
                    "description": "Optional. What the person specifically wants to "
                                   "know about the scene, if they asked something "
                                   "narrower than 'what do you see'.",
                }},
            ))

        # `commands` first, then the built-in, so the narrow local tools are
        # always ahead of the general one in the list the model reads.
        commands = self._kodama_commands()
        if commands:
            names = [command.name for _, command in commands]
            described = "; ".join(
                f"{command.name}: {command.description}"
                + (f" (argument: {', '.join(command.params)})" if command.params else "")
                for _, command in commands
            )
            tools.append(_schema(
                "execute_kodama_command",
                "Control the Kodama-Lite music player. Available commands — "
                + described
                + ". Plain spoken commands like 'pause' or 'next' are already handled "
                  "without you; use this for requests that need interpreting, such as "
                  "'put on something quiet' or 'I don't like this one'.",
                {
                    "command": {"type": "string", "enum": names,
                                "description": "Which command to run."},
                    "argument": {"type": "string",
                                 "description": "The command's argument where it takes "
                                                "one — a search query, a volume 0-100. "
                                                "Omit otherwise."},
                },
                required=["command"],
            ))

        if self.volume is not None:
            tools.append(_schema(
                "get_master_volume",
                "How loud this device is set, 0 to 100. This is the one master "
                "level for everything that makes a sound here — the assistant's "
                "own voice, the music player, games, calls and the browser.",
                {},
            ))
            tools.append(_schema(
                "set_master_volume",
                "Set how loud this device is, 0 to 100. This is the master "
                "level for everything: the assistant's voice, the music player, "
                "games, calls and the browser. Use it for 'turn it up', 'quieter', "
                "'mute' (0) and any specific level. Read the level back from the "
                "result rather than repeating the number you asked for — the "
                "device may not have accepted it.",
                {"percent": {
                    "type": "integer", "minimum": 0, "maximum": 100,
                    "description": "The level to set, 0 to 100. 0 is silent.",
                }},
                required=["percent"],
            ))

        if self.birthdays is not None:
            tools.append(_schema(
                "list_birthdays",
                "Every birthday saved in this device's own family calendar. "
                "This is the local calendar shown on the Calendar screen; it is "
                "not Google Calendar and nobody else can see it.",
                {},
            ))
            tools.append(_schema(
                "save_birthday",
                "Add a birthday to this device's own family calendar, or change "
                "one by passing its id. This is the local calendar shown on the "
                "Calendar screen; it is not Google Calendar, it is not shared, "
                "and you must not say that anybody else will see it.\n"
                "Ask a short follow-up question instead of guessing when you do "
                "not know the month and day, whether the date is solar (the "
                "ordinary calendar) or lunar (农历), or which person is meant. "
                "A date like 'the fifth' with no month is not enough.",
                {
                    "name": {"type": "string", "maxLength": 80,
                             "description": "Whose birthday it is, as the person "
                                            "said it."},
                    "calendar": {"type": "string", "enum": ["solar", "lunar"],
                                 "description": "'solar' for an ordinary date, "
                                                "'lunar' for a 农历 date. Ask if "
                                                "you are not sure."},
                    "month": {"type": "integer", "minimum": 1, "maximum": 12},
                    "day": {"type": "integer", "minimum": 1, "maximum": 31,
                            "description": "1-31 for solar, 1-30 for lunar."},
                    "year": {"type": "integer", "minimum": 1800, "maximum": 2100,
                             "description": "The year of birth, if it was given. "
                                            "Null otherwise — it is optional and "
                                            "must not be invented."},
                    "leap": {"type": "boolean",
                             "description": "A lunar leap month (闰月). Null "
                                            "unless the person said so."},
                    "note": {"type": "string", "maxLength": 400,
                             "description": "Anything else they said about it. "
                                            "Null if nothing."},
                    "id": {"type": "string", "maxLength": 64,
                           "description": "Only when changing an existing entry, "
                                          "using an id from list_birthdays. Null "
                                          "when adding a new one."},
                },
                required=["name", "calendar", "month", "day"],
            ))
            tools.append(_schema(
                "delete_birthday",
                "Remove a birthday from the local family calendar. Call "
                "list_birthdays first and pass the exact id of the one meant. "
                "The person is asked to confirm out loud before anything is "
                "removed, so do not say it has been deleted — say what you are "
                "about to remove and wait.",
                {"id": {"type": "string", "maxLength": 64,
                        "description": "The id from list_birthdays."}},
                required=["id"],
            ))

        if self.agent is not None:
            now = self.clock.as_dict() if self.clock is not None else {}
            tools.append(_schema(
                "create_reminder",
                "Set a reminder that this device will deliver at a given time. "
                "It survives a reboot and is sent to the paired phone as a "
                "notification.\n"
                "`when` must be an absolute local date and time that you have "
                "worked out yourself from the current time — "
                + (f"it is currently {now.get('date', '')} {now.get('time', '')} "
                   "here. " if now else "call get_current_time first if you are "
                                        "not sure. ")
                + "Never send 'tomorrow' or 'in an hour'; send the timestamp "
                  "those mean. If the person did not say enough to work one out "
                  "— 'remind me later' — ask them when.\n"
                "Read the time back from the result, not from what you sent: "
                "the device resolves it in its own timezone and that is the "
                "moment the reminder will actually arrive.",
                {
                    "when": {"type": "string", "maxLength": 64,
                             "description": "An absolute local time, like "
                                            "2026-09-03 09:00. No words."},
                    "text": {"type": "string", "maxLength": 500,
                             "description": "What to say when it arrives, in "
                                            "the person's own words where you "
                                            "can — 'call Mum', not 'the user "
                                            "wishes to telephone his mother'."},
                    "deliver": {"type": "string", "enum": ["push", "email"],
                                "description": "How to send it. 'push' is the "
                                               "paired phone and is the default; "
                                               "null means push."},
                },
                required=["when", "text"],
            ))
            tools.append(_schema(
                "read_reminders",
                "Every reminder this device is holding, waiting and recently "
                "delivered, with the time each is due.",
                {},
            ))
            tools.append(_schema(
                "delete_reminder",
                "Cancel a reminder that has not been delivered yet. Call "
                "read_reminders first and pass the exact id of the one meant.",
                {"id": {"type": "string", "maxLength": 64,
                        "description": "The id from read_reminders."}},
                required=["id"],
            ))

        apps = self._known_apps()
        if apps:
            tools.append(_schema(
                "open_known_app",
                "Open one of the applications or websites on this device: "
                + "; ".join(f"{name} — {label}" for name, (_, label)
                            in sorted(apps.items()))
                + ".\n"
                "**Only when the person has just asked for it to be opened or "
                "played.** Not because it would be useful, not because they "
                "mentioned it, and not as a step towards something else. "
                "Starting an application in somebody's living room is a person "
                "deciding, and the device refuses this tool outright on any "
                "turn where the request did not ask for one — so calling it "
                "otherwise wastes the turn and says so.",
                {"app": {"type": "string", "enum": sorted(apps),
                         "description": "Which one. There is nothing else this "
                                        "tool can open, and no way to give it "
                                        "a web address."}},
                required=["app"],
            ))

        if self.delegate is not None:
            tools.append(_schema(
                "delegate_agent_task",
                "Hand a long piece of work about this device to the maintenance "
                "agent, which can spend minutes on it. Use it for questions "
                "that need several checks — 'why did the screen go blank last "
                "night', 'is anything wrong with the camera', 'find out why "
                "music stopped yesterday' — and for research or browsing that "
                "cannot finish in this conversation.\n"
                "**Not for anything you can do here.** Setting the volume, "
                "reading the calendar, taking a picture, checking the weather "
                "and setting a reminder are all one call each; sending them to "
                "the agent spends one of the day's runs and takes minutes.\n"
                "It answers later, on the screen, not to you. Say one short "
                "sentence — 'I have started looking into that, it will be on "
                "the screen' — and nothing about what you expect it to find.",
                {
                    "task": {"type": "string", "maxLength": 2000,
                             "description": "What to look into, as a whole "
                                            "question. Include what the person "
                                            "actually said; the agent has none "
                                            "of this conversation."},
                    "reason": {"type": "string", "maxLength": 200,
                               "description": "One short line for the screen "
                                              "about what you have started. "
                                              "Null for the default."},
                },
                required=["task"],
            ))

        if self.prices is not None:
            known = self.prices.describe()
            tools.append(_schema(
                "get_asset_price",
                "The current price of a cryptocurrency, from a price service. "
                "**Always use this and never answer a price from memory** — a "
                "remembered price is months out of date and sounds exactly like "
                "a current one.\n"
                "Say the price, the currency and how recent it is. If the "
                "result carries a note, say that too: a price the device could "
                "not refresh is worth giving with its age attached, and worth "
                "nothing without it.",
                {
                    "asset": {"type": "string", "enum": known["assets"],
                              "description": "Which one. Only these are "
                                             "available; say so plainly if the "
                                             "person asked about something else."},
                    "currency": {"type": "string", "enum": known["currencies"],
                                 "description": "Which currency to quote in. "
                                                "Null for US dollars."},
                },
                required=["asset"],
            ))

        if self.photos is not None and self.photos.available():
            tools.append(_schema(
                "take_photo",
                "Take one picture with the camera on this device and **keep "
                "it**, in the transfer folder shown on the Files screen. Use "
                "this when somebody asks you to take a photo, a picture, or a "
                "snap — anything they mean to look at again later.\n"
                "This is not the same as describe_camera_image, which takes a "
                "picture in order to answer a question about what is in front "
                "of the camera and does not keep it. If somebody asks what you "
                "can see, use that one.\n"
                "Say the filename back from the result. The device decides it, "
                "and two pictures in the same second do not get the same name.",
                {"label": {
                    "type": "string", "maxLength": 40,
                    "description": "A couple of words about what it is, if they "
                                   "said — 'kitchen', 'the cat'. It becomes part "
                                   "of the filename. Null if they did not say.",
                }},
            ))

        # Only when there is something to ring. A tool that always answers
        # "no phone has registered for calls" is a tool the model discovers is
        # useless at runtime, in front of somebody who just asked for it.
        phones = self.calls.phones() if self.calls is not None else []
        if phones:
            tools.append(_schema(
                "call_phone",
                "Ring a paired phone from this device, so somebody can pick it "
                "up and talk to whoever is standing here. Use it for 'call my "
                "phone', 'ring me', 'call home'.\n"
                "The person at this device is asked out loud to confirm before "
                "anything rings, so do not say that the phone is ringing — say "
                "what you are about to do and stop."
                + (f" There is more than one paired phone ({', '.join(phones)}), "
                   "so ask which one if they did not say."
                   if len(phones) > 1 else ""),
                {"device": {
                    "type": "string", "enum": list(phones),
                    "description": "Which paired phone to ring. Null when there "
                                   "is only one. You cannot ring anything that "
                                   "is not in this list, and there is no way to "
                                   "give a phone number.",
                }},
            ))

        if self.web_search:
            # The API runs this one itself; there is no handler here and
            # nothing comes back through `call()`. Offered only for current
            # facts this device has no provider for — the weather, the news and
            # the clock all have one, and the prompt says to prefer them.
            tools.append({"type": "web_search"})

        return tools

    # ── dispatch ─────────────────────────────────────────────────────

    def call(self, name: str, arguments: str) -> str:
        """Run one tool call. Returns JSON. Never raises.

        `arguments` arrives as a JSON string from the model and is treated as
        untrusted input throughout: parsed defensively, and every value read
        out of it is either matched against an enum or passed to something that
        takes it as data.
        """
        handler = self._handlers.get(name)
        if handler is None:
            # Reachable when a model invents a tool name, which they do.
            log.warning("model asked for unknown tool %r", name)
            return _error(f"there is no tool called {name}")

        missing = self._missing_for(name)
        if missing is not None:
            # The tool was not in `schemas()`, so the model is guessing — which
            # models do, especially at a name they were offered yesterday and
            # is switched off today. Answered rather than raised: the generic
            # catch below would turn an `AttributeError` on a None service into
            # "failed unexpectedly", which is true and tells nobody anything.
            log.info("model asked for %r, which is not available here", name)
            return _error(missing)

        try:
            parsed = json.loads(arguments) if arguments else {}
            if not isinstance(parsed, dict):
                parsed = {}
        except ValueError:
            log.warning("tool %s was called with unparseable arguments: %r",
                        name, arguments[:200])
            parsed = {}

        log.info("tool %s(%s)", name, ", ".join(f"{k}={v!r}" for k, v in parsed.items()))
        try:
            return handler(parsed)
        except Exception:
            # A tool that throws must not end the turn. The model gets an
            # error it can speak about, and the trace goes to the journal.
            log.exception("tool %s failed", name)
            return _error(f"{name} failed unexpectedly")

    #: What each tool needs to exist, and what to say when it does not. The
    #: sentence is for the model to turn into speech, so it says what the
    #: device cannot do rather than which attribute was None.
    _NEEDS = {
        "get_weather": ("weather", "I cannot check the weather on this device"),
        "get_local_news": ("news", "I cannot check the news on this device"),
        "get_current_time": ("clock", "I cannot read the clock on this device"),
        # The camera one needs both halves — a camera with no vision model
        # behind it can take a picture nobody can describe.
        "describe_camera_image": ("camera",
                                  "there is no camera on this device"),
        "get_master_volume": ("volume", "I cannot control the volume here"),
        "set_master_volume": ("volume", "I cannot control the volume here"),
        "list_birthdays": ("birthdays", "there is no calendar on this device"),
        "save_birthday": ("birthdays", "there is no calendar on this device"),
        "delete_birthday": ("birthdays", "there is no calendar on this device"),
        "create_reminder": ("agent", "this device cannot set reminders"),
        "read_reminders": ("agent", "this device cannot set reminders"),
        "delete_reminder": ("agent", "this device cannot set reminders"),
        "call_phone": ("calls", "this device cannot make calls"),
        "take_photo": ("photos", "this device cannot keep photographs"),
        "get_asset_price": ("prices", "I cannot look up prices on this device"),
        "delegate_agent_task": ("delegate",
                                "there is no maintenance agent on this device"),
    }

    def _missing_for(self, name: str) -> str | None:
        needed = self._NEEDS.get(name)
        if needed is None:
            return None
        attribute, sentence = needed
        if getattr(self, attribute, None) is None:
            return sentence
        if name == "describe_camera_image" and self.vision is None:
            return "I cannot describe what the camera sees on this device"
        return None

    # ── the tools themselves ─────────────────────────────────────────

    def _get_weather(self, args: dict) -> str:
        weather = self.weather.current()
        if weather is None:
            return _error("the weather service could not be reached")
        payload = weather.as_dict()
        when = str(args.get("when") or "now").lower()
        if when == "now":
            # The forecast is dropped rather than sent and ignored. It is
            # about 400 tokens, on every weather question, for information the
            # model was not asked for and will not use.
            payload.pop("forecast", None)
        return _ok(**payload)

    def _get_local_news(self, args: dict) -> str:
        stories = self.news.as_dicts()
        if not stories:
            return _error("no local news feed could be reached")
        return _ok(stories=stories, count=len(stories))

    def _get_current_time(self, args: dict) -> str:
        return _ok(**self.clock.as_dict())

    def _describe_camera_image(self, args: dict) -> str:
        if not self.camera.available():
            return _error("the camera is not available on this device right now")
        capture = self.camera.capture_still()
        if capture is None:
            return _error("the camera did not take a picture")
        # `or ""` rather than a default, because strict mode sends an
        # explicit `null` for an argument the model has nothing to say about —
        # `args.get("question", "")` returns None in that case, not "".
        question = str(args.get("question") or "").strip()
        description = self.vision.describe(capture, question)
        if description is None:
            return _error("the picture was taken but could not be described")
        return _ok(description=description, taken_at=capture.taken_at)

    # ── the device's own state ───────────────────────────────────────

    def _get_master_volume(self, args: dict) -> str:
        state = self.volume.describe()
        return _ok(percent=state["level"], available=state["available"],
                   error=state.get("error", ""))

    def _set_master_volume(self, args: dict) -> str:
        """Set the one sink every application on this device meets at.

        The number is clamped rather than refused. Strict mode already bounds
        it 0-100 in the schema, and a model that sends 150 anyway means "as
        loud as it goes" — refusing that to be pedantic about a number nobody
        said out loud helps nobody in the room.

        What is **read back** is what the device actually has, not what was
        asked for. `VolumeControl.set()` rolls back when it cannot persist, so
        a reply that repeated the requested number would be the assistant
        confidently announcing a change that had already been undone.
        """
        try:
            percent = int(args.get("percent"))
        except (TypeError, ValueError):
            return _error("a volume needs to be a number from 0 to 100")
        percent = max(0, min(100, percent))

        if not self.volume.set(percent):
            state = self.volume.describe()
            return _error(state.get("error")
                          or "this device would not accept a new volume")
        state = self.volume.describe()
        return _ok(percent=state["level"], requested=percent,
                   muted=state["level"] == 0)

    # ── the family calendar ──────────────────────────────────────────

    def _list_birthdays(self, args: dict) -> str:
        entries = [{"id": item["id"], "name": item["name"],
                    "calendar": item["calendar"], "month": item["month"],
                    "day": item["day"], "year": item.get("year"),
                    "leap": item.get("leap", False), "note": item.get("note", "")}
                   for item in self.birthdays.list()]
        return _ok(birthdays=entries, count=len(entries),
                   calendar="this device's local family calendar")

    def _save_birthday(self, args: dict) -> str:
        """Add or change one entry, through the store's own validation.

        Nothing is re-checked here. `BirthdayStore.save()` decides what a valid
        month, day, year and lunar leap month are, and it writes atomically —
        a second copy of those rules in this file is a second copy to get out
        of step, and the one that would drift is this one.
        """
        from aipi5.calendar.store import BirthdayError

        payload = {
            "name": args.get("name"),
            "calendar": args.get("calendar"),
            "month": args.get("month"),
            "day": args.get("day"),
            "year": args.get("year"),
            "leap": bool(args.get("leap")),
            "note": args.get("note") or "",
        }
        # Only when changing an existing entry. An empty string is what strict
        # mode's `null` becomes, and passing it as an id would make the store
        # look for an entry called "".
        entry_id = str(args.get("id") or "").strip()
        if entry_id:
            payload["id"] = entry_id

        try:
            saved = self.birthdays.save(payload)
        except BirthdayError as exc:
            return _error(str(exc))
        return _ok(saved=saved, updated=bool(entry_id),
                   calendar="this device's local family calendar, not Google "
                            "Calendar")

    def _delete_birthday(self, args: dict) -> str:
        """Ask first, delete second, and never in one call.

        Deleting is the one thing in this file that cannot be undone by saying
        the opposite — the entry and its note are gone. So the id is resolved
        to a real person's name *here*, out of the store, and the question the
        device asks out loud names that person rather than an id. A model that
        misread which entry was meant is then caught by somebody hearing the
        wrong name, which is the only check that actually works.
        """
        if self.consent is None:
            return _error("this device cannot ask for confirmation right now, "
                          "so nothing was removed")
        entry_id = str(args.get("id") or "").strip()
        found = next((item for item in self.birthdays.list()
                      if item["id"] == entry_id), None)
        if found is None:
            return _error("there is no birthday with that id; call "
                          "list_birthdays and use an id from it")

        when = f"{found['month']}/{found['day']}"
        label = f"{found['name']} ({found['calendar']} {when})"
        pending = self.consent.ask(
            what=f"delete the birthday for {found['name']}",
            question=f"Do you want me to remove {label} from the calendar?",
            detail=label,
            action=lambda: _delete_now(self.birthdays, entry_id),
        )
        if pending is None:
            return _error("there is already a question waiting for an answer")
        return _ok(asked=True, question=pending.question,
                   removed=False,
                   instruction="Ask the person exactly this and stop. Nothing "
                               "has been removed yet and you must not say it "
                               "has.")

    # ── reminders ────────────────────────────────────────────────────
    #
    # Three forwards. The reminder itself is made in `aipi5-agent.service`, by
    # `AgentService.reminder_create`, which validates the timestamp and calls
    # `Schedule.add` — **no run, no budget, no model**. What that buys is a
    # reminder that is as dependable as an alarm clock rather than as
    # dependable as a conversation: the old route was `agent.ask`, so "remind
    # me at nine to call Mum" spent one of the day's runs and up to 24 model
    # steps, and a run that wandered off was a reminder that never got made
    # with nothing said about it.

    def _create_reminder(self, args: dict) -> str:
        when = str(args.get("when") or "").strip()
        text = str(args.get("text") or "").strip()
        if not when:
            return _error("a reminder needs an absolute time, like "
                          "2026-09-03 09:00. Ask the person when they want it.")
        if not text:
            return _error("a reminder needs something to say")
        deliver = str(args.get("deliver") or "push")

        status, answer = self.agent.reminder_create(when, text, deliver)
        if status != 200:
            return _error(str(answer.get("error", ""))
                          or "this device could not save the reminder")
        if not answer.get("ok"):
            return _error(str(answer.get("error", "")) or "that time did not work")
        # `when` and `deliver` come back off the record that was written, not
        # off what was sent. Reading the request back would be the assistant
        # confirming a time the device may have refused, adjusted, or read in a
        # different zone.
        return _ok(**answer["reminder"])

    def _read_reminders(self, args: dict) -> str:
        status, answer = self.agent.reminder_list()
        if status != 200 or not answer.get("ok"):
            return _error(str(answer.get("error", ""))
                          or "this device could not read its reminders")
        reminders = answer.get("reminders", [])
        return _ok(reminders=reminders, count=len(reminders))

    def _delete_reminder(self, args: dict) -> str:
        ident = str(args.get("id") or "").strip()
        if not ident:
            return _error("call read_reminders and pass the id of the one to "
                          "cancel")
        status, answer = self.agent.reminder_cancel(ident)
        if status != 200 or not answer.get("ok"):
            return _error(str(answer.get("error", ""))
                          or "this device could not cancel that reminder")
        return _ok(cancelled=answer.get("cancelled", {}))

    # ── handing work over ────────────────────────────────────────────

    def _delegate_agent_task(self, args: dict) -> str:
        """Start a maintenance run and come straight back.

        The answer does not arrive through this call and cannot: a run takes
        minutes, the voice turn is judged against 2500 ms, and holding the loop
        open for it would be an assistant that stopped responding to the room.
        So this returns a run id, and the run's checks and its answer arrive in
        the same transcript afterwards — see `AgentBridge`.

        What the model is told back is deliberately thin. It gets no tools, no
        transcript and no promise, because the next thing it does is speak one
        sentence and end the turn, and a model handed a plausible-looking
        summary will describe findings that do not exist yet.
        """
        task = str(args.get("task") or "").strip()
        if not task:
            return _error("there is nothing to look into")
        reason = str(args.get("reason") or "").strip()

        answer = self.delegate(task, reason)
        if not answer.get("ok"):
            return _error(str(answer.get("error", ""))
                          or "the maintenance agent could not take that on")
        return _ok(started=True, run=answer.get("run", ""),
                   appended=bool(answer.get("appended")),
                   instruction="Say one short sentence — that you have started "
                               "looking and the progress is on the screen — and "
                               "nothing about what it might find. You will not "
                               "be told the answer; it appears on the screen by "
                               "itself.")

    # ── opening something ────────────────────────────────────────────

    def _open_known_app(self, args: dict) -> str:
        """Start a named application. Refused unless the person asked.

        Two gates, and they are not redundant. The **enum** decides what can be
        opened at all: a value not in `_known_apps` reaches no launcher, so
        there is no URL, command or path a model can compose. The **flag**
        decides whether anything may be opened on this turn, from the raw
        transcript rather than from the model's account of it.

        Losing either one is the failure this device has already had: a music
        request the model inferred from a half-heard sentence starting a player
        that resumes its previous queue and begins playing into the room.
        """
        if not self._explicit_launch:
            log.info("refusing to open %r: the utterance did not ask for it",
                     args.get("app"))
            return _error("I only open things when the person has just asked "
                          "me to. Tell them what to say — 'open YouTube', or "
                          "'put some music on' — rather than opening it.")

        wanted = str(args.get("app") or "").strip().lower()
        apps = self._known_apps()
        found = apps.get(wanted)
        if found is None:
            return _error(f"there is nothing called {wanted!r} on this device")

        kind, label = found
        if kind == "kodama":
            outcome = self.launcher.open(who="the assistant, asked out loud")
            return _ok(opened=wanted, label=label, succeeded=outcome.ok,
                       detail=outcome.say("en"))

        from aipi5.browser.launcher import SITES

        site = next((s for s in SITES if s.name == wanted), None)
        if site is None:
            return _error(f"there is nothing called {wanted!r} on this device")
        outcome = self.browser.open(site, who="the assistant, asked out loud")
        return _ok(opened=wanted, label=label, succeeded=outcome.ok,
                   detail=outcome.say("en"))

    # ── a price, or nothing ──────────────────────────────────────────

    def _get_asset_price(self, args: dict) -> str:
        """A real quote with a timestamp, or an error. There is no third case.

        Both arguments are looked up in tables inside
        `aipi5/tools/prices.py`, so what reaches the provider's query string is
        a literal from that file whatever the model wrote — the same rule the
        Kodama command follows, and for the same reason.
        """
        from aipi5.tools.prices import PriceError

        try:
            quote = self.prices.quote(str(args.get("asset") or ""),
                                      str(args.get("currency") or "usd"))
        except PriceError as exc:
            return _error(str(exc))
        return _ok(**quote.as_dict())

    # ── taking a picture and keeping it ──────────────────────────────

    def _take_photo(self, args: dict) -> str:
        """One still, into the folder the Files screen already shows.

        No confirmation. A photograph of the room, taken because somebody in
        the room asked for one, is not in the same class as a phone ringing in
        a pocket — it is reversible by deleting the file, it goes nowhere off
        this device, and asking "shall I?" every time would make the feature
        annoying enough not to be used.

        The camera's owner is the thing that can genuinely fail here. A call, a
        game, hand control and the screensaver handoff all borrow it, and the
        answer says *who* has it: "the camera is being used by a video call" is
        something a person can act on, and "the camera did not take a picture"
        is not.
        """
        from aipi5.photos.capture import PhotoError

        try:
            saved = self.photos.take(str(args.get("label") or ""))
        except PhotoError as exc:
            return _error(str(exc))

        # The photograph goes into the transcript beside the sentence about it.
        # After the save rather than before, so what appears on the screen is a
        # picture that is actually on disk.
        if self.on_capture is not None:
            try:
                self.on_capture(saved)
            except Exception:                        # noqa: BLE001
                log.warning("could not publish the photograph to the screen",
                            exc_info=True)
        return _ok(**{k: v for k, v in saved.items() if k != "token"})

    # ── ringing a phone ──────────────────────────────────────────────

    def _call_phone(self, args: dict) -> str:
        """Ask first. Nothing rings from this call.

        A phone buzzing in somebody's pocket because a half-heard sentence
        sounded like a request is the sort of thing a house stops trusting a
        device over — and unlike the volume it cannot be undone by saying the
        opposite. So this parks the ring behind a question and answers the
        model with the sentence to say. What happens next is decided by a
        phrase matcher over the raw transcript, or by a finger on the panel.

        The device name is checked against the paired list **here** as well as
        in the controller. Two checks for the same thing, deliberately: this
        one keeps the question honest — "shall I ring Alex's phone?" must not
        be asked about a phone that does not exist — and the controller's is
        what actually stands between model output and a notification.
        """
        if self.consent is None:
            return _error("this device cannot ask for confirmation right now, "
                          "so nothing was rung")
        phones = self.calls.phones()
        if not phones:
            return _error("no phone has registered for calls on this device")

        device = str(args.get("device") or "").strip()
        if not device:
            if len(phones) > 1:
                # Not "the first one". Ringing the wrong person's phone is
                # worse than one more short question.
                return _error("there is more than one paired phone ("
                              + ", ".join(phones)
                              + "). Ask which one they mean.")
            device = phones[0]
        elif device not in phones:
            return _error(f"{device!r} is not a paired phone. The phones on "
                          f"this device are: " + ", ".join(phones))

        pending = self.consent.ask(
            what=f"ring {device}",
            question=f"Shall I ring {device}?",
            detail=device,
            action=lambda: self.calls.call_out(device).as_dict(),
        )
        if pending is None:
            return _error("there is already a question waiting for an answer")
        return _ok(asked=True, device=device, question=pending.question,
                   ringing=False,
                   instruction="Ask the person exactly this and stop. Nothing "
                               "is ringing yet and you must not say it is.")

    def _execute_kodama_command(self, args: dict) -> str:
        """Run one named Kodama command.

        The name is looked up in the *same* list the model was shown, which is
        itself filtered on `confirm`. So there is no way to reach a destructive
        command from here even if the model asks for one by name — it is not in
        the table, and an unknown name is refused rather than guessed at.
        """
        wanted = str(args.get("command", "")).strip()
        commands = {command.name: (plugin, command)
                    for plugin, command in self._kodama_commands()}
        found = commands.get(wanted)
        if found is None:
            log.warning("model asked for Kodama command %r, which is not offered",
                        wanted)
            return _error(f"{wanted!r} is not a command this player accepts")

        plugin, command = found
        if not plugin.available():
            return _error("the music player is not running, and you cannot start "
                          "it; tell the person to press the Music button on the "
                          "screen, or to say 'open the music player'")

        # At most one argument, and its name comes from the command's own
        # declaration rather than from the model. A command that takes no
        # argument is called with none, whatever the model sent.
        call_args = {}
        if command.params:
            slot = next(iter(command.params))
            value = str(args.get("argument") or "").strip()
            if not value:
                return _error(f"{command.name} needs a {slot}")
            call_args[slot] = value

        outcome = command.handler(**call_args)
        return _ok(command=command.name, succeeded=outcome.ok,
                   detail=outcome.say("en"))


#: What "open this" or "play this" looks like in the two languages spoken to
#: this device. Patterns rather than substrings, because English separates its
#: particles — "put **some music** on", "turn the record **on**" — and a
#: contiguous "put on" misses every sentence anybody actually says. Mandarin
#: has no spaces to find a boundary with, so those stay plain substrings.
#:
#: Erring towards *not* matching is the safe direction. A miss means somebody
#: says "open YouTube" again and the fast router catches it in about nine
#: milliseconds; a false match means an application starting in a living room.
#:
#: `\b` on the English verbs matters more than it looks. Without it "what's
#: playing" contains "play", and "what's playing" is a question about the
#: player rather than a request to start one.
_OPEN_PATTERNS = (
    r"\b(open|play|launch|start|resume|watch|stream)\b",
    r"\b(put|turn|switch)\b.{0,24}?\bon\b",
    r"\b(bring|pull)\b.{0,24}?\bup\b",
    r"\b(go to|fire up|listen to|show me)\b",
    # Mandarin. 打开 and 播放 are the plain forms; the 放… ones are how a
    # request for music is actually phrased, and 上 alone is deliberately not
    # here — it is a preposition far more often than it is a verb.
    r"打开|播放|放一|放点|放首|放个|放歌|来点|来首|启动|开一下|开个|听一|听点",
)


def asks_to_open(text: str) -> bool:
    """Did this sentence ask for something to be opened or played?

    Decided from the transcript, before the model reads it, and never revisited
    afterwards. The point is that it is not the model's opinion: a model that
    has decided somebody wants music will say so convincingly, and what this
    guards is an application starting in a room where nobody asked.
    """
    lowered = str(text or "").lower()
    return any(re.search(pattern, lowered) for pattern in _OPEN_PATTERNS)


def _delete_now(store, entry_id: str) -> dict:
    """What runs if — and only if — a person says yes. See `ConsentDesk`."""
    from aipi5.calendar.store import BirthdayError

    try:
        store.delete(entry_id)
    except BirthdayError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "deleted": entry_id}


def _schema(name: str, description: str, properties: dict,
            required: list[str] | None = None) -> dict:
    """One entry in the API's `tools` array, in `/v1/responses` shape.

    Flat — `name`, `description` and `parameters` sit on the tool rather than
    inside a nested `function` object, which is the one difference from the
    Chat Completions spelling `aipi5/agent/tools.py` still uses.

    **`strict: True`, and what it costs.** Strict mode is what makes the
    schema a guarantee instead of a suggestion: the model cannot invent a
    field, cannot omit a required one, and cannot send a string where a number
    belongs. Its price is that *every* property must appear in `required` —
    there is no such thing as an optional argument. So an argument that is
    genuinely optional is declared nullable and listed as required, and the
    model passes `null` when it has nothing to say. That is the shape below,
    and it is why `required` is a parameter of this function rather than
    simply every key.

    `additionalProperties: False` throughout, so a model that invents an extra
    field gets a schema violation rather than having it silently ignored — the
    ignored case is how an argument ends up somewhere nobody expected it.
    """
    required = list(required or [])
    declared = {}
    for field, spec in properties.items():
        if field in required:
            declared[field] = spec
            continue
        # Optional, so nullable. `type` may already be a list — a future
        # argument that takes a number or a string — and is normalised to one
        # either way rather than special-cased.
        spec = dict(spec)
        kinds = spec.get("type", "string")
        kinds = list(kinds) if isinstance(kinds, list) else [kinds]
        if "null" not in kinds:
            kinds.append("null")
        spec["type"] = kinds
        declared[field] = spec

    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": declared,
            # Every key, because strict mode admits no other kind. What makes
            # an argument optional is the `null` added above, not its absence
            # from this list.
            "required": list(declared),
            "additionalProperties": False,
        },
        "strict": True,
    }
