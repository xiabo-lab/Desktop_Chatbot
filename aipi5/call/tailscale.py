"""Where the phone reaches this device, when a tailnet is carrying the call.

Phase 3's problem is that a phone on a cellular network has no way to address a
Pi behind a home router, and the requirement rules out opening a port. Tailscale
solves it by making both devices members of the same private network: the phone
addresses the Pi at a stable `100.x` address that exists wherever either of them
happens to be.

Two consequences worth stating, because they change the shape of the rest:

**The call server stops listening on the network.** With `tailscale serve`
terminating TLS and proxying to loopback, `aipi5/call/server.py` binds
`127.0.0.1` and nothing else — the only thing accepting connections from
outside this machine is Tailscale, and only from devices in the tailnet. That
is strictly better than the phase 2 arrangement, where the server was on
`0.0.0.0` and every device on the house Wi-Fi could at least reach the door.

**The certificate becomes real.** Tailscale issues a Let's Encrypt certificate
for the `.ts.net` name and renews it, so the self-signed warning goes away
along with the ceremony of checking a fingerprint by hand. That matters beyond
convenience: teaching somebody to click through certificate warnings is a
lasting cost, and this removes the only place the feature asked for it.

**TURN probably becomes unnecessary**, though it stays configured. WireGuard
carries the media, so both peers see each other at tailnet addresses and pair
on host candidates. Whether that actually happens is not a thing to assume —
the route reporting in both pages says `host`, `srflx` or `relay` after every
connect, and that is the check.

Nothing here configures Tailscale. Bringing a machine onto a tailnet means
authenticating it against somebody's account, which is theirs to do.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

#: `tailscale status` talks to the local daemon over a unix socket. It is fast
#: when the daemon is up and hangs when it is wedged, which is the case this
#: bound exists for — this runs on the startup path.
TIMEOUT_S = 5.0


def installed() -> bool:
    return shutil.which("tailscale") is not None


def status() -> dict:
    """`tailscale status --json`, or an empty dict.

    Never raises. Calling is optional, and a deployment that has never heard of
    Tailscale must not have its startup fail because a binary is missing.
    """
    if not installed():
        return {}
    try:
        done = subprocess.run(["tailscale", "status", "--json"],
                              check=True, capture_output=True, timeout=TIMEOUT_S)
        return json.loads(done.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("could not read tailscale status: %s", exc)
        return {}


def _self(state: dict) -> dict:
    node = state.get("Self")
    return node if isinstance(node, dict) else {}


def dns_name(state: dict | None = None) -> str:
    """This machine's MagicDNS name, without the trailing dot.

    The trailing dot is correct in DNS and wrong in a URL — `https://host./`
    is a name a browser will not match against a certificate. Stripped here so
    no caller has to remember.
    """
    node = _self(status() if state is None else state)
    return str(node.get("DNSName", "")).rstrip(".")


def addresses(state: dict | None = None) -> list[str]:
    """This machine's tailnet addresses."""
    node = _self(status() if state is None else state)
    found = node.get("TailscaleIPs") or []
    return [str(a) for a in found if a]


def online(state: dict | None = None) -> bool:
    state = status() if state is None else state
    return str(state.get("BackendState", "")) == "Running"


def describe() -> dict:
    """For the settings page, and for deciding what to print in a pairing link."""
    state = status()
    if not state:
        return {"installed": installed(), "online": False}
    return {
        "installed": True,
        "online": online(state),
        "name": dns_name(state),
        "addresses": addresses(state),
    }


def serve_command(port: int) -> str:
    """The one command a person has to run, printed rather than executed.

    Not run for them: `tailscale serve` publishes a camera and a microphone to
    every device on the tailnet, which is an outward-facing change to somebody
    else's network and theirs to make.
    """
    return f"sudo tailscale serve --bg --https=443 http://127.0.0.1:{port}"


def serving(port: int) -> bool:
    """Whether `tailscale serve` is already pointing at our port.

    Best-effort: the shape of `tailscale serve status --json` has changed
    between releases, so this looks for the port anywhere in the blob rather
    than walking a structure that may not be there. A wrong answer costs a
    misleading line on the settings page and nothing else.
    """
    if not installed():
        return False
    try:
        done = subprocess.run(["tailscale", "serve", "status", "--json"],
                              check=False, capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return f"127.0.0.1:{port}" in done.stdout.decode("utf-8", "replace")
