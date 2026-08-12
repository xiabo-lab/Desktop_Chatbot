"""The signalling hub: one call, two seats, and a mailbox each.

Everything WebRTC needs to establish a session — the offer, the answer and the
ICE candidates — is opaque to this module. It moves JSON between exactly two
peers and holds the state machine that says whether there is a call.

**Long-poll rather than a WebSocket**, and that is a deliberate choice rather
than a shortcut. The whole of AIA's and AIPI5's HTTP is `ThreadingHTTPServer`
from the standard library, which has no WebSocket in it; adding one means a
dependency and an event loop next door to a voice loop that is already sharing
four cores with a wake recogniser, a speech recogniser and a person detector.
A call's signalling is perhaps thirty messages, all of them in the first two
seconds — a held GET answers each one within a millisecond of it being posted,
which is indistinguishable from a socket at this volume, and it dies correctly
when the client walks away instead of needing a ping/pong to notice.

**Exactly two seats, named rather than counted.** Version 1 is one phone and
one Pi, and making that a property of the type rather than a rule enforced
somewhere else means a second phone dialling in cannot become a third
participant in a conversation two people think is private. It is refused.
"""

from __future__ import annotations

import enum
import itertools
import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)

#: The two seats. A message is addressed to a role, not to a connection, so a
#: peer that reloads its page mid-call resumes where it was instead of losing
#: whatever was posted while it was gone.
PI = "pi"
PHONE = "phone"
ROLES = (PI, PHONE)

# How long a held GET waits before answering with nothing. Short enough that a
# phone which has gone to sleep or lost its network is noticed within a
# reasonable time, long enough that a two-minute silent call is not a hundred
# round trips. Also bounds how long a request thread is parked, which matters:
# `ThreadingHTTPServer` gives each one a thread.
POLL_TIMEOUT_S = 25.0

# A mailbox that is never collected must not grow without limit. A call
# generates tens of messages, so this is two orders of magnitude of headroom
# and still bounded — the failure it prevents is a phone that vanished
# mid-handshake leaving its ICE candidates queued forever.
MAX_MAILBOX = 256

# How long a ringing call waits for the Pi to answer before giving up. The Pi's
# page polls state twice a second and answers automatically, so this only ever
# expires when the screen is not running at all.
RING_TIMEOUT_S = 30.0

# How long a call may sit in `connecting` before it is declared failed. This is
# ICE gathering plus connectivity checks; on a LAN it completes in well under a
# second, and phase 3's TURN relay is what makes the slow cases slow.
CONNECT_TIMEOUT_S = 45.0

# How long the Pi keeps ringing a phone before giving up. Much longer than a
# ring inwards: an incoming call is answered by a page that is already running,
# while this one waits for a person to notice a notification, unlock a handset
# and tap it. Short enough that a call nobody took stops holding the device.
CALL_OUT_TIMEOUT_S = 45.0

# How long a *connected* call may go without the phone asking for messages
# before it is presumed gone.
#
# A connected call deliberately has no deadline — a long conversation is not a
# stuck one — which left exactly one way to be wrong: a phone that disappears
# without hanging up. The app killed, the handset switched off, the battery
# flat. The call then stays `connected` forever, the Brio stays lent to a
# browser, and the assistant has no camera until somebody restarts it. That is
# the requirement's "must not leave the camera locked", reached by the one
# route nothing else covers.
#
# The phone holds a long poll continuously, so its silence is a reliable
# signal. Three missed polls: long enough that a cellular handover or a tunnel
# through a lift does not end a conversation, short enough that a dead phone
# does not cost the afternoon.
PHONE_SILENT_S = POLL_TIMEOUT_S * 3


