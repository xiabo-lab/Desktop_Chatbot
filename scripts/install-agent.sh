#!/usr/bin/env bash
#
# Install the agent's privileged helper. Run once, as root, on the Pi, by hand.
#
#     sudo ./scripts/install-agent.sh            # install or update
#     sudo ./scripts/install-agent.sh --status   # what is installed now
#     sudo ./scripts/install-agent.sh --uninstall
#
# **`scripts/deploy.sh` does not run this, and must not.** The whole reason the
# helper's code lives in /usr/local/lib rather than in the repository is that a
# routine deploy should not be able to change what root executes. Re-running
# this is a deliberate act, and it is required whenever anything under
# aipi5/agent/helper/ changes.
#
# What it creates:
#
#   * a system user `aipi5-agent`, no login shell, no sudoers entry, not in any
#     of fuwenxu's groups. It cannot read /home/fuwenxu -- that directory is
#     mode 700 -- which is the point: every file the agent touches goes through
#     the helper, so there is one place where path policy lives.
#   * /usr/local/lib/aipi5-agent/, root-owned, holding the helper's code.
#   * the socket and service units, and the socket-activated helper itself.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_USER="aipi5-agent"
OWNER="fuwenxu"
LIB_DIR="/usr/local/lib/aipi5-agent"
UNIT_DIR="/etc/systemd/system"
UNITS=(aipi5-agent-helper.socket aipi5-agent-helper.service
       aipi5-agent.socket aipi5-agent.service)
KEY_DIR="/etc/aipi5-agent"
HELPER_SRC="$ROOT/aipi5/agent/helper"

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

(( EUID == 0 )) || die "run this with sudo -- it creates a user and a root service"

case "${1:-}" in
  --status)
    id "$AGENT_USER" 2>/dev/null || echo "no $AGENT_USER user"
    ls -l "$LIB_DIR" 2>/dev/null || echo "no $LIB_DIR"
    ls -l /run/aipi5-agent-helper.sock /run/aipi5-agent/api.sock 2>/dev/null \n      || echo "sockets not present"
    systemctl status aipi5-agent-helper.socket --no-pager -n 0 2>/dev/null || true
    systemctl status aipi5-agent-helper.service --no-pager -n 5 2>/dev/null || true
    exit 0 ;;
  --uninstall)
    log "Removing the helper"
    systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
    rm -f "${UNITS[@]/#/$UNIT_DIR/}"
    rm -rf "$LIB_DIR"
    systemctl daemon-reload
    warn "the $AGENT_USER user and /var/lib/aipi5-agent were left alone."
    warn "remove them by hand if you mean to: userdel $AGENT_USER"
    exit 0 ;;
  "") ;;
  *) die "unknown option $1" ;;
esac

[[ -d "$HELPER_SRC" ]] || die "no helper source at $HELPER_SRC"

# ── the user ────────────────────────────────────────────────────────
#
# A system account: no password, no login shell, no home to speak of. It gets
# nothing by being on this machine -- every capability it has, it has because
# policy.py says so.
if id "$AGENT_USER" &>/dev/null; then
  log "User $AGENT_USER already exists"
else
  log "Creating the $AGENT_USER user"
  useradd --system --no-create-home --home-dir /var/lib/aipi5-agent \
          --shell /usr/sbin/nologin "$AGENT_USER"
fi

# Belt and braces against a future mistake: this account must never gain sudo.
if [[ -e /etc/sudoers.d/$AGENT_USER ]]; then
  die "/etc/sudoers.d/$AGENT_USER exists. The agent must not have sudo -- the
      helper is the only way it reaches root, and a sudoers entry makes the
      helper decorative. Remove that file and run this again."
fi
if id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx sudo; then
  die "$AGENT_USER is in the sudo group. Remove it: gpasswd -d $AGENT_USER sudo"
fi

