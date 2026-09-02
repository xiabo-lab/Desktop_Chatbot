#!/usr/bin/env python3
"""Run the real AIPI5 page with deterministic Boxing snapshots.

This is a browser/UI development harness, not a second motion pipeline.  It
serves the production HTML and renderer while replacing unavailable Pi camera
hardware with a small sequence of representative pose and combat states.

Usage::

    python scripts/preview-boxing.py --port 8765

Open ``http://127.0.0.1:8765``, choose Game, Boxing, and a mode.  For visual
regression states, POST ``demo-player-hit``, ``demo-opponent-hit``,
``demo-parry`` or ``demo-over`` as the action to
``/api/game/command``. There are no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1] / "aipi5" / "ui" / "web"


class DemoState:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = ""
        self.mode = ""
        self.difficulty = "normal"
        self.phase = "ready"
        self.started_at = 0.0
        self.slow = False
        self.over = False
        self.debug = False
        self.player_lane = "middle"
        self.opponent_lane = 0
        self.opponent_damage = {
            "left_eye": 3, "right_eye": 0,
            "left_shoulder": 0, "right_shoulder": 0,
        }
        self.player_damage = {"left_shoulder": 0, "right_shoulder": 0}
        self.events: list[dict] = []

    def command(self, action: str) -> None:
        with self.lock:
            if action.startswith("mode-"):
                self.mode = action.removeprefix("mode-")
                self.phase = "ready"
            elif action.startswith("difficulty-"):
                self.difficulty = action.removeprefix("difficulty-")
            elif action in ("start", "restart"):
                self.phase = "playing"
                self.started_at = time.monotonic()
                self.slow = self.over = False
                self.events.append({"name": "boxing-ready"})
            elif action == "pause":
                self.phase = "paused"
            elif action == "resume":
                self.phase = "playing"
            elif action == "demo-parry":
                self.phase = "playing"
                self.mode = self.mode or "fight"
                self.started_at = time.monotonic() - 22
                self.slow = True
                self.events += [{"name": "perfect-parry"},
                                {"name": "slow-motion-start"}]
            elif action == "demo-player-hit":
                self.phase = "playing"
                self.mode = self.mode or "fight"
                self.started_at = time.monotonic() - 22
                self.events += [{
                    "name": "player-hit", "side": "left", "target": "head",
                    "zone": "right_eye", "damage": 2, "hook": True,
                    "combo": 5,
                }, {"name": "crowd-reaction", "strength": 4}]
            elif action.startswith("demo-player-lane-"):
                self.player_lane = action.removeprefix("demo-player-lane-")
            elif action.startswith("demo-opponent-lane-"):
                name = action.removeprefix("demo-opponent-lane-")
                self.opponent_lane = {"left": -1, "middle": 0, "right": 1}.get(name, 0)
            elif action.startswith("demo-injury-"):
                try:
                    level = max(0, min(3, int(action.rsplit("-", 1)[-1])))
                    self.opponent_damage = dict.fromkeys(self.opponent_damage, 0)
                    self.opponent_damage["left_eye"] = level
                except ValueError:
                    pass
            elif action.startswith("demo-damage-"):
                key = action.removeprefix("demo-damage-")
                if len(key) == 4 and all(character in "0123" for character in key):
                    zones = ("left_eye", "right_eye", "left_shoulder", "right_shoulder")
                    self.opponent_damage = dict(zip(zones, map(int, key)))
            elif action.startswith("demo-player-damage-"):
                key = action.removeprefix("demo-player-damage-")
                if len(key) == 2 and all(character in "0123" for character in key):
                    zones = ("left_shoulder", "right_shoulder")
                    self.player_damage = dict(zip(zones, map(int, key)))
            elif action == "demo-opponent-hit":
                self.phase = "playing"
                self.mode = self.mode or "fight"
                self.started_at = time.monotonic() - 22
                self.events += [{
                    "name": "opponent-hit", "target": "body", "damage": 10,
                }]
            elif action in ("demo-over", "end"):
                self.phase = "over"
                self.over = True
                self.mode = self.mode or "fight"
                self.events += [{"name": "knockout", "fighter": "opponent"},
                                {"name": "boxing-over", "result": "win"}]

    def snapshot(self) -> dict:
        with self.lock:
            mode, phase, slow = self.mode, self.phase, self.slow
            elapsed = max(0.0, time.monotonic() - self.started_at)
            if phase == "playing" and elapsed < 4:
                remaining = 4 - elapsed
                countdown = "READY" if remaining > 3 else str(max(1, int(remaining + .999)))
            else:
                countdown = ""
            duration = 90.0 if mode != "fight" else 180.0
            play_elapsed = max(0.0, elapsed - 4)
            time_left = duration if phase == "ready" else max(0.0, duration - play_elapsed)
            prompt = None
            if mode == "training" and phase == "playing" and not countdown:
                prompts = [
                    ("left_punch", "head"), ("right_punch", "body"),
                    ("dodge_left", ""), ("block", ""), ("parry", ""),
                ]
                kind, target = prompts[int(play_elapsed / 2.4) % len(prompts)]
                prompt = {"kind": kind, "target": target,
                          "stage": 1 if play_elapsed < 30 else 2 if play_elapsed < 60 else 3,
                          "time_left": 1.8, "progress": .42}
            opponent_hp = 0 if self.over else 141 if slow else 184
            player_hp = 71
            attack = {
                "kind": "right_cross", "side": "right", "target": "head",
                "damage": 10, "impact_in": .12, "parry_window": .24,
                "phase": "windup", "progress": .72, "extension": .72,
                "trajectory": [.52, .32, .57, .27],
            }
            game = {
                "kind": "boxing", "state": phase, "mode": mode,
                "difficulty": self.difficulty, "score": 2840,
                "best": 4220, "time_left": round(time_left, 2),
                "duration": duration, "combo": 4, "countdown": countdown,
                "round_started": not bool(countdown), "slow_motion": slow,
                "simulation_scale": .1 if slow else 1.0,
                "result": "win" if self.over else "",
                "stats": {
                    "punches_thrown": 48, "successful_hits": 39,
                    "missed_punches": 9, "accuracy": 81.2,
                    "successful_dodges": 7, "successful_blocks": 5,
                    "successful_parries": 3, "best_combo": 8,
                    "average_reaction_ms": 428, "best_reaction_ms": 251,
                },
                "motion": {
                    "gesture": "right_punch", "confidence": .94,
                    "guard": "two_hand_guard",
                    "posture": {"tracked": True, "torso_angle": 1.7,
                                "offset_x": {"left": -.34, "middle": 0,
                                             "right": .34}.get(self.player_lane, 0),
                                "offset_y": .02, "lane": self.player_lane},
                    "hands": {
                        "left": {"x": .40, "y": .34, "px": .42, "py": .36,
                                 "speed": .68, "acceleration": 2.1},
                        "right": {"x": .67, "y": .31, "px": .58, "py": .39,
                                  "speed": 2.46, "acceleration": 8.2},
                    },
                },
                "player": {"hp": player_hp, "max_hp": 100,
                           "display_hp": player_hp, "delayed_hp": 77,
                           "damage_matrix": {
                               "left_eye": 0, "right_eye": 0,
                               **self.player_damage,
                           },
                           "damage_key": "00" + "".join(
                               str(value) for value in self.player_damage.values()),
                           "injuries": [
                               {"zone": zone, "level": level, "kind": "body"}
                               for zone, level in self.player_damage.items() if level
                           ]},
                "opponent": {"hp": opponent_hp, "max_hp": 220,
                             "display_hp": opponent_hp,
                             "delayed_hp": max(opponent_hp, 194),
                             "damage_matrix": dict(self.opponent_damage),
                             "damage_key": "".join(
                                 str(value) for value in self.opponent_damage.values()),
                             "injuries": [
                                 {"zone": zone, "level": level,
                                  "kind": "face" if zone.endswith("eye") else "body"}
                                 for zone, level in self.opponent_damage.items() if level
                             ]},
            }
            if prompt:
                game["prompt"] = prompt
            if mode == "fight":
                game["ai"] = {"state": "attack", "guard": "none",
                              "lane": self.opponent_lane,
                              "difficulty": self.difficulty,
                              "adaptation": {"head": 11, "body": 4, "left": 5, "right": 10},
                              "attack": attack}

            body = {
                "nose": [.50, .24], "left_shoulder": [.39, .40],
                "right_shoulder": [.61, .40], "left_elbow": [.34, .49],
                "right_elbow": [.59, .36], "left_wrist": [.43, .32],
                "right_wrist": [.67, .31], "left_hip": [.43, .66],
                "right_hip": [.57, .66],
            }
            events, self.events = self.events, []
            return {
                "active": self.active, "error": "", "debug": self.debug,
                "games": catalogue(), "game": game,
                "gesture": {"name": "arms-crossed-x", "armed": True,
                            "crossed": False, "progress": 0, "hold_ms": 650},
                "pose": {"timestamp": round(time.monotonic(), 3),
                         "player": {"present": True, "confidence": .97,
                                    "body": body},
                         "ready": True, "advice": "", "persons": 1},
                "motion": {"running": True, "error": "",
                           "stats": {"frames": 820, "dropped": 2,
                                     "inference_fps": 29.8, "camera_fps": 89.7,
                                     "inference_ms": 18.2, "pipeline_ms": 31.4,
                                     "errors": 0}},
                "events": events,
            }


def catalogue() -> list[dict]:
    return [
        {"id": "fruit-ninja", "name": "Fruit Ninja", "glyph": "🍉",
         "blurb": "Slice the fruit with your hands. Mind the bombs.",
         "playable": True, "best": 920, "round_seconds": 120},
        {"id": "yoga", "name": "Yoga Coach", "glyph": "🧘",
         "blurb": "Hold the pose. Get it checked.", "playable": False, "best": 0},
        {"id": "boxing", "name": "Boxing", "glyph": "🥊",
         "blurb": "Train your reactions or fight an adaptive opponent.",
         "playable": True, "best": 4220},
        {"id": "workout", "name": "Workout", "glyph": "🏋",
         "blurb": "Counted reps, called out loud.", "playable": False, "best": 0},
    ]


STATE = DemoState()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *_args) -> None:
        pass

    def _json(self, value, status=200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/games":
            self._json({"games": catalogue()})
        elif path == "/api/game/status":
            self._json(STATE.snapshot())
        elif path == "/api/game/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while STATE.active:
                    body = json.dumps(STATE.snapshot())
                    self.wfile.write(f"data: {body}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(.1)
            except OSError:
                pass
        elif path == "/api/game/preview":
            picture = ROOT / "assets" / "fruit-ninja" / "ninja-head.png"
            body = picture.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            brief = ({"active": STATE.active, "state": STATE.phase}
                     if STATE.active else None)
            self._json({"game": brief, "mode": "active", "online": True})
        else:
            super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        path = urlparse(self.path).path
        if path == "/api/game/open":
            STATE.active = str(payload.get("game", "boxing"))
            STATE.mode = ""
            STATE.phase = "ready"
            self._json(STATE.snapshot())
        elif path == "/api/game/command":
            STATE.command(str(payload.get("action", "")))
            # Leave one-shot events for the SSE consumer. The production page
            # also treats the stream as authoritative after a command.
            self._json({"ok": True})
        elif path == "/api/game/close":
            STATE.active = ""
            self._json({"active": ""})
        elif path == "/api/game/debug":
            STATE.debug = bool(payload.get("on"))
            self._json({"ok": True})
        else:
            self._json({"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Boxing preview: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
