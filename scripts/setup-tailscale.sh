#!/usr/bin/env bash
#
# Make AIPI5 callable from anywhere, over a Tailscale tailnet.
#
#   ./scripts/setup-tailscale.sh          # check, and print what is left to do
#   ./scripts/setup-tailscale.sh --apply  # do the parts that need no account
#
# ── What this changes, in one paragraph ─────────────────────────────────
#
# Today the call server listens on 0.0.0.0:8443 with a self-signed certificate,
# so every device on the house Wi-Fi can at least reach the door, and the phone
# has to be taught to accept a certificate warning. Afterwards it listens on
# 127.0.0.1 only, `tailscale serve` terminates TLS with a real Let's Encrypt
# certificate that renews itself, and the only things that can reach it are
# devices signed in to your tailnet. That is a smaller attack surface *and* one
# fewer security decision asked of whoever holds the phone.
#
# ── What this script will not do ────────────────────────────────────────
#
# `tailscale up` authenticates this machine against your account, and
# `tailscale serve` publishes a camera and a microphone to your tailnet.
# Both are yours to run: the first needs a login, and the second is a decision
# about who in your household can see into this room. This script installs,
# checks, and prints the two commands.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
PORT="$("$PY" -c 'from aipi5.core import config; print(config.load().call.port)' 2>/dev/null || echo 8443)"

say() { printf '\n%s\n' "$*"; }
step=1
todo() { printf '\n  %d. %s\n' "$step" "$*"; step=$((step+1)); }

# ── 1. installed? ────────────────────────────────────────────────────────
if ! command -v tailscale >/dev/null 2>&1; then
  if [[ "$APPLY" == "1" ]]; then
    say "Installing Tailscale (this needs sudo)"
    curl -fsSL https://tailscale.com/install.sh | sh
  else
    say "Tailscale is not installed."
    echo "  Re-run with --apply, or: curl -fsSL https://tailscale.com/install.sh | sh"
    exit 0
  fi
fi
echo "tailscale: $(tailscale version | head -1)"

# ── 2. on the tailnet? ───────────────────────────────────────────────────
STATE="$(tailscale status --json 2>/dev/null | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("BackendState",""))
except Exception: print("")' || true)"

if [[ "$STATE" != "Running" ]]; then
  say "This machine is not on a tailnet yet (state: ${STATE:-unknown})."
  echo "  That step needs your account, so run it yourself:"
  echo
  echo "      sudo tailscale up"
  echo
  echo "  It prints a URL to open in a browser. Then re-run this script."
  exit 0
fi

NAME="$("$PY" -c 'from aipi5.call import tailscale; print(tailscale.dns_name())')"
echo "tailnet name: ${NAME:-(none — is MagicDNS enabled?)}"

if [[ -z "$NAME" ]]; then
  say "No MagicDNS name. Enable MagicDNS and HTTPS certificates for the tailnet"
  echo "  at https://login.tailscale.com/admin/dns, then re-run this."
  exit 0
fi

# ── 3. what is left ──────────────────────────────────────────────────────
say "Remaining steps:"

if ! tailscale serve status 2>/dev/null | grep -q "127.0.0.1:${PORT}"; then
  todo "Publish the call server to your tailnet:

         sudo tailscale serve --bg --https=443 http://127.0.0.1:${PORT}

     This is what makes it reachable, and it is why the next step is safe."
fi

if grep -qE '^\s*host:\s*0\.0\.0\.0' config/aipi5.yaml 2>/dev/null; then
  todo "Stop the call server listening on the network. In config/aipi5.yaml,
     under call:

         host: 127.0.0.1
         tls: false

     Tailscale is terminating TLS now, so the server does not, and it binds
     loopback only — nothing off this machine can reach it except through the
     tailnet. AIPI5 refuses to run tls: false on any other address."
fi

todo "Restart:  systemctl --user restart aipi5

     (aipi5, not aipi5-ui — the page is cached in the assistant's memory.)"

todo "Install Tailscale on the iPhone and sign in to the same tailnet, then:

         ./scripts/pair-phone.sh \"Fuwen's iPhone\"

     It will print an https://${NAME}/ link. No certificate warning."

say "Then test it properly: turn Wi-Fi OFF on the phone and call over 5G."
echo "  Afterwards, check which path the media took:"
echo
echo "      systemctl --user status aipi5 -n 50 | grep 'call route'"
echo
echo "  'host' means the tailnet carried it directly, which is what you want."
echo "  'relay' means it fell back to TURN — or to Tailscale's own DERP relay,"
echo "  which is slower but still works. Either way the call connects."
echo