class CallState(enum.Enum):
    """What the call is doing. Published to the screen, so it is also copy."""

    IDLE = "idle"
    #: A trusted phone has dialled and the Pi has not yet picked up.
    RINGING = "ringing"
    #: The Pi is ringing the phone. Waiting for somebody to pick it up there.
    #:
    #: Deliberately a separate state rather than a flag on RINGING, because the
    #: two have opposite rules: an incoming call is answered automatically
    #: because the caller was authenticated at the door, and an outgoing one
    #: must **never** be — the phone belongs to a person, and a Pi that could
    #: open its microphone unattended is the thing this whole feature is
    #: careful not to build.
    CALLING = "calling"
    #: Picked up. The two peers are exchanging offer/answer/candidates.
    CONNECTING = "connecting"
    #: Media is flowing.
    CONNECTED = "connected"
    #: Media was flowing and stopped. Still recoverable — see the requirement's
    #: connection-recovery section. Distinct from ENDED because the page keeps
    #: the call up and says "Reconnecting…" here, and tears it down there.
    RECONNECTING = "reconnecting"
    ENDED = "ended"


#: The states in which the Brio and the speaker belong to the call rather than
#: to the voice loop. Ringing is deliberately *not* one of them: the
#: requirement is explicit that an unauthorised request must never reach the
#: camera, and the corollary is that even an authorised one does not open it
#: until somebody has picked up.
LIVE = (CallState.CONNECTING, CallState.CONNECTED, CallState.RECONNECTING)

#: States in which a call exists but no capture device has been opened.
#: `CALLING` belongs here for the same reason `RINGING` does: a phone that is
#: being rung and has not answered must not have turned anything on at the Pi.
QUIET = (CallState.RINGING, CallState.CALLING)


