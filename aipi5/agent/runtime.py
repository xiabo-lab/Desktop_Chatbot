"""The agent runtime: a service that owns the conversation and the loop.

Runs as `aipi5-agent`, a user with no sudo entry, no login shell, and no way
into `/home/fuwenxu` — which is mode 700. Everything it learns about the device
it learns by asking the privileged helper.

**Its own process, and that is not only about privilege.** The single most
common thing this will ever be asked to do is restart the assistant, and the
assistant is where the phone's connection lives. If the runtime were inside
`aipi5.service` that request would kill it mid-task: no health check, no
rollback, and the transcript gone. The thing doing the verifying cannot be the
thing being restarted.

    phone --HTTPS--> CallServer (aipi5.service) --AF_UNIX--> AgentApi (here)
                                                                  |
                                                       HelperClient --AF_UNIX-->
                                                                  aipi5-agent-helper (root)

The mailbox is here rather than in the call server for the same reason: when the
assistant restarts, the phone loses its connection for fifteen seconds and then
re-polls from its cursor, and everything that happened while it was gone is
still waiting.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time
import uuid
from http import server as http_server
from pathlib import Path

from aipi5.agent.approvals import ApprovalDesk
from aipi5.agent.helper_client import HelperClient
from aipi5.agent.loop import AgentLoop, Budget
from aipi5.agent.prompts import system_prompt
from aipi5.agent.notes import Notes
from aipi5.agent import schedule as schedule_mod
from aipi5.agent.schedule import Schedule
from aipi5.agent.tools import AgentToolBox
from aipi5.call.mailbox import Mailbox

log = logging.getLogger("aipi5.agent")

#: One seat. The phone is the only reader; requests arrive as their own POSTs
#: rather than through the mailbox, so nothing is ever addressed to the runtime.
PHONE = "phone"

#: Deeper than a call's, because a run is a longer conversation than a
#: handshake and losing the front of a transcript is worse than losing the
#: front of an ICE exchange.
MAILBOX_DEPTH = 512

MAX_BODY = 64 * 1024
MAX_ASK = 4000

#: Where reminders live. The runtime's own StateDirectory, so they survive a
#: reboot, a redeploy and a restart of the assistant.
REMINDERS = Path("/var/lib/aipi5-agent/reminders.jsonl")
NOTES = Path("/var/lib/aipi5-agent/notes.jsonl")


class AgentService:
    """One run at a time, and the record of what has been said."""

    def __init__(self, client, toolbox, cfg):
        self.cfg = cfg
        self.mailbox = Mailbox((PHONE,), max_depth=MAILBOX_DEPTH,
                               poll_timeout_s=cfg.poll_timeout_s)
        self.toolbox = toolbox
        #: Reminders. **Nothing here sends anything** — the assistant does the
        #: delivering, because the push keys and any mail credentials are in a
        #: home this user cannot traverse. So a compromised agent can schedule
        #: a notification and still cannot reach the account that sends it.
        self.schedule = Schedule(REMINDERS)
        toolbox.schedule = self.schedule
        #: Preferences, carried into every future run. In the agent's own
        #: directory: these are its notes about what the person said, and
        #: nothing privileged depends on them.
        self.notes = Notes(NOTES)
        toolbox.notes = self.notes
        #: Wired here rather than passed in, so there is exactly one desk and
        #: it posts to the same mailbox as everything else. The toolbox holds
        #: the reference because it is the tools that have to wait.
        self.approvals = ApprovalDesk(self._emit, cfg.approval_timeout_s)
        toolbox.approvals = self.approvals
        self.loop = AgentLoop(
            client, toolbox, self._emit, system_prompt(),
            Budget(max_steps=cfg.max_steps, max_tool_calls=cfg.max_tool_calls,
                   max_runtime_s=cfg.max_runtime_s, max_tokens=cfg.max_tokens))
        self._lock = threading.Lock()
        self._run = ""
        self._state = "idle"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._today = time.strftime("%Y-%m-%d")
        self._runs_today = 0
        #: Its own lock, not `_lock`: a hand must work while a run is going.
        self._gesture_lock = threading.Lock()
        self._gesture_at = 0.0
        self._move_at = 0.0
        self._browser_seen: tuple[float, dict] | None = None

    # ── what the phone asks for ─────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = {"run": self._run, "state": self._state,
                        "busy": self._state != "idle",
                        "rev": self.mailbox.depth(PHONE),
                        "dropped": self.mailbox.dropped(PHONE),
                        "runs_today": self._runs_today,
                        "daily_limit": self.cfg.daily_run_limit}
        # Outside the lock: the desk has its own, and taking two in a fixed
        # order here would be one more ordering to get right for no gain.
        #
        # This is also what the assistant watches in order to ring the phone.
        # A backgrounded web app does not poll, so without a push there is
        # nobody to see the question — see `Housekeeping`'s agent job.
        snapshot["pending"] = self.approvals.snapshot()
        # What the assistant should deliver, on the poll it is already making.
        snapshot["notify"] = [{"id": r.id, "text": r.text, "deliver": r.deliver}
                              for r in self.schedule.due()[:5]]
        snapshot["browser"] = self._browser_state()
        return snapshot

    #: How stale the browser flag may be. The kiosk uses it to decide whether
    #: to run hand tracking at all, so a few seconds late costs a few seconds
    #: of a recogniser that had nothing to drive -- and asking the helper on
    #: every snapshot would mean a socket round trip at 1 Hz forever.
    BROWSER_CACHE_S = 4.0

    def _browser_state(self) -> dict:
        now = time.monotonic()
        with self._gesture_lock:
            if self._browser_seen and now - self._browser_seen[0] < self.BROWSER_CACHE_S:
                return self._browser_seen[1]
        answer = self.toolbox.helper.call("browser_state", {}, timeout=3.0)
        if answer.ok:
            state = answer.result
        elif self._browser_seen:
            # **A failed question is not an answer of "no".**
            #
            # This was read as "the browser has closed", and the assistant acts
            # on it: it gives the camera back, which ends the video stream the
            # gesture reader is holding. One slow round trip to a busy helper
            # therefore took hand control down for good -- observed on the
            # device as `camera reclaimed` followed five seconds later by
            # `camera lent`, in the middle of somebody testing it, with a page
            # left reading the last frame it ever received.
            #
            # Keeping the last known state costs nothing: a browser really
            # closing is noticed on the next tick, a second later.
            state = self._browser_seen[1]
        else:
            state = {"open": False}
        with self._gesture_lock:
            self._browser_seen = (now, state)
        return state

    def poll(self, since: int, timeout: float | None = None):
        messages, cursor = self.mailbox.collect(PHONE, since, timeout)
        return {"events": messages, "cursor": cursor, "agent": self.snapshot()}

    def ask(self, text: str) -> dict:
        text = (text or "").strip()[:MAX_ASK]
        if not text:
            return {"ok": False, "error": "there was nothing in that message"}

        with self._lock:
            if self._state != "idle":
                # Not a refusal: added to the run in flight. "Wait, check the
                # camera instead" is a normal thing to say to something working
                # on your behalf.
                if self.loop.push(text):
                    return {"ok": True, "run": self._run, "appended": True}
                return {"ok": False, "error": "nothing to add"}

            today = time.strftime("%Y-%m-%d")
            if today != self._today:
                self._today, self._runs_today = today, 0
            if self._runs_today >= self.cfg.daily_run_limit:
                return {"ok": False,
                        "error": f"that is {self.cfg.daily_run_limit} runs today, "
                                 f"which is the limit. It resets at midnight."}
            self._runs_today += 1
            run_id = "r-" + uuid.uuid4().hex[:8]
            self._run, self._state = run_id, "running"
            self._stop = threading.Event()

        self._emit({"type": "agent.ask", "run": run_id, "text": text})
        self._thread = threading.Thread(target=self._work, name="agent-run",
                                        args=(run_id, text), daemon=True)
        self._thread.start()
        return {"ok": True, "run": run_id}

    # ── the person's hand ───────────────────────────────────────────
    #
    # A gesture is *input*, not a request. It goes straight to the helper: no
    # model, no run, no approval. That is deliberate on both sides.
    #
    # Not the model, because a wave of the hand means one fixed thing and
    # asking a language model to confirm it would cost two seconds and a
    # fraction of a cent to decide what the sender already knew. Not an
    # approval, because every one of these is something the person could do by
    # reaching out and touching the screen -- a gesture is a longer arm, and an
    # arm does not need permission.
    #
    # It also runs while a task is in flight. `_lock` is not taken: the run
    # lock exists so two *runs* do not overlap, and someone scrolling a page
    # while the agent thinks is not an overlap, it is the point.

    #: What a gesture may ask for, and nothing else. The values are helper
    #: operation names -- a caller cannot reach an operation that is not a
    #: value here, so the kiosk page cannot name `apply_config` however it is
    #: compromised.
    GESTURES = {
        "scroll_up": ("browser_hand_scroll", {"direction": "up"}),
        "scroll_down": ("browser_hand_scroll", {"direction": "down"}),
        "back": ("browser_hand_history", {"way": "back"}),
        "forward": ("browser_hand_history", {"way": "forward"}),
        "click": ("browser_hand_click", {}),
        #: Not a gesture so much as the palm's position, sent while it drifts.
        #: Rate-limited separately below, because the whole point of it is to
        #: keep up with a moving hand.
        "move": ("browser_hand_move", {}),
        #: Not a gesture either -- the *absence* of one. Sent when the hand
        #: leaves, so the pointer does not sit there claiming to follow an arm
        #: that is no longer in front of the camera.
        "hide": ("browser_hand_hide", {}),
    }

    #: Gestures that carry a place on the page rather than only a name.
    PLACED = ("click", "move")

    #: The fastest a hand may act. A person completes a sweep in about half a
    #: second, so this drops the duplicates that come of one motion crossing
    #: the threshold on consecutive frames without making the control feel
    #: sticky.
    GESTURE_INTERVAL_S = 0.45

    #: The pointer may keep up with the hand, which a gesture may not. Eight a
    #: second is smooth enough to follow and slow enough that the four hops
    #: between the camera and the page stay well ahead of it.
    MOVE_INTERVAL_S = 0.11

    #: How long the assistant's "open YouTube" may take before it is told to
    #: stop waiting. `browser_open` navigates, waits for the page to settle,
    #: and then reads it — the read is for the agent and the voice command has
    #: no use for it, but it is the same op and one op is better than two that
    #: can drift. Generous, because the assistant does not block on this: see
    #: `aipi5/browser/launcher.py`.
    OPEN_TIMEOUT_S = 45.0

    # ── reminders, without a run ────────────────────────────────────
    #
    # `Schedule` lives here because reboot survival, retry and the delivery
    # handshake with `Housekeeping` all belong to one owner, and this is it.
    # What these three add is a way to reach that owner **without starting a
    # maintenance run**.
    #
    # Before them the only door was `agent.ask`, so "remind me at nine to call
    # Mum" spent one of the day's runs and up to 24 model steps deciding to
    # call `remind_me` — for a request whose whole content is a timestamp and a
    # sentence. It also meant the reminder was only as reliable as the model's
    # mood: a run that wandered off, hit its step ceiling, or decided to check
    # the journal first was a reminder that never got made, with nothing said
    # about it.
    #
    # So these are validation and a function call. No `AgentLoop`, no budget,
    # no `_work` thread — and no model anywhere on the path, which is what
    # makes "remind me" as dependable as an alarm clock rather than as
    # dependable as a conversation.

    def reminder_create(self, when: str, text: str, deliver: str = "push",
                        ) -> dict:
        """One reminder. `when` is an absolute local ISO timestamp.

        Resolved *here* rather than on the assistant's side, so there is one
        implementation of what "2026-09-02 09:00" means and it is the one that
        owns the file. `parse_when` is a validator and not a natural-language
        parser, deliberately: "next Tuesday" means something different
        depending on the day it is asked, and a parser that guesses is a
        reminder that arrives on the wrong day for reasons nobody can
        reconstruct afterwards.
        """
        try:
            at = schedule_mod.parse_when(when)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            item = self.schedule.add(at, text, deliver=deliver, run="assistant")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        log.info("reminder %s set for %s", item.id, when)
        # `describe()` rather than the raw record: what goes back is a human
        # time in this device's own zone, which is what has to be read out
        # loud. An epoch read aloud is not a confirmation of anything.
        return {"ok": True, "reminder": item.describe()}

    def reminder_list(self, limit: int = 20) -> dict:
        return {"ok": True, "reminders": self.schedule.listing(max(1, int(limit)))}

    def reminder_cancel(self, ident: str) -> dict:
        item = self.schedule.cancel(str(ident or "")[:64])
        if item is None:
            return {"ok": False,
                    "error": "there is no reminder waiting with that id"}
        return {"ok": True, "cancelled": item.describe()}

    def open_page(self, url: str) -> dict:
        """Open a page in the agent's browser. No run, no model, no approval.

        The assistant's spoken "open YouTube" arrives here rather than as an
        `agent.ask`, because there is nothing for a model to decide: the site
        was chosen in AIPI5's source (`aipi5/browser/launcher.py`) and the
        person said four words. A planning loop would cost seconds and money
        to arrive at the URL it was given.

        **This is the point of the whole detour.** The assistant can start a
        Chromium by itself and did — but a browser the helper did not start is
        one nothing holds a CDP pipe to, so the hand pilot has nothing to drive
        and `browser_state` never says a page is open. Routing the command
        through here means the window somebody asked for out loud, the window
        the phone drives, and the window a hand drives are all one window.

        Hand control needs no telling: it follows `browser.open` in the
        snapshot, which is why the cache below is dropped rather than left to
        expire — up to four seconds of a hand that does nothing is exactly the
        interval in which somebody decides the feature is broken.
        """
        answer = self.toolbox.helper.call("browser_open", {"url": url},
                                          timeout=self.OPEN_TIMEOUT_S)
        if not answer.ok:
            log.warning("could not open %s: %s", url, answer.why)
            return {"ok": False, "error": answer.why or "the browser refused"}
        with self._gesture_lock:
            self._browser_seen = None
        log.info("opened %s in the agent's browser, asked for by voice", url)
        # Deliberately not the page. `browser_open` answers with the AX tree
        # and up to six thousand characters of text, which is what a model
        # reads and is nothing but weight on a Unix socket to an assistant that
        # is about to say four words.
        return {"ok": True, "url": url,
                "title": str((answer.result or {}).get("title") or "")[:200]}

    def gesture(self, name: str, where: dict | None = None) -> dict:
        action = self.GESTURES.get(name)
        if action is None:
            return {"ok": False, "detail": f"unknown gesture {name!r}"}
        now = time.monotonic()
        # `hide` is paced with the pointer, not with the gestures: it is the
        # end of a pointer's life, and waiting half a second to stop showing a
        # hand that has gone is exactly the lag it exists to avoid.
        gap = (self.MOVE_INTERVAL_S if name in ("move", "hide")
               else self.GESTURE_INTERVAL_S)
        with self._gesture_lock:
            last = (self._move_at if name in ("move", "hide")
                    else self._gesture_at)
            if now - last < gap:
                return {"ok": False, "detail": "too soon after the last one"}
            if name in ("move", "hide"):
                self._move_at = now
            else:
                # A move does not restart the gesture clock, but every real
                # gesture holds off the next one -- including a move, so the
                # pointer does not jump while a sweep is still being read.
                self._gesture_at = self._move_at = now

        op, args = action
        args = dict(args)
        if name in self.PLACED:
            try:
                args["x"] = float((where or {}).get("x"))
                args["y"] = float((where or {}).get("y"))
            except (TypeError, ValueError):
                return {"ok": False, "detail": f"a {name} needs somewhere to go"}
            # How far through a dwell click the pointer is, so the marker can
            # show one coming. Optional and bounded here rather than trusted:
            # it only ever changes how a rectangle is drawn, but it arrives
            # from outside the helper like everything else.
            try:
                hold = float((where or {}).get("hold", 0.0))
            except (TypeError, ValueError):
                hold = 0.0
            args["hold"] = min(1.0, max(0.0, hold))
            # Whether sweeps are armed where the pointer is. Only ever changes
            # the colour of a rectangle, and is bounded to a bool regardless.
            args["home"] = bool((where or {}).get("home"))

        answer = self.toolbox.helper.call(op, args, timeout=15.0)
        if not answer.ok:
            return {"ok": False, "detail": answer.error or "refused"}
        # A gesture also counts as the browser being used: every helper
        # operation goes through `_get`, which touches it, so the sweep that
        # closes an idle window sees a person standing there without needing
        # to be told separately.
        return {"ok": True, "gesture": name, "result": answer.result}

    def stop(self, run_id: str = "") -> dict:
        with self._lock:
            if self._state == "idle":
                return {"ok": False, "error": "nothing is running"}
            if run_id and run_id != self._run:
                # Permissive about *which* run only when none was named. An
                # answer about a run that has already ended must not stop the
                # next one.
                return {"ok": False, "error": "that run has already finished"}
            self._stop.set()
        return {"ok": True, "run": self._run}

    # ── the run ─────────────────────────────────────────────────────

    def answer(self, token: str, allow: bool) -> dict:
        """A person answered the approval card on their phone."""
        return self.approvals.answer(token, allow)

    def delivered(self, ident: str, ok: bool, detail: str = "") -> dict:
        """The assistant reports whether a reminder actually went out.

        A failure is retried, up to a point. A phone that has been reset is
        not a reason to send the same notification every second until the
        heat death of the device.
        """
        if not self.schedule.delivered(ident, ok, detail):
            return {"ok": False, "error": "no reminder is waiting with that id"}
        if ok:
            self._emit({"type": "agent.reminder", "id": ident,
                        "detail": detail})
        return {"ok": True}

    def _work(self, run_id: str, text: str) -> None:
        # The toolbox carries the run so the helper's audit log can join a
        # privileged operation to the conversation that asked for it.
        self.toolbox.run = run_id
        # Rebuilt per run, not per process. The prompt carries the current
        # time, and a service that has been up for a week would otherwise tell
        # the model it was last Tuesday -- so "tomorrow at nine" would land a
        # week ago and be refused as being in the past.
        # Notes and the list of written procedures go in here rather than
        # being fetched by a tool call. A preference nobody asked about is a
        # preference that does not work, and a procedure the model does not
        # know exists is one it will not read.
        self.loop.system = system_prompt(
            notes=self.notes.as_prompt(),
            skills=self._skill_list())
        try:
            self.loop.run(run_id, text, self._stop)
        except Exception as exc:                    # noqa: BLE001
            log.exception("run %s fell over", run_id)
            self._emit({"type": "agent.error", "run": run_id, "error": str(exc)})
            self._emit({"type": "agent.done", "run": run_id, "ok": False,
                        "stopped": "something went wrong inside the agent"})
        finally:
            # A run that ends while a card is on the phone must take the card
            # with it. Otherwise the next thing somebody taps is an approval
            # for work that stopped happening minutes ago.
            self.approvals.cancel(run_id)
            self.toolbox.run = ""
            with self._lock:
                self._state, self._run = "idle", ""

    def _skill_list(self) -> str:
        """Names and one-line summaries. The bodies are fetched on demand."""
        answer = self.toolbox.helper.call("list_skills", {}, run=self._run)
        found = (answer.result or {}).get("skills") or []
        if not found:
            return ""
        lines = "\n".join("- **%s** — %s" % (s.get("name"),
                                              s.get("summary"))
                          for s in found)
        return ("\n## Written procedure\n\n"
                "This device has notes on how to do a few things, learned "
                "the hard way. Read one with `read_skill` when you are "
                "actually doing that job — they are longer than this list "
                "and worth it.\n\n" + lines + "\n")

    def _emit(self, event: dict) -> None:
        event.setdefault("at", time.time())
        with self._lock:
            if self._state != "idle":
                self._state = _state_of(event) or self._state
        self.mailbox.post(PHONE, event)


def _state_of(event: dict) -> str:
    if event.get("type") == "agent.state":
        return str(event.get("state") or "")
    if event.get("type") == "agent.tool":
        return "looking" if event.get("state") == "running" else ""
    return ""


# ── the socket the call server talks to ─────────────────────────────


class _UnixServer(socketserver.ThreadingMixIn, http_server.HTTPServer):
    """HTTP over AF_UNIX.

    `HTTPServer.server_bind` calls `getfqdn()` on the bound address, which for a
    Unix socket is a filesystem path — it either hangs on a DNS lookup for a
    string that is not a hostname or produces nonsense. Skipping straight to
    `TCPServer.server_bind` is the whole of the trick.
    """

    #: `getattr` rather than the attribute, so this module imports on a
    #: machine with no Unix sockets. The helper and the runtime only ever run
    #: on the Pi, but their *rules* -- what a gesture may ask for, what needs
    #: approval -- are tested on the laptop, and a check that only runs on the
    #: device is one that gets broken here and noticed a deploy later.
    #: Binding still fails loudly on such a machine, which is correct.
    address_family = getattr(socket, "AF_UNIX", -1)
    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "aipi5-agent", 0


class _Handler(http_server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: AgentService = None            # set by serve()

    def address_string(self) -> str:        # there is no peer address here
        return "local"

    def log_message(self, fmt, *args):      # the phone polls; DEBUG or nothing
        log.debug("%s", fmt % args)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/agent/v1/state":
            return self._json(200, self.service.snapshot())
        if path == "/agent/v1/poll":
            since = 0
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "since":
                    try:
                        since = int(value)
                    except ValueError:
                        since = 0
            return self._json(200, self.service.poll(since))
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/agent/v1/say":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "a length is required"})
        if length > MAX_BODY:
            return self._json(413, {"error": "that message was too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError:
            return self._json(400, {"error": "the body was not JSON"})

        kind = body.get("type")
        if kind == "agent.ask":
            return self._json(200, self.service.ask(str(body.get("text", ""))))
        if kind == "agent.stop":
            return self._json(200, self.service.stop(str(body.get("run", ""))))
        if kind == "agent.delivered":
            return self._json(200, self.service.delivered(
                str(body.get("id", "")), bool(body.get("ok")),
                str(body.get("detail", ""))[:200]))
        if kind == "agent.gesture":
            return self._json(200, self.service.gesture(
                str(body.get("gesture", ""))[:32], body.get("at")))
        if kind == "agent.open":
            # Bounded here and validated in the helper, which is where every
            # other URL this device visits is checked — `_check_url` refuses
            # anything but http/https and refuses this machine's own services.
            # Nothing about that changes because the caller is the assistant.
            return self._json(200, self.service.open_page(
                str(body.get("url", ""))[:2000]))
        if kind == "agent.answer":
            return self._json(200, self.service.answer(
                str(body.get("token", "")), bool(body.get("allow"))))
        # The assistant's own three. Deliberately a different prefix from
        # `agent.*`: these do not start a run, do not spend the daily budget,
        # and never reach the model — see `reminder_create` above. A reader
        # checking "what can be asked of this socket without a run" gets the
        # answer from the names.
        if kind == "assistant.reminder.create":
            return self._json(200, self.service.reminder_create(
                str(body.get("when", ""))[:64],
                str(body.get("text", ""))[:schedule_mod.MAX_TEXT],
                str(body.get("deliver", "push"))[:16]))
        if kind == "assistant.reminder.list":
            try:
                limit = int(body.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20
            return self._json(200, self.service.reminder_list(limit))
        if kind == "assistant.reminder.cancel":
            return self._json(200, self.service.reminder_cancel(
                str(body.get("id", ""))[:64]))
        return self._json(400, {"error": f"unknown message type {kind!r}"})

    def _json(self, code: int, payload: dict):
        """Answer, tolerating a client that has already gone.

        **The whole write, not just the body.** `AgentProxy.snapshot()` gives up
        after two seconds, and the assistant polls it every second — so a
        request whose answer arrives late finds a closed socket. The first
        version guarded only `wfile.write`, but `end_headers()` writes too, and
        every such poll left a `BrokenPipeError` traceback in the journal.
        Harmless, and it buries everything that is not.
        """
        body = json.dumps(payload, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            log.debug("the caller went away before the answer was written")


def serve(service: AgentService, path: Path) -> _UnixServer:
    """Bind the socket, or adopt the one systemd handed over.

    Taking it from systemd means it is created with the right owner and mode
    before this process starts, so there is no window in which it exists and is
    reachable by the wrong user.
    """
    handler = type("_Bound", (_Handler,), {"service": service})
    if (os.environ.get("LISTEN_FDS")
            and os.environ.get("LISTEN_PID") == str(os.getpid())):
        server = _UnixServer.__new__(_UnixServer)
        socketserver.BaseServer.__init__(server, str(path), handler)
        server.socket = socket.socket(fileno=3, family=socket.AF_UNIX,
                                      type=socket.SOCK_STREAM)
        server.server_name, server.server_port = "aipi5-agent", 0
        log.info("adopted the socket from systemd")
        return server

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = _UnixServer(str(path), handler)
    # The call server runs as `fuwenxu` and must be able to reach this. 0666
    # would do it and is wrong; the group is what the installer sets up.
    os.chmod(path, 0o660)
    return server


def agent_credentials(config_mod) -> str:
    """The OpenAI key, from systemd first.

    `config_mod.credentials()` looks at `$OPENAI_API_KEY` and then at key files
    inside the repository — and this process cannot see the repository's
    parent, because `ProtectHome=tmpfs` hides `/home` and only `~/AIPI5` is
    bound back, read-only. So the key is *handed over* rather than fetched:
    systemd's `LoadCredential=` puts it in a tmpfs owned by this user, mode
    0400, unmounted when the service stops.

    The environment is still honoured, because running this by hand to debug it
    should not require installing a credential first.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        try:
            key = (Path(directory) / "openai").read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("could not read the systemd credential: %s", exc)
        else:
            if key:
                log.info("using the OpenAI key systemd handed over")
                return key
    return config_mod.credentials()


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("AIPI5_AGENT_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Imported here rather than at module scope: `aipi5.core.config` pulls in
    # the AIA bridge, and a failure to find AIA should be a legible message
    # from this function rather than an ImportError at load.
    from aipi5.core import config as config_mod
    from aipi5.llm.client import OpenAIClient

    settings = config_mod.load()
    cfg = settings.agent
    if not cfg.enabled:
        log.error("the agent is disabled in the configuration; nothing to do")
        return 0

    key = agent_credentials(config_mod)
    if not key:
        log.error("no OpenAI key reached this process; the agent cannot think")
        return 2

    helper = HelperClient(cfg.helper_socket)
    if not helper.available():
        log.warning("the privileged helper is not at %s; every tool will refuse",
                    cfg.helper_socket)

    client = OpenAIClient(settings.openai, key)
    toolbox = AgentToolBox(helper)
    service = AgentService(client, toolbox, cfg)
    server = serve(service, Path(cfg.socket))

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    thread = threading.Thread(target=server.serve_forever, name="agent-api",
                             kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    log.info("agent ready on %s as uid %d; model %s, tools: %s",
             cfg.socket, os.geteuid(), settings.openai.agent,
             ", ".join(toolbox.names()))

    while not stopping.is_set():
        stopping.wait(1.0)

    log.info("agent stopping")
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
