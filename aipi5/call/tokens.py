"""Which phones may ring this device, and how a request proves it is one.

The security boundary for the whole feature. Automatic answering turns on a
camera and a microphone in somebody's home without anybody present agreeing to
it, so the question this file answers — "is this caller one of ours?" — is the
only thing between a stranger on the network and a live view of the room.

Four decisions, and the reasoning for each:

**Tokens live outside the repository and are generated, never chosen.** A file
under `~/.config/aipi5/`, mode 0600, holding 32 bytes of `secrets.token_*`
per device. The requirement forbids credentials in the repository and a test
in `tests/test_call.py` asserts that the default path is outside the tree; the
generation matters just as much, because a pairing secret somebody types is a
pairing secret somebody can guess.

**Only a hash is stored.** The file holds `sha256(token)`, so a backup of the
Pi's home directory — or this file read over the shoulder of a support session
— does not hand anybody a working phone. The token itself exists exactly twice:
once in the pairing URL, and once in the phone's local storage.

**Comparison is constant-time.** `secrets.compare_digest`, over the digest
rather than the token, because a byte-by-byte `==` against a secret is a timing
oracle and this endpoint is reachable from the network by design.

**Revocation is a supported operation, not a file edit.** The requirement asks
for it in as many words. `revoke()` removes the device and the next request
from that phone fails closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the trusted-device file lives by default. Outside the repository,
#: which is asserted by a test rather than left to reviewers to notice.
DEFAULT_STORE = Path.home() / ".config" / "aipi5" / "call-devices.json"

#: Bytes of entropy in a pairing token. 32 bytes is 256 bits; the URL-safe
#: encoding of it is 43 characters, which is short enough to survive being put
#: in a QR code at a size a phone camera reads across a kitchen.
TOKEN_BYTES = 32

# Failed authentications tolerated from one address before it is refused
# outright, and for how long. The requirement asks for rate limiting on
# incoming connection attempts; this is the cheap half of it, in front of
# everything else, so a brute force costs the attacker a lockout rather than
# costing the Pi a hash per guess.
MAX_FAILURES = 5
LOCKOUT_S = 300.0


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A fresh pairing secret. URL-safe, so it can go in a link or a QR code."""
    return secrets.token_urlsafe(TOKEN_BYTES)


