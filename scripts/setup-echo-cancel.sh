#!/usr/bin/env bash
#
# Install (or remove) echo cancellation for the video call.
#
#   ./scripts/setup-echo-cancel.sh            # install and verify
#   ./scripts/setup-echo-cancel.sh --remove   # put it back exactly as it was
#
# Run on the Pi. Needs no sudo: this is a per-user PipeWire drop-in.
#
# What it does NOT do is change the default sink, so the assistant's own speech
# keeps going straight to HDMI and is unaffected. `config/pipewire-echo-cancel.conf`
# explains why at length; the short version is that a mistake in the default
# sink makes the assistant silent in the room, and the call can opt in without
# anybody else having to.

set -euo pipefail

cd "$(dirname "$0")/.."

DROPIN_DIR="$HOME/.config/pipewire/pipewire.conf.d"
DROPIN="$DROPIN_DIR/99-aipi5-echo-cancel.conf"
WP_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WP_DROPIN="$WP_DIR/99-aipi5-reserve-aia-mic.conf"
SOURCE_NODE="aipi5_call_mic"
SINK_NODE="aipi5_call_speaker"

hdmi_sink_id() {
  wpctl status 2>/dev/null | sed -n '/Sinks:/,/Sources:/p' \
    | grep -iE "hdmi" | grep -oE "[0-9]+\." | head -1 | tr -d '.'
}

node_id() {   # node_id <node.name>
  wpctl status 2>/dev/null | grep -E "[0-9]+\. +$1\b" | grep -oE "[0-9]+\." | head -1 | tr -d '.'
}

if [[ "${1:-}" == "--remove" ]]; then
  back="$(hdmi_sink_id)"
  if [[ -n "$back" ]]; then
    wpctl set-default "$back" && echo "default output back to HDMI (node $back)"
  fi
  rm -f "$DROPIN" "$WP_DROPIN"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
  echo "Removed. The call will fall back to the browser's own cancellation."
  exit 0
fi

# The module is a Debian package away, and its absence is the one failure here
# that looks like nothing happening.
if [[ ! -f /usr/lib/aarch64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so ]]; then
  echo "libspa-aec-webrtc.so is missing." >&2
  echo "  sudo apt install pipewire-audio libspa-0.2-modules" >&2
  exit 1
fi

mkdir -p "$DROPIN_DIR" "$WP_DIR"
cp config/pipewire-echo-cancel.conf "$DROPIN"
echo "installed $DROPIN"

# Not optional, and not really about echo cancellation: PipeWire must not
# manage the capsule AIA opens exclusively through ALSA. Without it the
# canceller can latch onto AIA's microphone at boot and hold it, which stops
# the assistant starting at all. See the file for the measurements.
cp config/wireplumber-reserve-aia-mic.conf "$WP_DROPIN"
echo "installed $WP_DROPIN"

# wireplumber too: it is what actually instantiates devices, and a restart of
# pipewire alone leaves it holding stale links.
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 4

echo
echo "=== virtual devices ==="
ok=1
for node in "$SOURCE_NODE" "$SINK_NODE"; do
  if pw-cli ls Node 2>/dev/null | grep -q "\"$node\""; then
    echo "  ok    $node"
  else
    echo "  MISSING $node"
    ok=0
  fi
done

echo
echo "=== the real devices are still there ==="
for node in alsa_input.usb-046d_Brio_101 alsa_output.platform; do
  pw-cli ls Node 2>/dev/null | grep -o "node.name = \"$node[^\"]*\"" | head -1 || echo "  MISSING $node"
done

if [[ "$ok" != "1" ]]; then
  echo
  echo "Something did not come up. Check:  journalctl --user -u pipewire -n 40" >&2
  echo "Undo with:  ./scripts/setup-echo-cancel.sh --remove" >&2
  exit 1
fi

# ── the step that cannot be skipped ──────────────────────────────────────
#
# Chromium opens **more than one** playback stream, and `setSinkId` on the
# video element only moves the one attached to that element. The other has no
# target and follows the default sink. With HDMI as the default, the caller's
# voice therefore reached the speaker by two paths — one through the canceller
# and one around it. A canceller can only subtract what it played, so the
# second path is echo it can never remove.
#
# Measured in the live graph during a real call, which is the only place this
# was visible at all:
#
#     Chromium:output_FL |-> aipi5_call_speaker:playback_FL          (cancelled)
#     Chromium:output_FL |-> alsa_output...hdmi.hdmi-stereo:playback_FL  (not)
#
# Making the canceller the default closes that second path. It **cannot**
# silence the device: the canceller forwards its own playback straight to HDMI,
# so everything audible before is still audible, and `--remove` puts it back.
SPEAKER_ID="$(node_id "$SINK_NODE")"
if [[ -n "$SPEAKER_ID" ]]; then
  wpctl set-default "$SPEAKER_ID"
  echo "default output is now $SINK_NODE (node $SPEAKER_ID)"
else
  echo "WARNING: could not find $SINK_NODE to make it the default output." >&2
  echo "         Without this, some call audio bypasses the canceller." >&2
fi

echo
echo "=== audio routing now ==="
wpctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | grep -E "node.name|node.description" | head -2

cat <<'NOTE'

Installed. The call page picks these up by name on its next call — no restart
of aipi5 is needed for the devices themselves, though the page has to be
reloaded if it was already open (systemctl --user restart aipi5).

To check it is actually working, place a call and read:

    systemctl --user status aipi5 -n 60 | grep 'call audio'

`erle` is the number that matters. Before this change it measured 0.2 dB, which
is a canceller achieving nothing. Tens of dB is what working looks like.

NOTE
