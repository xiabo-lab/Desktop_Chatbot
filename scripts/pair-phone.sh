#!/usr/bin/env bash
#
# Authorise a phone to call this AIPI5, or revoke one.
#
#   ./scripts/pair-phone.sh "Fuwen's iPhone"    # trust a phone, print its link
#   ./scripts/pair-phone.sh --list              # who is trusted
#   ./scripts/pair-phone.sh --revoke "…"        # untrust
#
# Run this ON THE PI. The token it prints exists exactly once — only its
# SHA-256 is stored — so if the link is lost the phone is re-paired rather than
# recovered. That is the point: a store somebody can read is not a store that
# hands out working credentials.
#
# The link goes to the phone by whatever means you trust. It is a bearer
# credential for a camera and a microphone in your home, so: not a group chat,
# not a public paste, and not a photograph of this terminal posted anywhere.

set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

exec "$PY" - "$@" <<'PYTHON'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from aipi5.core import config as config_mod
from aipi5.call import tailscale, tls
from aipi5.call.tokens import TrustedDevices

settings = config_mod.load()
devices = TrustedDevices(settings.call.devices)
args = sys.argv[1:]

if not args or args[0] in ("-h", "--help"):
    print(__doc__ or "")
    print("usage: pair-phone.sh NAME | --list | --revoke NAME")
    raise SystemExit(0)

if args[0] == "--list":
    paired = devices.devices()
    if not paired:
        print("No phone is paired. Nothing can call this device.")
    for device in paired:
        seen = device["last_seen"]
        print(f"  {device['name']}"
              + (f"  (last called {seen:.0f})" if seen else "  (never called)"))
    print(f"\nstore: {devices.store}")
    raise SystemExit(0)

if args[0] == "--revoke":
    if len(args) < 2:
        raise SystemExit("which phone? --revoke NAME")
    gone = devices.revoke(args[1])
    print(f"revoked {gone} device(s) named {args[1]!r}"
          if gone else f"nothing is paired under {args[1]!r}")
    raise SystemExit(0 if gone else 1)

name = " ".join(args)
token = devices.pair(name)
port = settings.call.port

print(f"\nPaired: {name}\n")

tailnet = tailscale.dns_name()
if tailnet:
    # The address that works from anywhere, and the one with a real
    # certificate. Printed first and alone, because offering a LAN address
    # beside it invites somebody to use the one that only works at home and
    # then wonder why the call fails on the train.
    where = f"https://{tailnet}/" if not settings.call.tls \
            else f"https://{tailnet}:{port}/"
    print("Open this on the phone — it works from anywhere, including 5G:\n")
    print(f"    {where}#t={token}\n")
    if not tailscale.serving(port) and not settings.call.tls:
        print("WARNING: nothing is proxying to the call server yet. Run:\n")
        print(f"    {tailscale.serve_command(port)}\n")
    print("The phone must be signed in to the same tailnet.\n")

    if settings.call.tls:
        # We are still serving our own self-signed certificate, so there *will*
        # be a warning. Saying otherwise would teach somebody that a warning
        # here means something is broken — which is exactly backwards, and the
        # habit it builds is clicking through them.
        tls.ensure(settings.call.certificate, settings.call.private_key)
        print("The phone will warn once that the certificate is not trusted.\n"
              "That is expected: it is self-signed. Check this fingerprint\n"
              "against what the phone shows, then accept it.\n")
        print(f"    {tls.fingerprint(settings.call.certificate) or '(none)'}\n")
        print("To remove the warning for good, enable HTTPS certificates for\n"
              "the tailnet (admin console -> DNS), then run\n"
              "./scripts/setup-tailscale.sh again.\n")
    else:
        print("No certificate warning: Tailscale issues a real one for the\n"
              ".ts.net name, and renews it.\n")
else:
    # No tailnet. Fall back to phase 2's arrangement and say what it costs.
    tls.ensure(settings.call.certificate, settings.call.private_key)
    addresses = tls.local_addresses() or ["aipi5.local"]
    print("Open this on the phone, over the SAME Wi-Fi as the Pi:\n")
    for address in addresses:
        print(f"    https://{address}:{port}/#t={token}")
    print(f"    https://aipi5.local:{port}/#t={token}")
    print("""
This address only works at home. `./scripts/setup-tailscale.sh` is what makes
calls work from a cellular network.

The phone will warn that the certificate is not trusted. That is expected —
it is self-signed, because no certificate authority issues for a name on your
own network. Accept it once; the token is then stored in the browser and the
address bar is cleared, so the link is not worth screenshotting afterwards.

Check the fingerprint against what the phone shows before accepting:""")
    print(f"\n    {tls.fingerprint(settings.call.certificate) or '(no certificate)'}\n")

print("Then: Share → Add to Home Screen, and it behaves like an app.\n")
if not settings.call.enabled:
    print("NOTE: call.enabled is false in config/aipi5.yaml — the call server\n"
          "      will not listen until you set it true and restart aipi5.\n")
PYTHON