class TrustedDevices:
    """The phones allowed to ring this Pi, and the gate in front of them."""

    def __init__(self, store: Path | None = None):
        self.store = Path(store) if store else DEFAULT_STORE
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}      # digest -> record
        self._failures: dict[str, list] = {}     # address -> [count, until]
        #: (mtime_ns, size) of the file as we last read it. See `_reload_if_changed`.
        self._seen: tuple = (0, 0)
        self.load()

    # ── the file ─────────────────────────────────────────────────────

    def _stamp(self) -> tuple:
        try:
            info = self.store.stat()
            return (info.st_mtime_ns, info.st_size)
        except OSError:
            return (0, 0)

    def _reload_if_changed(self) -> bool:
        """Re-read the store when something else has written it.

        `scripts/pair-phone.sh` runs as its own process against the same file,
        so without this the *running* assistant never sees a phone that was
        just paired — and, far worse, never sees one that was just revoked.
        Revocation that silently needs a restart is not revocation; it is a
        promise the requirement makes and this class was quietly breaking.

        Found by rotating a token and watching the old one keep working while
        the new one was refused: the file on disk and the dictionary in memory
        had become two different answers.

        A stat per authentication, which is nothing next to the SHA-256 it is
        in front of, and it compares size as well as mtime because a pair and a
        revoke within the same filesystem timestamp tick would otherwise look
        like no change at all.
        """
        if self._stamp() == self._seen:
            return False
        log.info("the trusted-device file changed; re-reading it")
        self.load()
        return True

    def load(self) -> None:
        """Read the store. A missing file is no devices, not an error.

        A device file that has never been written is the normal state of a
        fresh install, and it must not stop the assistant starting — the voice
        loop, the music and the weather have nothing to do with calling.
        """
        # Read before the file, so a write that lands between the two is seen
        # as a change next time rather than being missed. The cost of being
        # wrong in this direction is one redundant re-read.
        stamp = self._stamp()
        try:
            raw = json.loads(self.store.read_text("utf-8"))
        except FileNotFoundError:
            with self._lock:
                self._devices = {}
                self._seen = stamp
            return
        except (OSError, ValueError) as exc:
            # Deliberately fails *closed*: a store that cannot be parsed leaves
            # zero trusted devices rather than being ignored. The alternative
            # is a corrupt file quietly becoming "trust nobody" in the log and
            # "trust anybody" in somebody's assumption.
            log.error("cannot read the trusted-device file %s: %s — no phone "
                      "will be able to call until this is fixed", self.store, exc)
            with self._lock:
                self._devices = {}
                self._seen = stamp
            return

        devices = raw.get("devices") if isinstance(raw, dict) else None
        with self._lock:
            self._seen = stamp
            self._devices = {
                str(d["digest"]): {"name": str(d.get("name", "a phone")),
                                   "added": d.get("added", 0),
                                   "last_seen": d.get("last_seen", 0)}
                for d in (devices or [])
                if isinstance(d, dict) and d.get("digest")
            }
        log.info("%d trusted phone(s) may call this device", len(self._devices))

    def _save_locked(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": [dict(digest=k, **v)
                               for k, v in self._devices.items()]}
        # Written to a temporary file beside the real one and renamed, so a
        # crash or a full disk cannot leave a half-written store — which, given
        # `load` fails closed, would mean no phone can call until somebody
        # notices.
        temporary = self.store.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), "utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.store)
        # Our own write is not a change to react to. Without this, recording
        # `last_seen` on a successful call would make the very next request
        # re-read the file we just wrote.
        self._seen = self._stamp()

    # ── managing devices ─────────────────────────────────────────────

    def pair(self, name: str) -> str:
        """Trust a new phone. Returns the token, which is not stored.

        This is the only moment the token exists in this process. It goes into
        the pairing URL the person opens on the phone and is then the phone's
        to keep; nothing here can print it again, which is the point.
        """
        token = new_token()
        with self._lock:
            self._devices[_digest(token)] = {
                "name": name, "added": time.time(), "last_seen": 0,
            }
            self._save_locked()
        log.info("paired a new phone: %s", name)
        return token

    def revoke(self, name: str) -> int:
        """Untrust every device with this name. Returns how many went."""
        with self._lock:
            gone = [k for k, v in self._devices.items() if v["name"] == name]
            for key in gone:
                del self._devices[key]
            if gone:
                self._save_locked()
        if gone:
            log.warning("revoked %d device(s) named %s", len(gone), name)
        return len(gone)

    def devices(self) -> list[dict]:
        """The paired phones, for the settings page. No secrets in the result."""
        self._reload_if_changed()
        with self._lock:
            return [{"name": v["name"], "added": v["added"],
                     "last_seen": v["last_seen"]}
                    for v in self._devices.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._devices)

    # ── the gate ─────────────────────────────────────────────────────

    def blocked(self, address: str) -> float:
        """Seconds this address is locked out for. 0 when it may try."""
        with self._lock:
            record = self._failures.get(address)
            if not record:
                return 0.0
            count, until = record
            if count < MAX_FAILURES:
                return 0.0
            left = until - time.monotonic()
            if left <= 0:
                del self._failures[address]
                return 0.0
            return left

    def authenticate(self, token: str, address: str = "") -> dict | None:
        """The device this token belongs to, or None.

        Constant-time against every stored digest — including when there are no
        devices at all, where the loop simply does not run and the answer is a
        uniform failure. The iteration does not stop at the first match, so the
        time taken says nothing about *which* device answered either.
        """
        if not token:
            self._failed(address)
            return None

        # Before anything is compared, so a phone paired or revoked a second
        # ago is already right. See `_reload_if_changed`.
        self._reload_if_changed()

        offered = _digest(token)
        found: dict | None = None
        with self._lock:
            for digest, record in self._devices.items():
                if secrets.compare_digest(digest, offered):
                    found = record
            if found is not None:
                found["last_seen"] = time.time()
                self._failures.pop(address, None)
                try:
                    self._save_locked()
                except OSError as exc:
                    # Not worth failing a call over. The device is trusted; all
                    # that is lost is the timestamp on the settings page.
                    log.debug("could not record last_seen: %s", exc)
                return dict(found)

        self._failed(address)
        return None

    def _failed(self, address: str) -> None:
        if not address:
            return
        with self._lock:
            count, _ = self._failures.get(address, (0, 0.0))
            count += 1
            self._failures[address] = [count, time.monotonic() + LOCKOUT_S]
        if count >= MAX_FAILURES:
            log.warning("locking out %s after %d failed call attempts for %.0f s",
                        address, count, LOCKOUT_S)
        else:
            log.warning("refused a call from %s (attempt %d)", address, count)
