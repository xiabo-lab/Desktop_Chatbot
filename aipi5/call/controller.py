"""Ringing a phone from this device, in one place.

The body of this used to live inside `_call_post` in `aipi5/ui/server.py`, and
that was fine while pressing the button on the panel was the only way to do it.
It stopped being fine the moment "call my phone" became something a person
could say out loud: the second caller would have been a second copy of the
sequence — start the signalling, tell the page, send the push, report which of
those worked — and the two copies would have drifted at the first change.

What is here is that sequence and nothing else. No HTTP, no request parsing, no
policy about who may ask. The route keeps its own reasoning about being bound to
loopback; the tool keeps its own about asking a person first.

**Nothing here takes a phone number, a URL or a push endpoint.** `device` is a
name that must already be in `Subscriptions`, and every one of those got there
by somebody pairing a phone with the token the call uses. A name that is not in
that list is refused rather than looked up, tried, or normalised — which is
what makes "the model cannot ring an arbitrary number" a property of the type
rather than a rule written down somewhere.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CallOutcome:
    """What happened, in enough detail to say something true out loud.

    Four separate things can go wrong or right and they are not the same thing,
    which is why this is a small object rather than a boolean:

    - `started`   the signalling session is up and the Pi is calling;
    - `notified`  the Web Push notification actually reached the service;
    - `answered`  somebody picked up — never true here, and that is the point;
    - `detail`    what to say about whichever of those did not happen.

    A ring that started with a notification that failed is the interesting
    case: the phone may still see it by polling if the app is open, so the call
    is genuinely up — but "it rang and nothing happened" has no explanation
    anywhere unless somebody is told the notification did not go.
    """

    def __init__(self, started: bool, device: str = "", session: str = "",
                 notified: bool = False, detail: str = "",
                 ice_servers=None):
        self.started = started
        self.device = device
        self.session = session
        self.notified = notified
        self.detail = detail
        self.ice_servers = ice_servers or []

    #: Never set here. A call is answered by somebody picking up a phone,
    #: which happens seconds later and is reported by the hub's own state —
    #: so a tool that claimed it had been answered would be claiming it on the
    #: strength of having asked.
    answered = False

    def as_dict(self) -> dict:
        return {"ok": self.started, "device": self.device,
                "session": self.session, "notified": self.notified,
                "answered": self.answered, "detail": self.detail}

    def __bool__(self) -> bool:
        return self.started


class CallController:
    """The one implementation of "ring a paired phone".

    Built with the pieces rather than with the `CallServer`, so a test can
    drive it with three small fakes and no TLS, no Tailscale and no network.
    """

    def __init__(self, *, hub, subscriptions, push, ice_servers,
                 on_change=lambda: None):
        self.hub = hub
        self.subscriptions = subscriptions
        self.push = push
        #: `CallServer.ice_servers` — a callable, because TURN credentials
        #: expire and a list captured at construction is a list that stops
        #: working some hours into the day.
        self.ice_servers = ice_servers
        self.on_change = on_change

    # ── who can be rung ─────────────────────────────────────────────

    def phones(self) -> list[str]:
        """Every paired phone that has registered for push, by name."""
        try:
            return list(self.subscriptions.names())
        except Exception:                            # noqa: BLE001
            log.exception("could not read the paired phones")
            return []

    def available(self) -> bool:
        return bool(self.phones())

    # ── the one sequence ────────────────────────────────────────────

    def call_out(self, device: str = "") -> CallOutcome:
        """Ring `device`, or the only paired phone when there is one.

        The order matters and is the reason this is one function. The
        signalling session is started first, then the page is told, then the
        notification goes — a push that arrived before the session existed
        would open a phone onto a call that is not there yet, and the phone's
        answer would be refused by a hub that had never heard of it.
        """
        phones = self.phones()
        if not phones:
            return CallOutcome(False, detail="no phone has registered for calls")

        device = str(device or "").strip()
        if not device:
            if len(phones) > 1:
                # Deliberately not "the first one". Ringing the wrong person's
                # phone is a worse outcome than asking which.
                return CallOutcome(
                    False, detail="there is more than one paired phone: "
                                  + ", ".join(phones))
            device = phones[0]
        elif device not in phones:
            return CallOutcome(False, device=device,
                               detail=f"{device!r} is not a paired phone")

        started, session, why = self.hub.call_out(device)
        if not started:
            return CallOutcome(False, device=device, detail=why)

        self.on_change()

        sent, detail = self.push.ring(device, {
            "type": "call", "session": session,
            "title": "AIPI5 is calling",
            "body": "Tap to answer",
        })
        if not sent:
            # The ring is still up — the phone may have the app open and see it
            # by polling — but say plainly that the notification did not go.
            # "It rang and nothing happened" otherwise has no explanation
            # anywhere.
            log.warning("calling %s but the notification failed: %s",
                        device, detail)
        return CallOutcome(True, device=device, session=session,
                           notified=sent, detail=detail,
                           ice_servers=self.ice_servers("aipi5"))
