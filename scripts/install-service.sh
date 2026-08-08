#!/usr/bin/env bash
#
# Install AIPI5 as systemd *user* services, so it starts with the desktop
# session — the same way AIA and Kodama-Lite are started.
#
#   ./scripts/install-service.sh              install, enable, start
#   ./scripts/install-service.sh --uninstall
#
# User services rather than system ones, deliberately: the assistant needs the
# session bus (to drive Kodama-Lite over MPRIS and to start it), and the
# Wayland display (for the touchscreen UI). A system service has neither.
#
# This means AIPI5 starts when the user session does. On a Pi that boots to an
# auto-login desktop that is effectively "on boot", which is what section 30
# asks for. On one that boots to a console with no login, nothing starts — and
# that is correct, because there would be no compositor and no session bus.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(aipi5.service aipi5-ui.service)
AIA_HOME="${AIA_HOME:-$HOME/AI_Assit}"

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ "${1:-}" == "--uninstall" ]]; then
  log "Stopping and removing"
  for unit in "${UNITS[@]}"; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
    rm -f "$UNIT_DIR/$unit"
  done
  systemctl --user daemon-reload
  echo "Removed. AIA is not re-enabled — 'systemctl --user start aia' if you want it back."
  exit 0
fi

# ── preflight ─────────────────────────────────────────────────────────
#
# Everything checked here is a thing that makes the service fail *after* it
# has started, in a way that reads as a different problem. A missing
# SenseVoice model presents as "the assistant does not respond"; a missing
# venv presents as a systemd unit that restarts every five seconds forever.

log "Checking prerequisites"
missing=0
check() { [[ -e "$2" ]] && echo "  ok   $1" || { echo "  MISSING  $1 ($2)"; missing=1; }; }
note()  { [[ -e "$2" ]] && echo "  ok   $1" || echo "  --   $1 (optional; $2)"; }

SENSEVOICE="$AIA_HOME/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
check "virtualenv"          "$ROOT/.venv/bin/python"
check "configuration"       "$ROOT/config/aipi5.yaml"
check "AIA checkout"        "$AIA_HOME/aia/__init__.py"
check "SenseVoice model"    "$SENSEVOICE/model.int8.onnx"
check "wake-word model"     "$AIA_HOME/models/vosk-model-small-cn-0.22"
check "English voice"       "$AIA_HOME/models/en_US-lessac-medium.onnx"
check "Mandarin voice"      "$AIA_HOME/models/zh_CN-huayan-medium.onnx"
check "Piper binary"        "$AIA_HOME/vendor/piper/piper"

# The venv must be able to see the system packages. picamera2 and
# hailo_platform are Debian packages with no usable wheel, so a venv built
# without this flag imports neither — and the failure is a *degraded* startup
# rather than an error, which means an assistant that silently has no camera
# and no person detection. Checked here because the fix is one line and the
# symptom is three subsystems quietly missing.
if grep -q '^include-system-site-packages = true' "$ROOT/.venv/pyvenv.cfg" 2>/dev/null; then
  echo "  ok   venv sees system packages"
else
  echo "  --   venv does NOT see system packages — no camera, no person detection"
  echo "       fix: sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg"
fi

# Optional, each with a named degraded mode. Not folded into the block above,
# because refusing to install a working assistant over a person-detection
# model would be refusing the thing for the sake of the accessory.
#
# The path comes from the configuration rather than being restated here. It is
# an absolute path into /usr/share/hailo-models on this device — the AI HAT+ 2
# is a Hailo-10H and its HEFs ship with `hailo-all` — so a hardcoded
# models/yolov8n.hef would report the real, working model as missing.
HEF="$(sed -n 's/^[[:space:]]*model:[[:space:]]*\(.*\.hef\)[[:space:]]*$/\1/p' \
       "$ROOT/config/aipi5.yaml" | head -1)"
