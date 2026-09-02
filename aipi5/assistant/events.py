"""What the Assistant page is shown, and the one rule about what it may hold.

Every surface reads this: the panel's transcript, the phone's, and anything
later. So it is a **presentation index** and deliberately not a second
transcript — the 24-hour audible room log in `ConversationLog` is the record of
what was said out loud, the agent's mailbox is the record of a maintenance run,
and both outlive this. What is here is a bounded ring of rows in the order a
person would have watched them happen, thrown away as it fills.

**The rule: a public event carries what could be said out loud, and nothing
else.** Not the JSON a tool returned, not the messages array, not the model's
reasoning. That is enforced here rather than asked for in a comment, because
the failure is silent and permanent: a tool result put in `text` "just for
debugging" is a file path, an API answer, or the contents of a config file
rendered onto a screen in a living room and polled by anything on loopback.
`_meta` refuses the names those arrive under and truncates everything else.

**Ids are monotonic and never reused**, so a cursor is meaningful on its own and
a reader that missed a window can tell it missed one — `dropped` counts what
was evicted before it was collected, which is the difference between a reader
that shows less than happened and one that knows it did.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field

#: What a row *is*. Small on purpose: a page renders one of seven shapes, and a
#: kind per feature would be a page that has to be edited every time the
#: assistant learns something.
#:
#: `user`      somebody asked for something
#: `assistant` the answer, as it was spoken or written
#: `tool`      the working — one line, dim, collapsible
#: `approval`  a question the run is blocked on, or the news that it is gone
#: `capture`   a photograph that belongs in the transcript
#: `done`      a delegated run finished, and what it cost
#: `error`     something failed in a way the person should see
KINDS = frozenset({"user", "assistant", "tool", "approval", "capture", "done",
                   "error"})

#: Where it came from. Not who: `voice` is the wake word or the Listen button,
#: `touch` is a button on the panel, `text` is the compose box or the phone,
#: `agent` is `aipi5-agent.service`, `system` is the device speaking about
#: itself. The distinction that matters is `agent` — it is the only source
#: whose text was not written by this process.
SOURCES = frozenset({"voice", "touch", "text", "agent", "system"})

#: How long a row of public text may be. Generous for an answer, far too short
#: for a log file or a JSON document, which is the point.
MAX_TEXT = 2000
#: A tool's one-line summary. "read 200 lines of the journal", not the lines.
MAX_SUMMARY = 200
MAX_META_VALUE = 500
MAX_META_KEYS = 12

#: Names a raw payload arrives under. Refused rather than truncated: a
#: truncated tool result on a screen is still a tool result on a screen, and
#: the person who added the key would not have seen the difference.
FORBIDDEN_META = frozenset({
    "result", "results", "output", "raw", "payload", "body", "content",
    "reasoning", "reasoning_content", "thinking", "thought", "chain_of_thought",
    "arguments", "args", "messages", "prompt", "system", "response",
    "api_key", "key", "secret", "token_usage", "usage",
})

#: How many rows are kept. A run posts a couple of dozen; a day of conversation
#: is a few hundred. Beyond this the oldest go, and `dropped` says so.
DEPTH = 400


class EventError(ValueError):
    """A row that must not be published. Raised at the sink, never at a reader."""


def _text(value, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _meta(value) -> dict:
    """Whatever a caller attached, reduced to something safe to render.

    Scalars only. A nested object is where a tool result hides — one level of
    `{"detail": {...}}` and the rule above is gone — so anything that is not a
    string, number or boolean is refused rather than flattened.
    """
    if not value:
        return {}
    if not isinstance(value, dict):
        raise EventError("event metadata must be an object")
    if len(value) > MAX_META_KEYS:
        raise EventError(f"an event may carry at most {MAX_META_KEYS} details")
    clean: dict = {}
    for key, item in value.items():
        name = str(key)
        if name.lower() in FORBIDDEN_META:
            raise EventError(
                f"{name!r} is where a raw tool result or model reasoning ends "
                f"up on a screen in somebody's living room; say what happened "
                f"in `text` instead")
        if isinstance(item, bool) or isinstance(item, (int, float)):
            clean[name] = item
        elif item is None:
            continue
        elif isinstance(item, str):
            clean[name] = item[:MAX_META_VALUE]
        else:
            raise EventError(f"{name!r} must be a string, number or boolean")
    return clean


@dataclass(frozen=True)
class AssistantEvent:
    """One row of the shared transcript.

    Frozen, because it is handed to several readers at once and a row that a
    renderer can edit is a row two pages disagree about.
    """

    id: int
    at: float
    kind: str
    source: str
    text: str = ""
    #: The delegated run this belongs to, where there is one.
    run: str = ""
    #: The tool a `tool` row is about, by name. Never its arguments.
    tool: str = ""
    #: Whether that tool succeeded. None where the question does not apply.
    ok: bool | None = None
    #: The `?t=` token a `capture` row's photograph is fetched with. A token,
    #: never a path — see `_camera_capture` in `aipi5/ui/server.py`.
    capture: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        row = {"id": self.id, "at": round(self.at, 3), "kind": self.kind,
               "source": self.source}
        if self.text:
            row["text"] = self.text
        if self.run:
            row["run"] = self.run
        if self.tool:
            row["tool"] = self.tool
        if self.ok is not None:
            row["ok"] = self.ok
        if self.capture:
            row["capture"] = self.capture
        if self.meta:
            row["meta"] = dict(self.meta)
        return row


class EventSink:
    """The one method `main.py`, the agent bridge and the UI server all hold.

    An interface rather than the log itself, so that a test can count what was
    published without a ring buffer, and so that the voice loop can be handed
    something that does nothing at all when the page is off.
    """

    def publish(self, kind: str, source: str, **fields) -> AssistantEvent | None:
        raise NotImplementedError


class NullSink(EventSink):
    """Validates and discards. What a service with no UI is given."""

    def publish(self, kind: str, source: str, **fields) -> AssistantEvent | None:
        _validate(kind, source, fields)
        return None


def _validate(kind: str, source: str, fields: dict) -> dict:
    """Everything checked before an id is spent on it."""
    if kind not in KINDS:
        raise EventError(f"{kind!r} is not a kind of event ({sorted(KINDS)})")
    if source not in SOURCES:
        raise EventError(f"{source!r} is not a source ({sorted(SOURCES)})")
    unknown = set(fields) - {"text", "run", "tool", "ok", "capture", "meta"}
    if unknown:
        raise EventError(f"an event has no {sorted(unknown)}")
    ok = fields.get("ok")
    if ok is not None and not isinstance(ok, bool):
        raise EventError("`ok` is true, false, or absent")
    return {
        "text": _text(fields.get("text")),
        "run": _text(fields.get("run"), 64),
        "tool": _text(fields.get("tool"), 64),
        "ok": ok,
        "capture": _text(fields.get("capture"), 64),
        "meta": _meta(fields.get("meta")),
    }


class EventLog(EventSink):
    """The rows, in order, with a cursor and a way to wait for the next one.

    Long-pollable for the same reason the agent's mailbox is: a delegated run
    posts a dozen rows in a few seconds, and a timer either shows them late or
    asks constantly while nothing at all is happening.
    """

    def __init__(self, depth: int = DEPTH, clock=time.time):
        self.depth = depth
        self.clock = clock
        self._lock = threading.Condition()
        self._rows: deque[AssistantEvent] = deque()
        self._ids = itertools.count(1)
        self._dropped = 0

    # ── writing ─────────────────────────────────────────────────────

    def publish(self, kind: str, source: str, **fields) -> AssistantEvent:
        checked = _validate(kind, source, fields)
        with self._lock:
            event = AssistantEvent(id=next(self._ids), at=self.clock(),
                                   kind=kind, source=source, **checked)
            self._rows.append(event)
            while len(self._rows) > self.depth:
                self._rows.popleft()
                self._dropped += 1
            self._lock.notify_all()
        return event

    # ── reading ─────────────────────────────────────────────────────

    def collect(self, since: int = 0, timeout: float | None = None
                ) -> tuple[list[dict], int]:
        """Rows after `since`, waiting up to `timeout` for the first of them.

        Returns (rows, cursor). The cursor is what to ask for next; it does not
        move when the batch is empty, because nothing was consumed.
        """
        deadline = None if not timeout else time.monotonic() + timeout
        with self._lock:
            while True:
                pending = [e for e in self._rows if e.id > since]
                if pending:
                    return [e.as_dict() for e in pending], pending[-1].id
                if deadline is None:
                    return [], since
                left = deadline - time.monotonic()
                if left <= 0:
                    return [], since
                # Capped at a second so a shutdown is noticed promptly even
                # while nothing is being published.
                self._lock.wait(min(left, 1.0))

    def cursor(self) -> int:
        with self._lock:
            return self._rows[-1].id if self._rows else 0

    def dropped(self) -> int:
        """Rows evicted before anybody collected them, since the process began."""
        with self._lock:
            return self._dropped

    def snapshot(self) -> dict:
        with self._lock:
            return {"cursor": self._rows[-1].id if self._rows else 0,
                    "rows": len(self._rows), "dropped": self._dropped,
                    "depth": self.depth}

    def clear(self) -> None:
        """Forget the rows. The counter keeps rising, so a page holding an old
        cursor asks for something that will never arrive rather than being
        handed the next session's rows as though they were the tail of its own.
        """
        with self._lock:
            self._rows.clear()
            self._lock.notify_all()
