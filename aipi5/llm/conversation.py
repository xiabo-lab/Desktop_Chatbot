"""What the assistant remembers of the conversation, and for how long.

Section 13 asks for follow-up questions to work and for history not to grow
without limit. Those pull in opposite directions and this module is where the
compromise is written down.

**Bounded by turns, not tokens.** A turn is what the person perceives — "the
last few things we talked about" — and it is also what a limit has to be
expressed in for anybody to reason about it. Tokens are the thing that actually
costs money and latency, but a token limit trims mid-conversation at a boundary
nobody can predict, so that a follow-up question sometimes works and sometimes
does not for reasons invisible from the room.

**Forgotten after a silence.** Somebody who walks up to the device an hour
later is starting a new conversation. Carrying the old one over makes the first
answer refer to something nobody in the room said, which reads as the assistant
having misheard rather than as it having remembered.

**Tool calls are part of the turn they belong to.** They are kept together and
dropped together, because the API rejects a `function_call` whose matching
`function_call_output` is missing, and rejects an output whose call it has not
seen — trimming that splits the pair produces a 400 that looks like a malformed
request rather than like a history that was cut in the wrong place. `_trim` is
the whole of the care that takes.

**These are `/v1/responses` input items, not chat messages.** An ordinary turn
is the same either way — `{"role": "user", "content": "..."}` is accepted by
both — and the two parts that differ are exactly the two that matter. A tool
call is an item of its own here rather than a field on the assistant message,
and its answer is a `function_call_output` carrying `call_id` rather than a
`tool` message carrying `tool_call_id`. The practical consequence is that this
history has items with no `role` in it, which `_trim` has to be read with in
mind: a turn still starts at a `user` role, and a roleless item belongs to
whichever turn it follows.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class Conversation:
    """A bounded, self-expiring list of `/v1/responses` input items.

    Holds the user's turns, the assistant's replies, and the tool calls and
    results in between. The system prompt is *not* here: it is rebuilt for
    every request because it carries the current time and the language of the
    utterance, both of which change turn to turn, and it goes as
    `instructions` rather than as a first item.
    """

    def __init__(self, max_turns: int = 8, idle_seconds: float = 600.0):
        self.max_turns = max_turns
        self.idle_seconds = idle_seconds
        self._messages: list[dict] = []
        self._last_activity = 0.0

    # ── the voice loop's side ────────────────────────────────────────

    def begin_turn(self, now: float | None = None) -> bool:
        """Note that somebody is speaking. True if the thread was forgotten.

        Called before the user message is added, so the decision to forget is
        made on the gap between this utterance and the last one — which is the
        gap the person experienced.
        """
        now = time.time() if now is None else now
        idle = now - self._last_activity
        forgot = False
        if self._messages and idle > self.idle_seconds:
            log.info("forgetting %d message(s) after %.0fs of silence",
                     len(self._messages), idle)
            self._messages.clear()
            forgot = True
        self._last_activity = now
        return forgot

    def user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def assistant(self, text: str) -> None:
        self._messages.append({"role": "assistant", "content": text})

    def assistant_tool_calls(self, calls) -> None:
        """The `function_call` items the model asked for, as it sent them.

        Stored as the API's own dictionaries rather than rebuilt from their
        parts: each `call_id` has to match the `call_id` on the
        `function_call_output` that follows, and reconstructing an item is how
        those come to differ — for which the API's answer is an error about
        mismatched ids that says nothing about where the ids came from.

        A list, and appended before any of them is answered. The model may ask
        for several at once, and the API rejects a history in which a call is
        left unanswered as firmly as one in which an answer has no call.
        """
        if isinstance(calls, dict):          # one item, passed bare
            calls = [calls]
        self._messages.extend(calls)

    def tool_result(self, call_id: str, content: str) -> None:
        self._messages.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": content,
        })

    # ── what goes on the wire ────────────────────────────────────────

    def items(self) -> list[dict]:
        """The `input` array: the history, and nothing else.

        No system item. It is rebuilt every turn and goes as `instructions`,
        which keeps it out of the thing `_trim` is cutting — a system prompt in
        the history is a system prompt that can be trimmed away, and the
        assistant then answers in the wrong language with no clue as to why.
        """
        return list(self._messages)

    def trim(self) -> None:
        """Drop the oldest turns until the history is within its limit.

        Called after a turn rather than before, so the turn that is happening
        is never trimmed out from under itself.
        """
        self._messages = _trim(self._messages, self.max_turns)

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)

    def describe(self) -> dict:
        return {
            "messages": len(self._messages),
            "items": len(self._messages),
            "turns": sum(1 for m in self._messages if m.get("role") == "user"),
            "max_turns": self.max_turns,
            "idle_seconds": self.idle_seconds,
            "last_activity": self._last_activity,
        }


def _trim(messages: list[dict], max_turns: int) -> list[dict]:
    """Keep the last `max_turns` user turns and everything that belongs to them.

    A turn starts at a user message and runs to just before the next one, so
    every assistant reply, tool call and tool result travels with the question
    that caused it. That is what keeps a `tool_calls` message and its results
    from being separated — the API rejects the first without the second, with
    an error that says nothing about history trimming.

    Anything before the first user message is dropped. It can only be a
    fragment of a turn whose question has already gone — which now includes a
    `function_call_output` whose call went with it, and that is the one the API
    refuses outright.
    """
    if max_turns <= 0:
        return []

    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(starts) <= max_turns:
        # Still within the limit — but if there is anything before the first
        # user message it is orphaned and goes anyway.
        return messages[starts[0]:] if starts else []

    return messages[starts[-max_turns]:]