[[ "$HEF" == /* ]] || HEF="$ROOT/${HEF:-models/none.hef}"
note  "Hailo person model"  "$HEF"
note  "Chromium"            "$(command -v chromium-browser || command -v chromium || echo /usr/bin/chromium)"

(( missing )) && {
  warn "Fetch AIA's models first: cd $AIA_HOME && ./scripts/get_sensevoice.sh && ./scripts/get_wake_model.sh"
  warn "Piper and the voices come from AIA's ./scripts/bench_m0.sh."
  exit 1
}

# The OpenAI key. Absent is a supported state — everything deterministic still
# works — so this is a note rather than a failure, but it is the single most
# likely reason for "it answers commands but won't talk to me".
#
# The systemd drop-in is checked too, and first, because it is the recommended
# place and it is invisible to this shell — a deploy that put the key there
# would otherwise be told, wrongly, that conversation is unavailable.
KEY_DROPIN="$HOME/.config/systemd/user/aipi5.service.d/10-openai-key.conf"
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "  ok   OpenAI key (from the environment)"
elif grep -qs 'OPENAI_API_KEY=' "$KEY_DROPIN"; then
  echo "  ok   OpenAI key (systemd drop-in)"
elif [[ -f "$ROOT/openai API.txt" ]]; then
  echo "  ok   OpenAI key (openai API.txt)"
else
  echo "  --   OpenAI key (not found; conversation will be unavailable)"
fi

# The units hardcode %h/AIPI5; anywhere else and they point at nothing.
[[ "$ROOT" == "$HOME/AIPI5" ]] || {
  warn "The units expect $HOME/AIPI5 but this checkout is at $ROOT."
  warn "Either move it, or edit systemd/*.service before installing."
  exit 1
}

# ── install ───────────────────────────────────────────────────────────
log "Installing units into $UNIT_DIR"
mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install -m 0644 "$ROOT/systemd/$unit" "$UNIT_DIR/$unit"
  echo "  $unit"
done
chmod +x "$ROOT/scripts/aipi5-ui.sh"
systemctl --user daemon-reload

# ── the launcher icon ─────────────────────────────────────────────────
#
# On the desktop and in the applications menu. The icon goes into the hicolor
# theme rather than being referenced by absolute path, because a `.desktop`
# file does not expand `%h` or `$HOME` in `Icon=` — an absolute path there
# works only for the user it was written for, and silently shows a blank tile
# for anybody else.
log "Installing the launcher"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
APPS="$HOME/.local/share/applications"
mkdir -p "$ICONS" "$APPS" "$HOME/Desktop"
install -m 0644 "$ROOT/scripts/aipi5.svg" "$ICONS/aipi5.svg"
install -m 0644 "$ROOT/scripts/aipi5.desktop" "$APPS/aipi5.desktop"
# The desktop copy has to be executable, and on this desktop environment also
# trusted, or it is shown as a plain text file with a warning on first click.
install -m 0755 "$ROOT/scripts/aipi5.desktop" "$HOME/Desktop/aipi5.desktop"
gio set "$HOME/Desktop/aipi5.desktop" metadata::trusted true 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$APPS" 2>/dev/null || true
echo "  AI Assistant on the desktop and in the menu"

# AIA and AIPI5 cannot both hold the microphone. The unit declares
# `Conflicts=aia.service`, which stops it on start — but leaving it *enabled*
# means the next login starts it first and the two fight on every boot.
if systemctl --user is-enabled aia.service >/dev/null 2>&1; then
  log "Disabling aia.service"
  echo "  AIPI5 is AIA's voice loop with a conversational layer on top; the"
  echo "  microphone allows exactly one reader, so they cannot both run."
  systemctl --user disable --now aia.service || true
fi

log "Stopping any hand-started instance"
# -f is unavoidable: these are python processes whose comm is "python".
# Anchored on the module path so it cannot match a bare shell.
pkill -f "python -m aipi5.main" 2>/dev/null || true
pkill -f "python -m aia.main" 2>/dev/null || true
sleep 2

log "Enabling and starting"
systemctl --user enable --now aipi5.service
systemctl --user enable --now aipi5-ui.service

sleep 15
log "Status"
for unit in "${UNITS[@]}"; do
  printf '  %-22s %-10s %s\n' "$unit" "$(systemctl --user is-active "$unit")" \
    "$(systemctl --user is-enabled "$unit" 2>/dev/null)"
done

cat <<'NOTE'

  Starts automatically with the desktop session from now on.

    journalctl --user -u aipi5 -f      watch a conversation happen
    systemctl --user restart aipi5     after changing the code
    systemctl --user stop aipi5        free the microphone
    systemctl --user stop aipi5-ui     get out of the full-screen UI

  The screen is also at http://127.0.0.1:8092/ — forward it with
  `ssh -L 8092:127.0.0.1:8092 aipi5.local` to watch from a laptop.

  The startup checks are in the first twenty lines of the journal. Anything
  reported there as unavailable is a degraded subsystem, not a failure: the
  assistant runs without it and says so across the top of the screen.

NOTE
