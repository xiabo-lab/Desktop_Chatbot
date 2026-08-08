"""What the model is allowed to do, and the gate everything goes through.

This is the security boundary of the whole assistant, so it is worth being
explicit about the shape of it: **the model never executes anything.** It emits
a tool name and a JSON blob. This module looks the name up in a fixed table,
validates the arguments, and calls a Python function. There is no path from
model output to a shell, a filesystem path, a URL, or an argument that is
interpolated into a command line. Section 14 requires that, and the way to make
it true rather than intended is for the dispatch table to be a dictionary of
literals that a reader can check in one screen.

Three rules that are easy to lose sight of and expensive to lose:

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

**Every tool answers, and none of them raises.** A tool that throws leaves the
model with a dangling call and the turn with an exception; a tool that returns
`{"error": ...}` leaves the model able to say "I can't check the news right
now", which is what the person in the room actually needs to hear.
"""

from __future__ import annotations

import json
import logging
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
                 vision=None, registry=None, launcher=None, settings=None):
        self.weather = weather
        self.news = news
        self.clock = clock
        self.camera = camera
        self.vision = vision
        self.registry = registry
        self.launcher = launcher
        self.settings = settings

        self._handlers: dict[str, Callable[[dict], str]] = {
            "get_weather": self._get_weather,
            "get_local_news": self._get_local_news,
            "get_current_time": self._get_current_time,
            "describe_camera_image": self._describe_camera_image,
            "open_kodama": self._open_kodama,
            "execute_kodama_command": self._execute_kodama_command,
        }

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

        if self.launcher is not None:
            tools.append(_schema(
                "open_kodama",
                "Start the Kodama-Lite music player if it is not already running. "
                "Call this before a music command when the player is not running.",
                {},
            ))

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

    # ── the tools themselves ─────────────────────────────────────────

    def _get_weather(self, args: dict) -> str:
        weather = self.weather.current()
        if weather is None:
            return _error("the weather service could not be reached")
        payload = weather.as_dict()
        when = str(args.get("when", "now")).lower()
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
        question = str(args.get("question", "") or "").strip()
        description = self.vision.describe(capture, question)
        if description is None:
            return _error("the picture was taken but could not be described")
        return _ok(description=description, taken_at=capture.taken_at)

    def _open_kodama(self, args: dict) -> str:
        result = self.launcher.open()
        return _ok(started=result.ok, detail=result.say("en"))

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
            return _error("the music player is not running; call open_kodama first")

        # At most one argument, and its name comes from the command's own
        # declaration rather than from the model. A command that takes no
        # argument is called with none, whatever the model sent.
        call_args = {}
        if command.params:
            slot = next(iter(command.params))
            value = str(args.get("argument", "") or "").strip()
            if not value:
                return _error(f"{command.name} needs a {slot}")
            call_args[slot] = value

        outcome = command.handler(**call_args)
        return _ok(command=command.name, succeeded=outcome.ok,
                   detail=outcome.say("en"))


def _schema(name: str, description: str, properties: dict,
            required: list[str] | None = None) -> dict:
    """One entry in the API's `tools` array.

    `additionalProperties: False` throughout, so a model that invents an extra
    field gets a schema violation rather than having it silently ignored — the
    ignored case is how an argument ends up somewhere nobody expected it.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }
