"""One-shot, short-lived permission to fetch one file.

This exists because of a browser rule with no way around it: **a download has
to be a plain link.** Safari saves a file to the Files app when the person taps
an ordinary URL; a `fetch` with an `Authorization` header cannot do that
without reading the whole file into the phone's memory first and handing it
back as a blob, which for the 500 MB video this feature exists to move is not a
download, it is a crash.

So the page asks — authenticated, with its bearer token, as everything else
does — for a ticket, and then points the browser at a URL carrying the ticket.

What keeps that safe is what a ticket is *not*. It is not the device token: it
cannot list, upload, delete, ring the Pi or open the camera. It names one file,
it works once, and it expires in a minute whether it is used or not. A ticket
in a browser history is a link that has already stopped working — which is the
property the whole design turns on, because a URL is the one thing here that
ends up written down.
"""

from __future__ import annotations

import secrets
import threading
import time

#: How long a ticket is worth anything. The gap between asking and the browser
#: starting the download is milliseconds; a minute is generous for a phone that
#: went through a tunnel in between, and short enough that a leaked URL is
#: almost always already dead.
TTL_S = 60.0

#: How many may be outstanding at once. Tickets are tiny, but they arrive from
#: the network and nothing that arrives from the network gets to grow forever.
MAX_OUTSTANDING = 64


class Tickets:
    """Issues and redeems download tickets. Safe from several threads."""

    def __init__(self, ttl_s: float = TTL_S):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._issued: dict[str, tuple[str, str, float]] = {}

    def issue(self, filename: str, device: str = "") -> str:
        """A ticket for one file. The caller has already authenticated."""
        ticket = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            if len(self._issued) >= MAX_OUTSTANDING:
                # Oldest first: the ones nobody used.
                oldest = min(self._issued, key=lambda k: self._issued[k][2])
                del self._issued[oldest]
            self._issued[ticket] = (filename, device, now + self.ttl_s)
        return ticket

    def redeem(self, ticket: str) -> str | None:
        """The filename this ticket was for, once. None if it is no good.

        Removed as it is read, inside the lock — so two requests racing with
        the same ticket cannot both be served, and a link that has been used is
        a link that has stopped working.
        """
        if not ticket:
            return None
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            found = self._issued.pop(ticket, None)
        if found is None:
            return None
        filename, _device, expires = found
        return filename if expires > now else None

    def _expire(self, now: float) -> None:
        """Drop what has run out. Caller holds the lock."""
        dead = [key for key, (_, _, expires) in self._issued.items()
                if expires <= now]
        for key in dead:
            del self._issued[key]

    def __len__(self) -> int:
        with self._lock:
            self._expire(time.monotonic())
            return len(self._issued)
