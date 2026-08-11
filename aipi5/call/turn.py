"""ICE servers for a call, and short-lived credentials for the relay.

Phase 2 needed none of this: two devices on one subnet find each other with
host candidates. Phase 3 is the phone on a cellular network and the Pi behind a
home router, where the two have no address in common and something has to
either discover a route or carry the packets.

**STUN discovers, TURN carries.** A STUN server tells each peer what its own
address looks like from outside, which is enough whenever both NATs will accept
a packet from an address they have just sent one to. When that fails — and on
mobile carriers it often does, because symmetric NAT gives a different external
port per destination — TURN relays the media. TURN is therefore the fallback
that makes a call connect at all, and also the expensive one: every byte of
video goes through it twice.

**Credentials are time-limited and computed here, never stored.** Coturn's
`use-auth-secret` mode (the "REST API" scheme, RFC 7635's ancestor) takes a
username of `<expiry>:<name>` and a password of
`base64(HMAC-SHA1(secret, username))`. The shared secret stays on the Pi; what
reaches the phone is a credential that stops working within the hour.

That matters more here than it looks. The alternative — a fixed TURN username
and password — has to be sent to the phone to be used, so it is in local
storage on a device somebody may lose, and a leaked one is an open relay
somebody else pays for. The scheme costs one HMAC per call.

**The secret is never in the repository.** It comes from an environment
variable or a file, both named in the configuration, and a test asserts the
default file is outside the tree.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: How long a generated TURN credential is good for. An hour is Coturn's usual
#: suggestion: long enough that a call started at the end of one is not cut off
#: — credentials are checked when the allocation is made, not continuously —
#: and short enough that one scraped off a phone is worth little.
DEFAULT_TTL_S = 3600

#: Where a secret file lives if the configuration names a relative path. Same
#: directory as the device store and the TLS key, and outside the checkout.
SECRET_DIR = Path.home() / ".config" / "aipi5"


def credentials(secret: str, name: str = "aipi5",
                ttl: int = DEFAULT_TTL_S, now: float | None = None) -> tuple[str, str]:
    """A Coturn `use-auth-secret` username and password.

    Split out and pure so it can be tested without a relay: the failure this
    guards against is a credential that Coturn rejects, which presents as a
    call that connects on the same network and fails on a different one — the
    single hardest thing here to notice.
    """
    expiry = int((time.time() if now is None else now) + ttl)
    username = f"{expiry}:{name}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"),
                      hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode("ascii")


def _secret(entry: dict) -> str:
    """The shared secret for one relay, from wherever it is kept.

    Order is environment, then file. Neither default is inside the repository,
    and an entry that names neither is treated as having no secret at all —
    which means static credentials, or none.
    """
    variable = entry.get("secret_env")
    if variable:
        value = os.environ.get(str(variable), "")
        if value:
            return value.strip()
        log.warning("turn_servers names secret_env %s, which is not set",
                    variable)

    named = entry.get("secret_file")
    if named:
        path = Path(str(named)).expanduser()
        if not path.is_absolute():
            path = SECRET_DIR / path
        try:
            return path.read_text("utf-8").strip()
        except OSError as exc:
            log.warning("cannot read the TURN secret from %s: %s", path, exc)
    return ""


def ice_servers(cfg, name: str = "aipi5") -> list[dict]:
    """What to hand a peer so it can find the other one.

    Built per call rather than baked into either page, because the credentials
    below expire — a page holding stale ones is a call that fails on the
    cellular path only, which is the hardest kind to reproduce.
    """
    servers: list[dict] = []
    for url in cfg.stun_servers:
        if url:
            servers.append({"urls": str(url)})

    for entry in cfg.turn_servers:
        urls = entry.get("urls")
        if not urls:
            log.warning("a turn_servers entry has no `urls`; skipping it")
            continue

        secret = _secret(entry)
        if secret:
            ttl = int(entry.get("ttl", DEFAULT_TTL_S))
            username, credential = credentials(secret, name, ttl)
        elif entry.get("username"):
            # Static credentials. Supported because a hosted TURN service may
            # only offer them, and refused silence would be worse than a
            # weaker scheme — but it is the second choice, and says so.
            username = str(entry["username"])
            credential = str(entry.get("credential", ""))
            log.debug("using static credentials for %s", urls)
        else:
            log.warning("no credentials for TURN server %s — it will be "
                        "offered without any and almost certainly refused", urls)
            servers.append({"urls": str(urls)})
            continue

        servers.append({"urls": str(urls), "username": username,
                        "credential": credential})

    return servers


def describe(cfg) -> dict:
    """For the settings page. Names the relays; never the secret or a password."""
    return {
        "stun": [str(u) for u in cfg.stun_servers],
        "turn": [{"urls": str(e.get("urls", "")),
                  "auth": ("secret" if _secret(e)
                           else "static" if e.get("username") else "none")}
                 for e in cfg.turn_servers],
    }
