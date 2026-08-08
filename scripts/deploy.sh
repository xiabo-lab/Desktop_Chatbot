#!/usr/bin/env bash
#
# Push this checkout to the Pi and install it. Run from a development machine.
#
#     ./scripts/deploy.sh                    # copy, install deps, install units
#     ./scripts/deploy.sh --code-only        # copy and restart; no pip, no units
#     ./scripts/deploy.sh --dry-run          # say what would happen
#
# Target: $AIPI5_HOST, default fuwenxu@aipi5.local.
#
# tar over ssh rather than rsync or `git archive`. rsync is not installed on a
# stock Pi OS image and this has to work on a device nobody has prepared;
# `git archive` is what AIA uses and needs this to be a git repository, which
# it is not yet. A tar stream needs nothing on either end that is not already
# there.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${AIPI5_HOST:-fuwenxu@aipi5.local}"
REMOTE="${AIPI5_REMOTE:-AIPI5}"

CODE_ONLY=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --code-only) CODE_ONLY=1 ;;
    --dry-run)   DRY=1 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
run()  { if (( DRY )); then echo "  would run: $*"; else "$@"; fi; }

# What goes. Everything else — the venv, __pycache__, the models, the captured
# audio, the conversation database — either belongs to the device or is
# rebuilt there.
#
# `openai API.txt` is NOT in this list. The key is deployed once, by hand, by
# somebody who has decided to (see below); a deploy script that quietly ships a
# credential every time it runs is how a key ends up on a machine nobody meant
# to put it on.
INCLUDE=(aipi5 config scripts systemd tests requirements.txt README.md REPORT.md .gitignore)

log "Target: $HOST:~/$REMOTE"

# Fail here rather than halfway through a tar stream. A deploy that dies with
# the code copied and the service not restarted leaves the Pi running the old
# code from a directory holding the new — which looks exactly like a change
# that had no effect.
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" 'true' 2>/dev/null; then
  warn "cannot reach $HOST without a password."
  warn "Install a key first:  ssh-copy-id -i ~/.ssh/aipi5_ed25519.pub $HOST"
  exit 1
fi

log "Checking the Pi"
ssh "$HOST" 'bash -s' <<'REMOTE_CHECK'
set -e
printf '  hostname   %s\n' "$(hostname)"
printf '  model      %s\n' "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
printf '  python     %s\n' "$(python3 --version 2>&1)"
if [ -d "$HOME/AI_Assit/aia" ]; then
  printf '  AIA        present at ~/AI_Assit\n'
else
  echo "  AIA        MISSING — AIPI5 imports its wake word, STT, TTS and router" >&2
  exit 1
fi
REMOTE_CHECK

log "Copying"
if (( DRY )); then
  echo "  would copy: ${INCLUDE[*]}"
else
  ssh "$HOST" "mkdir -p ~/$REMOTE"
  # --exclude on the *sending* side, so nothing large crosses the network and
  # nothing stale is written. The Pi's own venv and models must survive a
  # deploy: they are hundreds of megabytes and they are device-specific.
  tar -C "$ROOT" \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
      --exclude='models' --exclude='.bench' --exclude='*.db*' \
      -czf - "${INCLUDE[@]}" \
    | ssh "$HOST" "tar -C ~/$REMOTE -xzf -"
  echo "  copied ${#INCLUDE[@]} paths"
fi

if (( CODE_ONLY )); then
  log "Restarting (code only)"
  run ssh "$HOST" "systemctl --user restart aipi5 2>/dev/null || true"
  log "Done"
  exit 0
fi

log "Virtualenv and dependencies"
run ssh "$HOST" "bash -s" <<REMOTE_DEPS
set -e
cd ~/$REMOTE
[ -d .venv ] || python3 -m venv .venv
# AIA's requirements first: they are the voice path, and AIPI5's are three
# small packages on top. Both into the same venv, because AIPI5 imports AIA.
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r ~/AI_Assit/requirements.txt
.venv/bin/pip install --quiet -r requirements.txt
echo "  installed"
.venv/bin/python -c "import yaml, openai, requests; print('  imports ok')"
REMOTE_DEPS

log "Tests, on the Pi"
# The same 119 that pass on the development machine. Running them here catches
# the thing a laptop cannot: a Python version difference, a package that
# resolved to a different wheel on aarch64.
run ssh "$HOST" "cd ~/$REMOTE && PYTHONIOENCODING=utf-8 .venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -4"

log "Installing the services"
run ssh "$HOST" "cd ~/$REMOTE && chmod +x scripts/*.sh && ./scripts/install-service.sh"

log "Startup checks"
run ssh "$HOST" "journalctl --user -u aipi5 -n 60 --no-pager | sed -n '/startup checks/,/^.*ready/p'"

cat <<NOTE

  Deployed. Watch it work:

      ssh $HOST 'journalctl --user -u aipi5 -f'

  The key is not deployed by this script. Put it on the Pi once:

      scp 'openai API.txt' $HOST:~/$REMOTE/
      ssh $HOST 'chmod 600 ~/$REMOTE/"openai API.txt"'

  or better, keep it out of the project directory entirely:

      ssh $HOST 'mkdir -p ~/.config/aipi5 && systemctl --user edit aipi5'
      # [Service]
      # Environment=OPENAI_API_KEY=sk-...

NOTE
