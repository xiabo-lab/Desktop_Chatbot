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

**The model cannot start the music player.** There is no `open_kodama` tool and
this class holds no reference to `KodamaLauncher`, so there is no object here
to call `open()` on — which is a stronger guarantee than a tool that was
removed and could be added back without anybody noticing what it costs.

The player is opened by the Music button, or by asking for it out loud
(`aipi5/kodama/launcher.py` declares the phrases). Both are a person deciding.
The model used to be a third way, and it was the only one that could fire
without anybody asking for the player by name: `execute_kodama_command`
answered "call open_kodama first" whenever the player was down, so any music
request at all — including one the model inferred from a half-heard sentence —
launched an app that resumes its previous queue on startup and begins playing
into the room. Reported as the player "starting on its own", which is exactly
what it was. It now says what it cannot do and who can.

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
                 vision=None, registry=None, settings=None, web_search=False,
                 volume=None, birthdays=None, consent=None):
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
