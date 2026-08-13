#!/usr/bin/env bash
#
# Authorise this AIPI5 to read the photos you pick in Google Photos.
#
#   ./scripts/link-google-photos.sh              # authorise
#   ./scripts/link-google-photos.sh --status     # what is stored now
#   ./scripts/link-google-photos.sh --forget     # revoke and delete
#
# Run this ON THE PI, over ssh. It is the one part of the feature that needs a
# real browser and a keyboard, and it is needed exactly once — the refresh
# token it saves survives reboots, so nobody signs in again.
#
# ── before the first run ──────────────────────────────────────────────
#
# You need an OAuth client, which is free and takes about four minutes:
#
#   1. https://console.cloud.google.com/ — create a project (any name).
#   2. APIs & Services → Library → enable "Photos Picker API".
#      NOT "Photos Library API" — that one can no longer read your library.
#   3. APIs & Services → OAuth consent screen → External. Add yourself under
#      "Test users", and add the scope
#        .../auth/photospicker.mediaitems.readonly
#   4. **Then press "Publish app" so the status is "In production".**
#      This step is not optional and it is not about being reviewed. An OAuth
#      consent screen left at "Testing" issues refresh tokens that **expire
#      after seven days**, so the slideshow would work for a week and then
#      stop with `invalid_grant`. Publishing costs nothing and needs no
#      verification for your own use; you will see an "unverified app" warning
#      once, during step 6, and click through it.
#   5. Credentials → Create credentials → OAuth client ID → **Desktop app**.
#      There is no redirect URI to fill in: a Desktop app client may use any
#      http://127.0.0.1:<port> loopback address, which is what this script
#      listens on.
#   6. Download the JSON and put it on the Pi as
#        ~/.config/aipi5/google-photos-client.json
#      (mkdir -p ~/.config/aipi5 && chmod 700 ~/.config/aipi5 first)
#
# That file and the token this script writes both live outside the repository
# and are chmod 0600. Neither is ever printed here or logged by the assistant.
#
# ── the browser ───────────────────────────────────────────────────────
#
# Google redirects to a loopback address on THIS machine, so the browser you
# sign in with has to be able to reach the Pi's 127.0.0.1. Two ways:
#
#   ssh -L 8094:127.0.0.1:8094 aipi5            # then use your own browser
#   ./scripts/link-google-photos.sh             # and open the URL it prints
#
# — which is the clean path and needs nothing else. Failing that, open the URL
# anywhere, let the redirect fail, and paste the whole address bar back in when
# prompted; the code is in it.
#
# ── after this ────────────────────────────────────────────────────────
#
# Choosing which photos is done on the touchscreen: Settings → Screensaver →
# Choose Photos shows a QR code, and your phone does the picking. Google's API
# no longer lets an app browse your library, so a person picks and the Pi
# caches what they picked — see aipi5/photos/__init__.py.

set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

exec "$PY" - "$@" <<'PYTHON'
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path.cwd()))

from aipi5.core import config as config_mod
from aipi5.photos.auth import Client, GoogleAuth, GoogleAuthError, new_state

# Fixed, because Google requires the redirect URI to be one it was told about
# — and for a Desktop app client that means any port on 127.0.0.1, but a fixed
# one is what makes the `ssh -L` line in the header above copy-and-pasteable.
PORT = 8094
REDIRECT = f"http://127.0.0.1:{PORT}/"

DONE = """<!doctype html><meta charset="utf-8">
<title>AIPI5</title>
<body style="font:16px system-ui;padding:3rem;max-width:32rem">
<h1>%s</h1><p>%s</p><p>You can close this tab.</p>"""

settings = config_mod.load()
auth = GoogleAuth(settings.photos)
args = sys.argv[1:]

if args and args[0] in ("-h", "--help"):
    print("see the comments at the top of scripts/link-google-photos.sh")
    raise SystemExit(0)

if args and args[0] == "--status":
    described = auth.describe()
    print(f"client file : {described['client_file']} "
          f"({'present' if described['client_present'] else 'MISSING'})")
    print(f"token file  : {settings.photos.token_file} "
          f"({'present' if settings.photos.token_file.exists() else 'none'})")
    print(f"authorised  : {'yes' if described['authorised'] else 'no'}"
          + (f" ({described['account']})" if described['account'] else ""))
    if described["error"]:
        print(f"error       : {described['error']}")
    print(f"scope       : {described['scope']}")
    print(f"cache       : {settings.photos.cache_dir}")
    raise SystemExit(0)

