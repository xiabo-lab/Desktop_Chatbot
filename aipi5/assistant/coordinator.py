"""Where a sentence arrives, whoever said it and however they said it.

Four ways in — the wake word, the Listen button, the compose box on the panel,
the phone — and before this there were two ways *through*, with different tools
and different policy behind each. Whether "set the volume to thirty" reached
the short model turn or a 24-step maintenance run depended on which of two
pages somebody had happened to open. That is the bug this class exists to make
impossible: every one of the four calls `submit_text` or `submit_voice`, and
what happens next is decided by the same code every time.

**It is not another agent loop.** It owns presentation, cancellation, and the
plumbing for delegating long work. Which tool to use stays with the model, and
what a tool is allowed to do stays in the handler beside it. Adding a decision
here about *what* to do with a request would be a third router, and the second
one is already the thing this file is cleaning up after.

**The two services are untouched.** `delegate()` sends `agent.ask` down the
same Unix socket the phone has always used, and `AgentBridge` reads the same
mailbox back. The voice process gains no privilege from any of it: the proxy is
a socket path and a `json.dumps`, and `tests/test_agent_boundary.py` asserts
that it stays that.
"""

from __future__ import annotations

import logging
import threading
import time

from aipi5.assistant.events import EventLog, EventSink

log = logging.getLogger(__name__)

#: How long a second request waits for the one in flight before giving up. A
#: turn is judged against 2500 ms and bounded by the model timeout; anything
#: past this is a request whose asker has walked away.
TURN_WAIT_S = 25.0

#: How long the bridge holds each poll open. Comfortably inside the proxy's own
#: 30 s and matched to the agent's 20 s long poll.
BRIDGE_POLL_S = 20.0

#: How long the bridge keeps reading after a run says it is done. The mailbox
#: is ordered, so this only covers the case where `agent.done` and a trailing
#: row cross; it is not a guess at whether more is coming.
BRIDGE_DRAIN_S = 2.0

#: The floor under one turn of the poll loop, whatever the far side does.
#:
#: `pump` hands its timeout to the proxy, and the loop is written on the
#: assumption that a poll with nothing to report blocks out there for twenty
#: seconds. That assumption belongs to `aipi5-agent.service` rather than to
#: this file, and when it fails — a socket answering instantly, an older
#: runtime, a stub — a `while: pump()` is a busy loop. Measured: two of them
#: took a Pi core each and made the rest of the process miss timings by enough
#: to look like unrelated failures somewhere else entirely.
BRIDGE_FLOOR_S = 0.25

MAX_TASK = 2000


def translate(event: dict) -> dict | None:
    """One agent mailbox message, as a row of the shared transcript.

    Pure, so the mapping can be tested without a socket, a thread or a run.
    Returns None for a message that is not presentation — `agent.state` is
    replaced several times a run and belongs in the snapshot, and
    `agent.reminder` is a delivery instruction for `Housekeeping` rather than
    something to show anybody.

    **Nothing here forwards a tool's result.** `agent.tool` carries a `summary`
    the agent wrote for a person to read, and that is what is used; the result
    itself never leaves `aipi5-agent.service`.
    """
    kind = str(event.get("type", ""))
    run = str(event.get("run", ""))[:64]

    if kind == "agent.ask":
        return {"kind": "user", "text": event.get("text", ""), "run": run}
    if kind == "agent.say":
        return {"kind": "assistant", "text": event.get("text", ""), "run": run}
    if kind == "agent.tool":
        if event.get("state") == "running":
            # The "checking…" line. It is a state, replaced by the finished row
            # a moment later, and a transcript with both is a transcript that
            # says everything twice.
            return None
        return {"kind": "tool", "run": run,
                "tool": str(event.get("name", ""))[:64],
                "ok": bool(event.get("ok")),
                "text": str(event.get("summary", ""))[:200]}
    if kind == "agent.approval":
        return {"kind": "approval", "run": run,
                "text": str(event.get("what", "")) or "Approve this?",
                "meta": {"token": str(event.get("token", ""))[:128],
                         "detail": str(event.get("detail", "")),
                         "warning": str(event.get("warning", ""))}}
    if kind == "agent.approval.gone":
        return {"kind": "approval", "run": run, "text": "",
                "meta": {"gone": True,
                         "outcome": str(event.get("outcome", ""))[:64]}}
    if kind == "agent.error":
        return {"kind": "error", "run": run,
                "text": str(event.get("error", "")) or "Something went wrong."}
    if kind == "agent.done":
        return {"kind": "done", "run": run, "ok": bool(event.get("ok", True)),
                "text": str(event.get("stopped", "")),
                "meta": {"steps": int(event.get("steps", 0) or 0),
                         "tool_calls": int(event.get("tool_calls", 0) or 0),
                         "seconds": float(event.get("seconds", 0) or 0)}}
    # An event type this build does not know about is not an error. The agent
    # and the assistant are separate deployments and one can be newer.
    return None


