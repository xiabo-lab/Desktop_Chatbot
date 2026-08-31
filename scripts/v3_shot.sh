#!/usr/bin/env bash
# Show a v3 page on the Pi, screenshot it, and bring back both the picture and
# whatever the page reported.
#
#   scripts/v3_shot.sh <name> '<query>' [seconds] [page]
#
# The page talks back through the access log of the static server, because
# there is no console to read from another machine. Chromium is restarted every
# time: reusing a window meant a stale render was screenshotted more than once,
# and a screenshot that is silently of the previous pose is worse than no
# screenshot at all.
#
# The cleanup kills *every* chromium, not only the one it started. Killing the
# launcher alone leaves its renderers behind -- a hundred of them accumulated
# over one evening and took the Pi down to fifty megabytes free, at which point
# scp started failing and the failure looked like a network problem. The kiosk
# browser is a service and comes back with `systemctl restart aipi5-ui`, so
# there is nothing here worth being delicate about.
set -u
NAME=$1; QUERY=${2:-}; WAIT=${3:-14}; PAGE=${4:-stage.html}
OUT=${V3_OUT:-/c/Users/fuwen/AppData/Local/Temp/claude/C--Users-fuwen-Claude-AIPI5/cfc7c770-21de-44ff-97c2-dde37c4be9bc/scratchpad/shots}
mkdir -p "$OUT"
scp -qr aipi5/ui/web/assets/yoga/v3/. aipi5:~/AIPI5/aipi5/ui/web/assets/yoga/v3/ || exit 1
ssh aipi5 "export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
python3 - <<'EOF'
import os, signal, pathlib, time
me = os.getpid(); mine = {me, os.getppid()}
killed = 0
for e in pathlib.Path('/proc').iterdir():
    if not e.name.isdigit() or int(e.name) in mine: continue
    try: parts = [p for p in (e/'cmdline').read_bytes().decode('utf-8','ignore').split(chr(0)) if p]
    except OSError: continue
    if parts and os.path.basename(parts[0]).startswith('chromium'):
        try: os.kill(int(e.name), signal.SIGKILL); killed += 1
        except OSError: pass
if killed: print('  swept %d stale browser processes' % killed)
EOF
rm -rf /tmp/v3q /tmp/v3p /tmp/v3c /tmp/v3d /tmp/v3e /tmp/v3s* 2>/dev/null
sleep 2
# The static server is also the page's only way to talk back, so bring it up
# if it is gone -- it does not survive the machine running out of memory, and
# without it every run reports nothing and looks like a rendering failure.
if ! curl -sf -o /dev/null http://127.0.0.1:8099/assets/yoga/v3/rigdata.json; then
  cd ~/AIPI5/aipi5/ui/web
  setsid nohup python3 -m http.server 8099 --bind 127.0.0.1 >/tmp/v3.log 2>&1 </dev/null &
  sleep 4
fi
MARK=\$(wc -l < /tmp/v3.log)
setsid nohup chromium --ozone-platform=wayland \
  --app='http://127.0.0.1:8099/assets/yoga/v3/$PAGE?$QUERY' \
  --user-data-dir=/tmp/v3q --start-fullscreen --new-window \
  --disable-features=TranslateUI,PipeWireCamera,WebRtcPipeWireCamera \
  >/dev/null 2>&1 < /dev/null &
sleep $WAIT
grim /tmp/shot.png
# Leave nothing running. A browser held open between runs is a few hundred
# megabytes of a machine that has eight, and the shot has already been taken.
pkill -9 chromium 2>/dev/null || true
tail -n +\$((MARK+1)) /tmp/v3.log | grep -a '__v3' | sed 's|.*GET /__v3/||; s| HTTP.*||' \
  | python3 -c \"
import sys, urllib.parse
for l in sys.stdin: print(urllib.parse.unquote(l.strip()).replace('%09','\t'))
\"" 2>&1 | grep -v '^$'
scp -q aipi5:/tmp/shot.png "$OUT/$NAME.png" && echo "-> $OUT/$NAME.png"
