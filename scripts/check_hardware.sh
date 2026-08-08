#!/usr/bin/env bash
#
# Phase 2 of the implementation procedure: verify this Raspberry Pi before
# anything is installed on it. Run it on the device.
#
#     ssh fuwenxu@aipi5.local
#     cd ~/AIPI5 && ./scripts/check_hardware.sh
#
# Reports rather than fixes. Every line is something the assistant needs and
# somewhere to look when it is missing — the point is to find out on purpose,
# once, instead of discovering it as a subsystem that quietly does not work.

set -uo pipefail   # not -e: a failed probe is a result, not a reason to stop

ok()   { printf '  \033[1;32mok\033[0m   %-26s %s\n' "$1" "${2:-}"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %-26s %s\n' "$1" "${2:-}"; }
note() { printf '  --   %-26s %s\n' "$1" "${2:-}"; }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head "The Pi itself"
model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
case "$model" in
  *"Raspberry Pi 5"*) ok "model" "$model" ;;
  *) bad "model" "$model — this project is written for a Pi 5" ;;
esac

mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if (( mem_kb > 7000000 )); then ok "memory" "$((mem_kb / 1024)) MB"
else bad "memory" "$((mem_kb / 1024)) MB — 8 GB expected"; fi

# The active cooler and NVMe both matter: AIA's measured latency numbers assume
# them, and a throttled Pi is slow in a way that looks like a software
# regression.
throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
[[ "$throttled" == "0x0" ]] && ok "throttling" "none" \
                            || bad "throttling" "$throttled — check power and cooling"
temp=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)
note "temperature" "${temp:-unknown}"

root_dev=$(findmnt -no SOURCE / 2>/dev/null)
case "$root_dev" in
  *nvme*) ok "root filesystem" "$root_dev (NVMe)" ;;
  *) note "root filesystem" "$root_dev — SD card; expect slower model loads" ;;
esac

head "Display"
# The assistant's UI is designed for exactly this panel. A different mode is
# not fatal but the layout will not fit, and it is much easier to see here
# than to infer from a screenshot.
modes=$(wlr-randr 2>/dev/null | grep -E 'current|preferred' | head -3)
if [[ -n "$modes" ]]; then
  echo "$modes" | sed 's/^/       /'
  echo "$modes" | grep -q '1280x800' && ok "resolution" "1280x800 present" \
                                     || bad "resolution" "1280x800 not offered"
else
  note "resolution" "wlr-randr not available; check the display manually"
fi
[[ -n "${WAYLAND_DISPLAY:-}" ]] && ok "compositor" "$WAYLAND_DISPLAY" \
                                || note "compositor" "not in a Wayland session (ssh?)"

head "Microphone"
# Two USB microphones on this device enumerate under names that differ only by
# a card number that moves on re-plug, which is why AIA matches on the name.
if arecord -l 2>/dev/null | grep -q card; then
  arecord -l | grep '^card' | sed 's/^/       /'
  ok "capture device" "present"
else
  bad "capture device" "arecord sees no card"
fi
# Gain is not a matter of taste. Near full scale webrtcvad calls every frame
# speech, `silence_ms` is never satisfied, and every utterance runs to the
# 10 s cap — see AIA's README. 8 is the value both capsules were tuned to.
for card in $(arecord -l 2>/dev/null | sed -n 's/^card \([0-9]*\).*/\1/p'); do
  gain=$(amixer -c "$card" sget Mic 2>/dev/null | grep -o '\[[0-9]*%\]' | head -1)
  agc=$(amixer -c "$card" sget 'Auto Gain Control' 2>/dev/null | grep -o '\[o[nf]*\]' | head -1)
  note "card $card mixer" "gain=${gain:-n/a} agc=${agc:-n/a}"
done

head "Speaker"
# AIA is mute without pipewire-alsa: PipeWire owns the only HDMI sink and holds
# the ALSA device open, so PortAudio enumerates zero outputs and every reply is
# synthesised and dropped. It fails silently, which is why this is checked.
dpkg -s pipewire-alsa >/dev/null 2>&1 && ok "pipewire-alsa" "installed" \
  || bad "pipewire-alsa" "MISSING — every reply will be synthesised and never heard"
aplay -l 2>/dev/null | grep -q card && ok "playback device" "present" \
                                    || bad "playback device" "aplay sees no card"

head "Camera"
if command -v rpicam-hello >/dev/null 2>&1; then
  if rpicam-hello --list-cameras 2>&1 | grep -qi 'imx708'; then
    ok "Camera Module 3" "imx708 detected"
  elif rpicam-hello --list-cameras 2>&1 | grep -qi 'Available cameras'; then
    note "camera" "a camera is present but is not an imx708 (Module 3)"
  else
    bad "camera" "no camera detected"
  fi
else
  note "camera" "rpicam-hello not installed"
fi
python3 -c 'import picamera2' 2>/dev/null && ok "picamera2" "importable" \
  || bad "picamera2" "not installed — sudo apt install python3-picamera2"

head "AI HAT+ 2"
if command -v hailortcli >/dev/null 2>&1; then
  if hailortcli fw-control identify >/dev/null 2>&1; then
    ok "Hailo device" "$(hailortcli fw-control identify 2>/dev/null | grep -i 'Device Arch' | xargs)"
  else
    bad "Hailo device" "hailortcli cannot reach it — check the PCIe seating"
  fi
else
  note "Hailo" "hailortcli not installed — sudo apt install hailo-all"
fi
python3 -c 'import hailo_platform' 2>/dev/null && ok "hailo_platform" "importable" \
  || note "hailo_platform" "not importable; person detection would fall back to cpu"

head "Network"
timeout 3 bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/53' 2>/dev/null \
  && ok "route" "reachable" || bad "route" "no route out"
getent hosts api.openai.com >/dev/null 2>&1 \
  && ok "name resolution" "api.openai.com resolves" \
  || bad "name resolution" "cannot resolve api.openai.com"

head "Companion software"
systemctl --user is-enabled kodama-lite.service >/dev/null 2>&1 \
  && ok "kodama-lite.service" "$(systemctl --user is-active kodama-lite.service)" \
  || note "kodama-lite.service" "not installed — the music commands need it"
command -v playerctl >/dev/null 2>&1 && ok "playerctl" "$(playerctl --version)" \
  || bad "playerctl" "not installed — sudo apt install playerctl"
[[ -d "${AIA_HOME:-$HOME/AI_Assit}/aia" ]] \
  && ok "AIA checkout" "${AIA_HOME:-$HOME/AI_Assit}" \
  || bad "AIA checkout" "not found — AIPI5 is built on it"

printf '\n'
