#!/usr/bin/env bash
#
# Open the assistant's screen in Chromium, full-screen on the 1280x800 panel.
#
# Full-screen here, unlike AIA's UI which is deliberately only maximised. The
# reasoning is different because the window is: AIA's page is a scrollback and
# a settings screen that somebody reads and then leaves, so it wants the
# taskbar and a titlebar close button under a finger. This is the assistant's
# own face — it is what the device looks like when nobody is using it, and
# 28 px of taskbar across the top of a screensaver is wrong.
#
# The way back out is `systemctl --user stop aipi5-ui`, or Alt+F11 if labwc has
# been given a fullscreen binding — worth doing, because stock rc.xml on this
# Pi binds neither Alt+F4 nor a fullscreen toggle, which on a touch-only
# display leaves a full-screen window with no way out:
#
#   ~/.config/labwc/rc.xml
#   <keyboard>
#     <keybind key="A-F11"><action name="ToggleFullscreen"/></keybind>
#     <keybind key="A-F4"><action name="Close"/></keybind>
#   </keyboard>

set -euo pipefail

URL="${AIPI5_URL:-http://127.0.0.1:8092/}"
PROFILE="${XDG_CACHE_HOME:-$HOME/.cache}/aipi5-ui"

browser=""
for candidate in chromium-browser chromium; do
  if command -v "$candidate" >/dev/null 2>&1; then browser="$candidate"; break; fi
done
[[ -n "$browser" ]] || { echo "no chromium found; install chromium-browser" >&2; exit 1; }

# Wait for the assistant to be serving before opening anything. Pressing this
# at boot otherwise lands on Chromium's error page, which an --app window
# cannot navigate away from.
echo "waiting for $URL"
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null "$URL"; then break; fi
  sleep 1
done

# Tell Chromium the last session ended cleanly, whether or not it did.
#
# This is the fix for a blank white screen, and it is not cosmetic. Chromium
# was killed rather than asked to quit — which is what `systemctl restart`
# does, and this unit is restarted every time the assistant is — so the
# profile is left marked `exit_type: "Crashed"`. On the next start Chromium
# then tries to restore that dead session *instead of* opening the `--app`
# URL, and an --app window has no tabs, no address bar and no restore bubble,
# so what reaches the display is a white page with the URL as its title. The
# server is fine, the page is fine, and nothing in either log says anything is
# wrong.
#
# `--disable-session-crashed-bubble` suppresses the prompt but not the
# restore, so it does not help. Clearing the flag before every launch does,
# and it is correct here in a way it would not be on a desktop: this profile
# exists only to display one page, so there is never a session worth restoring.
PREFS="$PROFILE/Default/Preferences"
if [[ -f "$PREFS" ]]; then
  python3 - "$PREFS" "$URL" <<'PY' || true
import json, sys, pathlib, datetime
path = pathlib.Path(sys.argv[1])
origin = sys.argv[2].rstrip("/")
try:
    prefs = json.loads(path.read_text())
except (OSError, ValueError):
    raise SystemExit(0)          # unreadable is not worth failing a launch over
profile = prefs.setdefault("profile", {})
profile["exit_type"] = "Normal"
profile["exited_cleanly"] = True

# Grant the camera and the microphone to the assistant's own page, once, here.
#
# The video call is Chromium's on this end — see aipi5/call/__init__.py — so
# `getUserMedia` runs in this window, and by default that raises a permission
# bubble. On this device there is nobody to dismiss it: the panel is a kiosk
# with no keyboard, the bubble appears over a full-screen --app window, and the
# promise simply never settles. Measured on the Pi: the call reached
# `connecting`, Python let go of the Brio as it should, and Chromium never took
# it — a call that hangs with the camera belonging to nobody.
#
# Seeding the content setting rather than passing --use-fake-ui-for-media-stream
# is deliberate. That flag also auto-approves, but it approves *everything* for
# the life of the browser, and its name is a promise about fake devices that a
# future version could start keeping. This grants two permissions, to one
# origin, and is exactly what pressing Allow would have written.
#
# The timestamp is Chromium's: microseconds since 1601-01-01. An entry without
# a plausible one is pruned, which looks precisely like this never having run.
epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
stamp = str(int((now - epoch).total_seconds() * 1_000_000))
exceptions = profile.setdefault("content_settings", {}).setdefault("exceptions", {})
for what in ("media_stream_camera", "media_stream_mic"):
    exceptions.setdefault(what, {})[f"{origin},*"] = {
        "last_modified": stamp,
        "setting": 1,               # 1 = allow, and only for this origin
    }

path.write_text(json.dumps(prefs))
PY
fi

exec "$browser" \
  --app="$URL" \
  --class=aipi5-ui \
  --user-data-dir="$PROFILE" \
  --start-fullscreen \
  --window-size=1280,800 \
  --window-position=0,0 \
  `# A kiosk display has no keyboard and nobody to answer a prompt. Each of` \
  `# these turns off something that would otherwise put a bar or a dialog` \
  `# over the assistant's screen at an arbitrary moment.` \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  `# One --disable-features flag, not several: Chromium keeps only the last` \
  `# occurrence, so a second one silently discards the first.` \
  `#` \
  `# PipeWireCamera is what makes a video call possible on this device at all.` \
  `# Chromium prefers to reach cameras through the xdg-desktop-portal Camera` \
  `# interface, and this Pi has no backend that implements it — labwc brings` \
  `# xdg-desktop-portal-wlr, which does ScreenCast and not Camera, and neither` \
  `# gtk.portal nor gnome-keyring.portal fills the gap. The request therefore` \
  `# waits on a portal that will never answer: measured here as getUserMedia` \
  `# never settling, with the Brio released by Python and claimed by nobody.` \
  `# Disabling it sends Chromium to V4L2 directly, which is the same path the` \
  `# assistant's own camera code uses and the one the device is set up for.` \
  --disable-features=TranslateUI,PipeWireCamera,WebRtcPipeWireCamera \
  --no-first-run \
  --check-for-update-interval=31536000 \
  `# The page has no text input and nothing to save. Without this a stray` \
  `# long-press raises a context menu the user cannot dismiss on a` \
  `# touch-only screen.` \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  `# Do NOT touch the system keyring. Chromium otherwise asks GNOME Keyring` \
  `# for somewhere to keep secrets, and on a machine with no keyring yet that` \
  `# raises a modal "Choose password for new keyring" dialog — centred, on` \
  `# top, and waiting for a password nobody is going to type. On a kiosk that` \
  `# is the whole display gone, and it survives a restart because the keyring` \
  `# still does not exist. This page stores no credentials, so the basic` \
  `# store is not a downgrade here; it is the absence of a thing we never` \
  `# wanted.` \
  --password-store=basic \
  `# Same class of problem: without this, Chromium can raise its own` \
  `# first-run and default-browser prompts over the page.` \
  --no-default-browser-check
