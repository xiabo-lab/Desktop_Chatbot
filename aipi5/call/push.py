"""Ringing a phone whose app is closed.

Calls have gone one way until now: the phone rings the Pi, and the Pi answers
because the page on its screen is already running and polling. The reverse does
not work by symmetry, because there is nothing running on the phone to poll —
an installed web app is not a process, it is a page that exists while somebody
is looking at it.

Waking it is what this file is for. The mechanism is Web Push: the phone
registers a subscription with Apple, hands the Pi an endpoint, and the Pi posts
an encrypted payload to that endpoint when it wants to ring. Apple delivers it,
iOS shows a notification, and tapping it opens the app — which then places the
call as it always has.

**What this is not.** It is a notification, not a ringtone. It appears in the
notification list; it does not take over the screen, ring persistently, or
appear on the lock screen as a call. Only CallKit does that, and CallKit needs
a native app. Delivery is best-effort and occasionally slow. This is right for
"come and look at this" and wrong for anything urgent.

Three properties worth stating because they are easy to get wrong:

**The Pi pushes directly to Apple.** No cloud service in the middle, no account
anywhere. The subscription endpoint is an Apple URL and the Pi reaches it
outbound, which is the same shape as everything else here — the device talks
out, nothing talks in.

**The VAPID private key never leaves the Pi**, and is generated on it. It is
what proves to Apple that a push came from this application; a copy of it is a
licence to notify the phone.

**A subscription is a capability, not an identity.** Anyone holding it can send
that phone a notification. It is stored 0600 beside the device tokens, and only
an authenticated device can register one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the keys and subscriptions live. Same directory as the device store
#: and the TLS material, and outside the repository for the same reason.
DEFAULT_DIR = Path.home() / ".config" / "aipi5"

#: How long Apple should hold a ring if the phone is unreachable. A call is
#: worthless later — somebody standing at a Pi waiting is not helped by a
#: notification that arrives in an hour — so this is short on purpose.
TTL_S = 60

#: Apple refuses payloads over 4 KB. Ours is a few hundred bytes; this is the
#: guard against a future field making rings silently fail.
MAX_PAYLOAD = 3500


class PushKeys:
    """The VAPID key pair, generated once and kept.

    Regenerating invalidates every existing subscription — Apple ties the
    subscription to the key that created it — so this is written once and only
    replaced deliberately.
    """

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else DEFAULT_DIR
        self.path = self.dir / "push-vapid.json"
        self._lock = threading.Lock()
        self._keys: dict | None = None

    @property
    def available(self) -> bool:
        try:
            import py_vapid  # noqa: F401
            return True
        except ImportError:
            return False

    def load_or_create(self) -> dict | None:
        """The key pair, making one on first use. None if unusable."""
        with self._lock:
            if self._keys is not None:
                return self._keys
            try:
                data = json.loads(self.path.read_text("utf-8"))
                if data.get("private") and data.get("public"):
                    self._keys = data
                    return self._keys
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.error("cannot read %s: %s — outgoing calls will not ring "
                          "a closed app", self.path, exc)
                return None
            return self._create_locked()

    def _create_locked(self) -> dict | None:
        try:
            from py_vapid import Vapid02
        except ImportError:
            log.warning("pywebpush is not installed; the Pi cannot ring a "
                        "phone whose app is closed")
            return None
        try:
            from cryptography.hazmat.primitives.serialization import (
                Encoding, NoEncryption, PrivateFormat, PublicFormat)

            vapid = Vapid02()
            vapid.generate_keys()
            # Taken from the underlying cryptography objects rather than from
            # py_vapid's own helpers, which move between releases — 1.9.0 has
            # no `public_key_bytes`, and the failure was a silently empty key
            # and a phone that could never subscribe.
            #
            # The browser wants the raw uncompressed point, 65 bytes of
            # `0x04 || X || Y`, base64url with the padding stripped; anything
            # else is rejected by `pushManager.subscribe` with a message that
            # does not say why.
            raw = vapid.public_key.public_bytes(
                Encoding.X962, PublicFormat.UncompressedPoint)
            pem = vapid.private_key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            keys = {
                "private": pem.decode("ascii"),
                "public": _urlsafe(raw),
                "created": time.time(),
            }
        except Exception as exc:
            log.error("could not generate VAPID keys: %s", exc)
            return None

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(keys, indent=2), "utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError as exc:
            log.error("could not save VAPID keys to %s: %s", self.path, exc)
            return None

        self._keys = keys
        log.info("generated a VAPID key pair for outgoing calls")
        return keys

    def public(self) -> str:
        """The key the phone subscribes with. Safe to publish."""
        keys = self.load_or_create()
        return keys["public"] if keys else ""


def _urlsafe(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Subscriptions:
    """Which phones can be rung, and how.

    Keyed by the device name a token was paired under, so revoking a phone and
    re-pairing it replaces its subscription rather than accumulating dead ones
    — a stale endpoint is a push that fails every time the Pi tries to ring.
    """

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else DEFAULT_DIR
        self.path = self.dir / "push-subscriptions.json"
        self._lock = threading.Lock()
        self._subs: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self._subs = {}
            return
        except (OSError, ValueError) as exc:
            log.error("cannot read %s: %s — no phone can be rung", self.path, exc)
            self._subs = {}
            return
        with self._lock:
            self._subs = {str(k): v for k, v in (raw or {}).items()
                          if isinstance(v, dict) and v.get("endpoint")}
        log.info("%d phone(s) can be rung from this device", len(self._subs))

    def _save_locked(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._subs, indent=2), "utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def register(self, device: str, subscription: dict) -> bool:
        """Remember how to ring this phone. Replaces any previous one."""
        endpoint = str(subscription.get("endpoint", ""))
        keys = subscription.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") \
                or not keys.get("auth"):
            log.warning("refusing a malformed push subscription from %r", device)
            return False
        with self._lock:
            self._subs[device] = {
                "endpoint": endpoint,
                "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
                "registered": time.time(),
            }
            try:
                self._save_locked()
            except OSError as exc:
                log.error("could not save the push subscription: %s", exc)
                return False
        log.info("%s can now be rung from this device", device)
        return True

    def forget(self, device: str) -> bool:
        with self._lock:
            if device not in self._subs:
                return False
            del self._subs[device]
            try:
                self._save_locked()
            except OSError as exc:
                log.error("could not save after forgetting %s: %s", device, exc)
        log.info("%s will no longer be rung", device)
        return True

    def get(self, device: str) -> dict | None:
        with self._lock:
            found = self._subs.get(device)
            return dict(found) if found else None

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._subs)

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)


class Pusher:
    """Sends the ring. Never raises; a failed push is a call that did not ring."""

    def __init__(self, keys: PushKeys, subscriptions: Subscriptions,
                 subject: str = "mailto:aipi5@example.com"):
        self.keys = keys
        self.subs = subscriptions
        # VAPID requires a contact for the push service to complain to. Nobody
        # ever reads it and Apple does not check that it receives mail — but it
        # does check that it could be an address, and **refuses
        # `mailto:…@localhost` with `403 BadJwtToken`**: a bare hostname is not
        # a domain. That reads like a signing failure and is not one; the key,
        # the endpoint and the payload were all fine. `example.com` is reserved
        # by RFC 2606, so it is valid forever and reaches nobody. The expiry is
        # left to pywebpush; Apple accepted both 12 and 24 hours when measured.
        self.subject = subject

    def can_ring(self, device: str) -> bool:
        return bool(self.keys.available and self.subs.get(device))

    def ring(self, device: str, payload: dict) -> tuple[bool, str]:
        """Wake `device`. Returns (sent, detail).

        Blocking, and called from a request handler, so it is bounded by the
        HTTP timeout below rather than by Apple's patience.
        """
        subscription = self.subs.get(device)
        if subscription is None:
            return False, "no push subscription for that phone"
        keys = self.keys.load_or_create()
        if keys is None:
            return False, "no VAPID key"

        body = json.dumps(payload)
        if len(body) > MAX_PAYLOAD:
            return False, "payload too large"

        try:
            from pywebpush import WebPushException, webpush
            from py_vapid import Vapid02
        except ImportError:
            return False, "pywebpush is not installed"

        # The key is handed over as an object, not as the PEM text. Given a
        # string, pywebpush passes it to `Vapid.from_string`, which strips the
        # newlines and base64-decodes **the whole thing including the
        # `-----BEGIN PRIVATE KEY-----` line** — so a perfectly good PKCS8 PEM
        # comes back as "Could not deserialize key data", which reads like a
        # corrupt file rather than the wrong entry point. `from_pem` is the one
        # that understands what is actually on disk.
        try:
            signing_key = Vapid02.from_pem(keys["private"].encode("ascii"))
        except Exception as exc:
            log.error("the VAPID private key at %s cannot be loaded: %s",
                      self.keys.path, exc)
            return False, "the VAPID key could not be loaded"

        try:
            webpush(
                subscription_info={"endpoint": subscription["endpoint"],
                                   "keys": subscription["keys"]},
                data=body,
                vapid_private_key=signing_key,
                vapid_claims={"sub": self.subject},
                ttl=TTL_S,
                timeout=10,
            )
            return True, "sent"
        except WebPushException as exc:
            detail = f"{exc}"[:200]
            gone = getattr(exc, "response", None)
            # 404 and 410 mean the subscription is dead — the app was deleted,
            # or iOS expired it. Dropping it here is what stops every future
            # ring paying for a request that cannot succeed.
            if gone is not None and gone.status_code in (404, 410):
                self.subs.forget(device)
                detail = "the phone's subscription has expired; re-register it"
            log.warning("could not ring %s: %s", device, detail)
            return False, detail
        except Exception as exc:
            log.warning("could not ring %s: %s", device, exc)
            return False, f"{exc}"[:200]

    def describe(self) -> dict:
        return {
            "available": bool(self.keys.available),
            "public_key": self.keys.public(),
            "phones": self.subs.names(),
        }
