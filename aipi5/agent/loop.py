"""The agent loop: ask, look, look again, answer.

Different from `OpenAIClient.respond` in the ways that matter, and the
differences are the design rather than tuning:

    respond()                        AgentLoop.run()
    ---------                        ---------------
    MAX_TOOL_ROUNDS = 3              max_steps, default 24
    one 20 s request                 a 900 s budget for the whole run
    history trimmed per turn         history kept whole, compacted at 60
    returns a sentence to speak      streams typed events to a phone
    the microphone thread            its own thread, one run at a time

**Every ceiling ends in a sentence.** When a budget runs out the loop makes one
final request with no tools offered, so the run finishes with "I ran out of
steps; here is what I found" rather than stopping mid-thought. That is the same
trick `respond()` uses for its last round, and it is the difference between a
transcript that concludes and one that simply stops.

**Compaction never splits a tool-call pair.** An assistant message carrying
`tool_calls` and the `tool` messages answering it must travel together or the
API rejects the request, with an error that says nothing about history. This is
the property `Conversation._trim` already encodes for the voice path; the
reasoning is the same here and the code is not shared because the shapes are
not — that one cuts on turns, this one on token pressure.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: How many messages before the oldest half is folded into a summary. Chosen so
#: a normal run never reaches it and a pathological one does not grow forever.
COMPACT_AT = 60

#: Consecutive failed requests before the run gives up. Three is enough to ride
#: out a blip and few enough that a wrong API key does not burn the budget.
MAX_CONSECUTIVE_FAILURES = 3

#: Per-tool retries. A refusal is never retried — it is policy, and asking
#: again gets the same answer more slowly.
MAX_RETRIES_PER_TOOL = 2


@dataclass(frozen=True)
class Budget:
    """Every way a run can end other than by finishing."""

    max_steps: int = 24
    max_tool_calls: int = 40
    max_runtime_s: float = 900.0
    max_tokens: int = 400_000


@dataclass
class RunResult:
    ok: bool = True
    text: str = ""
    stopped: str = ""          # why it ended, if not by finishing
    steps: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentLoop:
    """One run at a time, on its own thread, reporting as it goes."""

    def __init__(self, client, toolbox, emit, system: str,
                 budget: Budget | None = None, clock=time.monotonic):
        self.client = client
        self.toolbox = toolbox
        #: `emit(event: dict)` — how the phone hears about this. Called from
        #: the run thread, so it must not block for long.
        self.emit = emit
        self.system = system
        self.budget = budget or Budget()
        #: Injected so a test can exhaust a 900-second budget instantly, the
        #: same way `screensaver/schedule.py` takes its clock.
        self.clock = clock
        self._lock = threading.Lock()
        self._pushed: list[str] = []

    def push(self, text: str) -> bool:
        """Add something the person said while a run was already going.

        Appended to the live run at its next step rather than refused. "Wait —
        stop, do X instead" is a normal thing to say to something working on
        your behalf, and a refusal would make the person start again.
        """
        if not text.strip():
            return False
        with self._lock:
            self._pushed.append(text.strip())
        return True

    def run(self, run_id: str, ask: str, stop: threading.Event) -> RunResult:
        started = self.clock()
        messages: list[dict] = [{"role": "system", "content": self.system},
                                {"role": "user", "content": ask}]
        result = RunResult()

        failures = 0
        while True:
            for extra in self._drain():
                messages.append({"role": "user", "content": extra})
                self.emit({"type": "agent.ask", "run": run_id, "text": extra,
                           "echo": True})

            reason = self._exhausted(result, started, stop)
            step = self._request(messages, run_id, result, final=bool(reason))

            if not step.ok:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    result.ok = False
                    result.stopped = "the model could not be reached"
                    result.text = step.error
                    self.emit({"type": "agent.error", "run": run_id,
                               "error": step.error})
                    break
                self.emit({"type": "agent.error", "run": run_id,
                           "error": f"{step.error} — trying again"})
                continue
            failures = 0

            result.steps += 1
            result.prompt_tokens += step.prompt_tokens
            result.completion_tokens += step.completion_tokens
            if step.text:
                result.text = step.text
                self.emit({"type": "agent.say", "run": run_id, "text": step.text})

            if reason or not step.tool_calls:
                result.stopped = reason
                break

            messages.append(step.message)
            for call in step.tool_calls:
                messages.append(self._invoke(call, run_id, result))
                if stop.is_set():
                    break

            if len(messages) > COMPACT_AT:
                messages = _compact(messages)

        result.seconds = self.clock() - started
        self.emit({"type": "agent.done", "run": run_id, "ok": result.ok,
                   "steps": result.steps, "tool_calls": result.tool_calls,
                   "seconds": round(result.seconds, 1),
                   "tokens": result.tokens, "stopped": result.stopped})
        return result

    # ── the pieces ──────────────────────────────────────────────────

    def _drain(self) -> list[str]:
        with self._lock:
            pushed, self._pushed = self._pushed, []
        return pushed

    def _exhausted(self, result: RunResult, started: float,
                   stop: threading.Event) -> str:
        """Why this must be the last step, or "" to carry on."""
        if stop.is_set():
            return "you asked me to stop"
        if result.steps >= self.budget.max_steps:
            return f"I reached my limit of {self.budget.max_steps} steps"
        if result.tool_calls >= self.budget.max_tool_calls:
            return f"I reached my limit of {self.budget.max_tool_calls} checks"
        if self.clock() - started >= self.budget.max_runtime_s:
            return f"I ran out of time after {self.budget.max_runtime_s:.0f}s"
        if result.tokens >= self.budget.max_tokens:
            return "I reached my limit for how much I could read"
        return ""

    def _request(self, messages, run_id, result, final):
        """One model request. `final` withholds the tools so this ends in prose."""
        if final:
            self.emit({"type": "agent.state", "run": run_id, "state": "finishing",
                       "step": result.steps})
        else:
            self.emit({"type": "agent.state", "run": run_id, "state": "thinking",
                       "step": result.steps, "max_steps": self.budget.max_steps})
        tools = None if final else self.toolbox.schemas()
        return self.client.step(messages, tools)

    def _invoke(self, call, run_id, result) -> dict:
        """Run one tool call and build the `tool` message answering it."""
        name = getattr(getattr(call, "function", None), "name", "") or "?"
        raw = getattr(getattr(call, "function", None), "arguments", "") or "{}"
        result.tool_calls += 1

        # Announced before it runs, not after: a journal read takes a moment
        # and the phone should say "reading the log" while it happens rather
        # than staying silent and then admitting it afterwards.
        self.emit({"type": "agent.tool", "run": run_id, "name": name,
                   "state": "running", "summary": _summary(name, raw)})

        started = self.clock()
        answer = self.toolbox.call(name, raw)
        for attempt in range(MAX_RETRIES_PER_TOOL):
            if not _retryable(answer):
                break
            log.info("%s failed transiently; retry %d", name, attempt + 1)
            answer = self.toolbox.call(name, raw)

        ms = (self.clock() - started) * 1000.0
        self.emit({"type": "agent.tool", "run": run_id, "name": name,
                   "state": "done", "ok": _succeeded(answer),
                   "ms": round(ms), "summary": _summary(name, raw)})
        return {"role": "tool", "tool_call_id": getattr(call, "id", ""),
                "content": answer}


def _retryable(answer: str) -> bool:
    try:
        return bool(json.loads(answer).get("retryable"))
    except (ValueError, AttributeError):
        return False


def _succeeded(answer: str) -> bool:
    try:
        return bool(json.loads(answer).get("ok"))
    except (ValueError, AttributeError):
        return False


def _summary(name: str, raw: str) -> str:
    """One short phrase for the phone. Never the model's reasoning."""
    try:
        args = json.loads(raw) if raw else {}
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "read_journal":
        return f"the {args.get('unit', '?')} log"
    if name == "service_status":
        return f"is {args.get('service', '?')} running"
    if name in ("read_config_file", "list_directory"):
        return str(args.get("path", "")).rsplit("/", 1)[-1] or "?"
    if name == "system_facts":
        return "disk, memory and temperature"
    return ""


def _compact(messages: list[dict]) -> list[dict]:
    """Fold the oldest half into a note, keeping the newest half verbatim.

    **The cut must not land between an assistant message carrying `tool_calls`
    and the `tool` messages that answer it.** The API rejects the second without
    the first, and the error it gives names neither history nor compaction — so
    the cut walks forward to the next safe boundary, which is a user message or
    an assistant message with no tool calls.
    """
    system, rest = messages[0], messages[1:]
    cut = len(rest) // 2
    while cut < len(rest) and not _safe_boundary(rest[cut]):
        cut += 1
    if cut >= len(rest):                    # nowhere safe; keep it all
        return messages
    dropped = rest[:cut]
    note = {"role": "assistant",
            "content": "[Earlier in this run I made "
                       f"{sum(1 for m in dropped if m.get('role') == 'tool')} "
                       "checks. Their details are no longer in front of me; "
                       "what I concluded from them is above.]"}
    return [system, note, *rest[cut:]]


def _safe_boundary(message: dict) -> bool:
    if message.get("role") == "user":
        return True
    return message.get("role") == "assistant" and not message.get("tool_calls")
