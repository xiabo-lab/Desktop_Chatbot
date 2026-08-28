"""What the agent may ask for, described to the model.

**A sibling of `aipi5/llm/tools.py`, never an entry in it.** That module opens
with a guarantee — no path from model output to a shell, a filesystem path, a
URL, or an argument interpolated into a command line — and the assistant is safe
to leave listening in a room because of it. This one deliberately reaches the
system, so it lives in a different process, under a different Unix user, behind
a helper that will only do what `policy.py` lists. `tests/test_agent_boundary.py`
asserts the two dispatch tables never meet.

What is shared is the *shape*, and only the shape: `_schema` comes from there so
both sets of schemas refuse extra properties the same way, and `call()` makes
the same promise — **it returns JSON and never raises**. A tool that throws
leaves the model holding a call with no result, which the API rejects on the
next request.

**Every path-shaped parameter is an enum.** Not a validated string — an enum.
There is nothing here for a model to compose, so the argument that reaches the
helper is one of a handful of literals this file wrote down. The one exception
is `set_config`, which takes a section and a key rather than a path at all.

**Changing anything is three beats: propose, ask, apply.** The helper is asked
what would happen, a person is shown that and waits, and only then is the helper
told to do the thing it described. The helper independently checks that the
second call matches the first, so a bug here cannot turn one approval into a
different change.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from aipi5.agent import schedule as schedule_mod
from aipi5.agent.helper_client import HelperClient
from aipi5.llm.tools import _schema

#: The helper refuses an `apply` that arrives less than three seconds after its
#: `propose` — nobody read a diff that fast, so it can only be a machine. That
#: guard belongs there and stays there. But it must never be able to fail a
#: *genuine* approval, and it can: somebody expecting the card and tapping
#: straight away is well inside three seconds. So the wait is served here
#: instead of being refused there. The helper is still the one enforcing it.
PROPOSAL_SETTLE_S = 3.5

log = logging.getLogger(__name__)

#: Four times the assistant's `RESULT_LIMIT`. A journal read is the largest
#: thing that comes back and 6000 characters is about forty lines of it, which
#: is not enough to diagnose anything.
RESULT_LIMIT = 24000

JOURNAL_UNITS = ["aipi5", "aipi5-ui", "kodama-lite", "aia", "aipi5-agent",
                 "tailscaled", "system"]
SERVICES = ["aipi5", "aipi5-ui", "kodama-lite", "aia", "aipi5-agent",
            "tailscaled"]
SINCE = ["-15m", "-1h", "-6h", "-24h", "today", "yesterday", "boot"]

#: What may be restarted. `aipi5-agent` is absent: restarting the runtime would
#: end the run mid-sentence, and the helper refuses it anyway.
RESTARTABLE = ["aipi5", "aipi5-ui", "kodama-lite"]

READABLE = [
    "/home/fuwenxu/AIPI5/config/aipi5.yaml",
    "/home/fuwenxu/AIPI5/systemd/aipi5.service",
    "/home/fuwenxu/AIPI5/systemd/aipi5-ui.service",
    "/home/fuwenxu/.config/systemd/user/aipi5.service",
    "/home/fuwenxu/.config/systemd/user/aipi5-ui.service",
    "/etc/chromium.d/00-rpi-vars",
]

LISTABLE = [
    "/home/fuwenxu/AIPI5/config",
    "/home/fuwenxu/AIPI5/systemd",
    "/home/fuwenxu/AIPI5/.deploy-backups",
    "/var/lib/aipi5-agent-helper/changes",
    "/var/log/aipi5-agent",
    "/etc/chromium.d",
]


class AgentToolBox:
    """The agent's dispatch table. Its own, and small.

    The interesting decisions — what may be read or written, which units exist,
    whether a line of the journal may leave the house — are all on the other
    side of the socket, in root-owned code this process cannot edit.
    """

    #: A restart plus its health check can legitimately take minutes:
    #: `aipi5.service` declares `TimeoutStartSec=180` and a cold start uses much
    #: of it. The helper bounds this properly; this only has to be longer.
    apply_timeout_s = 300.0

    #: Starting Chromium takes seconds and a navigation can take more; the
    #: helper bounds each CDP call properly, so this only has to outlast it.
    browser_timeout_s = 120.0

    #: A source change compiles, then runs the whole suite, then restarts the
    #: assistant and is health-checked. The suite alone is a minute.
    patch_timeout_s = 480.0

    def __init__(self, helper: HelperClient | None = None, approvals=None):
        self.helper = helper or HelperClient()
        #: The `ApprovalDesk`, or None. **None means no change can happen** —
        #: `_approve` refuses rather than assuming, so a misconfigured runtime
        #: is one that cannot alter the device rather than one that alters it
        #: without asking.
        self.approvals = approvals
        #: Set per run, so the helper's audit log can join an operation to the
        #: conversation that caused it.
        self.run = ""
        #: Set by the runtime. Reminders are the one thing the agent stores
        #: itself rather than asking the helper for — nothing about them is
        #: privileged, and the helper has no reason to know they exist.
        self.schedule = None
        #: Also set by the runtime. Notes are the agent's own words about the
        #: person's preferences; skills are procedure written by a person and
        #: installed as root, which the agent can read and never write.
        self.notes = None
        self._handlers: dict[str, Callable[[dict], str]] = {
            "read_journal": self._read_journal,
            "service_status": self._service_status,
            "read_config_file": self._read_config_file,
            "list_directory": self._list_directory,
            "system_facts": self._system_facts,
            "set_config": self._set_config,
            "restart_service": self._restart_service,
            "rollback": self._rollback,
            "list_changes": self._list_changes,
            "browser_open": self._browser_open,
            "browser_read": self._browser_read,
            "browser_click": self._browser_click,
            "browser_type": self._browser_type,
            "browser_back": self._browser_back,
            "browser_screenshot": self._browser_screenshot,
            "browser_close": self._browser_close,
            "remind_me": self._remind_me,
            "list_reminders": self._list_reminders,
            "cancel_reminder": self._cancel_reminder,
            "remember": self._remember,
            "forget": self._forget,
            "read_skill": self._read_skill,
            "patch_file": self._patch_file,
            "read_source": self._read_source,
        }

    # ── what the model is told ──────────────────────────────────────

    def schemas(self) -> list[dict]:
        return [
            _schema(
                "read_journal",
                "Read recent warnings and errors from a service's log on this "
                "Raspberry Pi. Only WARNING, ERROR and CRITICAL lines are "
                "available — ordinary activity is not, because the log also "
                "carries what people said to the device.",
                {
                    "unit": {"type": "string", "enum": JOURNAL_UNITS,
                             "description": "Which service's log to read. "
                                            "'system' is the whole machine."},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 500,
                              "description": "How many recent lines. Default 120."},
                    "since": {"type": "string", "enum": SINCE,
                              "description": "How far back to look. Default -6h."},
                    "contains": {"type": "string", "maxLength": 80,
                                 "description": "Keep only lines containing this "
                                                "text. Plain text, not a pattern."},
                },
                required=["unit"],
            ),
            _schema(
                "service_status",
                "Whether one of this Pi's services is running, when it last "
                "started, how many times it has restarted, and how it last "
                "exited.",
                {"service": {"type": "string", "enum": SERVICES}},
                required=["service"],
            ),
            _schema(
                "read_config_file",
                "Read one of this device's configuration files.",
                {"path": {"type": "string", "enum": READABLE}},
                required=["path"],
            ),
            _schema(
                "list_directory",
                "List the files in one of this device's directories.",
                {"path": {"type": "string", "enum": LISTABLE}},
                required=["path"],
            ),
            _schema(
                "system_facts",
                "Uptime, load, memory, disk space and CPU temperature for this "
                "Raspberry Pi.",
                {},
            ),
            _schema(
                "browser_open",
                "Open a web page on this device's screen, in a browser of the "
                "agent's own. It appears over the assistant's display and is "
                "closed again with browser_close. Returns what is on the page "
                "and a numbered list of the things that can be clicked or "
                "typed into.",
                {"url": {"type": "string", "maxLength": 2000,
                         "description": "An http:// or https:// address."}},
                required=["url"],
            ),
            _schema(
                "browser_read",
                "What is on the page that is already open, and the numbered "
                "list of things on it that can be used.",
                {},
            ),
            _schema(
                "browser_click",
                "Click one of the numbered items from the page listing.",
                {"ref": {"type": "integer", "minimum": 1, "maximum": 60,
                         "description": "The number beside the item."}},
                required=["ref"],
            ),
            _schema(
                "browser_type",
                "Type into whatever is focused on the page — click a search "
                "box first. Set submit to press Enter afterwards.",
                {
                    "text": {"type": "string", "maxLength": 500},
                    "submit": {"type": "boolean",
                               "description": "Press Enter after typing."},
                },
                required=["text"],
            ),
            _schema(
                "browser_back",
                "Go back to the previous page.",
                {},
            ),
            _schema(
                "browser_screenshot",
                "Save a picture of the page to the shared folder, so the "
                "person can look at it on their phone's Files screen.",
                {},
            ),
            _schema(
                "browser_close",
                "Close the browser and give the screen back to the assistant. "
                "Do this when the person is finished with it.",
                {},
            ),
            _schema(
                "remember",
                "Remember something for future conversations — a preference, "
                "or a fact about this household. It is shown to you at the "
                "start of every run afterwards, so keep it short and only "
                "record things that will still be true next month.",
                {"text": {"type": "string", "maxLength": 200}},
                required=["text"],
            ),
            _schema(
                "forget",
                "Forget one of the things you were told to remember.",
                {"id": {"type": "string", "maxLength": 20,
                        "description": "The note's id, shown with it."}},
                required=["id"],
            ),
            _schema(
                "read_skill",
                "Read one of this device's written procedures in full. The "
                "names and summaries are listed for you already; fetch the "
                "body when you are actually doing that job.",
                {"name": {"type": "string", "maxLength": 50}},
                required=["name"],
            ),
            _schema(
                "remind_me",
                "Set a reminder. It arrives as a notification on the person's "
                "phone at the time given, and survives the device being "
                "rebooted. Work out the absolute date and time yourself from "
                "what they said and the current time you were given.",
                {
                    "when": {"type": "string", "maxLength": 40,
                             "description": "The absolute local date and time, "
                                            "as 2026-08-28 09:00."},
                    "text": {"type": "string", "maxLength": 500,
                             "description": "What to say. Write it as the "
                                            "person would want to read it on a "
                                            "lock screen."},
                },
                required=["when", "text"],
            ),
            _schema(
                "list_reminders",
                "The reminders waiting, soonest first, and recently finished "
                "ones.",
                {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            ),
            _schema(
                "cancel_reminder",
                "Cancel a reminder that has not gone off yet.",
                {"id": {"type": "string", "maxLength": 40,
                        "description": "The id from list_reminders."}},
                required=["id"],
            ),
            _schema(
                "set_config",
                "Change one setting in this device's configuration. The person "
                "is shown exactly which line would change and has to approve "
                "it. The setting must already exist — this changes values, it "
                "does not add new ones. Applying it restarts the assistant, "
                "checks that it came back, and puts the old value back if it "
                "did not.",
                {
                    "section": {"type": "string", "maxLength": 40,
                                "description": "The top-level section, such as "
                                               "screensaver or openai."},
                    "key": {"type": "string", "maxLength": 60,
                            "description": "The setting inside it, such as "
                                           "day_start."},
                    "value": {"type": ["string", "number", "boolean"],
                              "description": "The new value, written as it "
                                             "should appear in the file."},
                },
                required=["section", "key", "value"],
            ),
            _schema(
                "read_source",
                "Read one of this device's own source files, so you can see "
                "what it does before changing it. Give the path relative to "
                "the checkout, like aipi5/ui/server.py.",
                {"path": {"type": "string", "maxLength": 200,
                          "description": "Relative to /home/fuwenxu/AIPI5, "
                                         "such as aipi5/tools/weather.py."}},
                required=["path"],
            ),
            _schema(
                "patch_file",
                "Change one exact piece of text in one of this device's source "
                "files. The person is shown what would be replaced and has to "
                "approve it. **The whole test suite then runs, and the change "
                "is only kept if it passes** — otherwise it is put back and "
                "the assistant is restarted on the old code. This takes a "
                "couple of minutes.",
                {
                    "path": {"type": "string", "maxLength": 200,
                             "description": "Relative to the checkout."},
                    "old": {"type": "string", "maxLength": 20000,
                            "description": "The exact text to replace, "
                                           "including its indentation. It must "
                                           "appear exactly once in the file, so "
                                           "include enough surrounding lines to "
                                           "be unique."},
                    "new": {"type": "string", "maxLength": 20000,
                            "description": "What to put there instead."},
                },
                required=["path", "old", "new"],
            ),
            _schema(
                "restart_service",
                "Restart one of this device's services and check that it came "
                "back. The person has to approve it first.",
                {"service": {"type": "string", "enum": RESTARTABLE}},
                required=["service"],
            ),
            _schema(
                "rollback",
                "Undo a change made earlier: put the file back as it was and "
                "restart whatever needs it. Does not need approval — undoing "
                "is the safe direction.",
                {
                    "change": {"type": "string", "maxLength": 80,
                               "description": "The change id, from list_changes."},
                    "force": {"type": "boolean",
                              "description": "Only when the file has been "
                                             "edited by somebody else since and "
                                             "the person has said to overwrite "
                                             "that edit."},
                },
                required=["change"],
            ),
            _schema(
                "list_changes",
                "What this agent has changed on this device, most recent "
                "first, including anything that was rolled back.",
                {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            ),
        ]

    def names(self) -> list[str]:
        return sorted(self._handlers)

    # ── dispatch ────────────────────────────────────────────────────

    def call(self, name: str, arguments: str) -> str:
        """Run one tool. Returns JSON. **Never raises.**

        The same contract as `ToolBox.call`, for the same reason: the model is
        going to be handed this string as a `tool` message, and there is no
        shape of failure better expressed by an exception than by a sentence
        the model can read and act on.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return _error(f"there is no tool called {name!r}")
        try:
            args = json.loads(arguments) if arguments else {}
            if not isinstance(args, dict):
                raise ValueError("arguments were not an object")
        except ValueError as exc:
            return _error(f"the arguments could not be read: {exc}")
        try:
            return handler(args)
        except Exception as exc:                    # noqa: BLE001
            log.exception("agent tool %s failed", name)
            return _error(f"{name} failed: {exc}")

    # ── looking ─────────────────────────────────────────────────────

    def _read_journal(self, args: dict) -> str:
        wanted = {"unit": args.get("unit")}
        # Only what the schema declares. An argument the model invented is
        # dropped here rather than travelling to a root process to be refused
        # there, which keeps the audit log free of noise that means nothing.
        for key in ("lines", "since", "contains"):
            if key in args:
                wanted[key] = args[key]
        return self._pass("read_journal", wanted)

    def _service_status(self, args: dict) -> str:
        return self._pass("service_status", {"service": args.get("service")})

    def _read_config_file(self, args: dict) -> str:
        return self._pass("read_config_file", {"path": args.get("path")})

    def _list_directory(self, args: dict) -> str:
        return self._pass("list_directory", {"path": args.get("path")})

    def _system_facts(self, args: dict) -> str:
        return self._pass("system_facts", {})

    def _list_changes(self, args: dict) -> str:
        wanted = {"limit": args["limit"]} if "limit" in args else {}
        return self._pass("list_changes", wanted)

    # ── changing ────────────────────────────────────────────────────

    def _set_config(self, args: dict) -> str:
        wanted = {"section": args.get("section"), "key": args.get("key"),
                  "value": args.get("value")}
        proposed_at = time.monotonic()
        proposed = self.helper.call("propose_config", dict(wanted, run=self.run),
                                    run=self.run)
        if not proposed:
            return _clip(json.dumps(proposed.as_dict(), default=str))
        plan = proposed.result
        if plan.get("no_op"):
            return json.dumps({"ok": True, "changed": False,
                               "detail": plan.get("detail", "")})

        allowed, why = self._approve(
            "set_config",
            "Change {}.{} from {} to {}".format(
                plan.get("section"), plan.get("key"),
                plan.get("before"), plan.get("after")),
            plan.get("diff", ""), plan.get("warning", ""))
        if not allowed:
            return json.dumps({"ok": False, "error": why,
                               "retryable": False, "approved": False})

        self._settle(proposed_at)
        applied = self.helper.call("apply_config", dict(wanted, run=self.run),
                                   run=self.run, timeout=self.apply_timeout_s)
        return _clip(json.dumps(applied.as_dict(), default=str))

    def _restart_service(self, args: dict) -> str:
        name = args.get("service")
        proposed_at = time.monotonic()
        proposed = self.helper.call(
            "restart_service", {"service": name, "propose": True, "run": self.run},
            run=self.run)
        if not proposed:
            return _clip(json.dumps(proposed.as_dict(), default=str))

        allowed, why = self._approve("restart_service", f"Restart {name}", "",
                                     proposed.result.get("warning", ""))
        if not allowed:
            return json.dumps({"ok": False, "error": why,
                               "retryable": False, "approved": False})

        self._settle(proposed_at)
        done = self.helper.call("restart_service",
                                {"service": name, "run": self.run},
                                run=self.run, timeout=self.apply_timeout_s)
        return _clip(json.dumps(done.as_dict(), default=str))

    def _source_path(self, given) -> str:
        """A path relative to the checkout, made absolute here.

        The model works in relative paths because that is how the project talks
        about itself. Whether the result may be touched is decided by root, in
        `policy.resolve_write` — this only saves a round trip for the obvious
        mistake of passing something absolute.
        """
        text = str(given or "").strip().lstrip("/")
        return f"/home/fuwenxu/AIPI5/{text}"

    def _read_source(self, args: dict) -> str:
        return self._pass("read_source",
                          {"path": self._source_path(args.get("path"))})

    def _patch_file(self, args: dict) -> str:
        wanted = {"path": self._source_path(args.get("path")),
                  "old": args.get("old"), "new": args.get("new")}
        proposed_at = time.monotonic()
        proposed = self.helper.call("propose_patch", dict(wanted, run=self.run),
                                    run=self.run)
        if not proposed:
            return _clip(json.dumps(proposed.as_dict(), default=str))

        plan = proposed.result
        allowed, why = self._approve(
            "patch_file", "Change %s" % args.get("path"),
            plan.get("diff", ""), plan.get("warning", ""))
        if not allowed:
            return json.dumps({"ok": False, "error": why,
                               "retryable": False, "approved": False})

        self._settle(proposed_at)
        applied = self.helper.call("apply_patch", dict(wanted, run=self.run),
                                   run=self.run, timeout=self.patch_timeout_s)
        return _clip(json.dumps(applied.as_dict(), default=str))

    def _rollback(self, args: dict) -> str:
        return self._pass("rollback_change",
                          {"change": args.get("change"),
                           "force": bool(args.get("force"))},
                          timeout=self.apply_timeout_s)

    @staticmethod
    def _settle(proposed_at: float) -> None:
        """Wait out the helper's minimum age, if the person beat it."""
        left = PROPOSAL_SETTLE_S - (time.monotonic() - proposed_at)
        if left > 0:
            time.sleep(left)

    # ── the browser ─────────────────────────────────────────────────
    #
    # Straight through to the helper, like the read tools. Everything that
    # decides what a browser may do — which CDP methods exist at all, which
    # addresses may be opened — is on the other side of the socket, in root's
    # code. There is no debugging port to reach around it: CDP lives on two
    # file descriptors the helper holds.
    #
    # These take the long timeout. Starting Chromium is slow, and a navigation
    # on a domestic connection is slower.

    def _browser(self, op: str, args: dict) -> str:
        return self._pass(op, args, timeout=self.browser_timeout_s)

    def _browser_open(self, args: dict) -> str:
        return self._browser("browser_open", {"url": args.get("url")})

    def _browser_read(self, args: dict) -> str:
        return self._browser("browser_read", {})

    def _browser_click(self, args: dict) -> str:
        return self._browser("browser_click", {"ref": args.get("ref")})

    def _browser_type(self, args: dict) -> str:
        return self._browser("browser_type",
                             {"text": args.get("text"),
                              "submit": bool(args.get("submit"))})

    def _browser_back(self, args: dict) -> str:
        return self._browser("browser_back", {})

    def _browser_screenshot(self, args: dict) -> str:
        return self._browser("browser_screenshot", {})

    def _browser_close(self, args: dict) -> str:
        return self._browser("browser_close", {})

    # ── reminders ───────────────────────────────────────────────────

    def _remind_me(self, args: dict) -> str:
        if self.schedule is None:
            return _error("reminders are not available on this device")
        try:
            at = schedule_mod.parse_when(args.get("when"))
            item = self.schedule.add(at, args.get("text", ""), "push", self.run)
        except ValueError as exc:
            # A refusal, not a failure: the model should fix the time and try
            # again rather than treat it as the device being broken.
            return json.dumps({"ok": False, "error": str(exc),
                               "retryable": False})
        return json.dumps({"ok": True, **item.describe()})

    def _list_reminders(self, args: dict) -> str:
        if self.schedule is None:
            return _error("reminders are not available on this device")
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = 20
        return _clip(json.dumps({"ok": True,
                                 "reminders": self.schedule.listing(
                                     max(1, min(50, limit)))}, default=str))

    def _cancel_reminder(self, args: dict) -> str:
        if self.schedule is None:
            return _error("reminders are not available on this device")
        item = self.schedule.cancel(str(args.get("id", "")))
        if item is None:
            return json.dumps({"ok": False, "retryable": False,
                               "error": "there is no reminder waiting with "
                                        "that id"})
        return json.dumps({"ok": True, "cancelled": item.describe()})

    # ── memory and procedure ────────────────────────────────────────

    def _remember(self, args: dict) -> str:
        if self.notes is None:
            return _error("this device is not keeping notes")
        try:
            note = self.notes.add(args.get("text", ""), self.run)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc),
                               "retryable": False})
        return json.dumps({"ok": True, "id": note.id, "text": note.text})

    def _forget(self, args: dict) -> str:
        if self.notes is None:
            return _error("this device is not keeping notes")
        note = self.notes.forget(str(args.get("id", "")))
        if note is None:
            return json.dumps({"ok": False, "retryable": False,
                               "error": "there is no note with that id"})
        return json.dumps({"ok": True, "forgotten": note.text})

    def _read_skill(self, args: dict) -> str:
        return self._pass("read_skill", {"name": args.get("name")})

    def _approve(self, op: str, what: str, detail: str, warning: str):
        """Put it to the person. No desk means no approval, so no change."""
        if self.approvals is None:
            return False, "there is nobody to approve this, so I have not done it"
        return self.approvals.ask(self.run, op, what, detail, warning)

    # ── the one place the helper is called ──────────────────────────

    def _pass(self, op: str, args: dict, timeout: float | None = None) -> str:
        result = self.helper.call(op, args, run=self.run, timeout=timeout)
        return _clip(json.dumps(result.as_dict(), default=str))


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message, "retryable": False})


def _clip(text: str) -> str:
    if len(text) <= RESULT_LIMIT:
        return text
    # Cut rather than summarise: the model can ask again with `lines` set
    # lower, and a summary written here would be this file guessing at what
    # mattered in somebody else's log.
    return text[:RESULT_LIMIT] + '… (truncated; ask for fewer lines)"}'