class AgentBridge:
    """`aipi5-agent.service`'s mailbox, arriving in the one transcript.

    A thread that lives only while a delegated run does. It holds its own
    cursor, which is what makes it survive the agent restarting under it and
    what lets it run alongside the browser's own poll — the mailbox is
    cursor-based and does not consume, so two readers both see everything.

    Never raises into its caller. A bridge that cannot reach the agent
    publishes one error row and stops; the run is still going on the other side
    of the socket and the page's own poll is still watching it.
    """

    def __init__(self, proxy, events: EventSink, *, poll_s: float = BRIDGE_POLL_S,
                 drain_s: float = BRIDGE_DRAIN_S):
        self.proxy = proxy
        self.events = events
        self.poll_s = poll_s
        self.drain_s = drain_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cursor = 0
        self._run = ""
        self._state = "idle"
        #: The agent could not be reached. Distinct from "the run ended": one
        #: error row is information, and one per poll against a socket that is
        #: not there is a transcript of nothing but that error — measured at
        #: 400 rows, which is the whole ring.
        self._failed = False

    # ── state, for the snapshot ─────────────────────────────────────

    @property
    def run(self) -> str:
        with self._lock:
            return self._run

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def busy(self) -> bool:
        return self.state != "idle"

    def alive(self) -> bool:
        """Whether this bridge still holds a thread. For tests and shutdown."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ── the loop ────────────────────────────────────────────────────

    def follow(self, run_id: str) -> None:
        """Take `run_id` as the run being watched. No thread, no I/O."""
        with self._lock:
            self._run = str(run_id or "")[:64]
            self._state = "running"
            self._failed = False

    def start(self, run_id: str) -> None:
        """Follow `run_id` and make sure something is reading the mailbox.

        Idempotent, and adopting rather than starting a second thread. The
        agent runs one at a time and appends a follow-up question to the run in
        flight, so "start" arriving twice means one conversation and not two —
        and two threads on one mailbox would draw every row twice.
        """
        self.follow(run_id)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, name="agent-bridge",
                                            daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._state = "idle"

    def pump(self, timeout: float | None = None) -> int:
        """One poll, translated and published. Returns how many rows landed.

        Separated from the loop so the translation, the cursor and the
        end-of-run detection can all be tested without a thread.
        """
        if self._failed:
            return 0
        status, payload = self.proxy.poll(self._cursor, timeout=timeout)
        if status != 200:
            self._failed = True
            self.events.publish(
                "error", "agent",
                text=str(payload.get("error", "")) or "the agent is not answering")
            self.stop()
            return 0

        cursor = payload.get("cursor")
        if isinstance(cursor, int) and cursor > self._cursor:
            self._cursor = cursor

        drawn = 0
        for message in payload.get("events", []) or []:
            if not isinstance(message, dict):
                continue
            row = translate(message)
            if row is None:
                continue
            self.events.publish(row.pop("kind"), "agent", **row)
            drawn += 1
            if message.get("type") == "agent.done":
                self.stop()

        snapshot = payload.get("agent")
        if isinstance(snapshot, dict):
            with self._lock:
                self._run = str(snapshot.get("run", "") or self._run)[:64]
                if not self._stop.is_set():
                    self._state = str(snapshot.get("state", "idle"))
        return drawn

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self.pump(timeout=self.poll_s)
                if self._failed:
                    return
                # A poll that came back with nothing sooner than it was asked
                # to is a far side that did not wait — see `BRIDGE_FLOOR_S`.
                # Waiting on the event rather than sleeping, so a stop is still
                # acted on at once.
                left = min(self.poll_s, BRIDGE_FLOOR_S) - (
                    time.monotonic() - started)
                if left > 0:
                    self._stop.wait(left)
            # One pass after the run ends, in case `agent.done` and a trailing
            # row crossed in the mailbox. A wait rather than a second polling
            # loop: `pump` gives its timeout to the *proxy*, and a proxy that
            # answers instantly — a socket that is gone, a stub in a test —
            # turns "poll for two seconds" into a spin.
            self._stop.wait(0)
            time.sleep(min(self.drain_s, 1.0))
            self.pump(timeout=0.0)
        except Exception:                            # noqa: BLE001
            log.exception("the agent bridge stopped")
        finally:
            with self._lock:
                self._state = "idle"
                self._thread = None


class Coordinator:
    """One door in, and the decision about what happens behind it.

    Everything it needs is injected, and every one of them is optional:
    `respond` is the short model turn (`Assistant.answer`), `agent` is the
    proxy where the agent is installed, `speak` is how a delegated answer
    reaches the room. A coordinator with none of them still publishes a
    transcript and still refuses cleanly, which is what a unit test wants and
    also what a device with no API key and no agent gets.
    """

    def __init__(self, *, events: EventLog | None = None, respond=None,
                 agent=None, speak=None, turn_wait_s: float = TURN_WAIT_S,
                 bridge=None, consent=None, on_turn=None, after_turn=None):
        self.events = events if events is not None else EventLog()
        #: `respond(text, language) -> str`. The short tool loop, in the voice
        #: process. None where conversation is disabled.
        self.respond = respond
        #: `AgentProxy`, or None where `aipi5-agent.service` is not installed.
        self.agent = agent
        #: `speak(text, language)`, for a delegated answer worth saying out
        #: loud. None where nothing is listening.
        self.speak = speak
        #: Where an everyday action that rings, sends or cannot be taken
        #: back waits for a person. Separate from the agent's approval desk
        #: and deliberately so — that one gates `patch_file` and lives in
        #: another process under another user, and putting "ring my phone"
        #: through it would mean spending a maintenance run to make a phone
        #: buzz. Both answer through `answer_approval` below.
        self.consent = consent
        #: `on_turn(text, source)` — told what was actually said, before the
        #: model sees it. One thing depends on it: `ToolBox.begin_turn` reads
        #: the raw utterance for a word that means *open* or *play*, and the
        #: launch tool is refused for the whole turn when there is none.
        #:
        #: It hangs off the coordinator rather than off `respond` because this
        #: is the single door — the wake word, the Listen button, the compose
        #: box and the phone all pass through here, so a permission set here is
        #: set the same way for all four. A rule that lived in the voice loop
        #: would be a rule the compose box did not have.
        self.on_turn = on_turn
        #: `after_turn()` — drop whatever `on_turn` granted. Belt and braces:
        #: the flag is set per turn and would be re-set by the next one anyway,
        #: but a permission that outlives its turn is the kind of thing nobody
        #: notices until it matters.
        self.after_turn = after_turn
        self.turn_wait_s = turn_wait_s
        self.bridge = bridge if bridge is not None else (
            AgentBridge(agent, self.events) if agent is not None else None)
        #: One turn at a time. Two overlapping submissions share a
        #: `Conversation`, and interleaving them produces a history where the
        #: model answers one question with the tool results of another.
        self._turn = threading.Lock()

    # ── the one door ────────────────────────────────────────────────

    def submit_voice(self, text: str, language: str = "en") -> dict:
        """Something said out loud that the fast router declined.

        The router still matches first, in `main.py`, in about nine
        milliseconds and without a network. This is the other branch.
        """
        return self._turn_for(text, "voice", language)

    def submit_text(self, text: str, source: str = "text",
                    language: str = "en") -> dict:
        """Typed on the panel, dictated into the compose box, or sent by phone.

        The same path as `submit_voice` on purpose. It used to be `agent.ask`
        unconditionally, which meant that typing "set the volume to thirty"
        started a maintenance run with a 24-step budget to do something the
        voice loop does in one call — and that saying it out loud and typing it
        got different answers.
        """
        if source not in ("text", "touch"):
            source = "text"
        return self._turn_for(text, source, language)

    def _turn_for(self, text: str, source: str, language: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "there was nothing in that message"}

        if not self._turn.acquire(timeout=self.turn_wait_s):
            # Not queued. Whoever is waiting on this has been waiting half a
            # minute, and an answer that arrives after they have gone is worse
            # than being told plainly.
            log.warning("a request arrived while another was still running")
            return {"ok": False,
                    "error": "I am still working on the last thing. Ask again "
                             "in a moment."}
        try:
            self.events.publish("user", source, text=text)
            if self.respond is None:
                message = ("I cannot have a conversation right now — the "
                           "language model is not available on this device.")
                self.events.publish("error", "system", text=message)
                return {"ok": False, "error": message}
            # Coerced rather than trusted. `respond` is `Assistant.answer`,
            # which returns a sentence — but it is injected, it is the one
            # thing here that runs somebody else's code, and a `TypeError` on
            # this line is a voice loop that stops answering.
            if self.on_turn is not None:
                # Before `respond`, and from `text` rather than from anything
                # the model produces. See `ToolBox.begin_turn`.
                try:
                    self.on_turn(text, source)
                except Exception:                    # noqa: BLE001
                    log.exception("preparing the turn failed")
            reply = self.respond(text, language)
            reply = str(reply or "").strip()
            if reply:
                self.events.publish("assistant", source, text=reply)
            return {"ok": True, "text": reply, "run": self.run()}
        finally:
            if self.after_turn is not None:
                try:
                    self.after_turn()
                except Exception:                    # noqa: BLE001
                    log.exception("finishing the turn failed")
            self._turn.release()

    # ── delegating, and following what was delegated ────────────────

    def delegate(self, task: str, reason: str = "") -> dict:
        """Hand a long investigation to `aipi5-agent.service`.

        Called from a tool in the short loop, not from a decision made here:
        the model is the thing that knows "why did the screen go blank last
        night" cannot be answered in three rounds and "set the volume to
        thirty" can. See `delegate_agent_task` in `aipi5/llm/tools.py`.

        Returns the run id. The answer does not come back through this call —
        it arrives in the transcript, minutes later, through `AgentBridge`.
        """
        task = (task or "").strip()[:MAX_TASK]
        if not task:
            return {"ok": False, "error": "there was nothing to look into"}
        if self.agent is None:
            return {"ok": False,
                    "error": "the maintenance agent is not installed on this "
                             "device, so there is nothing to hand this to"}

        status, answer = self.agent.say({"type": "agent.ask", "text": task})
        if status != 200 or not answer.get("ok"):
            detail = str(answer.get("error", "")) or "the agent is not answering"
            self.events.publish("error", "agent", text=detail)
            return {"ok": False, "error": detail}

        run_id = str(answer.get("run", ""))[:64]
        self.events.publish("tool", "system", tool="delegate_agent_task",
                            ok=True, run=run_id,
                            text=(reason or "Looking into that. Progress is on "
                                            "the screen."))
        if self.bridge is not None:
            self.bridge.start(run_id)
        return {"ok": True, "run": run_id,
                "appended": bool(answer.get("appended"))}

    def cancel(self, run_id: str = "") -> dict:
        """Stop the delegated run. Silence is not a stop; this is explicit."""
        if self.agent is None:
            return {"ok": False, "error": "there is no run to stop"}
        run_id = str(run_id or self.run())[:64]
        status, answer = self.agent.say({"type": "agent.stop", "run": run_id})
        if status != 200:
            return {"ok": False,
                    "error": str(answer.get("error", "")) or "the agent is not "
                                                             "answering"}
        return answer if answer else {"ok": True}

    def answer_approval(self, token: str, allow: bool) -> dict:
        """Yes or no to the card, from the panel or from the phone.

        `allow` is coerced to a bool here and the token is forwarded verbatim.
        Neither is ever derived from model output: an approval is a person
        touching a button, and the desk on the other side binds the answer to
        the token it issued. Anything else — a timeout, a stale token, an empty
        answer — is a no over there, not something to interpret here.
        """
        token = str(token or "")[:128]
        if not token:
            return {"ok": False, "error": "that approval is no longer waiting"}
        # The local desk first, and by token rather than by asking both. The
        # two issue different tokens and neither will recognise the other's, so
        # forwarding a local one to the agent would turn "no" into "the agent
        # is not answering" — a refusal that reads like a fault.
        if self.consent is not None:
            pending = self.consent.waiting()
            if pending is not None and pending.token == token:
                return self.consent.answer(token, allow is True)
        if self.agent is None:
            return {"ok": False, "error": "there is nothing waiting to be approved"}
        status, answer = self.agent.say({"type": "agent.answer", "token": token,
                                         "allow": bool(allow)})
        if status != 200:
            return {"ok": False,
                    "error": str(answer.get("error", "")) or "the agent is not "
                                                             "answering"}
        return answer if answer else {"ok": True}

    # ── what a page needs to draw itself ────────────────────────────

    def run(self) -> str:
        return self.bridge.run if self.bridge is not None else ""

    def collect(self, since: int = 0, timeout: float | None = None):
        return self.events.collect(since, timeout)

    def snapshot(self) -> dict:
        """Enough to draw the page, and nothing that costs a socket round trip.

        The pending approval and the run state come from the agent's own
        snapshot, which `AgentProxy` caches for two seconds — so a page polling
        this at 1 Hz does not wake the agent twice a second.
        """
        state = {"cursor": self.events.cursor(),
                 "dropped": self.events.dropped(),
                 "run": self.run(),
                 "busy": bool(self.bridge is not None and self.bridge.busy()),
                 "agent": self.agent is not None,
                 "conversation": self.respond is not None,
                 "pending": None}
        if self.consent is not None:
            # Read first, so a local question is what the page draws while one
            # is open. They cannot both be waiting in practice — a delegated
            # run and a spoken confirmation are different moments — and if they
            # ever are, the one this process is holding is the one whose action
            # is sitting in memory waiting to be run or dropped.
            state["pending"] = self.consent.snapshot()
        if self.agent is not None:
            live = self.agent.snapshot()
            if isinstance(live, dict):
                state["pending"] = live.get("pending")
                state["busy"] = bool(live.get("busy")) or state["busy"]
                state["run"] = str(live.get("run", "") or state["run"])[:64]
                state["state"] = str(live.get("state", "idle"))
        return state

    def close(self) -> None:
        if self.bridge is not None:
            self.bridge.stop()
