#!/usr/bin/env bash
#
# Let this device send email. Run once, on the Pi, by hand.
#
#     ssh -N -L 8095:127.0.0.1:8095 aipi5     # in one terminal, leave it up
#     ssh aipi5 './AIPI5/scripts/link-gmail.sh'
#
# The tunnel is there for the reason the Google Photos one needs it: Google's
# redirect goes to `localhost`, and the only browser on this device is a kiosk
# with no address bar. So the redirect has to land on a port your own machine
# can reach.
#
# What it grants is `gmail.send` and nothing else. That scope is write-only --
# there is no read call it authorises -- so this cannot list, search or open
# the mailbox it sends from. See docs/gmail-setup.md for the console steps that
# have to be done first, and for what each failure actually means.
#
# The token is written to ~/.config/aipi5/gmail-token.json, mode 0600, and
# **it belongs to fuwenxu, not to the agent.** The agent user cannot traverse
# this home at all: it decides that mail should go out and what it should say,
# and the assistant is what holds the credential and sends it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8095
REDIRECT="http://127.0.0.1:${PORT}/"
CONFIG_DIR="$HOME/.config/aipi5"
CLIENT="$CONFIG_DIR/google-photos-client.json"
TOKEN="$CONFIG_DIR/gmail-token.json"

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  --forget)
    if [[ -f "$TOKEN" ]]; then
      rm -f "$TOKEN"
      log "Forgotten. This device can no longer send email."
      echo "Revoke it at Google's end too, if you mean to:"
      echo "  https://myaccount.google.com/permissions"
    else
      echo "Nothing to forget."
    fi
    exit 0 ;;
  --status)
    "$ROOT/.venv/bin/python" - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.home() / "AIPI5"))
from aipi5.agent.mail import Mailer
print(Mailer().describe())
PY
    exit 0 ;;
  "") ;;
  *) die "unknown option $1" ;;
esac

[[ -f "$CLIENT" ]] || die "no OAuth client at $CLIENT.
      This reuses the one Google Photos already uses. If Photos has never been
      linked, do that first, or see docs/gmail-setup.md."

if [[ -f "$TOKEN" ]]; then
  warn "this device is already linked. Carrying on will replace the token."
fi

log "Linking Gmail"
"$ROOT/.venv/bin/python" - "$CLIENT" "$TOKEN" "$PORT" "$REDIRECT" <<'PY'
"""The OAuth dance, reusing the Photos client and its PKCE helpers.

Written against `aipi5/photos/auth.py` rather than beside it: the flow is
identical and the pieces that are fiddly -- the verifier, `access_type=offline`
with `prompt=consent`, the atomic 0600 save -- are already correct there and
have a comment each explaining why.
"""

import json
import os
import pathlib
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path.home() / "AIPI5"))
import requests                                          # noqa: E402
from aipi5.photos.auth import Client, GoogleAuth, new_state   # noqa: E402

client_file, token_file, port, redirect = sys.argv[1:5]
port = int(port)
SCOPE = "https://www.googleapis.com/auth/gmail.send"

client = Client.read(pathlib.Path(client_file))
verifier, challenge = GoogleAuth.challenge()
state = new_state()

# `access_type=offline` **with** `prompt=consent`. Without the second, an
# account that has authorised this client before completes the flow perfectly
# and hands back no refresh token -- leaving the device with one hour of
# access and no way to renew, which looks like it worked until it stops.
url = ("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": client.client_id,
    "redirect_uri": redirect,
    "response_type": "code",
    "scope": SCOPE,
    "access_type": "offline",
    "prompt": "consent",
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
}))

caught = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        caught.update({k: v[0] for k, v in query.items()})
        body = ("Gmail was linked. You can close this tab."
                if caught.get("code") else
                "That did not work: " + caught.get("error", "no code came back"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<h2>{body}</h2>".encode())


server = HTTPServer(("127.0.0.1", port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

print("\n  Open this on a machine signed in as the account you want the Pi")
print("  to send from, with the tunnel up:\n")
print(f"    ssh -N -L {port}:127.0.0.1:{port} aipi5\n")
print(url + "\n")
print("  Waiting for the redirect (five minutes)…")

deadline = time.time() + 300
while time.time() < deadline and "code" not in caught and "error" not in caught:
    time.sleep(0.5)
server.shutdown()

if "code" not in caught:
    raise SystemExit("  Nothing came back: " +
                     (caught.get("error") or "the tunnel may not be up"))
if caught.get("state") != state:
    raise SystemExit("  The redirect carried the wrong state; refusing it.")

answer = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
    "client_id": client.client_id,
    "client_secret": client.client_secret,
    "code": caught["code"],
    "code_verifier": verifier,
    "grant_type": "authorization_code",
    "redirect_uri": redirect,
})
payload = answer.json() if answer.content else {}
if answer.status_code != 200 or "refresh_token" not in payload:
    raise SystemExit(f"  Google refused the exchange: "
                     f"{payload.get('error_description') or payload.get('error') or answer.status_code}")

address = ""
try:
    who = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                       timeout=20,
                       headers={"Authorization": "Bearer " + payload["access_token"]})
    if who.status_code == 200:
        address = (who.json() or {}).get("emailAddress", "")
except requests.RequestException:
    pass

# Mode in the open() call, not a chmod afterwards: the window between the two
# is a window in which a refresh token is world-readable.
path = pathlib.Path(token_file)
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
temporary = path.with_suffix(".json.tmp")
handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(handle, "w", encoding="utf-8") as out:
    json.dump({"refresh_token": payload["refresh_token"],
               "account": address, "scope": SCOPE,
               "obtained": time.time()}, out)
os.replace(temporary, path)

print(f"\n  Linked. This device will send as: {address or '(unknown)'}")
print(f"  Token: {path}")
PY

log "Restarting the assistant so it picks the token up"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user restart aipi5 || warn "restart it yourself: systemctl --user restart aipi5"

echo
echo "Now ask the agent from your phone:  \"email me in two minutes to test this\""
