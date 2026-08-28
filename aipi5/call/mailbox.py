"""A long-poll JSON mailbox between named seats.

This was the inside of `SignalingHub` and is now its own thing, because a
second caller wants exactly this and none of the rest. Nothing here knows what
a call is: it moves dictionaries between names, in order, and lets a reader
park until one arrives. `aipi5/call/signaling.py` adds the call state machine
on top; `aipi5/agent/` uses a second instance for the phone's agent console.

**Long-poll rather than a WebSocket**, and that is a deliberate choice rather
than a shortcut — the reasoning is in `signaling.py`'s docstring and has not
changed: the whole of AIA's and AIPI5's HTTP is `ThreadingHTTPServer` from the
standard library, which has no WebSocket in it, and a held GET answers a posted
message within a millisecond at these volumes.

**Its own lock, not the caller's.** The hub used to wait for messages on the
same condition variable that guards its state machine. Separating them is not
only tidier: a `collect()` parked for twenty-five seconds no longer shares a
lock with the code deciding whether a call is up. The ordering to keep is that
a caller may hold its own lock while calling in here, and this module never
calls back out — so there is no cycle to deadlock on.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

log = logging.getLogger(__name__)

# How long a held GET waits before answering with nothing. Short enough that a
# phone which has gone to sleep or lost its network is noticed within a
# reasonable time, long enough that a two-minute silent call is not a hundred
# round trips. Also bounds how long a request thread is parked, which matters:
# `ThreadingHTTPServer` gives each one a thread.
POLL_TIMEOUT_S = 25.0

# A mailbox that is never collected must not grow without limit. A call
# generates tens of messages, so this is two orders of magnitude of headroom
# and still bounded — the failure it prevents is a peer that vanished
# mid-handshake leaving its messages queued forever.
MAX_MAILBOX = 256


class Mailbox:
    """Ordered JSON delivery between a fixed set of named seats.

    Thread-safe by construction: every public method takes the lock, and the
    only thing that waits does so on a condition variable rather than by
    sleeping and re-checking.

    **The seats are fixed at construction and a message to an unknown one is
    refused**, which is what makes "exactly two participants" a property of the
    type rather than a rule enforced somewhere else.
    """

    def __init__(self, seats, *, max_depth: int = MAX_MAILBOX,
                 poll_timeout_s: float = POLL_TIMEOUT_S):
        self.seats = tuple(seats)
        self.max_depth = max_depth
        self.poll_timeout_s = poll_timeout_s
        self._lock = threading.Condition()
        # One counter across every seat, so a cursor is meaningful on its own
        # and two seats can never hand back the same sequence number.
        self._sequence = itertools.count(1)
        self._boxes: dict[str, list[tuple[int, dict]]] = {s: [] for s in self.seats}
        #: monotonic time each seat last asked for its messages.
        self._seen: dict[str, float] = {}
        #: How many messages have been evicted unread, per seat.
        #:
        #: Overflow used to be a `log.warning` and nothing else, which is fine
        #: for a call — a peer that far behind has lost the handshake and is
        #: going to be hung up on anyway. It is not fine for a longer-lived
        #: reader: dropping the front of a transcript and saying nothing gives
        #: it a hole it cannot see. Counting the loss lets a caller say "N
        #: messages lost" instead of quietly showing less than happened.
        self._dropped: dict[str, int] = {s: 0 for s in self.seats}

    def post(self, to: str, message: dict) -> bool:
        """Put one message in a seat's box. False if there is no such seat."""
        if to not in self._boxes:
            return False
        with self._lock:
            box = self._boxes[to]
            box.append((next(self._sequence), message))
            if len(box) > self.max_depth:
                # The oldest go first. A peer this far behind has lost the
                # handshake anyway, and the alternative is unbounded memory
                # held for a peer that is not coming back.
                lost = len(box) - self.max_depth
                del box[:lost]
                self._dropped[to] += lost
                log.warning("the %s mailbox overflowed; dropped %d, %d in total",
                            to, lost, self._dropped[to])
            self._lock.notify_all()
        return True

    def collect(self, seat: str, since: int,
                timeout: float | None = None) -> tuple[list[dict], int]:
        """Everything queued for `seat` after `since`, waiting if there is none.

        Returns (messages, cursor). The cursor is the sequence to ask for next
        and moves even when the batch is empty, so a caller cannot get stuck
        re-reading the same tail.
        """
        if seat not in self._boxes:
            return [], since
        wait_for = self.poll_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + wait_for
        with self._lock:
            # Asking for messages is what proves a peer is still there. Marked
            # on entry rather than on return, so a poll that waits out its
            # whole timeout still counts as the peer being present.
            self._seen[seat] = time.monotonic()
            while True:
                pending = [(seq, m) for seq, m in self._boxes[seat] if seq > since]
                if pending:
                    return [m for _, m in pending], pending[-1][0]
                left = deadline - time.monotonic()
                if left <= 0:
                    # Nothing arrived. The cursor is unchanged, which is
                    # correct — there is nothing new to have consumed.
                    return [], since
                # Capped at a second so a shutdown is noticed promptly even
                # when nothing is being posted.
                self._lock.wait(min(left, 1.0))

    def dropped(self, seat: str) -> int:
        """How many messages were evicted unread. Never resets except on reset().

        A caller that cares reads this beside `collect` and tells its reader
        what it lost. `SignalingHub` does not care — a call whose mailbox
        overflowed is over — which is why this counts rather than acting.
        """
        with self._lock:
            return self._dropped.get(seat, 0)

    def last_seen(self, seat: str) -> float | None:
        """When `seat` last collected, on the monotonic clock. None if never."""
        with self._lock:
            return self._seen.get(seat)

    def reset(self) -> None:
        """Empty every box and forget who has been asking.

        The sequence deliberately keeps rising. A cursor held by a peer that
        has not noticed the reset then asks for something that will never
        arrive, rather than being handed the next session's messages as though
        they were the tail of its own.
        """
        with self._lock:
            for seat in self.seats:
                self._boxes[seat] = []
            self._seen = {}
            self._dropped = {s: 0 for s in self.seats}
            self._lock.notify_all()

    def depth(self, seat: str) -> int:
        """How many messages are queued for a seat. For tests and diagnostics."""
        with self._lock:
            return len(self._boxes.get(seat, ()))