class SignalingHub:
    """One call between one phone and this Pi.

    Thread-safe by construction: every public method takes the lock, and the
    only thing that waits does so on a condition variable rather than by
    sleeping and re-checking. Four kinds of thread reach this — the phone's
    HTTPS handler, the Pi page's loopback handler, the voice loop asking
    whether a call is up, and the reaper.
    """

    def __init__(self, *, ring_timeout_s: float = RING_TIMEOUT_S,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 phone_silent_s: float = PHONE_SILENT_S,
                 call_out_timeout_s: float = CALL_OUT_TIMEOUT_S):
        self._lock = threading.Condition()
        self._sequence = itertools.count(1)
        self._mailboxes: dict[str, list[tuple[int, dict]]] = {r: [] for r in ROLES}
        self._state = CallState.IDLE
        self._session = ""
        self._caller = ""
        self._since = 0.0
        self._deadline = 0.0
        #: monotonic time each seat last asked for its messages.
        self._seen: dict[str, float] = {}
        self.ring_timeout_s = ring_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.phone_silent_s = phone_silent_s
        self.call_out_timeout_s = call_out_timeout_s

    # ── what the screen and the voice loop ask ───────────────────────

    @property
    def state(self) -> CallState:
        with self._lock:
            return self._state

    @property
    def live(self) -> bool:
        """True when the call owns the camera, the microphone and the speaker."""
        with self._lock:
            return self._state in LIVE

    def snapshot(self) -> dict:
        """What `/api/state` publishes, so the page can draw the call."""
        with self._lock:
            return {
                "state": self._state.value,
                "session": self._session,
                "caller": self._caller,
                "since": self._since or None,
                "live": self._state in LIVE,
                # Filled in by the assistant, which is what knows whether a
                # phone has registered for push — the hub deliberately knows
                # nothing about notifications.
                "can_ring": False,
            }

    # ── the state machine ────────────────────────────────────────────

    def ring(self, caller: str) -> tuple[bool, str, str]:
        """A trusted phone is dialling. Returns (accepted, session, why not).

        Refused when a call is already up, which is what makes "one phone, one
        Pi" a property of this object. The caller has already been
        authenticated by the time this is reached — see `aipi5/call/tokens.py`
        — so a refusal here is a busy signal and not a rejection.
        """
        with self._lock:
            if self._state is not CallState.IDLE and not self._expired_locked():
                return False, "", "a call is already in progress"

            self._reset_locked()
            self._state = CallState.RINGING
            self._session = uuid.uuid4().hex[:16]
            self._caller = caller
            self._since = time.time()
            self._deadline = time.monotonic() + self.ring_timeout_s
            log.info("call: ringing — %s (session %s)", caller, self._session)
            # The Pi's page learns about this through `/api/state`, not through
            # its mailbox: it may not have joined yet, and a page that has just
            # been navigated to the call view has nothing to collect. State is
            # the thing that survives not being connected.
            self._lock.notify_all()
            return True, self._session, ""

    def call_out(self, device: str) -> tuple[bool, str, str]:
        """The Pi is ringing a phone. Returns (started, session, why not).

        The mirror of `ring`, and almost all of it is the same — a session, a
        deadline, a state the screen can draw. What differs is what happens
        next: nothing, until somebody picks up the phone. There is no
        auto-answer on this side and there must not be.

        Note which way the media still flows: the **phone remains the caller**
        in WebRTC terms, making the offer once it opens. Who rings and who
        offers are separate questions, and keeping the offer where it already
        works means an outgoing call reuses the entire proven media path
        instead of a mirrored copy of it.
        """
        with self._lock:
            if self._state is not CallState.IDLE and not self._expired_locked():
                return False, "", "a call is already in progress"

            self._reset_locked()
            self._state = CallState.CALLING
            self._session = uuid.uuid4().hex[:16]
            self._caller = device
            self._since = time.time()
            # Longer than a ring inwards: somebody has to notice a
            # notification, unlock a phone and tap it, which is not the same as
            # a page that answers in half a second.
            self._deadline = time.monotonic() + self.call_out_timeout_s
            log.info("call: ringing %s (session %s)", device, self._session)
            self._lock.notify_all()
            return True, self._session, ""

    def picked_up(self, session: str) -> bool:
        """Somebody answered on the phone. Only valid while we are calling it."""
        with self._lock:
            if self._state is not CallState.CALLING or session != self._session:
                return False
            self._state = CallState.CONNECTING
            self._deadline = time.monotonic() + self.connect_timeout_s
            log.info("call: the phone picked up (session %s)", self._session)
            # The Pi's page is waiting on this to open the Brio: until now it
            # has shown "calling" without touching a capture device, for the
            # same reason `RINGING` does not own the camera.
            self._post_locked(PI, {"type": "answered", "session": self._session})
            return True

    def answer(self, session: str) -> bool:
        """The Pi picked up. Only valid while that exact session is ringing."""
        with self._lock:
            if self._state is not CallState.RINGING or session != self._session:
                return False
            self._state = CallState.CONNECTING
            self._deadline = time.monotonic() + self.connect_timeout_s
            log.info("call: answered (session %s)", self._session)
            self._post_locked(PHONE, {"type": "answered", "session": self._session})
            return True

    def connected(self, session: str) -> bool:
        """Media is flowing. Reported by whichever peer notices first."""
        with self._lock:
            if session != self._session:
                return False
            if self._state not in (CallState.CONNECTING, CallState.RECONNECTING,
                                   CallState.CONNECTED):
                return False
            if self._state is not CallState.CONNECTED:
                log.info("call: connected (session %s)", self._session)
            self._state = CallState.CONNECTED
            self._deadline = 0.0        # a call that is up has no deadline
            return True

    def reconnecting(self, session: str) -> bool:
        """The transport dropped. The page stays up and says so."""
        with self._lock:
            if session != self._session or self._state is not CallState.CONNECTED:
                return False
            self._state = CallState.RECONNECTING
            self._deadline = time.monotonic() + self.connect_timeout_s
            log.warning("call: reconnecting (session %s)", self._session)
            return True

    def hang_up(self, session: str = "", why: str = "") -> bool:
        """Either side ended it, or something gave up on its behalf.

        Idempotent, and deliberately tolerant of a stale session: both peers
        send this, a page being torn down sends it from `pagehide`, and the
        reaper sends it. Every one of those can arrive after the call is
        already down, and none of them is a fault.
        """
        with self._lock:
            # ENDED counts as already down, not as a call to end. Without it
            # the second `bye` — and there is always a second, because both
            # peers send one — posts another pair of messages into mailboxes
            # the peers are still draining, and each of those makes the other
            # side hang up a call that is already over.
            if self._state in (CallState.IDLE, CallState.ENDED):
                return False
            if session and session != self._session:
                return False
            log.info("call: ended%s (session %s)", f" — {why}" if why else "",
                     self._session)
            # Told to both seats before the reset, so a peer still polling
            # learns why rather than watching its mailbox go quiet.
            self._post_locked(PI, {"type": "bye", "reason": why})
            self._post_locked(PHONE, {"type": "bye", "reason": why})
            self._state = CallState.ENDED
            self._deadline = time.monotonic() + 5.0
            self._lock.notify_all()
            return True

    # ── the mailboxes ────────────────────────────────────────────────

    def post(self, to: str, message: dict, session: str = "") -> bool:
        """Put one signalling message in a seat's mailbox."""
        if to not in ROLES:
            return False
        with self._lock:
            if session and session != self._session:
                log.debug("dropping a %s for a stale session",
                          message.get("type"))
                return False
            self._post_locked(to, message)
            return True

    def _post_locked(self, to: str, message: dict) -> None:
        box = self._mailboxes[to]
        box.append((next(self._sequence), message))
        if len(box) > MAX_MAILBOX:
            # The oldest go first. A peer this far behind has lost the
            # handshake anyway, and the alternative is unbounded memory held
            # for a phone that is not coming back.
            del box[:-MAX_MAILBOX]
            log.warning("the %s mailbox overflowed; dropping the oldest", to)
        self._lock.notify_all()

    def collect(self, role: str, since: int,
                timeout: float = POLL_TIMEOUT_S) -> tuple[list[dict], int]:
        """Everything queued for `role` after `since`, waiting if there is none.

        Returns (messages, cursor). The cursor is the sequence to ask for next
        and moves even when the batch is empty, so a caller cannot get stuck
        re-reading the same tail.
        """
        if role not in ROLES:
            return [], since
        deadline = time.monotonic() + timeout
        with self._lock:
            # Asking for messages is what proves a peer is still there. Marked
            # on entry rather than on return, so a poll that waits out its
            # whole timeout still counts as the phone being present.
            self._seen[role] = time.monotonic()
            while True:
                pending = [(seq, m) for seq, m in self._mailboxes[role]
                           if seq > since]
                if pending:
                    return [m for _, m in pending], pending[-1][0]
                left = deadline - time.monotonic()
                if left <= 0:
                    # Nothing arrived. The cursor is unchanged, which is
                    # correct — there is nothing new to have consumed.
                    return [], since
                self._lock.wait(min(left, 1.0))

    # ── housekeeping ─────────────────────────────────────────────────

    def _reset_locked(self) -> None:
        for role in ROLES:
            self._mailboxes[role] = []
        self._state = CallState.IDLE
        self._session = ""
        self._caller = ""
        self._since = 0.0
        self._deadline = 0.0
        self._seen = {}

    def _phone_gone_locked(self) -> bool:
        """True when a connected call's phone has stopped asking for messages.

        Only meaningful once media is up: before that the deadlines above
        already cover it, and during `ringing` the phone has not started
        polling at all.
        """
        if self._state not in (CallState.CONNECTED, CallState.RECONNECTING):
            return False
        last = self._seen.get(PHONE)
        if last is None:
            return False
        return (time.monotonic() - last) > self.phone_silent_s

    def _expired_locked(self) -> bool:
        """True when the current state has outlived its deadline, and clears it.

        This is what stops a phone that vanished between dialling and offering
        from holding the device busy forever. Checked lazily, on the paths that
        care, rather than from a timer thread — there is no work to do while
        nothing is asking.
        """
        if not self._deadline or time.monotonic() < self._deadline:
            return False
        stale = self._state
        log.info("call: %s timed out; releasing", stale.value)
        self._reset_locked()
        return True

    def sweep(self) -> bool:
        """Expire a stuck call. Called from the voice loop, on every frame."""
        with self._lock:
            if self._state is CallState.IDLE:
                return False
            if self._phone_gone_locked():
                log.warning("the phone has not polled for %.0fs; ending the call",
                            self.phone_silent_s)
                # Through `hang_up`'s body rather than a bare reset, so the Pi's
                # page is told and tears its own side down instead of holding
                # a peer connection to nobody.
                self._post_locked(PI, {"type": "bye", "reason": "the phone went away"})
                self._state = CallState.ENDED
                self._deadline = time.monotonic() + 5.0
                self._lock.notify_all()
                return True
            if not self._expired_locked():
                return False
            self._lock.notify_all()
            return True