if args and args[0] == "--forget":
    auth.forget()
    print("disconnected. The photos already cached on this device are still "
          "there; the assistant clears them when you disconnect from the "
          "Settings page, or delete " + str(settings.photos.cache_dir))
    raise SystemExit(0)

try:
    client = Client.read(settings.photos.client_file)
except GoogleAuthError as exc:
    print(f"\n{exc}\n", file=sys.stderr)
    raise SystemExit(1)

verifier, challenge = GoogleAuth.challenge()
state = new_state()
url = GoogleAuth.authorisation_url(client, REDIRECT, challenge, state)

caught: dict = {}
ready = threading.Event()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass          # the script narrates; an access log on top is noise

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        code = (query.get("code") or [""])[0]
        given = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]
        # The state check is the whole reason this listener is not simply
        # "take the first code that arrives": without it, anything on this
        # machine that can reach loopback could hand us a code for an account
        # nobody chose.
        if code and given == state:
            caught["code"] = code
            body = DONE % ("AIPI5 is connected.",
                           "Google Photos authorisation was saved on the Pi.")
        else:
            caught["error"] = error or "the redirect carried no usable code"
            body = DONE % ("That did not work.", caught["error"])
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        ready.set()


try:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
except OSError as exc:
    print(f"cannot listen on {REDIRECT} — {exc}", file=sys.stderr)
    raise SystemExit(1)

threading.Thread(target=server.serve_forever, daemon=True).start()

print("\nCopy the whole line between the rules into a browser on YOUR computer,")
print("signed in to the Google account whose photos you want on the screen.")
print("Expect one 'Google hasn't verified this app' warning: Advanced → Go to.\n")
# Flush left and fenced, because this is copied by hand out of a terminal that
# wraps it. An indent makes a double-click select the wrong thing.
print("-" * 72)
print(url)
print("-" * 72)
print(f"\nWaiting for the redirect to {REDIRECT} … (Ctrl-C to give up)")
print("That address must reach THIS machine, so start the session as:")
print("    ssh -L 8094:127.0.0.1:8094 aipi5")
print("\nIf the browser cannot reach it, let the redirect fail and paste the")
print("whole failed address — the one with ?code= in it — here instead:\n")

# **Nothing is opened for you, deliberately.** This used to call
# `webbrowser.open(url)` as a convenience and it was actively harmful: over an
# interactive ssh session Python's `webbrowser` finds w3m and launches it
# *inside the terminal you are reading these instructions in*, hijacking the
# screen with a text browser that cannot complete a Google sign-in because it
# has no JavaScript. It looked fine when tested non-interactively — with no
# TERM set, no console browser is registered and the call is a silent no-op —
# which is exactly why it survived to be found by a person instead.
#
# There is no case where opening a browser on this Pi is the right answer: the
# only browser here is a full-screen kiosk with no address bar. The URL goes
# to a browser on your own machine, by being copied.
#
# Both ways in remain: the loopback listener catches the redirect through
# `ssh -L`, and the prompt below catches a browser that could not reach it.
# Whichever answers first wins, which is why the input runs on its own thread
# rather than blocking the one that would notice the redirect.


def ask():
    try:
        pasted = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if pasted:
        query = parse_qs(urlparse(pasted).query)
        code = (query.get("code") or [""])[0]
        if code:
            caught["code"] = code
        else:
            caught["error"] = "that address has no ?code= in it"
        ready.set()


threading.Thread(target=ask, daemon=True).start()

try:
    ready.wait()
except KeyboardInterrupt:
    print("\ngiving up")
    raise SystemExit(1)

server.shutdown()

if "code" not in caught:
    print(f"\nauthorisation failed: {caught.get('error', 'no code')}",
          file=sys.stderr)
    raise SystemExit(1)

try:
    auth.exchange(client, caught["code"], verifier, REDIRECT)
except GoogleAuthError as exc:
    print(f"\n{exc}", file=sys.stderr)
    raise SystemExit(1)

print(f"\nSaved to {settings.photos.token_file} (0600).")
print("Restart the assistant so it picks the authorisation up:")
print("  systemctl --user restart aipi5")
print("\nThen choose photos on the touchscreen: Settings → Screensaver →")
print("Choose Photos, and scan the QR code with your phone.")
PYTHON
