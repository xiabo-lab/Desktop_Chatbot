#!/usr/bin/env bash
#
# Configure a Coturn relay for AIPI5 calls, and print the YAML to paste.
#
#   ./scripts/setup-turn.sh turn.example.net          # on the RELAY host
#   ./scripts/setup-turn.sh --client turn.example.net # on the PI
#
# ── Where this runs, and why it is not the Pi ────────────────────────────
#
# Run the server half on a host with a **public address and no NAT in front of
# it** — a small VPS is the usual answer. Not on the Pi: a relay behind the
# same router as one of the peers can only be reached by opening a port on that
# router, which is the thing the requirement says calls must work without, and
# it would relay the phone's video into the house and back out again over the
# same upstream link the call is already using.
#
# ── Why a secret rather than a username and password ─────────────────────
#
# Coturn's `use-auth-secret` mode. The Pi holds one shared secret and mints a
# username and password per call that expire within the hour; the phone never
# sees anything reusable. A fixed TURN password has to reach the phone to be
# used, which puts it in local storage on a device somebody can lose, and a
# leaked one is an open relay on somebody else's bill.
#
# The secret is generated here and never committed — `--client` writes it to
# ~/.config/aipi5/turn-secret with mode 0600, which is where aipi5/call/turn.py
# looks by default.

set -euo pipefail

CLIENT=0
if [[ "${1:-}" == "--client" ]]; then CLIENT=1; shift; fi
HOSTNAME_ARG="${1:-}"

if [[ -z "$HOSTNAME_ARG" ]]; then
  echo "usage: setup-turn.sh [--client] <turn-hostname-or-ip>" >&2
  exit 2
fi

SECRET_FILE="$HOME/.config/aipi5/turn-secret"

if [[ "$CLIENT" == "1" ]]; then
  # ── the Pi side ────────────────────────────────────────────────────────
  mkdir -p "$(dirname "$SECRET_FILE")"
  if [[ -s "$SECRET_FILE" ]]; then
    echo "Using the existing secret at $SECRET_FILE"
  else
    # `openssl rand` rather than a passphrase somebody chooses. This is
    # compared by HMAC and never typed, so there is no reason for it to be
    # anything a person could remember.
    openssl rand -hex 32 > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
    echo "Wrote a new secret to $SECRET_FILE (mode 0600)"
  fi
  echo
  echo "Put this in config/aipi5.yaml under call:, then restart aipi5:"
  echo
  cat <<YAML
  stun_servers:
    - stun:${HOSTNAME_ARG}:3478
  turn_servers:
    - urls: turn:${HOSTNAME_ARG}:3478
      secret_file: turn-secret
      ttl: 3600
YAML
  echo
  echo "The relay needs the SAME secret. On ${HOSTNAME_ARG}, run:"
  echo
  echo "    ./scripts/setup-turn.sh ${HOSTNAME_ARG}"
  echo
  echo "and give it this secret when it asks:"
  echo
  echo "    $(cat "$SECRET_FILE")"
  echo
  exit 0
fi

# ── the relay side ───────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
  echo "Run the server half as root (it writes /etc/turnserver.conf)." >&2
  exit 1
fi

read -r -p "Shared secret (from the Pi's setup-turn.sh --client): " SECRET
[[ -n "$SECRET" ]] || { echo "a secret is required" >&2; exit 2; }

command -v turnserver >/dev/null 2>&1 || {
  echo "Installing coturn"; apt-get update -qq && apt-get install -y coturn; }

# The external address as the Internet sees it. Coturn has to advertise this in
# its relay candidates; behind any address translation it would otherwise offer
# an address nothing can reach, and the call fails only for the peers that
# needed the relay — which is the subset hardest to reproduce.
EXTERNAL="$(curl -s --max-time 10 https://api.ipify.org || true)"
[[ -n "$EXTERNAL" ]] || { echo "could not determine the public address" >&2; exit 1; }

cp -n /etc/turnserver.conf /etc/turnserver.conf.orig 2>/dev/null || true
cat > /etc/turnserver.conf <<CONF
# Written by AIPI5 scripts/setup-turn.sh
listening-port=3478
external-ip=${EXTERNAL}
realm=${HOSTNAME_ARG}
server-name=${HOSTNAME_ARG}

# Time-limited credentials. The Pi computes them; see aipi5/call/turn.py.
use-auth-secret
static-auth-secret=${SECRET}

# This relay exists for one household's video calls. Everything below narrows
# it from "a relay" to "that relay", because an open TURN server is bandwidth
# somebody else spends and a way to bounce traffic through this host.
no-cli
no-multicast-peers
no-tcp-relay
# Refuse to relay to anything internal — a TURN server will happily forward to
# a private address on its own network if asked, which turns it into a probe
# for whatever else is on this host's LAN.
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=::1
denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff

# One phone and one Pi. Generous, and still an upper bound.
user-quota=12
total-quota=100
CONF
chmod 640 /etc/turnserver.conf

sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn 2>/dev/null || true
systemctl enable --now coturn
systemctl restart coturn

echo
echo "coturn is running on ${HOSTNAME_ARG} (external ${EXTERNAL})."
echo
echo "Open UDP 3478 to this host, and the relay range:"
echo "    ufw allow 3478/udp && ufw allow 49152:65535/udp"
echo
echo "Check it from anywhere with:"
echo "    https://icetest.info/  — or trickle-ice, entering:"
echo "    turn:${HOSTNAME_ARG}:3478 with a username/password from the Pi"
echo
echo "A 'relay' candidate appearing there is the whole test."