# ── the code ────────────────────────────────────────────────────────
log "Installing the helper to $LIB_DIR"
install -d -o root -g root -m 0755 "$LIB_DIR"
# Every .py in the directory, rather than a list. A list is a thing to forget:
# adding changes.py without adding it here installed a helper that imported a
# module which was not on the machine, left it crash-looping, and had the
# installer report success.
shopt -s nullglob
helper_files=("$HELPER_SRC"/*.py)
shopt -u nullglob
(( ${#helper_files[@]} )) || die "no .py files in $HELPER_SRC"

for source_file in "${helper_files[@]}"; do
  file="$(basename "$source_file")"
  # CRLF is what a Windows checkout produces and scp copies it raw. Python
  # tolerates it, but strip it anyway so the file reads correctly to anybody
  # inspecting what root is about to run.
  sed 's/\r$//' "$source_file" > "$LIB_DIR/$file"
  chown root:root "$LIB_DIR/$file"
  chmod 0644 "$LIB_DIR/$file"
done

# The written procedures, beside the code and owned the same way. **Read-only
# to the agent by construction**: there is no operation that writes one, so a
# web page the agent was asked to read cannot leave an instruction behind for
# next time.
install -d -o root -g root -m 0755 "$LIB_DIR/skills"
shopt -s nullglob
skill_files=("$HELPER_SRC/../skills"/*.md)
shopt -u nullglob
for source_file in "${skill_files[@]}"; do
  file="$(basename "$source_file")"
  sed 's/$//' "$source_file" > "$LIB_DIR/skills/$file"
  chown root:root "$LIB_DIR/skills/$file"
  chmod 0644 "$LIB_DIR/skills/$file"
done
for installed in "$LIB_DIR/skills"/*.md; do
  [[ -f "$HELPER_SRC/../skills/$(basename "$installed")" ]] || rm -f "$installed"
done

# And drop anything left from an earlier install that is no longer in the
# source, so a module somebody deleted cannot keep running as root.
for installed in "$LIB_DIR"/*.py; do
  [[ -f "$HELPER_SRC/$(basename "$installed")" ]] || {
    warn "removing $(basename "$installed"), no longer in the source"
    rm -f "$installed"
  }
done

log "Checking it parses under the system interpreter"
/usr/bin/python3 -m py_compile "$LIB_DIR"/*.py \
  || die "the helper does not compile; nothing has been enabled"
rm -rf "$LIB_DIR/__pycache__"

# ── state ───────────────────────────────────────────────────────────
# Two directories, deliberately not one:
#
#   /var/lib/aipi5-agent          the runtime's, owned by the agent
#   /var/lib/aipi5-agent-helper   root's, holding the rollback backups
#
# They were the same directory once. systemd gives a StateDirectory to
# the user its unit runs as, so the runtime took ownership and the agent
# could edit the backup it would be restored from.
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0750 /var/lib/aipi5-agent
install -d -o root -g "$AGENT_USER" -m 0750 /var/lib/aipi5-agent-helper
install -d -o root -g "$AGENT_USER" -m 0750 /var/lib/aipi5-agent-helper/changes
install -d -o root -g "$AGENT_USER" -m 0750 /var/log/aipi5-agent

# ── the units ───────────────────────────────────────────────────────
log "Installing the units"
for unit in "${UNITS[@]}"; do
  [[ -f "$ROOT/systemd/$unit" ]] || die "missing systemd/$unit"
  sed 's/\r$//' "$ROOT/systemd/$unit" > "$UNIT_DIR/$unit"
  chmod 0644 "$UNIT_DIR/$unit"
done
systemctl daemon-reload

# ── the key ─────────────────────────────────────────────────────────
#
# Handed to the runtime by systemd's LoadCredential=, not read from the
# repository. The agent user cannot see /home/fuwenxu at all, so the key has to
# live somewhere it can be given rather than somewhere it can be fetched.
install -d -o root -g root -m 0755 "$KEY_DIR"
if [[ -s "$KEY_DIR/openai.key" ]]; then
  log "OpenAI key already present at $KEY_DIR/openai.key"
else
  drop_in="/home/$OWNER/.config/systemd/user/aipi5.service.d/10-openai-key.conf"
  if [[ -r "$drop_in" ]]; then
    log "Copying the OpenAI key from the assistant's drop-in"
    sed -n 's/^Environment=OPENAI_API_KEY=//p' "$drop_in" | head -1       > "$KEY_DIR/openai.key"
  elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
    log "Taking the OpenAI key from the environment"
    printf '%s' "$OPENAI_API_KEY" > "$KEY_DIR/openai.key"
  fi
  if [[ -s "$KEY_DIR/openai.key" ]]; then
    chown root:root "$KEY_DIR/openai.key"
    chmod 0400 "$KEY_DIR/openai.key"
  else
    rm -f "$KEY_DIR/openai.key"
    warn "no OpenAI key found. The agent will start and refuse to think."
    warn "put one at $KEY_DIR/openai.key (mode 0400, root:root) and restart"
    warn "aipi5-agent.service."
  fi
fi

log "Starting the sockets"
systemctl enable --now aipi5-agent-helper.socket
systemctl restart aipi5-agent-helper.service
systemctl enable --now aipi5-agent.socket

# The runtime is only started when the configuration says the agent is on. A
# checkout deployed to a device must not stand up a service nobody asked for.
if grep -qE '^[[:space:]]*enabled:[[:space:]]*true'      <(sed -n '/^agent:/,/^[a-z]/p' "/home/$OWNER/AIPI5/config/aipi5.yaml" 2>/dev/null); then
  log "agent.enabled is true; starting the runtime"
  systemctl enable --now aipi5-agent.service
  systemctl restart aipi5-agent.service
else
  warn "agent.enabled is false in config/aipi5.yaml, so the runtime was not"
  warn "started. Set it true and run: sudo systemctl enable --now aipi5-agent"
fi

sleep 1
if [[ -S /run/aipi5-agent-helper.sock ]]; then
  log "Installed"
  ls -l /run/aipi5-agent-helper.sock
  echo
  echo "Operations the helper will perform:"
  /usr/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/local/lib/aipi5-agent")
import ops
for name in sorted(ops.OPS):
    print(f"  {name}{'  (mutating)' if name in ops.MUTATING else ''}")
PY
  echo
  echo "Audit log: /var/log/aipi5-agent/helper.jsonl"
  echo "Re-run this script after any change under aipi5/agent/helper/."
else
  die "the socket did not appear; see: systemctl status aipi5-agent-helper"
fi
