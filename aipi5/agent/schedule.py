"""Reminders that survive a reboot.

A few dozen rows in a JSON Lines file, not a database. "There is no SQLite
anywhere in AIPI5" is currently true and worth keeping true: a scheduled-task
list is a handful of records, and a schema plus a migration path is a great deal
of machinery to carry for something a text editor can fix at three in the
morning.

**The clock is injected**, the way `aipi5/screensaver/schedule.py` takes its
own. Moving the Pi's system clock to test a time-of-day behaviour disturbs TLS,
Tailscale and the call certificates; passing `now` disturbs nothing and lets a
test check a reminder eight days out in a millisecond.

**Nothing here delivers anything.** This module decides *what is due*; the
assistant does the sending, because the push keys and any mail credentials live
in a home the agent user cannot traverse. That split is not an inconvenience —
it means a compromised agent can schedule a notification and still cannot reach
the account that sends it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Far enough ahead to be useful, near enough that a mistyped year is caught.
MAX_AHEAD_S = 365 * 24 * 3600
#: A reminder for a moment already past is almost always a timezone mistake,
#: but a minute of slack lets "remind me in a minute" work.
MAX_BEHIND_S = 60.0

MAX_TEXT = 500
MAX_PENDING = 100

#: Deliveries that failed are retried, and then given up on rather than
#: retried forever into a phone that has been factory reset.
MAX_ATTEMPTS = 5

PENDING, SENT, CANCELLED, FAILED = "pending", "sent", "cancelled", "failed"


@dataclass
class Reminder:
    id: str
    at: float
    text: str
    deliver: str = "push"
    state: str = PENDING
    created: float = 0.0
    run: str = ""
    attempts: int = 0
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def describe(self, now: float | None = None) -> dict:
        """What the model and the phone are told. Human times, not epochs."""
        now = time.time() if now is None else now
        return {"id": self.id, "text": self.text, "state": self.state,
                "deliver": self.deliver,
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at)),
                "in_seconds": round(self.at - now)}


class Schedule:
    """Every reminder this device is holding, and which of them are due."""

    def __init__(self, path: Path | str, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._items: list[Reminder] = []
        self._load()

    # ── reading ─────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                self._items.append(Reminder(**row))
            except (ValueError, TypeError) as exc:
                # One unreadable line must not lose the rest. This file is
                # meant to be editable by hand at three in the morning, and
                # something edited by hand is something that can be edited
                # wrong.
                log.warning("skipping an unreadable reminder: %s", exc)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".jsonl.tmp")
        body = "".join(json.dumps(item.as_dict(), default=str) + "\n"
                       for item in self._items)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    # ── writing ─────────────────────────────────────────────────────

    def add(self, at: float, text: str, deliver: str = "push",
            run: str = "") -> Reminder:
        now = self.clock()
        text = (text or "").strip()
        if not text:
            raise ValueError("a reminder needs something to say")
        if len(text) > MAX_TEXT:
            raise ValueError(f"that is longer than {MAX_TEXT} characters")
        try:
            at = float(at)
        except (TypeError, ValueError):
            raise ValueError("a time is required")
        if at < now - MAX_BEHIND_S:
            raise ValueError("that time has already passed. If you meant "
                             "tomorrow, say so — I read times in this "
                             "device's own timezone.")
        if at > now + MAX_AHEAD_S:
            raise ValueError("that is more than a year away")
        # `push` only, and `email` was removed rather than left accepted.
        # `Housekeeping.deliver_due` rings the paired phone for everything due
        # and never reads this field, so storing `email` recorded an intention
        # the device has no way to carry out -- and the reminder arrived on the
        # phone anyway, with nobody told it had been changed.
        if deliver not in ("push",):
            raise ValueError(f"I cannot deliver by {deliver!r}; this device "
                             f"sends reminders to the paired phone")

        with self._lock:
            if sum(1 for i in self._items if i.state == PENDING) >= MAX_PENDING:
                raise ValueError(f"there are already {MAX_PENDING} reminders "
                                 f"waiting, which is the limit")
            item = Reminder(id="m-" + uuid.uuid4().hex[:8], at=at, text=text,
                            deliver=deliver, created=now, run=run)
            self._items.append(item)
            self._save_locked()
        return item

    def cancel(self, ident: str) -> Reminder | None:
        with self._lock:
            for item in self._items:
                if item.id == ident and item.state == PENDING:
                    item.state = CANCELLED
                    self._save_locked()
                    return item
        return None

    def due(self) -> list[Reminder]:
        """Everything whose moment has arrived and which nobody has sent."""
        now = self.clock()
        with self._lock:
            return [i for i in self._items
                    if i.state == PENDING and i.at <= now]

    def delivered(self, ident: str, ok: bool, detail: str = "") -> bool:
        """The assistant reports what happened. Retried until it gives up."""
        with self._lock:
            for item in self._items:
                if item.id != ident or item.state != PENDING:
                    continue
                if ok:
                    item.state = SENT
                    item.detail = detail
                else:
                    item.attempts += 1
                    item.detail = detail
                    if item.attempts >= MAX_ATTEMPTS:
                        item.state = FAILED
                    else:
                        # Try again in a minute rather than on every tick, so
                        # a phone that is off does not burn the attempts in
                        # five seconds.
                        item.at = self.clock() + 60.0
                self._save_locked()
                return True
        return False

    def pending(self) -> list[Reminder]:
        with self._lock:
            return [i for i in self._items if i.state == PENDING]

    def listing(self, limit: int = 20) -> list[dict]:
        now = self.clock()
        with self._lock:
            items = sorted(self._items, key=lambda i: i.at)
        upcoming = [i for i in items if i.state == PENDING]
        past = [i for i in items if i.state != PENDING][-limit:]
        return [i.describe(now) for i in (upcoming + past)[:limit]]

    def prune(self, keep: int = 200) -> int:
        """Forget the oldest finished ones. Pending is never pruned."""
        with self._lock:
            finished = [i for i in self._items if i.state != PENDING]
            if len(finished) <= keep:
                return 0
            drop = set(id(i) for i in
                       sorted(finished, key=lambda i: i.at)[:len(finished) - keep])
            before = len(self._items)
            self._items = [i for i in self._items if id(i) not in drop]
            self._save_locked()
            return before - len(self._items)


def parse_when(text: str, now: float | None = None) -> float:
    """An absolute local time, from what the model wrote.

    The model is given the current date and time in its prompt and asked for an
    ISO timestamp, so this is a validator rather than a natural-language parser.
    Deliberately so: "next Tuesday" means something different depending on which
    day it is asked, and a parser that guesses is a reminder that arrives on the
    wrong day for reasons nobody can reconstruct afterwards.

    Naive timestamps are read in **this device's** timezone, which is what
    somebody standing in front of it means.
    """
    import datetime

    if not isinstance(text, str) or not text.strip():
        raise ValueError("a time is required, like 2026-08-28 09:00")
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(f"I could not read {text!r} as a time. Use something "
                         f"like 2026-08-28 09:00.")
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()
